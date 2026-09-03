# Architecture

What this document is: an as-built description of the system, organized around
the same cascade the [design spec](reconagent-design-description.md) lays out,
stating plainly where the implementation matches the plan, where it deviates,
and — for Tier 2 and Tier 3 — why they were not built. Build history and the
exact verification steps behind every number here are in [`PROGRESS.md`](PROGRESS.md).
The judge-facing summary is [`README.md`](README.md); this document is the
detail underneath it.

---

## 1. What was built vs. what was planned

The spec's build sequencing (§12) calls for three tiers. **Tier 1 is complete.
Tier 2 and Tier 3 were not started** — not because time ran out mid-attempt,
but because Tier 1's own evaluation results (§8 below) gave no evidence that
either was needed, and the spec itself makes both conditional on exactly that
evidence existing first.

| Tier | Spec's condition to build it | What Tier 1's numbers show | Verdict |
|---|---|---|---|
| 2 — Fellegi-Sunter (Splink), hybrid fuzzy text | "if the deterministic-plus-subset-sum stages leave a real recall gap" | 0.00% false-clear on main; the holdout's only non-zero number is 3 cases correctly reported as genuine subset-sum ties (`TIE_AMBIGUOUS`), not misses — see §5 | Condition not met. Not built. |
| 3 — TigerBeetle ledger, cross-encoder ablation | "attempt only with time to spare, and only after Tier 1 and 2 are solid" | Tier 2 isn't started, so Tier 3's own precondition doesn't hold | Not built. |

This is stated as a design decision, not an apology. Building Tier 2 against a
dataset with no measured recall gap would have added two heavyweight
dependencies (Splink, a dense-embedding index) to chase a number that is
already at its floor, and would have made the one thing this repo can
currently claim — that every reported number is independently re-derived and
checked, not just asserted — harder to keep true as scope grew. The three
holdout ties in §5 are the one honest, measured argument for Tier 2 that
exists in this dataset, and it's a single well-understood failure mode
(genuine subset-sum ambiguity), not a general "the deterministic stages don't
generalize" pattern — the kind of targeted evidence that would justify Tier 2
specifically for that one failure mode if the volume of such cases grows,
rather than justifying the tier wholesale today.

---

## 2. Data flow, end to end

```
Razorpay settlement export (CSV)  ─┐
Bank statement (MT103 + camt.053) ─┼─▶ parsers ─▶ CanonicalRecord ─▶ matching cascade ─▶ MatchResult
Invoice / order ledger (CSV)      ─┘                                       │
                                                                            ▼
                                                          FX & compliance layer (decompose_variance,
                                                          reconcile_refund_fx, EDPMS aging, timing)
                                                                            │
                                                                            ▼
                                                              eval harness (reconagent/eval.py)
                                                                  → reports/eval_report.{json,md}
```

Everything downstream of ingestion consumes `CanonicalRecord` — the matcher,
the FX layer, and the eval harness never touch a raw CSV row or XML element
directly. Every monetary value is an integer count of minor units or a
`Decimal` from the moment it's parsed; a `float` reaching a money-path field
is a raised exception (`FloatMoneyError`), not a style violation caught in
review.

---

## 3. Synthetic ground-truth data (§2, §12 of the spec)

`scripts/generate_synthetic.py` emits the entire labelled dataset the cascade
is built and graded against. There is no real merchant data anywhere in this
repo, and there never will be.

**Real message formats, not CSV stand-ins.** Per spec §5/§12, the bank
statement is emitted as actual SWIFT MT103 text (block 1/2/3/4 structure,
`:50K:`/`:59:`/`:70:`/`:71A:`/`:32A:` fields, the SWIFT decimal comma, 35-char
line wrapping) and as valid ISO 20022 camt.053 XML (namespaced, `Dbtr`/`Cdtr`
blocks, `RmtInf/Ustrd`, `Bal` OPBD/CLBD). MT103 is cross-border only — a
domestic NEFT sweep is not an inbound wire — and both formats carry the same
underlying credits where a case appears in both, so a parser can be tested
against either for the same ground truth.

**Defect classes, main / holdout counts:**

| Defect class | Main | Holdout | Simulates |
|---|---:|---:|---|
| `clean_match` | 107 | 22 | Reference/UTR quoted verbatim, amount agrees |
| `subset_sum_bundle` | 12 | 7 | One bank credit sweeps several settlements net of fees |
| `fx_drift_benign` | 10 | 8 | Applied rate inside the band |
| `fx_drift_flagged` | 5 | 3 | Applied rate outside the band |
| `missing_remitter` | 6 | 6 | Ordering-customer info degraded in SWIFT transit |
| `partial_payment` | 6 | 4 | Credit covers only part of a settlement |
| `refund_fx_asymmetry` | 2 | 1 | Refund converts at its own FX event |
| `timing_pending` | 3 | 1 | Inside the T+2..T+7 nostro window |
| `edpms_open` | 2 | 2 | Open shipping-bill obligation against its FEMA deadline |
| `fee_mismatch` | 1 | 0 | Export's own tax column is stale vs. what was actually netted |
| `data_entry_error` | 1 | 0 | Credited amount is a digit transposition of the correct net |
| **Total cases** | **155** | **54** | |

**Subset-sum bundles are adversarially decoyed.** Every bundle ships a decoy
subset of the same settlement pool landing 1–3 minor units from the credit —
inside a naive tolerance band, at residual zero only for the correct subset.
This is what makes §5 below a real test of the solver rather than a
formality.

**The adversarial holdout** is generated by the same code with every defect
knob turned to its nastiest setting (decoy distance 1 minor unit instead of
3, FX deviations hugging the tolerance band from both sides, partial-payment
coverage at 97–99%, more mangled remitter narration) and is never tuned
against — enforced by a `HOLDOUT_` filename prefix and a
`DO_NOT_TUNE_ON_THESE_FILES.txt` marker, not just a convention.

`fee_mismatch`/`data_entry_error` were added to main after the first
end-to-end evaluation round, appended strictly after the original generation
loop so every previously-generated record stays byte-identical (verified via
`git diff`, not assumed) — see §9. The holdout gap for these two classes is
still open; see §10.

Full methodology, the tolerance-vs-label distinction, and reproducibility
details: [`README.md`](README.md) links here; the generator's own module
docstring is the executable version of this section.

---

## 4. Ingestion & the money boundary (§3 of the spec)

`reconagent/records.py` defines `CanonicalRecord`, the one shape a Razorpay
settlement, a bank credit (MT103 or camt.053), or an invoice all collapse
into. `reconagent/money.py` is the single choke point every raw value passes
through before becoming a money-path field: `parse_minor`/`parse_rate` raise
`FloatMoneyError` — a real exception, not a warning — the instant a `float`
reaches them, and reject excess decimal precision rather than silently
rounding it away.

Three field-level parsers (`razorpay.py`, `mt103.py`, `camt053.py`) target
specific fields rather than tokenizing whole lines — the MT103 parser walks
block 4 tag by tag and rejoins wrapped `:70:` continuations by concatenation;
the camt.053 parser is namespace-checked before it reads anything, and raises
rather than silently returning zero records on a namespace mismatch, because
an empty result must never look identical to "wrong file." A settlement's
`amount_minor` is its **capture net only** — a refund row is deliberately not
netted in, because it converts at its own FX event and settles as its own
bank movement; folding it in would produce a figure matching no bank credit,
and it would fail silently (the arithmetic still balances).

Malformed-input behavior (what raises vs. what's handled) is tabulated in
[`README.md`](README.md#ingestion--parsing) and enforced by
`tests/test_ingest_malformed.py`.

---

## 5. The matching cascade — Stage 1 and Stage 2 (§4 of the spec)

**Stage 1 (deterministic):** reference identity (UTR, end-to-end id, invoice
or order id, or a reference recovered from free-text narration as a whole
token, never a substring) plus amount within a ₹1 tolerance — sized from the
paisa-level rounding a fee/GST computation accumulates, not copied from the
answer key's labelling tolerance.

**Stage 2 (bounded subset-sum) is the spec's own headline correction (§4),
and it is measured, not just implemented.** A first-fit solver — take the
first subset inside tolerance — gets **8 of 12 main bundles and 7 of 7
holdout bundles wrong**, because every bundle ships an adversarial decoy. The
actual solver enumerates every admissible subset within a bounded DFS
(`MAX_CARDINALITY=8`, `MAX_POOL=64`, `NODE_BUDGET=2,000,000` — all explicit
parameters, and exceeding one is a *reported* `truncated` outcome, never a
silent wrong answer) and takes the **minimum absolute residual**, which the
correct subset always has exactly. Result: **zero decoys picked on either
split.**

**Genuine ties are a distinct, reported outcome — `TIE_AMBIGUOUS` — not a
miss.** When two or more distinct subsets land on the identical minimum
residual, there is no arithmetic basis to prefer one, and the solver reports
that honestly rather than guessing. This happens on 3 of the holdout's 7
bundles (its pools are larger and denser than main's by design). Verified
directly: in all 3, the ground-truth-correct subset *is* among the tied
candidates — the solver found the right answer and correctly declined to pick
it out from indistinguishable siblings. This is why `TIE_AMBIGUOUS` is scored
separately from `false_clear` in the eval harness (§9): scoring an honest,
correct abstention identically to "found no evidence at all" would hide a
real distinction from a reader of the false-clear number.

**Results:**

| | Main | Holdout |
|---|---|---|
| Credits | 152 | 53 |
| Correct | 152/152 | 50/53 |
| False-match rate | **0.00%** | **0.00%** |
| False-clear rate | **0.00%** | **0.00%** |
| Tie-ambiguous rate | 0.00% | 5.66% (3/53) |
| Decoys picked | 0 | 0 |

**A known, unfixed structural weakness:** credits are resolved oldest-first
and greedily — a wrong Stage 2 match can lock away a settlement a later
credit needed. Observed directly at a lower `MAX_CARDINALITY` (6) as a
cascading holdout failure; at the shipped value (8) it does not bite, but the
fix (a global assignment pass) is out of scope and the vulnerability is
documented, not hidden.

---

## 6. The FX & compliance layer (§5, §6 of the spec) — the differentiator

**The FX tolerance band is derived from the main-set label distribution, not
copied from the answer key's labelling metadata**: 3σ about zero over 25
legitimate international legs is 68.87 bps, rounded *down* to 65 (a
deliberate pre-committed choice — §9 leads with false-clear rate, so a
borderline case going to review is cheaper than a bad rate silently
clearing). That rounding-down choice is load-bearing: on the holdout, the
highest legitimate deviation is 49.20 bps and the lowest flagged one is 67.84
bps — a band of 70 (what rounding to nearest would give) misclassifies two of
three holdout flagged cases as benign.

**Variance decomposition** solves `net = gross − MDR − GST − FX_spread −
refund_adjustments`, written un-simplified so the reader can watch the FX
spread cancel out of the pure-amount arithmetic — which is exactly why a
benign and a flagged drift both leave residual zero and are separated by the
*rate* check, not the money. Each candidate cause (`FEE_MISMATCH`,
`DATA_ENTRY_ERROR`) is accepted only if restating its own term, and only that
term, closes the residual exactly; if two or more candidates both close it,
the result is `UNRESOLVED` with both listed — never resolved by rule order.
The identity closes to exactly zero on all 302 settlement rows across both
splits.

**Refund FX asymmetry**: a refund reconciles against its *own* conversion
event, not the capture's, so a "full" refund is confirmed full in the
foreign currency while the INR residual is non-zero by construction and
reported as expected, not a break.

**EDPMS aging**: `outstanding > 0 AND (partially realised OR overdue)` is an
open exception; a freshly issued export invoice with months to run is a young
receivable, not an exception on day one. `as_of` is a required parameter
everywhere — never `date.today()` — so an aging view is reproducible.

**Results, verified independently against the answer key:**

| | Main | Holdout |
|---|---|---|
| FX drift classified correctly | 15/15 | 11/11 |
| False-clear (flagged read as benign) | 0/5 | 0/3 |
| False-flag (benign read as flagged) | 0/10 | 0/8 |
| Refund FX asymmetry residuals correct | 2/2 | 1/1 |
| EDPMS open set | exact | exact |
| Timing-pending correctly held | 3/3 | 1/1 |

---

## 7. The exception taxonomy, as far as it goes

Spec §6 calls for a full named taxonomy (`FX_DRIFT_BENIGN`, `FX_DRIFT_FLAG`,
`FEE_MISMATCH`, `MISSING_SENDER`, `TIMING_PENDING`, `PARTIAL_PAYMENT`,
`REFUND_FX_ASYMMETRY`, `EDPMS_OPEN`, `DATA_ENTRY_ERROR`, `MANUAL_REVIEW`),
decided by rules and arithmetic, never the LLM. **What's built is the
arithmetic that decides six of these categories** (`decompose_variance`'s
`NO_VARIANCE`/`BENIGN_FX_DRIFT`/`FLAGGED_FX_DRIFT`/`FEE_MISMATCH`/
`DATA_ENTRY_ERROR`/`UNRESOLVED`, plus the matcher's own `PARTIAL`/
`TIE_AMBIGUOUS`/`UNMATCHED`, plus EDPMS aging and timing-pending as separate
checks) — the arithmetic backbone the full taxonomy would sit on top of. What
is **not** built: a single unified taxonomy module that collects all of these
into one named-category output, the confidence-calibrated abstention gate
that routes categories to auto-match/queue/clean-miss, and the LLM
explanation call. That unit (spec's "Subagent E") was deliberately deferred
at the Tier 1.5 checkpoint pending a scope decision, and stayed out of scope
for the rest of this build — see [`PROGRESS.md`](PROGRESS.md) for exactly
when and why.

**Explicitly not built, and not attempted:** the audit log / API layer
(spec's "Subagent G" — FastAPI service, hash-chained Postgres journal). Same
reason: deferred at the same checkpoint, never revisited.

---

## 8. Evaluation methodology and headline results (§9 of the spec)

`reconagent/eval.py` is the one place every metric in this repo is computed —
`classify(result, case)` is the single function every number routes through,
specifically so a bug in the definition is a bug in one place, not
independently re-derived (and possibly re-broken) in five report sections.

**Metric definitions** — a wrong match subset, a link asserted where the
truth is `UNMATCHED`, or the right subset with the wrong resolution
(`MATCHED` claimed where truth is `PARTIAL`) all count as `false_match`: each
is a concrete, wrong claim about the books. `false_clear` is truth having a
real link that the system asserted nothing for. `tie_ambiguous` — added at
the Tier 1.5 review specifically to fix a scoring gap, see §10 — is truth
having a real link where the system's answer is a genuine Stage 2 tie: found
the right answer among indistinguishable siblings, correctly declined to
pick. `false_match_rate`'s denominator is every linked case; `false_clear`
and `tie_ambiguous` share a narrower denominator (cases where a real link
exists to have been missed), so the two rates are directly comparable to each
other and neither is diluted by an unrelated population.

**Headline results:**

| split | false-match rate | false-clear rate | tie-ambiguous rate |
|---|---|---|---|
| main | **0.00%** (0/152) | **0.00%** (0/152) | 0.00% (0/152) |
| holdout | **0.00%** (0/53) | **0.00%** (0/53) | 5.66% (3/53) |

**Mutation testing — the credibility centerpiece §9 asks for.** A "0%
false-match" claim is worthless unless the metric provably moves when real
errors are introduced. The harness corrupts the *matcher's output*, not the
input (corrupting input would re-test the matcher, not the metric), and
sweeps corruption rate:

| mutation rate | false-match rate |
|---|---|
| 0% | 0.00% |
| 5% | 5.26% |
| 20% | 19.74% |
| 50% | 50.00% |

Monotonic, asserted so in a test. A dedicated case swaps a real bundle's
labelled subset for its labelled decoy: verdict flips `correct → false_match`
— the metric catches exactly the failure mode Stage 2 exists to prevent.

**Throughput** at 200/1,000/5,000 settlements: 11,907 / 330 / 172 records/sec
— not linear. Profiled, not guessed at: the cause is the synthetic
generator's fixed 31-day calendar packing more settlements into the same
window as scale rises, saturating Stage 2's pool at its cap well before 1,000
settlements and driving the combinatorial search cost up from there — not an
unindexed comparison in the driving loop, which was measured and found to
cost under 1% of wall time even at scale. One real inefficiency inside the
search (a per-DFS-node resum) was found and fixed, proven algebraically and
empirically equivalent (20,000 randomized trials, zero result differences on
either split), for a genuine but modest 10–15% throughput gain. The remaining
ceiling is structural and stated as such: comfortably sub-second at the
spec's actual target volumes (a merchant's monthly statement — hundreds to
low thousands of records), already multi-second in the low thousands, and the
search starts hitting its node budget outright by 5,000.

**FX attribution is reported, but deliberately not scored for accuracy in
this pass** — matching correctness and FX-attribution correctness are
different failure surfaces, and blending them into one number would hide
both. The decomposition breakdown (below) is a descriptive tally of what the
FX layer actually produced, wired natively into the report:

| attribution | main | holdout |
|---|---|---|
| `NO_VARIANCE` | 172 | 79 |
| `BENIGN_FX_DRIFT` | 23 | 18 |
| `FLAGGED_FX_DRIFT` | 5 | 3 |
| `FEE_MISMATCH` | 1 | 0 |
| `DATA_ENTRY_ERROR` | 1 | 0 |
| `UNRESOLVED` | 0 | 0 |

`UNRESOLVED` does no unearned work — it never fires on real data in either
split.

---

## 9. What changed at Tier 1.5 review, and why it matters

After the first end-to-end run, four gaps were found and closed before
declaring Tier 1 done — stated here because each one changed a number a
reader might otherwise take at face value:

1. **The FX attribution table above didn't exist in the report** — it had to
   be reconstructed by hand-calling the FX module. Now wired natively into
   `reports/eval_report.md`.
2. **`FEE_MISMATCH` and `DATA_ENTRY_ERROR` had zero ground-truth-validated
   cases** — implemented and unit-tested against hand-mutated fixtures, but
   never proven against real generated data. One case of each was added to
   the main split, appended strictly after the original generation loop so
   every prior record stays byte-identical (verified via `git diff`, not
   assumed) — see §10 for what's still open.
3. **The throughput cliff was profiled and its real cause identified** — see
   §8. Neither of the two most obvious guesses (an unindexed comparison; "the
   pool just grows") was the actual mechanism.
4. **Genuine Stage 2 ties were being scored identically to real misses.** The
   holdout's false-clear rate read 5.66% before this fix and 0.00% after —
   not because anything got easier, but because those 3 cases were never
   misses. This is the single most consequential correction in this list: it
   changed a headline number, and the corrected number is the one reported
   throughout this document.

Every one of these four fixes was independently re-derived through a second
call path before being accepted — not just taken on a subagent's or a single
test run's word. The verification steps are in `PROGRESS.md`.

---

## 10. Known limitations, stated plainly

- **`FEE_MISMATCH` and `DATA_ENTRY_ERROR` are validated on exactly one
  main-set case each, with zero holdout coverage.** Both are real,
  arithmetically verified cases (`decompose_variance` returns the correct
  category with a single, unambiguous candidate for each), not fixtures —
  but n=1 on one split is not the same claim as the ~150-case validation
  everything else in this document rests on. Don't read the FEE_MISMATCH/
  DATA_ENTRY_ERROR rows in §6/§8's tables as carrying the same statistical
  weight as the FX-drift or subset-sum rows next to them.
- **No overdue EDPMS receipt exists in either split** — the EDPMS aging
  logic's "overdue" branch is unit-tested by moving the `as_of` date past a
  real invoice's deadline, not validated against a generated overdue case.
- **Stage 2's greedy, oldest-first credit ordering is a known structural
  weakness** (§5) — not fixed, not hidden.
- **Stage 1's evidence-field reporting has a pre-existing nondeterminism**:
  when a credit carries two equally-valid reference fields to the same
  settlement, which one is named in the `evidence`/`reason` text of the
  `MatchResult` can differ across separate process runs (Python's hash
  randomization affects which field a `set` yields first). This never
  changes which settlement gets matched or any headline metric — only the
  human-readable explanation text for a small number of matches isn't
  stable run to run. Found during the throughput investigation, out of scope
  to fix there, not yet fixed.
- **Tier 2 and Tier 3 are not built at all** — see §1.
