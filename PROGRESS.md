# PROGRESS

Updated after every integrated unit of work. Source of truth for scope:
`reconagent-design-description.md`. Rules: `CLAUDE.md`.

## Status: Tier 1 — skeleton committed, A dispatched

### Tier 1 subagents
| # | Unit | State | Commit | Notes |
|---|------|-------|--------|-------|
| A | Synthetic data generator + ground_truth.json | not started | — | MT103 text + camt.053 XML, labelled defects, adversarial holdout |
| B | Ingestion & parsing (Razorpay / MT103 / camt.053) | not started | — | Decimal-or-minor-units enforced at boundary |
| C | Stage 1 deterministic + Stage 2 subset-sum | not started | — | |
| D | FX tolerance, variance decomposition, EDPMS aging | not started | — | tolerance band = parameter |
| F | Eval harness (false-match / false-clear headline, mutation test) | not started | — | runs last, against C+D output |
| E | Exception taxonomy, abstention gate, LLM explanation | **deferred** | — | Tier 1.5 checkpoint decides |
| G | FastAPI + hash-chained Postgres audit log | **deferred** | — | Tier 1.5 checkpoint decides |

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

## Open decisions (flagged, not guessed)
- LangChain is dropped from §11's tooling list for this build. If E is built, its single
  bounded explanation call is a raw `httpx` POST — the "no code path can alter a decision"
  boundary stays readable directly instead of traced through an abstraction layer.
- If E is built: provider and key come from an environment variable read at runtime,
  supplied via a local `.env` outside this session. Nothing hardcoded. E's tests stub the
  LLM call whether or not a live key is present.
- Postgres for the audit log (G): local `psql` is available, no database created yet.
