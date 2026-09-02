# Project rules — read before any work

## Identity & git hygiene
- All commits use the git identity already configured locally (`git config user.name` / `user.email`) — never override it.
- Commit messages are plain, conventional-commit style (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`).
- NEVER add "Generated with Claude Code," "Co-Authored-By: Claude," any Anthropic/AI/assistant
  attribution, emoji signatures, or tool-generated trailers to any commit message, PR description,
  or file header. Commit messages describe only what changed and why, in the first person plural
  or imperative mood, exactly as a human engineer would write them.
- Before the first commit, confirm `git config commit.template` is unset and there is no
  `includeCoAuthoredBy` behavior active; if Claude Code's settings default to inserting a
  co-author trailer, disable it in `.claude/settings.json` before committing anything.

## Source of truth
- The authoritative spec is `reconagent-design-description.md` in the repo root. Read it in full
  before writing any code. If an implementation decision isn't covered there, flag it rather than
  guessing.

## Build discipline
- Money is `Decimal` or integer minor units everywhere. A float anywhere in a money-path variable
  is a bug, not a style issue — reject it at ingestion.
- Nothing is marked "done" without a passing test. No tier advances until the current tier's
  tests pass against the synthetic ground-truth set, including the adversarial holdout.
- Report false-match rate and false-clear rate as headline metrics in every eval run, not just
  match rate.

