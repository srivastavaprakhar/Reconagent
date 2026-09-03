# Reconagent

A cross-border-aware, three-way settlement reconciliation engine: Razorpay's
settlement export, the merchant's bank statement, and the merchant's invoice
ledger, tied together down to the paisa.

Design spec: [`reconagent-design-description.md`](reconagent-design-description.md).
Build state and eval numbers: [`PROGRESS.md`](PROGRESS.md).

Every monetary value is an integer count of minor units or a `Decimal`. Never a
float — float arithmetic silently breaks equality checks in exactly the place a
reconciliation engine can least afford it.

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python scripts/generate_synthetic.py     # regenerate data/
.venv/bin/pytest
```

---

## Synthetic ground-truth data — generation methodology

`scripts/generate_synthetic.py` emits the entire labelled dataset the matching
cascade is built and graded against. There is no real merchant data anywhere in
this repo, and there never will be.

### What it emits

| Path | What it is |
|------|-----------|
| `data/razorpay_settlements.csv` | Razorpay settlement recon export, real column set (`entity_id`, `debit`/`credit`, `fee`, `tax`, `settlement_utr`, `conversion_rate`, `base_amount`, `base_currency`, …) |
| `data/bank_statement.mt103` | Real SWIFT MT103 text — block 1/2/3/4 structure, `-}` trailers, cross-border credits only |
| `data/bank_statement.camt053.xml` | Real ISO 20022 camt.053 XML — every credit, domestic and cross-border |
| `data/invoice_ledger.csv` | The merchant's own third source of truth |
| `data/fx_reference_rates.csv` | FBIL daily reference rates, the benchmark the FX validator checks applied rates against |
| `data/ground_truth.json` | The answer key. Labels every case |
| `data/holdout/` | The adversarial holdout, same schema, `HOLDOUT_`-prefixed |

Both statement formats carry the same underlying credits where a case appears in
both, so a parser can be tested against either format for the same ground truth.
MT103 is cross-border only because that is what is true of a real statement — a
domestic NEFT/RTGS sweep shows up as a statement entry, not as an inbound MT103.

### Why real message formats, not CSV stand-ins

The spec (§5, §12) is explicit that the generator produces actual MT103 text and
camt.053 XML rather than flat CSVs with MT-ish column names. The parser and the
data model cost the same either way, and building against realistic formats from
day one avoids a rewrite later. It is also the difference between a demo that
matches two CSVs and one that parses what a bank actually sends.

Concretely, the MT103 output carries the fields the fuzzy matcher will need to
target by name rather than by tokenizing a whole statement line:

- `:50K:` ordering customer (name, address, account)
- `:59:` beneficiary
- `:70:` remittance information — free text, wrapped at the real 35-character limit
- `:71A:` charge details (`SHA`/`OUR`/`BEN`)
- `:32A:` value date / currency / amount, with the SWIFT decimal **comma**
- `:33B:` and `:36:` original currency amount and applied rate, so an FX case is
  reconstructible from the wire alone
- `:72:` sender-to-receiver information carrying the RBI purpose code

The camt.053 side carries the same information in structured `Dbtr`/`Cdtr`
blocks and a dedicated `RmtInf`/`Ustrd` element, plus `Bal` (OPBD/CLBD),
`BkTxCd` domain/family/subfamily, and `Refs/EndToEndId` linking back to the
MT103's `:20:`.

### Defect classes and proportions

Clean matches dominate, as they do in production — a dataset where every record
is a defect would flatter the matcher and tell you nothing about false-match rate
at realistic volume.

| Defect class | Main | Holdout | What it simulates |
|---|---:|---:|---|
| `clean_match` | 107 | 22 | Reference/UTR quoted verbatim, amount agrees. The boring high-volume majority |
| `subset_sum_bundle` | 12 | 7 | One bank credit sweeps several settlements net of fees — no single record matches it |
| `fx_drift_benign` | 10 | 8 | Applied rate inside the band; legitimate interbank spread and provider markup |
| `fx_drift_flagged` | 5 | 3 | Applied rate outside the band; goes to variance decomposition, not auto-rejected |
| `missing_remitter` | 6 | 6 | Ordering-customer information absent or degraded in SWIFT transit |
| `partial_payment` | 6 | 4 | Credit covers only part of a settlement; the remainder stays open |
| `refund_fx_asymmetry` | 2 | 1 | Full refund converting at its *own* FX event, so INR does not net to zero |
| `timing_pending` | 3 | 1 | Settled inside the T+2..T+7 nostro window — a hold, not a break |
| `edpms_open` | 2 | 2 | Export receipt with an open shipping-bill obligation against its FEMA deadline |
| **Total cases** | **153** | **54** | |

### The subset-sum cases are decoyed on purpose

Every bundle ships with a decoy: a *different* subset of open settlements whose
nets sum to within **3 minor units** of the credit (**1 minor unit** in the
holdout). The correct subset always sums to the credit exactly — residual zero.

This makes the bundles solvable but unforgiving. A solver that accepts the first
subset inside its tolerance band will pick the decoy roughly half the time. A
solver that gathers every admissible subset and takes the one with the **minimum
absolute residual** gets all 19 right. Verified: across both splits there is no
bundle where two subsets tie at the minimum residual, and no bundle where the
minimum-residual subset is not the labelled one.

The decoy rows are worked backwards from a chosen net to the gross that produces
it, so `net == base − fee − GST` holds on the decoy exactly as it does on a real
settlement. A decoy with fabricated arithmetic would be trivially excluded for
the wrong reason.

### Tolerances are labelling metadata, not configuration

`ground_truth.json`'s `conventions` block publishes the tolerances the data was
*labelled* with (`amount_tolerance_minor: 100`, `fx_tolerance_bps: 50`). These are
published so a grader can see what a label means. They are **not** an input to
the matcher or the FX validator, which own their own bands as parameters — per
§5, the tolerance band is calibrated by the validator, not handed to it by the
data it is judging.

### The adversarial holdout

`data/holdout/` is generated by the same code path with every defect knob turned
to its nastiest setting, and is never to be tuned against. A
`DO_NOT_TUNE_ON_THESE_FILES.txt` and the `HOLDOUT_` filename prefix make that
hard to do by accident.

| Knob | Main | Holdout |
|---|---|---|
| Bundle cardinality | 2–4 of a 4–6 pool | 4–7 of a 6–9 pool |
| Decoy distance from credit | 3 minor units | **1 minor unit** |
| Benign FX deviation | ±44 bps | ±48 bps — right up against the 50 bps band |
| Flagged FX deviation | 150–400 bps | **55–90 bps** — barely outside the band |
| Partial-payment coverage | 40–80% | **97–99%** — nearly indistinguishable from clean |
| Remitter mangling | moderate | aggressive |
| EDPMS days to deadline | 5–60 | −25 to 10, i.e. some already overdue |
| Defect share of all cases | ~30% | ~59% |

The point of the holdout is not more volume, it is cases sitting on the exact
boundary where a threshold tuned on the main set stops generalising.

### Reproducibility

All randomness comes from a single seeded `random.Random`; nothing is derived
from the wall clock, and the statement period is a fixed calendar
(2026-08-01 … 2026-08-31) rather than "today". A rerun at the same `--seed` and
`--scale` is byte-identical — verified by regenerating over a copy and diffing.

```
.venv/bin/python scripts/generate_synthetic.py --seed 20260903 --scale 200
```

`--scale` sets the settlement count, which is how the eval harness will generate
datasets at several record-count scales to report throughput.

### Money handling

Every amount is an integer count of minor units in memory. It becomes a
fixed-point decimal string only at the serialization boundary, via
`Decimal(minor).scaleb(-2)`, which is exact. No float is constructed anywhere on
a money path, and `ground_truth.json` contains no JSON float — checked
programmatically, not by eye.
