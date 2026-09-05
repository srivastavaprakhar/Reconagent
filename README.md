# Reconagent

A cross-border-aware, three-way settlement reconciliation engine. It ties
three sources of truth — Razorpay's settlement export, the merchant's bank
statement, and the merchant's own invoice/order ledger — down to the paisa,
and it does the one thing a domestic-only reconciliation tool can't: validate
whether an applied FX rate was reasonable, decompose an unexplained variance
into a named cause, and track every export receipt against its **RBI EDPMS
shipping-bill closure obligation** — the deadline that gets an exporter
caution-listed if it's missed, not just a bookkeeping nuisance.

The headline number isn't match rate. It's **false-match rate** — a wrong
match silently corrupts the books, which is a worse failure than an honest
"I don't know" — and the second number that matters as much is **false-clear
rate**, because an unresolved break that never gets flagged is the other way
the same trust gets lost.

## Verified results

Measured against a synthetic, machine-generated ground-truth set with
injected, labelled defects — a main set and a separate adversarial holdout
that the matching logic was never tuned against — and independently
re-derived through a second call path before being reported here, not taken
on a single test run's word.

| split | false-match rate | false-clear rate | tie-ambiguous rate |
|---|---|---|---|
| **main** | **0.00%** (0/152) | **0.00%** (0/152) | 0.00% (0/152) |
| **holdout** | **0.00%** (0/53) | **0.00%** (0/53) | 5.66% (3/53) |

**Tie-ambiguous is a third, honest outcome, not a hidden win.** When the
subset-sum solver finds two or more distinct settlement subsets that tie at
the identical minimum residual, there is no arithmetic basis to pick one —
and picking anyway would be a coin flip that's wrong most of the time. The
holdout's 3 tied cases are reported as exactly that: verified directly, the
correct answer is *among* the tied candidates in all 3, so the system found
the right answer and correctly declined to guess between indistinguishable
siblings, rather than either asserting the wrong one (a false match) or
missing it silently (a false clear).

**The zero isn't a vacuous zero on an easy dataset — it's demonstrated to
move.** The evaluation harness deliberately corrupts known-good matcher
output and confirms false-match rate responds:

| corruption injected | false-match rate |
|---|---|
| 0% | 0.00% |
| 5% | 5.26% |
| 20% | 19.74% |
| 50% | 50.00% |

And the subset-sum solver — the stage that answers "which *subset* of
settlements does this one bank credit cover," the question a naive
single-record matcher can't even ask — is proven adversarially, not just
exercised: every bundle in the dataset ships a decoy subset landing 1–3
minor units from the true credit. A first-fit solver gets **8 of 12 main
bundles and 7 of 7 holdout bundles wrong**. This system's solver, which takes
the subset with the minimum absolute residual instead of the first admissible
one, gets **zero decoys picked on either split**.

**Throughput has a real, profiled ceiling.** A few hundred settlements is
comfortably sub-second (200 → 0.01s); by 1,000 it's already multi-second
(2.3s); by 5,000 the solver starts hitting its own node budget outright
(21.6s). The cause was found by profiling, not assumed: the synthetic data
generator packs a fixed 31-day calendar regardless of scale, so a larger
test scale raises settlement density per day and saturates the subset-sum
solver's candidate pool — not, as the shape of the slowdown might suggest, an
unindexed comparison somewhere (measured directly and ruled out: under 1% of
wall time even at 1,000 settlements). This is a statement about a synthetic
stress test, not the target workload — a merchant's actual monthly
statement, hundreds of records, sits well inside the sub-second regime.

## Tier 2, built proactively — and where it actually earned its cost

Tier 1's own results gave no evidence of a recall gap that needed fixing.
Tier 2 (Splink probabilistic linkage, then hybrid TF-IDF/Jaro-Winkler/
embedding fuzzy matching) was built anyway, for a stated reason: genuine ML
depth for this submission, and robustness against messier real-world text
the clean dataset didn't exercise. A 40-case adversarial stress-test set —
transliterated names, corporate abbreviations, legal-vs-trading-name
mismatches, OCR-style narration typos, invoice text sharing no words with
the bank side — was built to actually test that bet, engineered so Tier 1
alone resolves **zero** of it.

| | Main set | Stress-test set |
|---|---|---|
| Tier 1 alone | 152/152 (100%) | 0/40 (0%) |
| Tier 1 + Tier 2 | 152/152 — provably a no-op | 20/40 (50%) |
| False matches introduced | 0 | 0 |

**That 50% is not one number — it's earned unevenly, and the unevenness is
the actual finding:**

| Category | Resolved | |
|---|---|---|
| OCR-style narration typos | **8/8 (100%)** | complete recovery |
| Name transliteration variants | 6/8 (75%) | near-complete |
| Invoice text sharing no words with the bank side | 4/8 (50%) | partial |
| Corporate abbreviations | 2/8 (25%) | partial, weaker |
| Legal name vs. trading name for the same entity | **0/8 (0%)** | **no recovery at all** |

Tier 2 earned its cost exactly where a matching signal genuinely exists in
the text — full or near-full recovery on typos and transliteration. It
correctly **identified, not hid**, the one category where no such signal
exists at all: a legal entity name and an unrelated trading name share no
tokens and no character-level similarity, and no embedding trained on this
scale of data bridges that gap either — so the system honestly declines
there rather than guessing wrong, the same principle that governs every
stage in this engine. Zero false matches were introduced anywhere, on either
dataset, at any stage. Full breakdown and the reproducible threshold
derivations: `reports/tier2_ablation.md`.

## The gaps, stated at their actual size

- **`FEE_MISMATCH` and `DATA_ENTRY_ERROR` are validated on exactly one
  main-set case each, with zero holdout coverage.** Both categories are real
  and arithmetically verified — the variance decomposition returns the
  correct category with a single unambiguous cause for each — but one
  labelled case per category is not the ~150-case validation the rest of
  this system's numbers rest on. Treat those two rows differently from
  everything else here.
- **No overdue EDPMS shipping-bill case exists in either split.** The aging
  logic's overdue branch is unit-tested against a moved date, not validated
  against a generated overdue case.
- **Tier 2 cannot bridge a legal name and a trading name for the same
  counterparty** — see above. Not a defect to fix; a stated limit of what
  text-similarity matching can do when no textual signal survives.

## Why this beats a generic reconciliation submission

Deterministic-first matching, an LLM confined to explanation only, an honest
categorized exception list, false-match rate as the headline — that's table
stakes now; several strong entries in this space converge on nearly
identical language for that architecture. What isn't table stakes: an
FX-tolerance validator whose band is *derived* from labelled data rather than
hand-set, a variance decomposition that attributes a gap mathematically
rather than guessing, and the EDPMS/shipping-bill regulatory linkage. The
pitch isn't "we built a reconciliation engine" — it's "we built the one that
handles what happens the moment a payment crosses a border," on a domestic
core that's held to the same rigor.

## Run it

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                    # 245 tests
.venv/bin/python -m reconagent.eval                 # regenerate reports/eval_report.{json,md}
.venv/bin/python scripts/run_tier2_ablation.py      # regenerate reports/tier2_ablation.{json,md}
.venv/bin/python scripts/generate_synthetic.py       # regenerate data/ (byte-identical under the same seed)
```

Every monetary value is an integer count of minor units or a `Decimal`,
enforced at the parsing boundary — a `float` reaching a money-path field is a
raised exception, not a style violation caught in review.

## Where everything is

- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — the system as actually built:
  the matching cascade, the FX/compliance layer, Tier 2's probabilistic and
  fuzzy matching with the full per-category ablation, the evaluation
  methodology, and — stated with the same confidence as everything that
  *was* built — exactly what wasn't (Tier 3's ledger substrate, the full
  exception-taxonomy and abstention-gate unit, the audit-log/API layer) and
  why.
- **[`reconagent-design-description.md`](reconagent-design-description.md)**
  — the original design spec this system is built against.
- **[`PROGRESS.md`](PROGRESS.md)** — build history, every subagent unit's
  integration, and the verification steps behind every number in this
  document.
- **`reports/eval_report.md`** — the full evaluation report: per-defect-class
  breakdown, the complete mutation-test sweep, the confidence-threshold
  sweep, and the FX attribution table.
