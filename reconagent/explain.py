"""The narrow language-model piece from the design spec's Subagent E (spec
section 7): phrase an already-decided verdict for a human reader. Nothing
else.

Spec section 7 -- "The language model's one job" -- is explicit about the
boundary this module exists to hold: the model writes a one-line, plain-
English explanation of an exception that rules and arithmetic have already
decided. It is never given latitude to decide whether two records match
(spec section 6: "the category is decided by rules and arithmetic, never
by the language model"), and it has no code path through which it could
alter a financial figure. Two design choices make that structurally true
rather than a matter of discipline:

1. `Verdict` is the only shape `explain()` accepts, and every field on it
   is a plain, already-stringified fact -- a `str`, or a tuple of `str`s
   and `(str, str)` pairs. There is nothing mutable on it to smuggle a
   reference through, and nothing the model sees is a live pointer back
   into `VarianceDecomposition` or `MatchResult`.
2. `explain()` returns `str` and only `str`. There is no second return
   value, no callback, no mutation of the `Verdict` passed in. Whatever
   the model's response contains -- including something that reads like
   an instruction to change the verdict -- is text, and text is the only
   thing this function is capable of handing back.

A raw `httpx.post` call, not the SDK's Tool Runner and not LangChain: an
earlier decision on this project (see PROGRESS.md) dropped LangChain from
the tooling list for exactly this reason -- a direct HTTP call keeps "no
code path can alter a decision" something a reader can see in this file,
rather than something they have to trust an abstraction layer to uphold.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from reconagent.fx import VarianceDecomposition
from reconagent.match import MatchResult

# The environment variable explain() reads the Anthropic API key from.
# Never hardcode a key here; never commit one.
API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_MODEL = "claude-opus-5"
_MAX_TOKENS = 300
_REQUEST_TIMEOUT_SECONDS = 30.0

# A one-line gloss has no business being long. Whatever text comes back is
# collapsed to one line and capped here before it is returned -- decoration
# on the record, not a second source of truth for it, so there is nothing
# to "validate" beyond making sure it stays a short, plain string.
_MAX_EXPLANATION_CHARS = 400

_SYSTEM_PROMPT = (
    "You write a single plain-English sentence describing a reconciliation "
    "verdict that a separate rules engine has already decided from "
    "arithmetic. You do not decide, revise, confirm, question, or "
    "recompute anything: the category, the amounts, and the resolution "
    "below are fixed facts, not a proposal. Describe what already "
    "happened and why, for a human reviewer glancing at a report row. "
    "Reply with exactly one sentence and nothing else -- no preamble, no "
    "bullet list, no code, no suggested correction."
)


class MissingApiKeyError(RuntimeError):
    """Raised by `explain()` when no Anthropic API key is available.

    `explain()` never guesses at a credential and never silently falls
    back to an unauthenticated call -- if `api_key` is not passed and
    `ANTHROPIC_API_KEY` is unset, it raises this rather than returning a
    string built with no basis. Catch this and skip the explanation (or
    fall back to `verdict.category`/`verdict.facts` directly) when a key
    genuinely isn't available.
    """


@dataclass(frozen=True)
class Verdict:
    """The only thing `explain()` accepts, and the entire interface surface
    the language model ever sees.

    Every field is a plain, immutable value -- a `str`, or a tuple of
    `str`s / `(str, str)` pairs -- stringified at construction time, never
    a `Decimal`, a dataclass, or anything else the model's text could be
    mistaken for or fed back into. There is no mutable reference anywhere
    on this type for a response to smuggle a change through.

    Build one with `Verdict.from_variance` (a `VarianceDecomposition` from
    `reconagent.fx.decompose_variance`) or `Verdict.from_match` (a
    `MatchResult` from `reconagent.match`, optionally with the
    `reconagent.eval.classify()` verdict string attached for context).
    """

    kind: str  # "variance" | "match" -- which upstream verdict this came from
    category: str  # the already-decided label: attribution, or resolution
    record_id: str
    amounts: tuple[tuple[str, str], ...]  # (label, formatted value) pairs
    facts: tuple[str, ...]  # short facts: agreement/disagreement, arithmetic
    confidence: str | None = None  # pre-formatted; never Decimal math to redo

    @staticmethod
    def from_variance(decomposition: VarianceDecomposition) -> "Verdict":
        """Build a Verdict from a variance decomposition (reconagent.fx).

        `category` is `decomposition.attribution` -- decided by arithmetic,
        per spec section 6, never by this module or the model it drives.
        """
        amounts = (
            ("expected_net_minor", str(decomposition.expected_net_minor)),
            ("actual_net_minor", str(decomposition.actual_net_minor)),
            ("residual_minor", str(decomposition.residual_minor)),
            ("mdr_minor", str(decomposition.mdr_minor)),
            ("gst_minor", str(decomposition.gst_minor)),
            ("fx_spread_minor", str(decomposition.fx_spread_minor)),
        )
        facts = (decomposition.signature,) + tuple(
            f"candidate cause: {c}" for c in decomposition.candidates
        )
        if decomposition.fx is not None and decomposition.fx.deviation_bps is not None:
            facts = facts + (
                f"applied rate {decomposition.fx.applied_rate} vs reference "
                f"{decomposition.fx.reference_rate}: {decomposition.fx.deviation_bps} bps "
                f"deviation, tolerance +/-{decomposition.fx.tolerance_bps} bps",
            )
        return Verdict(
            kind="variance",
            category=decomposition.attribution,
            record_id=decomposition.settlement_id,
            amounts=amounts,
            facts=facts,
        )

    @staticmethod
    def from_match(result: MatchResult, classification: str | None = None) -> "Verdict":
        """Build a Verdict from a matching-cascade result (reconagent.match).

        `category` is `result.resolution` -- decided by the deterministic
        matcher or the bounded subset-sum solver, never by this module.
        `classification`, if given, is the `reconagent.eval.classify()`
        verdict string ("correct" / "false_match" / "false_clear" /
        "tie_ambiguous") folded in as context only; it never overrides
        `resolution`.
        """
        category = result.resolution if classification is None else f"{result.resolution} ({classification})"
        amounts = (
            ("credit_amount_minor", str(result.credit_amount_minor)),
            ("settlement_net_sum_minor", str(result.settlement_net_sum_minor)),
            ("residual_minor", str(result.residual_minor)),
        )
        facts = (result.reason,) + tuple(
            f"{fc.field}: credit={fc.credit_value!r} settlement={fc.settlement_value!r} "
            f"({'agreed' if fc.agreed else 'disagreed'})"
            for fc in result.evidence
        )
        return Verdict(
            kind="match",
            category=category,
            record_id=result.bank_txn_id,
            amounts=amounts,
            facts=facts,
            confidence=str(result.confidence),
        )


def _build_prompt(verdict: Verdict) -> str:
    amount_lines = "\n".join(f"- {label}: {value}" for label, value in verdict.amounts) or "- (none)"
    fact_lines = "\n".join(f"- {f}" for f in verdict.facts) or "- (none)"
    confidence_line = f"\nConfidence: {verdict.confidence}" if verdict.confidence is not None else ""
    return (
        f"Record: {verdict.record_id}\n"
        f"Decided category: {verdict.category}\n"
        f"Amounts (already computed, do not recompute):\n{amount_lines}\n"
        f"Facts (already established, do not re-evaluate):\n{fact_lines}"
        f"{confidence_line}\n\n"
        "Write the one-sentence gloss now."
    )


def _sanitize(text: str) -> str:
    """Collapse to one line and cap the length. This is a shape check, not
    a re-parse: whatever the model wrote stays exactly what it wrote, just
    trimmed to fit a report row -- nothing here reads the text for
    structure or feeds any part of it back into a decision."""
    one_line = " ".join(text.split())
    if len(one_line) > _MAX_EXPLANATION_CHARS:
        one_line = one_line[:_MAX_EXPLANATION_CHARS].rstrip() + "..."
    return one_line


def explain(verdict: Verdict, *, api_key: str | None = None) -> str:
    """A one-line, plain-English gloss of `verdict`, phrased by an LLM.

    Returns `str`. Nothing else -- no side effect, no mutation of
    `verdict`, no richer return shape. The model is handed only what
    `Verdict` carries (see its docstring) and is asked to describe it, not
    decide it; there is no code path here through which its response
    could change `verdict.category`, an amount, or anything upstream.

    Reads the API key from `api_key` if given, else from the
    `ANTHROPIC_API_KEY` environment variable. Raises `MissingApiKeyError`
    if neither is set -- this function never guesses at a credential and
    never makes an unauthenticated call.

    Makes exactly one `httpx.post` call to the Anthropic Messages API.
    """
    key = api_key or os.environ.get(API_KEY_ENV_VAR)
    if not key:
        raise MissingApiKeyError(
            f"{API_KEY_ENV_VAR} is not set and no api_key was passed to "
            "explain(). Set the environment variable, pass api_key= "
            "explicitly, or skip the explanation and report the verdict's "
            "own fields directly."
        )

    response = httpx.post(
        _MESSAGES_URL,
        headers={
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": _MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _build_prompt(verdict)}],
            "output_config": {"effort": "low"},
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    return _sanitize(text)
