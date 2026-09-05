"""Append-only audit log for reconciliation decisions, on TigerBeetle.

Spec section 8 wants every decision -- which stage resolved it, the confidence
score, the fields compared, and the timestamp -- written to an append-only log
that an auditor can trust. TigerBeetle is the substrate: a double-entry ledger
with deterministic execution, so the append-only property is enforced by the
database rather than asserted by us.

WHY TIGERBEETLE'S MODEL FITS
----------------------------
TigerBeetle has no generic "log table" -- it has `Account` and `Transfer`.
That constraint is the point. We model reconciliation as what it actually is:
money moving out of an unreconciled pool into either a reconciled pool or an
exceptions pool.

    SUSPENSE  --(one transfer per decision)-->  RECONCILED  (MATCHED/PARTIAL)
              --(one transfer per decision)-->  EXCEPTIONS  (everything else)

Two invariants come free from the substrate, and neither is something we
check in Python:

1. **Append-only is enforced, not conventional.** A transfer's id is a
   deterministic hash of its `bank_txn_id`, so re-submitting a decision for a
   bank credit that was already logged is rejected by TigerBeetle itself with
   `EXISTS_WITH_DIFFERENT_*`. There is no UPDATE and no DELETE in the API to
   begin with; the derived id closes the remaining hole, which is writing a
   second, contradictory row for the same credit. See `test_audit_log.py`'s
   tamper test -- the write is refused and the original record survives byte
   for byte.

2. **The books have to balance.** Debits out of SUSPENSE must equal credits
   into RECONCILED plus credits into EXCEPTIONS, because TigerBeetle will not
   commit a transfer that doesn't balance. A decision cannot be silently
   dropped or double-counted without the account balances disagreeing, and
   `verify_log` reads those balances back from the ledger to prove it.

FIELD MAPPING (spec section 8's four fields)
--------------------------------------------
    which stage resolved it  ->  Transfer.code          (STAGE_CODES)
    the confidence score     ->  Transfer.user_data_32  (basis points, 0..10000)
    the fields compared      ->  Transfer.user_data_128 (digest, see below)
    the timestamp            ->  Transfer.timestamp     (assigned by the cluster)

plus:
    the amount               ->  Transfer.amount        (integer minor units)
    what it resolved to      ->  Transfer.user_data_64  (digest of settlement_ids)

`amount` is TigerBeetle's native 128-bit unsigned integer and we pass
`credit_amount_minor` straight into it. No float ever touches this path, which
is the project's money rule holding all the way down to the storage engine.

"The fields compared" is stored as a 128-bit digest rather than the text
itself -- TigerBeetle rows are fixed 128-byte structs with no blob column. The
digest is over a canonical serialization of the evidence (see
`_evidence_digest`), so it is tamper-evident: the auditor keeps the evidence
alongside, and any edit to it fails to reproduce the digest the ledger holds.

ID MAPPING
----------
TigerBeetle ids are 128-bit integers; our identifiers are strings. Every
string id is folded through BLAKE2b-128 (`_hash128`) -- deterministic, stable
across processes and runs, and collision-resistant far past the scale of a
reconciliation run. 0 and 2^128-1 are reserved by TigerBeetle, so the hash is
nudged off them in the (never-observed) event it lands there.

SETUP
-----
Needs a running TigerBeetle cluster. For a local single-replica one:

    curl -Lo tigerbeetle.zip https://mac.tigerbeetle.com && unzip tigerbeetle.zip
    ./tigerbeetle format --cluster=0 --replica=0 --replica-count=1 \\
        --development ./recon.tigerbeetle
    ./tigerbeetle start --addresses=3033 --development ./recon.tigerbeetle

then point this module at it with `TIGERBEETLE_ADDRESS=3033` (or pass
`address=` explicitly). `tests/test_audit_log.py` starts its own throwaway
cluster, so the suite needs only the binary on PATH or in
`TIGERBEETLE_BINARY`, and skips cleanly when neither is present.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Iterator, Protocol, Sequence

import tigerbeetle as tb

from reconagent.fuzzy import STAGE_FUZZY
from reconagent.match import STAGE_DETERMINISTIC, STAGE_SUBSET_SUM
from reconagent.probabilistic import STAGE_PROBABILISTIC

__all__ = [
    "AuditRecord",
    "LEDGER",
    "STAGE_CODES",
    "AuditLog",
    "decision_id",
]

# One ledger; all three accounts live on it. Cross-ledger transfers are
# rejected by TigerBeetle, which is exactly what we want -- a decision can
# never wander into some other book.
LEDGER = 1

# Chart of accounts. Every bank credit enters as unreconciled and leaves to
# exactly one of the other two.
ACCOUNT_SUSPENSE = 1  # bank credits not yet attributed
ACCOUNT_RECONCILED = 2  # attributed to settlements (MATCHED / PARTIAL)
ACCOUNT_EXCEPTIONS = 3  # everything the cascade declined to resolve

ACCOUNT_CODE = 1

# `Transfer.code` is a u16 and must be non-zero. Stable, explicit numbers --
# never derived from the string, so renaming a stage constant can't silently
# re-code history already on the ledger.
STAGE_CODES: dict[str, int] = {
    STAGE_DETERMINISTIC: 1,
    STAGE_SUBSET_SUM: 2,
    STAGE_PROBABILISTIC: 3,
    STAGE_FUZZY: 4,
}
STAGE_CODE_UNKNOWN = 999
_CODE_TO_STAGE = {code: stage for stage, code in STAGE_CODES.items()}

# Resolutions that mean the credit was attributed to settlements.
RESOLVED = frozenset({"MATCHED", "PARTIAL"})

# TigerBeetle reserves both ends of the 128-bit id range.
_ID_MIN = 1
_ID_MAX = (1 << 128) - 2

# A single get_account_transfers query is capped by the message body size at
# 8189 results. Reconciliation runs here are ~150 decisions, so one query is
# plenty.
# ponytail: single-page read, paginate on timestamp_min if a run ever exceeds
# ~8k decisions.
_QUERY_LIMIT = 8189


class Decision(Protocol):
    """The shape `MatchResult`, `ProbabilisticMatchResult` and
    `FuzzyMatchResult` have in common. The confidence field is named
    differently on each (`confidence` / `match_probability` /
    `combined_score`), so it is read via `_confidence` rather than declared
    here."""

    bank_txn_id: str
    stage: str
    resolution: str
    settlement_ids: tuple[str, ...]
    credit_amount_minor: int
    reason: str


@dataclass(frozen=True)
class AuditRecord:
    """One decision, read back off the ledger.

    `timestamp` is TigerBeetle's own, in nanoseconds since the Unix epoch,
    assigned by the primary at commit time -- not a clock we control, which is
    the point of asking the ledger for it.
    """

    decision_id: int
    stage: str
    resolved: bool
    amount_minor: int
    confidence_bp: int
    evidence_digest: int
    settlements_digest: int
    timestamp: int

    @property
    def confidence(self) -> Decimal:
        """Confidence back as a Decimal in [0, 1], to the 4 decimal places
        the basis-point encoding preserves."""
        return Decimal(self.confidence_bp) / Decimal(10_000)


# ---------------------------------------------------------------------------
# Deterministic string -> 128-bit id folding
# ---------------------------------------------------------------------------


def _hash128(*parts: str) -> int:
    """Fold strings to a TigerBeetle-legal 128-bit id.

    Parts are joined with a NUL, which cannot occur in any identifier we
    parse, so ("a", "bc") and ("ab", "c") can't collide by concatenation.
    """
    digest = hashlib.blake2b("\0".join(parts).encode("utf-8"), digest_size=16).digest()
    return min(max(int.from_bytes(digest, "big"), _ID_MIN), _ID_MAX)


def _hash64(*parts: str) -> int:
    digest = hashlib.blake2b("\0".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def decision_id(bank_txn_id: str) -> int:
    """The ledger id for the decision about one bank credit.

    Derived, not random: this is what makes a second, contradictory write for
    the same credit a TigerBeetle-level error instead of an extra row.
    """
    return _hash128("decision", bank_txn_id)


def _confidence_bp(decision: Any) -> int:
    """Confidence as integer basis points, 0..10000.

    Each result type names its score differently. Decimal throughout, and the
    quantisation to an int happens once, here -- no float on the path.
    """
    for attr in ("confidence", "match_probability", "combined_score"):
        raw = getattr(decision, attr, None)
        if raw is not None:
            score = Decimal(raw)
            break
    else:
        raise AttributeError(f"{type(decision).__name__} carries no confidence score")
    bp = int((score * 10_000).to_integral_value())
    return min(max(bp, 0), 10_000)


def _evidence_digest(decision: Any) -> int:
    """Digest over "the fields compared", canonically serialised.

    Stage 1/2 expose `evidence` (a tuple of `FieldComparison`); Stage 3
    exposes `comparison_weights` (field -> log2 Bayes factor); Stage 4 exposes
    its named sub-scores. Whichever is present is rendered to one canonical
    string and hashed, so the ledger holds a fingerprint of exactly the
    evidence that was on the table when the call was made.
    """
    parts: list[str] = [decision.stage, decision.resolution, decision.reason]

    evidence = getattr(decision, "evidence", None)
    if evidence:
        parts += [
            f"{c.field}={c.credit_value}|{c.settlement_value}|{int(c.agreed)}" for c in evidence
        ]

    weights = getattr(decision, "comparison_weights", None)
    if weights:
        parts += [f"{field}={weights[field]}" for field in sorted(weights)]

    for attr in ("tfidf_cosine", "jaro_winkler", "dense_score", "primary_rank", "dense_rank"):
        value = getattr(decision, attr, None)
        if value is not None:
            parts.append(f"{attr}={value}")

    return _hash128(*parts)


def _settlements_digest(decision: Any) -> int:
    return _hash64(*decision.settlement_ids)


def _to_transfer(decision: Any) -> tb.Transfer:
    """Map one decision onto a TigerBeetle transfer."""
    resolved = decision.resolution in RESOLVED
    return tb.Transfer(
        id=decision_id(decision.bank_txn_id),
        debit_account_id=ACCOUNT_SUSPENSE,
        credit_account_id=ACCOUNT_RECONCILED if resolved else ACCOUNT_EXCEPTIONS,
        # Integer minor units, straight through. The money rule holds to the disk.
        amount=decision.credit_amount_minor,
        ledger=LEDGER,
        code=STAGE_CODES.get(decision.stage, STAGE_CODE_UNKNOWN),
        user_data_128=_evidence_digest(decision),
        user_data_64=_settlements_digest(decision),
        user_data_32=_confidence_bp(decision),
    )


def _to_record(transfer: tb.Transfer) -> AuditRecord:
    return AuditRecord(
        decision_id=int(transfer.id),
        stage=_CODE_TO_STAGE.get(transfer.code, "unknown"),
        resolved=int(transfer.credit_account_id) == ACCOUNT_RECONCILED,
        amount_minor=int(transfer.amount),
        confidence_bp=transfer.user_data_32,
        evidence_digest=int(transfer.user_data_128),
        settlements_digest=int(transfer.user_data_64),
        timestamp=transfer.timestamp,
    )


class AuditLogError(RuntimeError):
    """A write the ledger refused, or a log that failed verification."""


class AuditLog:
    """A connection to the audit ledger. Use as a context manager."""

    def __init__(self, address: str | None = None, cluster_id: int = 0) -> None:
        self._client = tb.ClientSync(
            cluster_id=cluster_id,
            replica_addresses=address or os.environ.get("TIGERBEETLE_ADDRESS", "3000"),
        )

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- setup -------------------------------------------------------------

    def ensure_accounts(self) -> None:
        """Create the three accounts if they aren't there yet.

        Idempotent: TigerBeetle reports EXISTS for an account already on the
        ledger with identical fields, which is a success for our purposes.
        """
        accounts = [
            tb.Account(id=account_id, ledger=LEDGER, code=ACCOUNT_CODE, flags=tb.AccountFlags.NONE)
            for account_id in (ACCOUNT_SUSPENSE, ACCOUNT_RECONCILED, ACCOUNT_EXCEPTIONS)
        ]
        for result in self._client.create_accounts(accounts):
            status = result.status
            if status not in (
                tb.CreateAccountStatus.CREATED,
                tb.CreateAccountStatus.EXISTS,
            ):
                raise AuditLogError(f"could not create audit accounts: {status!r}")

    # -- write -------------------------------------------------------------

    def write(self, decision: Any) -> int:
        """Append one decision. Returns its ledger id."""
        return self.write_all([decision])[0]

    def write_all(self, decisions: Sequence[Any]) -> list[int]:
        """Append many decisions in one round trip. Returns their ledger ids
        in input order.

        Raises on any rejection -- including an attempt to rewrite a decision
        already on the ledger, which is the append-only guarantee doing its
        job and must never pass silently.
        """
        transfers = [_to_transfer(d) for d in decisions]
        # ponytail: one batch; TigerBeetle caps a request at 8189 transfers,
        # chunk here if a run ever gets that big.
        for transfer, result in zip(transfers, self._client.create_transfers(transfers)):
            if result.status != tb.CreateTransferStatus.CREATED:
                raise AuditLogError(f"ledger refused transfer {int(transfer.id)}: {result.status!r}")
        return [int(t.id) for t in transfers]

    # -- read --------------------------------------------------------------

    def read_all(self) -> list[AuditRecord]:
        """Every decision on the ledger, in commit order.

        Ordering is the ledger's own: TigerBeetle assigns strictly increasing
        timestamps, so this is the true append order, not a sort we applied.
        """
        transfers = self._client.get_account_transfers(
            tb.AccountFilter(
                account_id=ACCOUNT_SUSPENSE,
                user_data_128=0,  # 0 means "don't filter on this field"
                user_data_64=0,
                user_data_32=0,
                code=0,
                timestamp_min=0,
                timestamp_max=0,
                limit=_QUERY_LIMIT,
                flags=tb.AccountFilterFlags.DEBITS | tb.AccountFilterFlags.CREDITS,
            )
        )
        return [_to_record(t) for t in transfers]

    def lookup(self, bank_txn_id: str) -> AuditRecord | None:
        """The decision logged for one bank credit, or None."""
        found = self._client.lookup_transfers([decision_id(bank_txn_id)])
        return _to_record(found[0]) if found else None

    def balances(self) -> dict[str, int]:
        """Posted balances, in minor units, straight off the ledger."""
        accounts = self._client.lookup_accounts(
            [ACCOUNT_SUSPENSE, ACCOUNT_RECONCILED, ACCOUNT_EXCEPTIONS]
        )
        by_id = {int(a.id): a for a in accounts}
        return {
            "suspense_debits": int(by_id[ACCOUNT_SUSPENSE].debits_posted),
            "reconciled": int(by_id[ACCOUNT_RECONCILED].credits_posted),
            "exceptions": int(by_id[ACCOUNT_EXCEPTIONS].credits_posted),
        }

    # -- verify ------------------------------------------------------------

    def verify_log(self, decisions: Iterable[Any]) -> list[AuditRecord]:
        """Read the log back and prove it still says what we wrote.

        Three checks, in increasing order of what they'd catch:

        1. Every decision is present, and each stored field reproduces from
           the decision -- so an edited evidence trail, a rewritten
           confidence, or a changed amount all fail to reproduce their digest.
        2. Timestamps are strictly increasing, i.e. the log really is a log.
        3. The books balance: debits out of suspense equal credits into
           reconciled plus exceptions. This one is the ledger auditing itself
           -- the balances are TigerBeetle's own running totals, maintained by
           the state machine, not a sum we computed over the rows we just read.

        Returns the verified records. Raises `AuditLogError` on any break.
        """
        records = self.read_all()
        by_id = {r.decision_id: r for r in records}

        for decision in decisions:
            expected = _to_transfer(decision)
            record = by_id.get(int(expected.id))
            if record is None:
                raise AuditLogError(f"decision for {decision.bank_txn_id!r} is missing from the log")
            mismatches = [
                name
                for name, want, got in (
                    ("stage", expected.code, STAGE_CODES.get(record.stage, STAGE_CODE_UNKNOWN)),
                    ("amount_minor", int(expected.amount), record.amount_minor),
                    ("confidence_bp", expected.user_data_32, record.confidence_bp),
                    ("evidence_digest", int(expected.user_data_128), record.evidence_digest),
                    ("settlements_digest", int(expected.user_data_64), record.settlements_digest),
                    (
                        "resolved",
                        int(expected.credit_account_id) == ACCOUNT_RECONCILED,
                        record.resolved,
                    ),
                )
                if want != got
            ]
            if mismatches:
                raise AuditLogError(
                    f"logged decision for {decision.bank_txn_id!r} disagrees with the "
                    f"decision on: {', '.join(mismatches)}"
                )

        for earlier, later in zip(records, records[1:]):
            if later.timestamp <= earlier.timestamp:
                raise AuditLogError(
                    f"log is not append-ordered: {later.timestamp} follows {earlier.timestamp}"
                )

        posted = self.balances()
        if posted["suspense_debits"] != posted["reconciled"] + posted["exceptions"]:
            raise AuditLogError(f"ledger does not balance: {posted}")

        return records


def iter_decision_summaries(records: Sequence[AuditRecord]) -> Iterator[str]:
    """One human-readable line per logged decision, for a report or a CLI."""
    for r in records:
        outcome = "RECONCILED" if r.resolved else "EXCEPTION"
        yield (
            f"{r.timestamp} {outcome:<10} stage={r.stage:<22} "
            f"amount_minor={r.amount_minor:>12} confidence={r.confidence}"
        )
