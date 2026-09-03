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

---

## Ingestion & parsing

`reconagent/` turns the three raw sources in `data/` into one normalized shape
every downstream unit (matcher, subset-sum solver, FX validator, eval harness)
consumes. This is a boundary layer, not a framework — plain dataclasses, no
ORM, no pydantic, no base-class hierarchy for one implementation per format.

### The canonical record

`reconagent/records.py` defines `CanonicalRecord`, the one shape a Razorpay
settlement, a bank credit (MT103 or camt.053), or an invoice all collapse
into, distinguished by `source`:

```
source, record_id, counterparty_name, narration, amount_minor, currency,
booking_date, value_date, created_at, settled_at,
utr, end_to_end_id, invoice_id, order_id, payment_id,
conversion_rate, foreign_amount_minor, foreign_currency, base_amount_minor,
channel, rows
```

Fields that don't apply to a given source are left at their default rather
than growing per-source variants — e.g. a Razorpay settlement has no natural
`counterparty_name` in its own columns (the counterparty is Razorpay itself,
a constant not worth carrying as text), so it's left blank rather than
hardcoded.

`rows` exists only on `razorpay_settlement` records: it carries the raw
`SettlementRow` per CSV row that fed the aggregate, because a refund row
converts at its own FX event and the variance-decomposition layer (spec §5,
owned by a later unit) needs that rate, not just the settlement's net.

### The money boundary

`reconagent/money.py` is the single choke point every raw value must pass
through before it becomes a money-path field. `parse_minor` and `parse_rate`
raise `FloatMoneyError` — a real, named exception, not a logged warning — the
instant a `float` (or `bool`, which is a `float`-shaped footgun via
`isinstance(x, int)`) reaches them. `Decimal(some_float)` is exactly the
accident this exists to prevent: it looks like a safe conversion but silently
inherits the float's binary rounding error, so the boundary parses only from
strings, ints already in minor units, or a `Decimal` built from a string.
Excess precision (e.g. a third decimal place on an INR amount) is also
rejected rather than silently rounded away — a reconciliation engine that
guesses which way to round a discrepancy has already lost the thing that
makes it trustworthy.

Every parser routes its amounts and rates through this module; there is no
parallel path that reaches a money field any other way.

### Three field-level parsers, one CSV loader

- **`reconagent/razorpay.py`** groups `razorpay_settlements.csv` rows by
  `settlement_id` and computes net as `sum(credit) - sum(debit)` across the
  group, so a settlement with a payment row plus a later refund row nets
  correctly instead of reading only the first row.
- **`reconagent/mt103.py`** splits the file into messages on the generator's
  `\n$\n` delimiter, then walks block 4 line by line, attaching a line that
  doesn't open a new `:NN:` tag to whichever tag came before it — this is
  what makes a `:70:` wrapped mid-word across four 35-character lines
  rejoinable by straight concatenation instead of read as separate fields.
  `:32A:` is parsed into its date/currency/amount components with an
  explicit regex, not tokenized generically. The SWIFT decimal comma is
  converted to a dot immediately before handing the string to
  `money.parse_minor`/`parse_rate` — the comma handling lives here, in the
  wire-format-specific parser, not in the general money boundary.
- **`reconagent/camt053.py`** is namespace-aware: it checks the document root
  is exactly `{urn:iso:std:iso:20022:tech:xsd:camt.053.001.02}Document`
  before reading anything, and raises `Camt053ParseError` rather than
  silently returning zero records if the namespace is missing or wrong — an
  empty result and "this file has no credits this period" should never look
  the same as "this is the wrong file". It targets `RltdPties/Dbtr/Nm` and
  `RmtInf/Ustrd` specifically, plus `CdtDbtInd` (used to skip a non-credit
  entry rather than misread it as one — this dataset's camt.053 covers every
  *credit*, and a debit needs handling this unit doesn't own), and both
  `BookgDt` and `ValDt`.
- **`reconagent/invoices.py`** parses `invoice_ledger.csv`. It isn't one of
  the three wire/export formats the task called out by name, but the spec
  frames the invoice ledger as the third of the system's three sources of
  truth (§3), and the canonical record's `invoice_id`/`order_id` fields are
  otherwise unpopulatable from anywhere — so it's included, deliberately
  small, as a flat CSV with no format-specific parsing to justify its own
  section.

### Malformed input: what raises, what doesn't

| Input | Behavior | Why |
|---|---|---|
| Truncated MT103 (no `{4:`/no `-}` trailer) | raises `MT103ParseError` | can't trust field boundaries in a corrupt message |
| MT103 missing `:20:` or `:32A:` | raises `MT103ParseError` | no id or no amount means no record |
| `:32A:` with an invalid date (e.g. month 13) | raises `MT103ParseError` | a silently-wrong value date breaks T+2..T+7 timing downstream |
| `:70:` wrapped mid-word across lines | rejoins exactly, no error | real SWIFT behavior, not corruption |
| camt.053 with missing/wrong namespace | raises `Camt053ParseError` | an empty result must not look like "no credits" |
| camt.053 entry with no `RmtInf` | narration defaults to `""` | a blank remittance field is real bank behavior |
| camt.053 entry missing `NtryRef`/`Amt` | raises `Camt053ParseError` | no id or no amount means no record |
| camt.053 `CdtDbtInd` other than `CRDT` | entry is skipped | out of scope for this unit, and skipping beats misreading a debit as a credit |
| CSV row with an empty amount | raises `RazorpayParseError`/`InvoiceParseError` | can't guess a settlement's net |
| CSV amount with a comma instead of `.` | raises (via `money.parse_minor`) | rejected, not silently reinterpreted |
| Settlement whose rows don't net as a single row | **not an error** — aggregates `sum(credit) - sum(debit)` across every row sharing a `settlement_id` | this is what the aggregation is for (a payment row plus a refund row is normal data, not corruption) |
| A `float` reaching any money or rate field | raises `FloatMoneyError` | the one rule this whole layer exists to make unbreakable |

### Tests

`tests/test_money.py` exercises the money boundary directly (float, bool,
excess precision, wrong separator, empty string). `tests/test_ingest.py`
parses the real `data/` files and `data/holdout/` — counts are computed from
the raw files rather than hardcoded, so they don't rot if `--scale` changes —
spot-checks exact field values against the source text/XML, and checks the
MT103/camt.053 cross-format correspondence (shared `:20:`/`EndToEndId`, exact
amounts). `tests/test_ingest_malformed.py` covers the edge cases in the table
above against hand-built minimal fixtures.

### A refund is not netted into its settlement

A settlement's `amount_minor` is its **capture** net — credits minus debits over
the non-refund rows. When a settlement carries a refund row, that refund is
deliberately left out of the total.

This is spec §5 taken literally: a refund converts at its own FX event and
settles as its own bank movement. Netting it against the capture produces a
figure that matches no bank credit, and it fails *silently* — the arithmetic
still balances, so the only symptom is the subset-sum solver missing exactly the
refund cases. The refund rows stay on `.rows`, which is where the FX layer wants
them anyway, since each carries its own conversion rate.

Regression-tested against the answer key: for every linked case in both splits,
the sum of parsed settlement nets equals `settlement_net_sum_minor`.

### Decisions flagged, not buried

- The invoice ledger parser (above) is scope this unit added beyond the three
  named formats, because the canonical record's invoice-side fields need a
  loader from somewhere and the spec names the ledger as one of the three
  sources of truth.
- `CanonicalRecord.amount_minor` for a `razorpay_settlement` record is the
  settlement's **net** in INR (`base_currency`), not the gross in the
  transaction's own currency — the gross/foreign leg is on
  `foreign_amount_minor`/`foreign_currency`. This mirrors what actually lands
  in the bank account, which is what a bank-credit record needs to compare
  against.
- A camt.053 entry with `CdtDbtInd != "CRDT"` is skipped rather than raised
  on, on the basis that this dataset's camt.053 covers only credits by
  construction and a debit is a different, out-of-scope record shape — not a
  malformed one. No such row currently exists in `data/`; this is a defensive
  default for real-world statements that do carry debits.
