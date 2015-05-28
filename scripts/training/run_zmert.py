#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Runs the Z-MERT and PRO tuners.
"""
from __future__ import print_function
import argparse
from collections import namedtuple
import logging
import os
import shutil
import signal
import stat
from subprocess import CalledProcessError, Popen, PIPE, check_output, call
import sys
import re

JOSHUA = os.environ.get('JOSHUA')

EXAMPLE = r"""
Example invocation:

$JOSHUA/scripts/support/run_zmert.py \
  /path/to/source.txt \
  /path/to/reference.en \
  --tuner zmert \
  --tunedir working-dir \
  --decoder /path/to/decoder/command \
  --decoder-output /path/to/decoder/nbest/output \
  --decoder-config /path/to/joshua.config

--tuner can be one of zmert or pro. If the path to the reference is a prefix
with ".0", ".1", etc extensions, they are treated as multiple references 
(extensions "0", "1", etc also works --- i.e., the path to the reference can
have a trailing period). The decoder command should decode your source file and
produce output at the --decoder-output location in the Joshua n-best format, e.g.,

  0 ||| example candidate translation ||| tm_pt_0=1 lm_0=17 ||| -34.2
"""

ZMERT_CONFIG_TEMPLATE = """### MERT parameters
# target sentences file name (in this case, file name prefix)

-r       <REF>
-rps     <NUMREFS>                   # references per sentence
-p       <TUNEDIR>/params.txt        # parameter file
-m       BLEU 4 closest              # evaluation metric and its options
-maxIt   10                          # maximum MERT iterations
-ipi     20                          # number of intermediate initial points per iteration
-cmd     <DECODER_COMMAND>           # file containing commands to run decoder
-decOut  <DECODER_OUTPUT>            # file produced by decoder
-dcfg    <DECODER_CONFIG>            # decoder config file
-N       300                         # size of N-best list
-v       1                           # verbosity level (0-2; higher value => more verbose)
"""

PRO_CONFIG_TEMPLATE = """### Part 1: parameters similar to Z-MERT
# target sentences file name (in this case, file name prefix)
-r	 <REF>

# references per sentence
-rps <NUMREFS>			

# parameter file
-p	 <TUNEDIR>/params.txt

#metric setting:
-m	 BLEU 4 closest
#-m	 TER nocase punc 5 5 joshua/zmert/tercom-0.7.25/tercom.7.25.jar 1
#-m	 TER-BLEU nocase punc 20 50  joshua/zmert/tercom-0.7.25/tercom.7.25.jar 1 4 closest
#-m	 METEOR en norm_yes keepPunc 2  #old meteor interface  #Z-MERT Meteor interface(not working)
#-m	 Meteor en lowercase '0.5 1.0 0.5 0.5' 'exact stem synonym paraphrase' '1.0 0.5 0.5 0.5' #CMU meteor interface

# maximum PRO iterations
-maxIt	 30

# file containing commands to run decoder
-cmd	 <TUNEDIR>/decoder_command   

# file prodcued by decoder
-decOut	 <TUNEDIR>/tune.output.nbest

# decoder config file
-dcfg	 <TUNEDIR>/joshua.config

# size of N-best list
-N	 300

# verbosity level (0-2; higher value => more verbose)
-v	 1


### Part2: PRO parameters
#-trainingMode can be 1,2,3,4
#1: train dense feature weights only
#2: train dense & sparse feature weights together
#3: train sparse feature weights only(with dense feature weights fixed) also works)
#4: treat sparse features as one component(summary feature), train dense and summary feature weights together

-trainingMode	1

#-nbestFormat can be "sparse" or "dense"
#for trainingMode 1: either "dense" or "sparse"
#for trainingMode 2-4: use "sparse" format

-nbestFormat	dense	#dense or sparse

#use one of the classifiers(and the corresponding parameter setting) below:
#1.perceptron paramters
-classifierClass	joshua.pro.ClassifierPerceptron
-classifierParams	'30 0.5 0.0'

#2.MegaM parameters
#-classifierClass	joshua.pro.ClassifierMegaM
#-classifierParams	'./megam_command ./megam_train.data ./megam_weights'

#3.Stanford Max-Ent parameters
#-classifierClass	joshua.pro.ClassifierMaxEnt
#-classifierParams	'./maxent_prop_file'

#4.LibSVM parameters
#-classifierClass	joshua.pro.ClassifierSVM
#-classifierParams	'./libsvm_command ./libsvm_train.data ./libsvm_train.data.model'

# num of candidate samples
-Tau	8000

# num of top candidates
-Xi	50

# linear interpolation coef. range:[0,1]. 1=using new weights only; 0=using previous weights only
-interCoef	0.5	

# threshold for sample selection
-metricDiff	0.05	
"""

PARAMS_TEMPLATE = """<PARAMS>
WordPenalty        ||| -2.844814  Opt  -Inf  +Inf  -5   0
OOVPenalty         ||| 1          Fix     0     0   0   0
normalization = absval 1 lm_0
"""

def write_template(template, path, lookup):
    """Writes a template file, substituting variables of the form <NAME> for values found
    in the 'lookup' hash.
    """

    out = open(path, 'w')
    for line in template.split('\n'):
        line = re.sub(r'<(.*?)>', lambda m: '{0}'.format(lookup[m.group(1)]), line)
        out.write(line + '\n')
    out.close()

def parse_tm_line(line):
    """Parses a TM line and returns the owner, span, and path. Works on both the
    old TM format:

      tm = moses pt 0 /path/to/grammar

    and the new one:

      tm = moses -owner pt -path /path/to/grammar -maxspan 0
    """
    line = re.sub(r'tm\s*=\s*', '', line).strip()

    owner = ''
    maxspan = ''
    path = ''
    if '-path' in line:
        # new format
        grammartype, rest = line.split(' ', 1)
        tokens = rest.split(' ')
        for i in range(0, len(tokens), 2):
            key = tokens[i]
            value = tokens[i+1]
            if key == '-path':
                path = value
            elif key == '-owner':
                owner = value
            elif key == '-maxspan':
                maxspan = value
    else:
        # old format
        grammartype, owner, maxspan, path = line.split(' ')

    return (owner, maxspan, path)

def get_features(grammar_path):
    """Opens the grammar at grammar_path and returns the list of features. Works for
    both packed grammars and unpacked grammars. For packed grammars, the feature list
    is complete, but for unpacked ones, only the features found on the first line are
    returned. Dense features (unlabeled ones) are returned as sequential numbers
    starting at 0."""

    features = check_output("%s/scripts/training/get_grammar_features.pl %s" % (JOSHUA, grammar_path), shell=True)
    return features.strip().split('\n')

def get_num_refs(prefix):
    """Determines how many references there are."""

    for ext in ['.', '']:
        if os.path.exists('%s%s0' % (prefix, ext)):
            suffix = 0
            while os.path.exists('%s%s%d' % (prefix, ext, suffix)):
                suffix += 1
            return suffix

    if os.path.exists(prefix):
        return 1

    return 0
    
def remove_if_present(file):
    if (os.path.isfile(file)):
        os.unlink(file)

def setup_configs(template, template_dest, target, num_refs, tunedir, command, config, output):
    """Writes the config files for both Z-MERT and PRO (which run on the same codebase).
    Both of them write the file "params.txt", but they use different names for the config file,
    so that is a parameter."""

    local_config = os.path.join(tunedir, 'joshua.config')
    remove_if_present(local_config)
    os.symlink(config, local_config)

    write_template(template, template_dest,
                   { 'REF': target,
                     'NUMREFS': num_refs,
                     'TUNEDIR': tunedir,
                     'DECODER_COMMAND': command,
                     'DECODER_CONFIG': local_config,
                     'DECODER_OUTPUT': output })

    # Parse the config file, looking for tms, lms, and feature
    # functions for which we need to provide initial weights
    params = []
    lm_i = 0
    for line in open(config):
        if line.startswith('tm ='):
            owner, span, path = parse_tm_line(line)

            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(config), path)

            for f in get_features(path):
                if re.match(r'^\d+$', f):
                    params.append('tm_%s_%s ||| 1.0 Opt -Inf +Inf -1 +1' % (owner, f))
                else:
                    params.append('%s ||| 0.0 Opt -Inf +Inf -1 +1' % (f))

        elif line.startswith('feature-function ='):
            if 'LanguageModel' in line:
                params.append('lm_%d ||| 1.0 Opt 0.1 +Inf +0.5 +1.5' % (lm_i))
                lm_i += 1
            else:
                ff = line.split(' ')[2]
                if ff in ['SourcePath', 'PhrasePenalty', 'Distortion']:
                    params.append('%s ||| 1.0 Opt -Inf +Inf -1 +1' % (ff))
                    
    paramstr = '\n'.join(params)
    write_template(PARAMS_TEMPLATE, '%s/params.txt' % (tunedir),
                   { 'REF': target,
                     'NUMREFS': num_refs,
                     'TUNEDIR': tunedir,
                     'PARAMS': paramstr })


def run_zmert(tunedir, source, target, command, config, output):
    """Runs Z-MERT after setting up all its crazy file requirements."""

    setup_configs(ZMERT_CONFIG_TEMPLATE, '%s/mert.config' % (tunedir),
                  target, get_num_refs(target), tunedir, command, config, output)

    tuner_mem = '4g'
    call("java -d64 -Xmx%s -cp %s/class joshua.zmert.ZMERT -maxMem 4000 %s/mert.config > %s/mert.log 2>&1" % (tuner_mem, JOSHUA, tunedir, tunedir), shell=True)

    final_config_path = os.path.join(tunedir, 'joshua.config.final')
    remove_if_present(final_config_path)
    os.symlink(os.path.join(tunedir,'joshua.config.ZMERT.final'), final_config_path)

    
def run_pro(tunedir, source, target, command, config, output):
    """Runs PRO after setting up all its crazy file requirements."""

    setup_configs(PRO_CONFIG_TEMPLATE, '%s/pro.config' % (tunedir),
                  target, get_num_refs(target), tunedir, command, config, output)

    tuner_mem = '4g'
    call("java -d64 -Xmx%s -cp %s/class joshua.pro.PRO -maxMem 4000 %s/pro.config > %s/pro.log 2>&1" % (tuner_mem, JOSHUA, tunedir, tunedir), shell=True)

    final_config_path = os.path.join(tunedir, 'joshua.config.final')
    remove_if_present(final_config_path)
    os.symlink(os.path.join(tunedir,'joshua.config.ZMERT.final'), final_config_path)

def error_quit(message):
    logging.error(message)
    sys.exit(2)

def handle_args(clargs):
    """
    Process the command-line options
    """
    class MyParser(argparse.ArgumentParser):
        def error(self, message):
            logging.error('ERROR: %s\n' % message)
            self.print_help()
            print(EXAMPLE)
            sys.exit(2)

    # Parse the command-line arguments.
    parser = MyParser(description='run the Z-MERT or PRO tuners ')

    parser.add_argument('source', help='path to source file')
    parser.add_argument('target', help='path to reference file (optionally a prefix)')
    parser.add_argument(
        '-d', '--tunedir', default='SDFW',
        help='path to tuning directory')
    parser.add_argument(
        '--tuner', default='zmert',
        help='which tuner to use: zmert (default) or pro')
    parser.add_argument(
        '--decoder', default='tune/decoder_command',
        help='The path to the decoder or wrapper script. This script is responsible for '
             'producing the output file in the location specified by the path passed to '
             '--decoder-output-file. It is not passed the source file, so it needs to arrange '
             'for that on its own.'
    )
    parser.add_argument(
        '--decoder-config', default='tune/model/joshua.config',
        help='location of decoder configuration file. This file is used to read the set of '
             'feature functions so that tuning parameters can be setup for each weight'
    )
    parser.add_argument(
        '--decoder-output-file', default='tune/output.nbest',
        help='location of n-best output file produced by --decoder')
    parser.add_argument(
        '--decoder-log-file', default='tune/joshua.log',
        help='location of decoder n-best log file')
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='print informational messages'
    )

    return parser.parse_args(clargs)

def main(argv):
    opts = handle_args(argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if opts.verbose else logging.WARNING,
        format='* %(message)s'
    )

    if not os.path.exists(opts.tunedir):
        os.makedirs(opts.tunedir)

    if opts.tuner == 'zmert':
        run_zmert(opts.tunedir, opts.source, opts.target, opts.decoder, opts.decoder_config, opts.decoder_output_file)

    elif opts.tuner == 'pro':
        run_pro(opts.tunedir, opts.source, opts.target, opts.decoder, opts.decoder_config, opts.decoder_output_file)


if __name__ == "__main__":
    try:
        assert JOSHUA
    except AssertionError:
        error_quit('ERROR: The JOSHUA environment variable must be defined.')

    main(sys.argv)
