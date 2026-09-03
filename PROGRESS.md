# PROGRESS

Updated after every integrated unit of work. Source of truth for scope:
`reconagent-design-description.md`. Rules: `CLAUDE.md`.

## Status: Tier 1.5 review in progress — closing four gaps found at checkpoint, one commit each

### Tier 1.5 fix 1 — F now reports the FX attribution table natively
`reconagent/eval.py` calls `reconagent.fx.decompose_variance` directly and adds
a "FX variance attribution" table to `reports/eval_report.md` (all six
categories always shown, zeros included) instead of that breakdown only being
reconstructable by hand-calling the FX module. Descriptive tally, not graded
against ground truth's `expected_exception_category` -- the existing note that
matching accuracy and FX accuracy are separate failure surfaces stays in place,
unchanged, directly above the new table.

### Tier 1.5 fix 2 — closed the FEE_MISMATCH/DATA_ENTRY_ERROR ground-truth gap (main split)
A's generator gained two case-builder functions, appended strictly *after* the
existing weighted-deck loop and consuming the same seeded RNG in its
then-current state -- verified via `git diff data/`: every existing settlement,
invoice, bank entry, and case is byte-identical; only new rows/entries/cases
were added, plus the totals and closing balance that describe them.

- **MAIN-00154 / `fee_mismatch`** (`setl_dFDWWnBI7gfwmP`): fee booked correctly
  at 2% MDR (INR 100.00 on INR 5,000.00 gross); the settlement export's own `tax`
  column is stale at the pre-rate-change 12% (INR 12.00) instead of the statutory
  18% (INR 18.00), while the amount actually credited used the correct 18%.
  Verified: `decompose_variance` returns `attribution=FEE_MISMATCH`,
  `candidates=('FEE_MISMATCH',)`, residual âˆ’600 minor units, no ambiguity.
- **MAIN-00155 / `data_entry_error`** (`setl_n5eXZlX1WZ68z1`): fee and GST both
  correct; the credited amount is the correct net (INR 6,346.60) with two adjacent
  digits transposed (INR 6,436.60) -- a swap that is mathematically guaranteed to
  be a multiple-of-9, digit-permutation difference, exactly the fingerprint
  `_is_transposition` checks for. Verified: `attribution=DATA_ENTRY_ERROR`,
  single candidate, residual 9000 minor units.

New main-split totals: 155 cases / 202 settlements / 152 bank credits / 202
invoices. Holdout is unchanged -- this gap remains open there deliberately
(scoped out of this fix, not forgotten; see Open decisions below).

New decomposition breakdown, main: `NO_VARIANCE=172, BENIGN_FX_DRIFT=23,
FLAGGED_FX_DRIFT=5, FEE_MISMATCH=1, DATA_ENTRY_ERROR=1, UNRESOLVED=0` (sums to
202). Matching accuracy unaffected: re-verified 152/152 correct, false-match 0,
false-clear 0 -- both new settlements are single-settlement, single-credit,
UTR-quoted clean matches at Stage 1, by design, so the anomaly lives only in
the decomposition layer.

`reconagent/eval.py`'s `COVERAGE_GAPS` reworded from a blanket "no
FEE_MISMATCH/DATA_ENTRY_ERROR case in ground truth" (now false for main) to
scope the still-true claim to the holdout set specifically.

### Tier 1 subagents
| # | Unit | State | Commit | Notes |
|---|------|-------|--------|-------|
| A | Synthetic data generator + ground_truth.json | **done** | (this commit) | 153 main cases + 54 holdout; 40 tests pass. Extended post-Tier-1.5 to 155 main cases (see below). |
| B | Ingestion & parsing (Razorpay / MT103 / camt.053) | **done** | (this commit) | 96 tests pass; float rule enforced on the record type, not just the parsers |
| C | Stage 1 deterministic + Stage 2 subset-sum | **done** | (this commit) | main 150/150 at commit time, now 152/152 against the extended dataset (unchanged code); holdout 50/53, false-match 0 on both, 0 decoys picked |
| D | FX tolerance, variance decomposition, EDPMS aging | **done** | (this commit) | 43 tests; band derived at 65 bps, 0 false-clear / 0 false-flag both splits |
| F | Eval harness (false-match / false-clear headline, mutation test) | **done** | (this commit) | 184 total tests; numbers below |
| E | Exception taxonomy, abstention gate, LLM explanation | **deferred** | — | Tier 1.5 checkpoint decides |
| G | FastAPI + hash-chained Postgres audit log | **deferred** | — | Tier 1.5 checkpoint decides |

### Dataset (A)
Main (at A's original commit): 153 cases / 200 settlements / 150 bank credits / 200 invoices.
Extended below to 155 / 202 / 152 / 202 once FEE_MISMATCH/DATA_ENTRY_ERROR cases landed.
Holdout: 54 cases, every defect knob hardened. Generator deterministic under
`--seed`, verified by regenerate-and-diff. No float on any money path, verified
programmatically. Subset-sum bundles verified unambiguous: the labelled subset is
the unique minimum-|residual| candidate in all 19 bundles across both splits.

### C results (verified independently against the answer key)
Main (at C's original commit): 150/150 credits correct, false-match 0, false-clear 0, 0.02s.
Re-verified 152/152 against the extended main dataset, still false-match 0, false-clear 0.
Holdout: 50/53, false-match 0, 3 abstentions, 0.84s.
Zero decoys picked on either split; a first-fit control posts the wrong subset on
8/12 main and 7/7 holdout bundles, so min-residual is load-bearing, not decorative.

The 3 holdout misses are genuine exact-sum collisions — several distinct subsets
of the open pool hit the credit at residual exactly 0, labelled subset among them.
Unresolvable by amount; needs a non-amount signal (Stage 3/4). Measured argument
for Tier 2.

**Ceiling on Stage 2, and the risk it creates for the abstention gate:** a
spurious subset is arithmetically indistinguishable from a real one, and the
confidence bands overlap (genuine 0.55-0.90, spurious 0.46 in my probe). Stage 2
is only safe because Stage 1 empties the pool first. Any threshold E picks cannot
cleanly separate the two.

### F results — TIER 1.5 HEADLINE NUMBERS (verified independently, reproduced via a second call path)

| split | false-match rate | false-clear rate | correct |
|---|---|---|---|
| main | **0.00%** (0/150) | **0.00%** (0/150) | 150/150 |
| holdout | **0.00%** (0/53) | **5.66%** (3/53) | 50/53 |

(Superseded below by the Tier 1.5 rerun against the extended 152-credit main set: still 152/152, 0.00%/0.00%.)

Mutation test proves the metric moves: 0%/5%/20%/50% corruption of the
matcher's *output* -> 0.00%/5.33%/20.00%/50.00% false-match, monotonic,
plus a dedicated bundle-decoy swap that flips a real case from correct to
false_match. Confidence threshold sweep produced (input for a future
abstention gate, not a recommendation).

Throughput is **not linear**: 11,169 rec/s at 200 settlements, 299 at 1,000,
162 at 5,000 -- a sharp phase transition as Stage 2's pool goes from sparse to
dense. Neither dataset used elsewhere in Tier 1 (150-200 settlements) enters
this regime.

Coverage gap carried forward from D, now stated in the eval report's own
output: no FEE_MISMATCH, no DATA_ENTRY_ERROR, no overdue EDPMS case in either
split -- no real accuracy number exists for those three paths.

**Full report:** `reports/eval_report.md` / `reports/eval_report.json`.

### D results (verified independently against the answer key)
Band 65 bps, derived from main-set labels (3σ-about-zero, rounded down), never
read from the answer key. Main: FX drift 15/15, false-clear 0/5, false-flag 0/10,
refund residuals 2/2, EDPMS exact, timing 3/3. Holdout: 11/11, 0/3, 0/8, 1/1,
exact, 1/1. Decomposition identity closes to exactly zero on all 302 rows.

Holdout margin is tighter than D's own report stated: D quoted 24.13 bps by
measuring only `fx_drift_benign` cases, but the highest *legitimate* leg is a
refund leg at 49.20 bps. True corridor: 49.20 (legit) < 65 (band) < 67.84
(flagged) — 15.8 bps headroom, not 24.1. Still correct on every case; the margin
is just thinner than claimed.

Rounding down was pre-committed and turned out load-bearing: a 70 bps band
(round-to-nearest) clears 2 of 3 holdout flagged cases as benign.

### Eval numbers
None yet. Headline metrics once F lands: **false-match rate**, **false-clear rate**
(main set and adversarial holdout), then precision/recall, then throughput by scale.

### Scope decision (2026-09-03)
Tier 1 is cut to exactly design doc §12: **A → B → (C, D) → F**. E and G are not
dispatched. Rationale on record: A+B+C+D+F alone yield a complete, defensible result —
match rate, false-match rate, false-clear rate, throughput, and a residual attribution
table (FX_DRIFT / FEE_MISMATCH / DATA_ENTRY_ERROR / UNRESOLVED) out of D's decomposition
math. Building E or G before F has produced numbers is adding scope ahead of the evidence
that justifies it.

**Tier 1.5 checkpoint — hard stop.** After F reports against both the main synthetic set
and the adversarial holdout, work halts and the numbers go back for a decision on whether
E and G are built, built reduced, or deferred behind Tier 2. Not a pass-through.

### Tier 2 / Tier 3
Blocked on the Tier 1.5 checkpoint. Requires explicit go-ahead before either starts.

## Repo conventions
- Python 3.12, venv at `.venv`, pytest. `.venv/bin/pytest` runs the suite.
- Package `reconagent/`, tests `tests/`, generated data `data/`, scripts `scripts/`.
- Commit attribution: `.claude/settings.json` sets `attribution.sessionUrl: false`
  (confirmed effective) and `attribution.coAuthoredBy: false` (key unverified).
  Real enforcement is `.githooks/commit-msg`, wired via `core.hooksPath`, which
  rejects any commit message mentioning Claude/Anthropic/AI attribution. Verified to
  fail closed: probe commits carrying "Generated with Claude Code" and a
  "Co-Authored-By: Claude" trailer were both rejected, neither entered history.

## Review findings on B, fixed before commit
Three defects the unit's own passing tests did not catch:
1. `CanonicalRecord`/`SettlementRow` had no `__post_init__`, so the float ban was
   enforced only by the parsers. Every downstream unit constructs these records
   directly — the rule would have rotted the moment C or D did. Guard added to the
   types themselves.
2. `parse_rate` accepted any `Decimal`, including `Decimal(some_float)`, returning
   a rate carrying 48 digits of binary noise straight into D's tolerance compare.
   Now rejects rates beyond 8 dp (real feeds publish 4).
3. The Razorpay parser netted refund rows into the settlement total, so the three
   refund cases produced amounts matching no bank credit. It fails silently — the
   arithmetic balances, and the only symptom is the subset-sum solver missing
   exactly those cases. Capture net now excludes refund rows; refunds stay on
   `.rows` for D. Verified against the answer key across both splits.

One test (`test_float_in_canonical_record_construction_raises`) claimed to exercise
record construction but passed the float to `parse_minor` first, so it re-tested
`parse_minor`. Replaced with tests that construct the record directly.

## Constraints discovered in the data (binding on downstream units)
- **Subset-sum solver (C) must rank by minimum absolute residual, not first-fit.**
  Every bundle carries a decoy subset landing 3 minor units from the credit (1 in
  the holdout), inside the labelling tolerance. The correct subset is always
  residual-zero and is the unique argmin, but a first-admissible-match solver will
  take the decoy roughly half the time.
- A settlement's `amount_minor` is the **capture** net; refunds are not netted in
  and live on `.rows` with their own conversion rate. D owns refund-FX asymmetry.
- Tolerances in `ground_truth.json.conventions` are labelling metadata only. C and D
  own their own bands as parameters; do not read them from the answer key.

## Coverage gaps in the dataset (for F, and for the Tier 1.5 decision)
- Neither split has a `FEE_MISMATCH` or `DATA_ENTRY_ERROR` case, and neither has
  an overdue EDPMS receipt. D implements and unit-tests all three by mutating one
  term of a real record, but they have no ground-truth validation. F cannot report
  a real number for them. Closing this needs new labelled cases from A.

## Open decisions (flagged, not guessed)
- LangChain is dropped from §11's tooling list for this build. If E is built, its single
  bounded explanation call is a raw `httpx` POST — the "no code path can alter a decision"
  boundary stays readable directly instead of traced through an abstraction layer.
- If E is built: provider and key come from an environment variable read at runtime,
  supplied via a local `.env` outside this session. Nothing hardcoded. E's tests stub the
  LLM call whether or not a live key is present.
- Postgres for the audit log (G): local `psql` is available, no database created yet.
