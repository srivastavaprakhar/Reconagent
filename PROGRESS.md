# PROGRESS

Updated after every integrated unit of work. Source of truth for scope:
`reconagent-design-description.md`. Rules: `CLAUDE.md`.

## Status: Tier 3 complete — TigerBeetle built, cross-encoder ablation a confirmed negative result. Holding for review before any further work.

### Tier 3, item 3a — TigerBeetle audit-log substrate (built)

Correction on the record first: the original plan scheduled a "Subagent G"
(FastAPI + audit log, Postgres fallback included) as part of Tier 1. That
unit was deferred at the Tier 1.5 checkpoint and never revisited. **No
audit-log persistence layer existed at all before this item** — this was
not a substrate swap, it was building the log itself.

Time-boxed at 30 minutes total with a hard 15-minute checkpoint (server
running + one working client round-trip, or abandon and build the Postgres
fallback instead). Checkpoint cleared in ~2.5 minutes; total effort under
15 minutes. Postgres was not even running locally when checked, so the
fallback path was never exercised.

`reconagent/audit_log.py`: decisions modelled as double-entry transfers —
`SUSPENSE → RECONCILED` for MATCHED/PARTIAL, `SUSPENSE → EXCEPTIONS`
otherwise. Spec §8's four fields map onto native transfer fields (stage ->
`code`, confidence -> `user_data_32` bps, a digest of compared fields ->
`user_data_128`, timestamp -> TigerBeetle's own cluster-assigned value).
Money stays an integer all the way to the storage engine.

**Two guarantees enforced by the substrate, not asserted in Python:**
append-only is enforced by TigerBeetle's own API (no UPDATE/DELETE, and a
transfer id derived from `bank_txn_id` blocks a contradictory resubmission
outright — **tamper-proof, not just tamper-evident**, confirmed by a test
that attacks the raw client directly and shows the forged write is refused
while the original record survives byte-for-byte); and the ledger's own
running balances must reconcile (`verify_log` reads TigerBeetle's balances
back, not a sum over the rows it just wrote).

**Verified independently, not taken on the build's own report:** binary
re-downloaded fresh into a separate location; full test suite re-run
against it (15/15 passed, 260/260 including the rest of the project's suite
with the binary present, 1 skipped unrelated to this unit); the real
demonstration re-executed from a clean server instance — `match_all`
against `data/`'s actual 152 settlements/credits, all 152 written, read
back, and verified with no exception, ledger balances internally
consistent (`suspense_debits == reconciled == 6,175,281,891` minor units,
`exceptions == 0`, matching that `data/` resolves entirely at Tier 1).

Not built: a live API surface, or wiring Tier 2/FX decision types into the
writer (the demonstration uses Tier 1 output, which is what `data/` actually
produces; the other result types carry compatible fields but aren't yet
piped through). Real remaining scope, stated plainly.

### Tier 3, item 3b — cross-encoder ablation (built and run — confirmed negative result)

Dispatched in parallel with 3a (independent, no shared files). `git diff` on
`reconagent/match.py`/`probabilistic.py`/`fuzzy.py` confirmed empty by a test
that checks git directly, not a prose scan — the live cascade was never
touched, matching the standing instruction.

`cross-encoder/stsb-distilroberta-base`, off-the-shelf, no fine-tuning
(chosen over the faster `stsb-TinyBERT-L-4` on the reasoning that a negative
result from a toy model proves nothing — worth noting `stsb-MiniLM-L-6-v2`,
the usual first pick, is gone from the Hub, 401, not a network fault).
Threshold `0.829885`, derived from `data/`'s own labelled population the
same way every other stage in this project derives its threshold — 175
known positives (range 0.108-0.866) against 30,529 known negatives (range
0.056-0.828), the worst separation of any stage built so far, with only 3
of 175 positives clearing the negative ceiling.

**Result against the 20-case `stress_test/` residual Tier 1+2 leaves
unresolved: 0 correct, 0 wrong, 20 deferred. `legal_vs_trading_name` —
the category this ablation specifically targeted — resolves 0/8, identical
to Tier 2.** Highest score any residual credit's best candidate reached was
0.644592, well short of the 0.829885 threshold.

**The nuance kept rather than flattened:** the model's *ranking* carries
real signal even though its *calibration* doesn't — it puts the true
settlement first for 15/20 residual credits (4/8 on `legal_vs_trading_name`
specifically). Accepting the top-ranked candidate unconditionally, no
threshold at all, would have produced 5 false matches out of 20 -- a 25%
false-match rate. No threshold rescues this: even one set at `data/`'s own
weakest known positive admits 99.57% of `data/`'s own known negatives.
There is no threshold that buys the recall without buying the false
matches.

Per the standing instruction: zero lift means the human-in-the-loop
integration gate is never triggered. Not wired in, stays a reported
ablation finding. Consistent with, not a failure to reproduce, spec §11's
own research caveat.

Verified independently before commit: `git status`/pyproject diff clean
(one dependency line, `sentence-transformers>=3.0`); `git diff --stat` on
every cascade file, `data/`, `stress_test/`, and every prior script/test
confirmed empty; the report JSON's `ranking_diagnostic` block
(5+4+4+2=15/20 ranked-first, matching the claim exactly) read directly, not
taken from the unit's own prose; the anti-cheat test (function-source scan,
scoped to the eight matching functions rather than the whole file, since
this is a flat report generator whose grading half legitimately reads
`stress_test/ground_truth.json`) read and confirmed real; the
no-cascade-modification test confirmed to check `git diff` directly rather
than scan prose; `tests/test_cross_encoder_ablation.py` re-run standalone,
17/17 passed.

### Tier 2 (built earlier, proactively — not reactively)

Tier 1's own evaluation showed no recall gap on the existing synthetic set
that would trigger Tier 2 under the design spec's own stated condition (§12).
Tier 2 is being built anyway, on a different, explicit basis: genuine ML
depth appropriate to this submission, and robustness against real-world
messiness the current clean dataset doesn't exercise. This is a proactive
build, not a reaction to a gap Tier 1 didn't have — recorded here so the
framing doesn't drift as later units land, and repeated in `ARCHITECTURE.md`
once the Tier 2 section is written there.

### Tier 2, unit 1 — stress-test dataset (`stress_test/`)

Separate from `data/` and `data/holdout/`, same `ground_truth.json` schema
(so `reconagent.eval`'s scoring machinery works against it unmodified —
verified directly: `compute_metrics` ran with no changes). 40 cases, 8 each
across five categories designed to defeat Tier 1's deterministic and
subset-sum matching specifically: transliteration variants, abbreviations
beyond the existing narration-mangling function's coverage, legal-name-vs-
trading-name mismatches, OCR-style narration typos, and invoice descriptions
sharing zero tokens with the settlement/bank text. Real MT103 text and real
camt.053 XML throughout, consistent with the rest of the project.

**Proof the dataset is genuinely hard, not asserted:** ran the unmodified
Tier 1 cascade (`reconagent.match.match_all`) against it directly —
**40/40 UNMATCHED, 0 accidentally resolved.** Locked into a test
(`test_tier1_cannot_resolve_the_overwhelming_majority_of_stress_cases`), not
left as a one-off claim.

**One real bug found in review, fixed before commit:** the amount-delta
generator (a signed mismatch meant to sit outside Tier 1's 100-minor-unit
tolerance but "close" — 0.20%-2.50% of the settlement net) also capped the
delta at a flat 8,000 minor units. Against this dataset's settlement range
(~INR 73K-50L), even the *lowest* bps draw already exceeded that cap, so
**all 40 deltas silently collapsed to the identical value 8000** regardless
of the random bps drawn — verified directly by computing every case's actual
delta, not by trusting the subagent's own report (which claimed a
300-8,000-unit range, i.e. real variation, that the generated data didn't
actually show). Ceiling removed in favour of a true sanity backstop far
above this dataset's amounts; regenerated; re-verified 40 distinct delta
values (973-106,862 rupees) and the 40/40-UNMATCHED proof still holds
post-fix. A regression test now asserts the distribution itself isn't
suspiciously uniform, not just that individual values look plausible in
isolation -- the existing per-case tolerance test would not have caught a
constant value, since a constant delta is still nonzero and still outside
tolerance.

### Tier 2, unit 5 — LLM explanation layer (built in parallel with units 2-3, per instruction)

`reconagent/explain.py`: `Verdict` (frozen, every field a plain stringified
value already-built from a `VarianceDecomposition` or `MatchResult`) is the
entire interface surface the LLM ever sees; `explain(verdict) -> str` makes
one raw `httpx.post` to the Anthropic Messages API and returns only a
sanitized string. No LangChain, per the earlier decision recorded above.
`ANTHROPIC_API_KEY` read from the environment; unset raises
`MissingApiKeyError` rather than guessing or silently degrading.

Verified structurally, not just by the unit's own claim: read the module
directly — `explain()`'s only return path is `_sanitize(text)`, a `str`;
nothing else comes back, no mutation of the input `Verdict`. The adversarial
test constructs an LLM response that explicitly tries to talk the system
into a different category, a different amount, and a fabricated settlement
id, and proves the original `Verdict` is byte-identical after the call
(frozen-dataclass equality) while the adversarial text passes through
inertly as display text only.

218 passed, 1 skipped (the live-API test, correctly gated on the env var
being set, which it isn't in this environment) — suite unaffected outside
`reconagent/explain.py`/`tests/test_explain.py`.

### Tier 2, unit 2 — Splink probabilistic pass (Stage 3)

`reconagent/probabilistic.py` — Splink 4.0.17, DuckDB-backed. Comparisons:
amount (exact / 0.5% / 1.5% / 3% bands), date (settled_at vs value/booking
date, 1/3/10-day bands), counterparty name (Jaro-Winkler, `NameComparison`).
Currency + a 30-day window is blocking, never a fuzzy comparison, mirroring
Tier 1's own pooling discipline. A razorpay_settlement record carries no
counterparty name of its own (Razorpay is the counterparty on that feed) --
the settlement-side name is resolved by joining through the invoice ledger,
which is why `invoices` is a required-in-practice parameter beyond Tier 1's
own contract.

**Training population and threshold, both derived from `data/` only, never
`stress_test/` or `data/holdout/`** -- verified two ways: independently
confirmed `match_with_tier2`'s default model is lazily trained from
`DEFAULT_DATA_DIR` (main) regardless of what split it's later asked to score,
and a source-level test (`test_probabilistic_module_never_reads_stress_or_holdout_ground_truth`)
regex-scans the module's actual string literals -- not its prose -- for a
`ground_truth.json` path naming either forbidden split. 140 single-settlement
training pairs from main's 152 linked cases (bundles excluded from m-training
as a wrong lesson for a pairwise model). Threshold `0.25`, sitting strictly
between the known-negative ceiling (0.213390) and the known-positive floor
(0.288721) -- both numbers reproduced independently, not taken from the
report.

**A real bug found and fixed mid-build, not papered over:** an early
per-credit design (one Splink `predict()` call per deferred credit) silently
corrupted the name comparison's term-frequency adjustment -- a 1-row credit
table makes every name look maximally rare -- and measurably cost recall
(9/40 vs 15/40 on the stress set, same model, same threshold). Fixed by
batching every deferred credit into one `predict()` call per invocation.

**Results, independently reproduced (not taken from the unit's own report):**

| | Main (`data/`) | Stress test |
|---|---|---|
| Tier 1 alone vs Tier 1+2 | 152/152 identical | -- |
| Stage 3 invoked at all | 0 times (nothing left unresolved) | 40 credits |
| Resolved correctly | -- | 15/40 |
| Resolved wrong | -- | **0/40** |
| Correctly deferred | -- | 25/40 |

Per-category: `legal_vs_trading_name` defers all 8 (weakest name signal, as
expected -- genuinely different strings for the same entity); `ocr_typo`/
`transliteration` resolve best (5/8 each); `abbreviation_variant` mostly
defers (1/8); `invoice_description_mismatch` splits evenly (4/8). Zero false
matches across the entire stress set -- the one number that had to hold for
this stage to be worth using at all.

**Composed entry point** (the interface the next two units build on):
`match_with_tier2(credits, settlements, *, invoices=(), splink_model=None) -> list[MatchResult | ProbabilisticMatchResult]`.
Runs Tier 1 unmodified first; escalates only what it leaves
UNMATCHED/AMBIGUOUS/TIE_AMBIGUOUS.

### Tier 2, unit 3 — hybrid fuzzy text matching (Stage 4)

`reconagent/fuzzy.py`. Primary signal: `0.6 x char-ngram(2,4) TF-IDF cosine +
0.4 x Jaro-Winkler` on counterparty name (settlement side resolved through
the invoice-ledger join, same technique Stage 3 used). Secondary: FAISS
(`faiss-cpu`, chosen over ChromaDB -- no server/persistence needed for an
in-process index over a few hundred already-blocked vectors) over a
corpus-local LSA embedding (TF-IDF -> TruncatedSVD, 64 components) -- not a
pretrained transformer, since network access to fetch one isn't guaranteed
here; documented as a real choice, not a stub. Fused via reciprocal rank
fusion (`1/(60+rank)` per signal, k=60, the spec's own suggested default),
never the dense signal alone. Blocking mirrors Stage 3 exactly: currency
match + 30-day window.

**Composed entry point** (what a later unit calls directly):
`match_with_full_cascade(credits, settlements, invoices=(), *, splink_model=None, fuzzy_model=None)`.
Calls `match_with_tier2` unmodified, resolves whatever it still leaves
deferred, splices in Stage 4's result only where it clears its own gates.

**Two independent gates, both derived from `data/` only, never
`stress_test/` or `data/holdout/`** -- verified: `DEFAULT_MATCH_THRESHOLD =
0.032655` sits strictly above the known-negative RRF ceiling (0.032522) on
main's full 30,704-pair blocked candidate space; `PRIMARY_SCORE_FLOOR =
0.660730` sits at the upper edge of a genuine bimodal gap in main's 175
known-positive `primary_score` values (0.422075-0.660730, empty on both
splits' data). Anti-cheat source scan replicated from Stage 3's own pattern.

**Why the second gate exists — a real bug, caught and fixed properly, not
papered over.** The first working version shipped with rank-agreement alone
and produced a genuine false match on `stress_test`: winning rank 1 on both
signals only means "the least-mediocre of however many candidates survived
blocking," not "a good match" -- `data/`'s own blocked pools rarely surface
this because its true matches are found by UTR, not name, so the gap only
showed up once this ran against a cross-border pool dense with plausible-
looking distractors. The fix is a second, independent gate on absolute
textual quality, still derived from `data/` alone (the bimodal split above)
-- not tuned against the specific case that surfaced it. This is the same
"a stress-test failure is motivation to look harder at the training data,
not license to peek at the answer key" discipline the rest of this build has
held throughout.

**Results, independently reproduced (not taken from the unit's own report):**

| | Main (`data/`) | Stress test |
|---|---|---|
| Stage 3 alone vs full cascade | 152/152 identical (no-op) | 15 -> 20 correct |
| Resolved wrong | 0 | **0/40, both stages** |
| Still deferred after Stage 4 | -- | 20/40 |

Per-category gain over Stage 3 alone: `ocr_typo_narration` +3 (now 8/8),
`transliteration_variant` +1, `abbreviation_variant` +1,
`invoice_description_mismatch` +0. **`legal_vs_trading_name` stays 0/8 --
an honest negative result, reported as one, not hidden.** The corpus-local
embedding isn't semantic enough to bridge a legal name and an unrelated
trading name once the name/text signal is this weak, and the conservative
floor correctly declines rather than guessing wrong. This is exactly the
category Stage 3 structurally couldn't touch either -- the one place in this
whole ablation where more matching machinery measurably did not help, stated
as plainly as everywhere it did.

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

### Tier 1.5 fix 3 — throughput cliff, profiled, and neither original guess was right

The 37x drop between 200 and 1,000 settlements was investigated by profiling
(cProfile + instrumented pool sizes and DFS node counts), not by acting on
either hypothesis on the table. Result: **it is neither** an unindexed O(n^2)
comparison in the driving loop nor simply "the subset-sum pool grows" in the
abstract -- both were measured directly and the driving loop's own rescan cost
under 1% of wall time even at 1,000 settlements.

The real mechanism: `scripts/generate_synthetic.py` packs a **fixed 31-day
calendar regardless of `--scale`**, so a larger scale raises settlement
density per day, and `_pool()`'s 30-day window admits denser candidate pools
as a direct result. Measured: mean pool size rises from 30/64 at scale 200 to
60/64 at scale 1,000 to 64/64 (truncating on 375 of 380 deferred credits) at
scale 5,000; mean DFS nodes per deferred credit rises 1,915 -> 62,092 ->
112,833. At scale 5,000 some credits hit `NODE_BUDGET` outright -- the search
is being cut off, not finishing.

One real inefficiency was found and fixed inside that search: `_enumerate`'s
cardinality prune recomputed `sum(amounts[j+1:j+slots])` -- a fresh slice-sum
-- on every DFS node, up to 8.18M calls at scale 1,000. Replaced with a
prefix-sum array built once per search, an O(1) lookup per node instead.
**Proven algebraically equivalent** (`prefix[hi]-prefix[j+1] == sum(...)`,
verified in review) and empirically identical: 20,000 randomized trials with
matching node counts, plus a field-for-field diff of every `MatchResult` on
both splits (152 main + 53 holdout) showing zero mismatches. Headline numbers
unchanged: main 152/152, false-match 0, false-clear 0; holdout 50/53, false-
match 0, false-clear 3 (same 3 genuine ties as before). ~10-15% faster at
1,000/5,000 settlements -- real, but modest, since the eliminated overhead
was never the dominant cost; millions of DFS nodes still run.

**Stated plainly, not papered over:** the remaining ceiling is structural.
Stage 2 cost is O(D x f(pool_size)) where pool_size saturates at `MAX_POOL`
well before 1,000 settlements given this generator's calendar, and `f` is the
combinatorial DFS. For the spec's actual target volumes -- a merchant's
monthly statement, hundreds to low thousands of records, not sustained
high-frequency streaming -- a few hundred settlements is comfortably
sub-second; ~1,000 is already multi-second; 5,000 starts hitting per-credit
`NODE_BUDGET` truncation rather than a clean search. Documented in
`reconagent/match.py`'s own module docstring so this doesn't need
rediscovering from a profiler next time.

**One pre-existing nondeterminism found, unrelated to this fix, flagged not
fixed:** `_credit_references` returns a `set`, so which of two equally-valid
reference fields gets reported in a Stage 1 result's `confidence`/`reason`/
`evidence` can differ across separate process runs (hash randomization).
Never affects `settlement_ids`/`resolution`, so no headline metric moves --
but the *explanation text* for a small number of matches isn't stable across
runs. Out of scope for a performance pass; noted for whoever touches Stage 1
next.

### Tier 1.5 fix 4 — the 3 holdout ties now report as TIE_AMBIGUOUS, not a miss

Scoped exactly to Stage 2's genuine subset-sum tie, per instruction -- Stage 1
already has a different, unrelated `AMBIGUOUS` case (a credit's narration
referencing several settlements' UTRs, a reference collision) that stays
`AMBIGUOUS`, untouched, still scored as a false clear if it ever fires (it
doesn't, on either split, today).

`reconagent/match.py` gained a `TIE_AMBIGUOUS` constant, used only in Stage 2's
existing tie branch (`if st.best_count > 1:` -- one line changed, confirmed by
diff: line 344's Stage 1 `resolution=AMBIGUOUS` untouched, line 692's Stage 2
branch is the only occurrence changed to `TIE_AMBIGUOUS`). No new routing or
queue mechanism was built -- E stays out of scope; a `TIE_AMBIGUOUS` result is
simply not in `ASSERTED = (MATCHED, PARTIAL)`, the same "not a match" status
`AMBIGUOUS`/`UNMATCHED` already had.

`reconagent/eval.py`'s `classify()` gained a fourth verdict, `"tie_ambiguous"`:
returned when ground truth has a link and the system's resolution is
`TIE_AMBIGUOUS`. False-clear rate's **denominator is unchanged**
(`true_link_count` -- the population that could have been missed doesn't
shrink); only the numerator excludes honest ties. A new `tie_ambiguous_rate`,
same denominator, is reported alongside it in the headline table -- not
silently subtracted away.

Verified independently (not just via the unit's own tests): the exact 3
holdout cases (`HOLDOUT-00032`, `HOLDOUT-00014`, `HOLDOUT-00041`,
`bank_txn_id`s `BNKH000013`/`BNKH000031`/`BNKH000040`) now classify as
`TIE_AMBIGUOUS`. Tally invariant `correct + false_match + false_clear +
tie_ambiguous == total_linked` holds exactly on both splits (main
152+0+0+0=152; holdout 50+0+0+3=53). Main is completely unaffected (main has
no ties).

**Corrected headline, both splits:**

| split | false-match rate | false-clear rate | tie-ambiguous rate |
|---|---|---|---|
| main | 0.00% (0/152) | 0.00% (0/152) | 0.00% (0/152) |
| holdout | 0.00% (0/53) | **0.00% (0/53)** | 5.66% (3/53) |

Holdout false-clear rate moves from 5.66% to 0.00% -- not because anything got
easier, but because those 3 cases were never misses. They were the matcher
finding the correct answer among mathematically indistinguishable siblings and
honestly declining to guess, and they were being scored identically to "found
no evidence at all" before this fix.

**One process note, on record:** the dispatch that built this fix was killed
mid-response by a transient DNS/network failure (not a rate limit) right after
its final regeneration step. Nothing partial was committed -- verified via
`git status` and `git log` before touching anything further. The interrupted
work was inspected directly rather than assumed broken or discarded on
reflex: syntax-checked, diffed line-by-line (confirming the Stage 1/Stage 2
split held exactly as scoped), the full suite re-run (192 green), and every
headline number above re-derived independently through a second call path.
It was complete and correct -- only the agent's own final report-back message
was lost -- so it was salvaged rather than re-run from scratch.

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
