# Tier 2 ablation: does Splink + hybrid fuzzy matching improve recall?

## Finding

On the main dataset, Tier 2 (Splink + hybrid fuzzy) provides zero measurable value: Tier 1 alone and Tier 1+2 produce identical match rates (100.00%). Tier 1 already resolves the main set completely, so Tier 2 never gets a case to add value on there -- provably a no-op, not a close call. On the stress-test dataset (built specifically so Tier 1 alone resolves nothing: match rate 0.00% under Tier 1 alone), Tier 2 raises match rate to 50.00% (+50.00%), with false_match count going from 0 to 0. Tier 2's gain on the stress set is uneven across defect classes, improving: abbreviation_variant (2/8), invoice_description_mismatch (4/8), ocr_typo_narration (8/8), transliteration_variant (6/8). It shows no improvement at all over Tier 1 (still 0 resolved) on: legal_vs_trading_name (0/8). In plain language: Tier 2 provides zero value on the main set (Tier 1 already resolves it completely) and partial, uneven value on the stress set -- real gains on several defect classes, no gain whatsoever on others. This is not softened or rounded up in either direction; it is what compute_metrics measured on this run.

## Delta tables (Tier 1 vs Tier 1+2)

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

## Full four-way metrics (detail, for a reader who wants to check the numbers)

### main_tier1

- total_linked: 152, true_link_count: 152, asserted_count: 152
- correct: 152, false_match: 0, false_clear: 0, tie_ambiguous: 0
- false_match_rate: 0.00%, false_clear_rate: 0.00%, tie_ambiguous_rate: 0.00%
- match_rate: 100.00%, precision: 100.00%, recall: 100.00%

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

### main_tier1+2

- total_linked: 152, true_link_count: 152, asserted_count: 152
- correct: 152, false_match: 0, false_clear: 0, tie_ambiguous: 0
- false_match_rate: 0.00%, false_clear_rate: 0.00%, tie_ambiguous_rate: 0.00%
- match_rate: 100.00%, precision: 100.00%, recall: 100.00%

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

### stress_tier1

- total_linked: 40, true_link_count: 40, asserted_count: 0
- correct: 0, false_match: 0, false_clear: 40, tie_ambiguous: 0
- false_match_rate: 0.00%, false_clear_rate: 100.00%, tie_ambiguous_rate: 0.00%
- match_rate: 0.00%, precision: n/a, recall: 0.00%

| defect class | total | correct | false match | false clear | tie ambiguous | match rate |
|---|---|---|---|---|---|---|
| abbreviation_variant | 8 | 0 | 0 | 8 | 0 | 0.00% |
| invoice_description_mismatch | 8 | 0 | 0 | 8 | 0 | 0.00% |
| legal_vs_trading_name | 8 | 0 | 0 | 8 | 0 | 0.00% |
| ocr_typo_narration | 8 | 0 | 0 | 8 | 0 | 0.00% |
| transliteration_variant | 8 | 0 | 0 | 8 | 0 | 0.00% |

### stress_tier1+2

- total_linked: 40, true_link_count: 40, asserted_count: 20
- correct: 20, false_match: 0, false_clear: 20, tie_ambiguous: 0
- false_match_rate: 0.00%, false_clear_rate: 50.00%, tie_ambiguous_rate: 0.00%
- match_rate: 50.00%, precision: 100.00%, recall: 50.00%

| defect class | total | correct | false match | false clear | tie ambiguous | match rate |
|---|---|---|---|---|---|---|
| abbreviation_variant | 8 | 2 | 0 | 6 | 0 | 25.00% |
| invoice_description_mismatch | 8 | 4 | 0 | 4 | 0 | 50.00% |
| legal_vs_trading_name | 8 | 0 | 0 | 8 | 0 | 0.00% |
| ocr_typo_narration | 8 | 8 | 0 | 0 | 0 | 100.00% |
| transliteration_variant | 8 | 6 | 0 | 2 | 0 | 75.00% |

