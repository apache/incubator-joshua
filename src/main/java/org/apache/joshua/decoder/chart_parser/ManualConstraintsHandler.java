/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *  http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
package org.apache.joshua.decoder.chart_parser;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;

import org.apache.joshua.corpus.Vocabulary;
import org.apache.joshua.decoder.ff.tm.Grammar;
import org.apache.joshua.decoder.ff.tm.Rule;
import org.apache.joshua.decoder.segment_file.ConstraintRule;
import org.apache.joshua.decoder.segment_file.ConstraintSpan;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * @author Zhifei Li, zhifei.work@gmail.com
 */

public class ManualConstraintsHandler {

  private static final Logger LOG = LoggerFactory.getLogger(ManualConstraintsHandler.class);

  // TODO: each span only has one ConstraintSpan
  // contain spans that have LHS or RHS constraints (they are always hard)
  private HashMap<String, ConstraintSpan> constraintSpansForFiltering;

  // contain spans that have hard "rule" constraint; key: start_span; value:
  // end_span
  private ArrayList<Span> spansWithHardRuleConstraint;
  private Chart chart;

  private Grammar grammarForConstructManualRule;

  public ManualConstraintsHandler(Chart chart, Grammar grammarForConstructManualRule,
      List<ConstraintSpan> constraintSpans) {
    this.chart = chart;
    this.grammarForConstructManualRule = grammarForConstructManualRule;
    initialize(constraintSpans);
  }

  private void initialize(List<ConstraintSpan> constraintSpans) {
    /**
     * Note that manual constraints or OOV handling is not part of seeding
     * */
    /**
     * (1) add manual rule (only allow flat rules) into the chart as constraints (2) add RHS or LHS
     * constraint into constraintSpansForFiltering (3) add span signature into
     * setOfSpansWithHardRuleConstraint; if the span contains a hard "RULE" constraint
     */
    if (null != constraintSpans) {

      for (ConstraintSpan cSpan : constraintSpans) {
        if (null != cSpan.rules()) {
          boolean shouldAdd = false; // contain LHS or RHS constraints?
          for (ConstraintRule cRule : cSpan.rules()) {
            /**
             * Note that LHS and RHS constraints are always hard, while Rule constraint can be soft
             * or hard
             **/
            switch (cRule.type()) {
              case RULE:
                // == prepare the feature scores
                // TODO: this require the input always specify the right number of
                // features
                float[] featureScores = new float[cRule.features().length];

                for (int i = 0; i < featureScores.length; i++) {
                  if (cSpan.isHard()) {
                    featureScores[i] = 0; // force the feature cost as zero
                  } else {
                    featureScores[i] = cRule.features()[i];
                  }
                }

                /**
                 * If the RULE constraint is hard, then we should filter all out all consituents
                 * (within this span), which are contructed from regular grammar
                 */
                if (cSpan.isHard()) {
                  if (null == this.spansWithHardRuleConstraint) {
                    this.spansWithHardRuleConstraint = new ArrayList<Span>();
                  }
                  this.spansWithHardRuleConstraint.add(new Span(cSpan.start(), cSpan.end()));
                }

                int arity = 0; // only allow flat rule (i.e. arity=0)
                Rule rule =
                    this.grammarForConstructManualRule.constructManualRule(
                        Vocabulary.id(cRule.lhs()), Vocabulary.addAll(cRule.foreignRhs()),
                        Vocabulary.addAll(cRule.nativeRhs()), featureScores, arity);

                // add to the chart
                chart.addAxiom(cSpan.start(), cSpan.end(), rule, new SourcePath());
                LOG.info("Adding RULE constraint for span {}, {}; isHard={}",
                    cSpan.start(), cSpan.end(),  cSpan.isHard() + "" + rule.getLHS());
                break;
              default:
                shouldAdd = true;
            }
          }
          if (shouldAdd) {
            LOG.info("Adding LHS or RHS constraint for span {}, {}",
                cSpan.start(), cSpan.end());
            if (null == this.constraintSpansForFiltering) {
              this.constraintSpansForFiltering = new HashMap<String, ConstraintSpan>();
            }
            this.constraintSpansForFiltering.put(getSpanSignature(cSpan.start(), cSpan.end()),
                cSpan);
          }
        }
      }
    }

  }

  // ===============================================================
  // Manual constraint annotation methods and classes
  // ===============================================================

  /**
   * if there are any LHS or RHS constraints for a span, then all the applicable grammar rules in
   * that span will have to pass the filter.
   * 
   * @param i LHS of span, used for genrating the span signature
   * @param j RHS of span, used for genrating the span signature
   * @param rulesIn {@link java.util.List} of {@link org.apache.joshua.decoder.ff.tm.Rule}'s
   * @return filtered {@link java.util.List} of {@link org.apache.joshua.decoder.ff.tm.Rule}'s
   */
  public List<Rule> filterRules(int i, int j, List<Rule> rulesIn) {
    if (null == this.constraintSpansForFiltering) return rulesIn;
    ConstraintSpan cSpan = this.constraintSpansForFiltering.get(getSpanSignature(i, j));
    if (null == cSpan) { // no filtering
      return rulesIn;
    } else {

      List<Rule> rulesOut = new ArrayList<Rule>();
      for (Rule gRule : rulesIn) {
        // gRule will survive, if any constraint (LHS or RHS) lets it survive
        for (ConstraintRule cRule : cSpan.rules()) {
          if (shouldSurvive(cRule, gRule)) {
            rulesOut.add(gRule);
            break;
          }
        }
      }
      return rulesOut;
    }
  }

  /**
   * should we filter out the gRule based on the manually provided constraint cRule
   * @param cRule constraint rule
   * @param gRule rule which may be filtered
   * @return true if this gRule should survive
   */
  public boolean shouldSurvive(ConstraintRule cRule, Rule gRule) {

    switch (cRule.type()) {
      case LHS:
        return (gRule.getLHS() == Vocabulary.id(cRule.lhs()));
      case RHS:
        int[] targetWords = Vocabulary.addAll(cRule.nativeRhs());

        if (targetWords.length != gRule.getEnglish().length) return false;

        for (int t = 0; t < targetWords.length; t++) {
          if (targetWords[t] != gRule.getEnglish()[t]) return false;
        }

        return true;
      default: // not surviving
        return false;
    }
  }

  /**
   * if a span is *within* the coverage of a *hard* rule constraint, then this span will be only
   * allowed to use the mannual rules
   * @param startSpan beginning node (int) for span
   * @param endSpan end node (int) for span
   * @return true if this span containers a rule constraint
   */
  public boolean containHardRuleConstraint(int startSpan, int endSpan) {
    if (null != this.spansWithHardRuleConstraint) {
      for (Span span : this.spansWithHardRuleConstraint) {
        if (startSpan >= span.startPos && endSpan <= span.endPos) return true;
      }
    }
    return false;
  }

  private String getSpanSignature(int i, int j) {
    return i + " " + j;
  }

  private static class Span {

    int startPos;
    int endPos;

    public Span(int startPos, int endPos) {
      this.startPos = startPos;
      this.endPos = endPos;
    }
  }

}
