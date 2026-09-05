# Tier 3 ablation: does a pretrained cross-encoder recover the Tier 1+2 residual?

Model: `cross-encoder/stsb-distilroberta-base` -- off-the-shelf, **not** fine-tuned on this project's data.

## Headline

HEADLINE: zero false matches across all 20 residual cases -- the cross-encoder never confidently asserted a wrong settlement link. On the 20 cases Tier 1 (deterministic + subset-sum) and Tier 2 (Splink + hybrid fuzzy) leave unresolved in stress_test/, the pretrained cross-encoder (cross-encoder/stsb-distilroberta-base, off-the-shelf, no fine-tuning) resolved 0 correctly, 0 incorrectly, and deferred 20. On legal_vs_trading_name -- the category this ablation primarily targeted, where the two sides' names are genuinely different strings and semantic understanding is supposed to beat lexical similarity -- the cross-encoder resolved 0/8, exactly as Tier 2 did. No lift, stated plainly. This is CONSISTENT WITH, not a failure to reproduce, the research literature's own caveat (spec section 11) that dense/LM methods do not reliably beat classical and probabilistic methods on short structured financial strings. On the other residual categories: abbreviation_variant 0/6 correct, 0 wrong, 6 deferred, invoice_description_mismatch 0/4 correct, 0 wrong, 4 deferred, transliteration_variant 0/2 correct, 0 wrong, 2 deferred. Why nothing cleared the bar: the highest score any residual credit's best candidate reached was 0.644592, against a threshold of 0.829885 derived from data/'s own labelled population. The ordering-versus-calibration split is the honest version of this negative result: the cross-encoder ranks the TRUE settlement first for 15/20 residual credits, so its ordering does carry real information -- but accepting that top-ranked candidate unconditionally would have produced 5 false matches, and there is no lower threshold available to reach those cases honestly -- even a threshold at data/'s own weakest known positive already admits 30399 of data/'s 30529 known negatives (99.57%). There is no threshold that buys the recall without buying the false matches.

## Integration recommendation

DO NOT INTEGRATE -- no lift. Zero false matches, but also zero cases resolved: the cross-encoder recovers nothing Tier 1+2 could not already handle, so wiring it in would add a transformer dependency, model weights and per-pair inference cost for a measured gain of zero. Stays a reported ablation finding.

## Threshold derivation (from `data/` only, never `stress_test/`)

- blocked candidate pairs scored: 30,704 (175 known positive, 30,529 known negative)
- known-positive scores: min 0.107633, mean 0.449878, max 0.866071
- known-negative scores: min 0.055597, mean 0.346492, max 0.828032
- known positives scoring BELOW the known-negative maximum: 172/175
- known negatives scoring ABOVE the weakest known positive: 30,399/30,529 (99.57%)
- known positives strictly above the known-negative maximum: [0.831737, 0.855163, 0.866071]
- **threshold = 0.829885**, the midpoint of that gap -- zero false matches against `data/`'s full known-negative population, at the cost of accepting only 3/175 known positives.

## Residual result (`stress_test/`, the 20 cases Tier 1+2 leave unresolved)

- residual credits: 20, open settlements: 20, blocked candidate pairs: 400
- highest score any residual credit's best candidate reached: 0.644592

| outcome | count |
|---|---|
| correct | 0 |
| **wrong (false match)** | **0** |
| deferred | 20 |
| total | 20 |

| defect class | total | correct | wrong | deferred |
|---|---|---|---|---|
| abbreviation_variant | 6 | 0 | 0 | 6 |
| invoice_description_mismatch | 4 | 0 | 0 | 4 |
| legal_vs_trading_name | 8 | 0 | 0 | 8 |
| transliteration_variant | 2 | 0 | 0 | 2 |

## Ranking diagnostic: ordering vs calibration

Whether the model's *ordering* is informative, ignoring the acceptance threshold entirely. This is a diagnostic, not a proposed policy -- accepting a top-ranked candidate with no threshold is exactly the behaviour spec section 9 rejects.

- true settlement ranked first for 15/20 residual credits
- accepting that top candidate unconditionally would have produced **5 false matches** (25% of the residual)

| defect class | total | true settlement ranked first |
|---|---|---|
| abbreviation_variant | 6 | 5 |
| invoice_description_mismatch | 4 | 4 |
| legal_vs_trading_name | 8 | 4 |
| transliteration_variant | 2 | 2 |

## Scope

This is an ablation, not an integration. `reconagent/match.py`, `reconagent/probabilistic.py` and `reconagent/fuzzy.py` are unmodified -- the live cascade does not call anything in this report.

