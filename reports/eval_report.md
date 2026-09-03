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
| 200 | 152 | 0.0128 | 11907 |
| 1000 | 751 | 2.2785 | 330 |
| 5000 | 3728 | 21.6185 | 172 |

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
