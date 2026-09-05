# Reconciliation matcher -- evaluation report

## Headline: false-match rate and false-clear rate

tie-ambiguous rate is reported alongside false-clear rate, not folded into it: it is the same denominator (cases ground truth says are linked) but a different failure mode -- Stage 2 found the right settlements among several that tied on residual and honestly declined to guess, rather than finding no evidence at all.

| split | false-match rate | false-clear rate | tie-ambiguous rate |
|---|---|---|---|
| main | 0.00% (0/152) | 0.00% (0/152) | 0.00% (0/152) |
| holdout | 0.00% (0/53) | 0.00% (0/53) | 5.66% (3/53) |

## Match rate, precision, recall

| split | match rate | precision | recall |
|---|---|---|---|
| main | 100.00% | 100.00% | 100.00% |
| holdout | 94.34% | 100.00% | 94.34% |

## Tier 2 ablation: does Splink + hybrid fuzzy matching improve recall?

Tier 1 vs Tier 1+2, on the main set and the adversarial stress-test set built specifically because Tier 1 resolves nothing on it. Full detail: `reports/tier2_ablation.md`.

On the main dataset, Tier 2 (Splink + hybrid fuzzy) provides zero measurable value: Tier 1 alone and Tier 1+2 produce identical match rates (100.00%). Tier 1 already resolves the main set completely, so Tier 2 never gets a case to add value on there -- provably a no-op, not a close call. On the stress-test dataset (built specifically so Tier 1 alone resolves nothing: match rate 0.00% under Tier 1 alone), Tier 2 raises match rate to 50.00% (+50.00%), with false_match count going from 0 to 0. Tier 2's gain on the stress set is uneven across defect classes, improving: abbreviation_variant (2/8), invoice_description_mismatch (4/8), ocr_typo_narration (8/8), transliteration_variant (6/8). It shows no improvement at all over Tier 1 (still 0 resolved) on: legal_vs_trading_name (0/8). In plain language: Tier 2 provides zero value on the main set (Tier 1 already resolves it completely) and partial, uneven value on the stress set -- real gains on several defect classes, no gain whatsoever on others. This is not softened or rounded up in either direction; it is what compute_metrics measured on this run.

### Main dataset (`data/`)

| metric | tier 1 | tier 1+2 | delta |
|---|---|---|---|
| match rate | 100.00% | 100.00% | +0.00% |
| recall | 100.00% | 100.00% | +0.00% |
| false match (count) | 0 | 0 | +0 |
| false clear (count) | 0 | 0 | +0 |

| defect class | total | tier1 correct | tier1+2 correct | tier1 match rate | tier1+2 match rate | delta |
|---|---|---|---|---|---|---|
| clean_match | 107 | 107 | 107 | 100.00% | 100.00% | +0.00% |
| data_entry_error | 1 | 1 | 1 | 100.00% | 100.00% | +0.00% |
| edpms_open | 2 | 2 | 2 | 100.00% | 100.00% | +0.00% |
| fee_mismatch | 1 | 1 | 1 | 100.00% | 100.00% | +0.00% |
| fx_drift_benign | 10 | 10 | 10 | 100.00% | 100.00% | +0.00% |
| fx_drift_flagged | 5 | 5 | 5 | 100.00% | 100.00% | +0.00% |
| missing_remitter | 6 | 6 | 6 | 100.00% | 100.00% | +0.00% |
| partial_payment | 6 | 6 | 6 | 100.00% | 100.00% | +0.00% |
| refund_fx_asymmetry | 2 | 2 | 2 | 100.00% | 100.00% | +0.00% |
| subset_sum_bundle | 12 | 12 | 12 | 100.00% | 100.00% | +0.00% |

### Stress-test dataset (`stress_test/`)

| metric | tier 1 | tier 1+2 | delta |
|---|---|---|---|
| match rate | 0.00% | 50.00% | +50.00% |
| recall | 0.00% | 50.00% | +50.00% |
| false match (count) | 0 | 0 | +0 |
| false clear (count) | 40 | 20 | -20 |

| defect class | total | tier1 correct | tier1+2 correct | tier1 match rate | tier1+2 match rate | delta |
|---|---|---|---|---|---|---|
| abbreviation_variant | 8 | 0 | 2 | 0.00% | 25.00% | +25.00% |
| invoice_description_mismatch | 8 | 0 | 4 | 0.00% | 50.00% | +50.00% |
| legal_vs_trading_name | 8 | 0 | 0 | 0.00% | 0.00% | +0.00% |
| ocr_typo_narration | 8 | 0 | 8 | 0.00% | 100.00% | +100.00% |
| transliteration_variant | 8 | 0 | 6 | 0.00% | 75.00% | +75.00% |

## Coverage gaps

- no FEE_MISMATCH case in the holdout set
- no DATA_ENTRY_ERROR case in the holdout set
- no overdue EDPMS receipt in ground truth as of 2026-08-31

FX metrics: FX attribution accuracy is not included in this pass -- matching accuracy and FX attribution accuracy are different failure surfaces and this harness reports the former only.

## FX variance attribution (descriptive tally, not a matching-accuracy metric)

This table counts what `decompose_variance` produced for every settlement in the split -- it is not graded against ground truth here, so there is no correct/wrong column the way the per-defect-class breakdown below has one. Whether an attribution is *correct* would require grading against ground truth's `expected_exception_category`, a different question from "does this category exist and get produced", and is out of scope for this table. See the FX metrics note above: matching accuracy and FX attribution are different failure surfaces and stay separate.

| attribution | main | holdout |
|---|---|---|
| NO_VARIANCE | 172 | 79 |
| BENIGN_FX_DRIFT | 23 | 18 |
| FLAGGED_FX_DRIFT | 5 | 3 |
| FEE_MISMATCH | 1 | 0 |
| DATA_ENTRY_ERROR | 1 | 0 |
| UNRESOLVED | 0 | 0 |

## Per-defect-class breakdown (main)

| defect class | total | correct | false match | false clear | tie ambiguous | match rate |
|---|---|---|---|---|---|---|
| clean_match | 107 | 107 | 0 | 0 | 0 | 100.00% |
| data_entry_error | 1 | 1 | 0 | 0 | 0 | 100.00% |
| edpms_open | 2 | 2 | 0 | 0 | 0 | 100.00% |
| fee_mismatch | 1 | 1 | 0 | 0 | 0 | 100.00% |
| fx_drift_benign | 10 | 10 | 0 | 0 | 0 | 100.00% |
| fx_drift_flagged | 5 | 5 | 0 | 0 | 0 | 100.00% |
| missing_remitter | 6 | 6 | 0 | 0 | 0 | 100.00% |
| partial_payment | 6 | 6 | 0 | 0 | 0 | 100.00% |
| refund_fx_asymmetry | 2 | 2 | 0 | 0 | 0 | 100.00% |
| subset_sum_bundle | 12 | 12 | 0 | 0 | 0 | 100.00% |

## Throughput

| scale (settlements) | credits | seconds | records/sec |
|---|---|---|---|
| 200 | 152 | 0.0124 | 12249 |
| 1000 | 751 | 2.2610 | 332 |
| 5000 | 3728 | 21.2258 | 176 |

## Mutation test (harness credibility check, not a matcher metric)

Corrupts the matcher's *output* -- swaps a wrong settlement id/subset into already-computed, correct MatchResults -- to confirm false-match rate actually moves in response to real error, rather than reporting a vacuous zero.

| mutation rate | credits mutated | false-match rate |
|---|---|---|
| 0% | 0 | 0.00% |
| 5% | 8 | 5.26% |
| 20% | 30 | 19.74% |
| 50% | 76 | 50.00% |

Bundle wrong-subset check (MAIN-00003): true subset ['setl_ZkaJ0x6iq3cGOZ', 'setl_lmavHcmYTngV5s'] swapped for ['setl_NmPpS9Eus5hL6n', 'setl_hnBbv1PsmFGOad'] -- verdict correct -> false_match.

## Confidence threshold sweep (input for the abstention gate; no threshold is chosen here)

| threshold | main false-match | main false-clear | holdout false-match | holdout false-clear |
|---|---|---|---|---|
| 0.0 | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.1 | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.2 | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.3 | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.4 | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.5 | 0.00% | 0.00% | 0.00% | 0.00% |
| 0.6 | 0.00% | 0.00% | 0.00% | 7.55% |
| 0.7 | 0.00% | 5.92% | 0.00% | 7.55% |
| 0.8 | 0.00% | 5.92% | 0.00% | 7.55% |
| 0.9 | 0.00% | 13.82% | 0.00% | 22.64% |
| 1.0 | 0.00% | 100.00% | 0.00% | 94.34% |
