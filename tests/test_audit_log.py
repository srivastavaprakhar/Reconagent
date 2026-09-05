"""The audit log on TigerBeetle, exercised against real decision data.

These tests start their own single-replica TigerBeetle cluster in a temp
directory and tear it down afterwards, so they need only the `tigerbeetle`
binary -- on PATH, or pointed at by `TIGERBEETLE_BINARY`. Without it they skip
rather than fail, which keeps the suite green on a machine that has never
heard of TigerBeetle.

    curl -Lo tigerbeetle.zip https://mac.tigerbeetle.com && unzip tigerbeetle.zip
    TIGERBEETLE_BINARY=$PWD/tigerbeetle .venv/bin/pytest tests/test_audit_log.py
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from decimal import Decimal
from pathlib import Path

import pytest

tb = pytest.importorskip("tigerbeetle")

from reconagent.audit_log import (  # noqa: E402
    ACCOUNT_EXCEPTIONS,
    ACCOUNT_RECONCILED,
    ACCOUNT_SUSPENSE,
    LEDGER,
    RESOLVED,
    AuditLog,
    AuditLogError,
    decision_id,
)
from reconagent.camt053 import parse_camt053_file  # noqa: E402
from reconagent.match import match_all  # noqa: E402
from reconagent.razorpay import parse_razorpay_settlements  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
STRESS = REPO / "stress_test"


def _binary() -> str | None:
    return os.environ.get("TIGERBEETLE_BINARY") or shutil.which("tigerbeetle")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def cluster_factory(tmp_path_factory):
    """Starts throwaway single-replica clusters, all torn down at module exit.

    A factory rather than a single fixture because the chart of accounts is
    fixed module constants -- two test groups that both want to assert on
    account balances need two ledgers, not two account sets.
    """
    binary = _binary()
    if binary is None:
        pytest.skip("tigerbeetle binary not found (set TIGERBEETLE_BINARY or add to PATH)")

    root = tmp_path_factory.mktemp("tigerbeetle")
    servers: list[subprocess.Popen] = []

    def start(name: str) -> str:
        data_file = root / f"{name}.tigerbeetle"
        port = _free_port()
        subprocess.run(
            [
                binary, "format",
                "--cluster=0", "--replica=0", "--replica-count=1",
                "--development", str(data_file),
            ],
            check=True, capture_output=True,
        )
        server = subprocess.Popen(
            [binary, "start", f"--addresses={port}", "--development", str(data_file)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        servers.append(server)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.fail(f"tigerbeetle exited early:\n{server.communicate()[0].decode()}")
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return str(port)
            time.sleep(0.1)
        pytest.fail("tigerbeetle did not start listening within 30s")

    try:
        yield start
    finally:
        for server in servers:
            server.terminate()
            server.wait(timeout=10)


@pytest.fixture(scope="module")
def cluster(cluster_factory):
    return cluster_factory("audit")


@pytest.fixture(scope="module")
def decisions():
    """Every decision Tier 1 produces over `data/`'s real settlements and
    credits. Not a synthetic fixture -- the same call `scripts/` makes."""
    settlements = parse_razorpay_settlements(DATA / "razorpay_settlements.csv")
    credits = parse_camt053_file(DATA / "bank_statement.camt053.xml")
    return match_all(credits, settlements)


@pytest.fixture(scope="module")
def log(cluster, decisions):
    """The ledger, with every real decision written to it once."""
    with AuditLog(address=cluster) as audit:
        audit.ensure_accounts()
        audit.write_all(decisions)
        yield audit


# ---------------------------------------------------------------------------
# Real data round-trips
# ---------------------------------------------------------------------------


def test_real_decisions_exist(decisions):
    """Guard the fixture itself -- if the cascade stops producing decisions,
    every test below would pass vacuously."""
    assert len(decisions) > 100
    assert any(d.resolution in RESOLVED for d in decisions)
    assert all(isinstance(d.credit_amount_minor, int) for d in decisions)


def test_write_and_read_back_every_decision(log, decisions):
    records = log.read_all()
    assert len(records) == len(decisions)
    assert {r.decision_id for r in records} == {decision_id(d.bank_txn_id) for d in decisions}


def test_verify_log_passes_on_untampered_log(log, decisions):
    """Full verification: every field reproduces, order is monotonic, and
    TigerBeetle's own account balances agree."""
    assert len(log.verify_log(decisions)) == len(decisions)


def test_spot_checked_fields_match_what_was_written(log, decisions):
    """Not just counts -- the actual stage, amount and confidence of
    individual decisions survive the round trip."""
    for decision in decisions[:5] + decisions[-5:]:
        record = log.lookup(decision.bank_txn_id)
        assert record is not None, decision.bank_txn_id
        assert record.stage == decision.stage
        assert record.amount_minor == decision.credit_amount_minor
        assert record.resolved is (decision.resolution in RESOLVED)
        # Confidence survives to 4 decimal places, as Decimal on both ends.
        assert record.confidence == (Decimal(decision.confidence) * 10_000).to_integral_value() / 10_000


def test_amounts_are_integer_minor_units(log):
    """The money rule, held all the way to the storage engine."""
    for record in log.read_all():
        assert isinstance(record.amount_minor, int)
        assert isinstance(record.confidence, Decimal)


def test_ledger_balances_against_the_decisions(log, decisions):
    """TigerBeetle's own running totals, not a sum over rows we read."""
    posted = log.balances()
    expected_reconciled = sum(
        d.credit_amount_minor for d in decisions if d.resolution in RESOLVED
    )
    expected_exceptions = sum(
        d.credit_amount_minor for d in decisions if d.resolution not in RESOLVED
    )
    assert posted["reconciled"] == expected_reconciled
    assert posted["exceptions"] == expected_exceptions
    assert posted["suspense_debits"] == expected_reconciled + expected_exceptions


def test_timestamps_come_from_the_cluster(log):
    """Strictly increasing, and assigned by TigerBeetle rather than by us."""
    stamps = [r.timestamp for r in log.read_all()]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert all(t > 1_500_000_000_000_000_000 for t in stamps)  # sane ns-since-epoch


# ---------------------------------------------------------------------------
# Tampering
# ---------------------------------------------------------------------------


def test_tampering_with_a_logged_decision_is_refused_by_the_ledger(log, decisions):
    """The append-only guarantee, tested by attacking it.

    We bypass `AuditLog.write` entirely and go at the raw client, trying to
    overwrite an already-logged decision with a different amount, stage and
    confidence -- the shape of a real cover-up. TigerBeetle refuses it,
    because the transfer id is derived from the bank credit and a transfer id
    can only be written once. The original record must survive untouched.
    """
    victim = decisions[0]
    before = log.lookup(victim.bank_txn_id)
    assert before is not None

    forged = tb.Transfer(
        id=decision_id(victim.bank_txn_id),  # same decision, different story
        debit_account_id=ACCOUNT_SUSPENSE,
        credit_account_id=ACCOUNT_RECONCILED
        if before.resolved is False
        else ACCOUNT_EXCEPTIONS,
        amount=before.amount_minor + 999_99,
        ledger=LEDGER,
        code=99,
        user_data_128=1,
        user_data_64=1,
        user_data_32=1,
    )
    (result,) = log._client.create_transfers([forged])
    assert result.status != tb.CreateTransferStatus.CREATED
    assert "EXISTS" in result.status.name, result.status

    after = log.lookup(victim.bank_txn_id)
    assert after == before, "a refused tamper still changed the record"


def test_verify_log_detects_a_decision_that_disagrees_with_the_log(log, decisions):
    """The other direction: the ledger is intact but the decision record an
    auditor was handed has been edited. Verification must catch the
    disagreement rather than trust the paperwork.
    """
    import dataclasses

    doctored = dataclasses.replace(
        decisions[0],
        credit_amount_minor=decisions[0].credit_amount_minor + 1,
    )
    with pytest.raises(AuditLogError, match="amount_minor"):
        log.verify_log([doctored])


def test_verify_log_detects_edited_evidence(log, decisions):
    """Editing the compared-fields trail breaks its digest, even though the
    amount and confidence still line up."""
    import dataclasses

    doctored = dataclasses.replace(decisions[0], reason="looked fine to me")
    with pytest.raises(AuditLogError, match="evidence_digest"):
        log.verify_log([doctored])


def test_verify_log_detects_a_missing_decision(log, decisions):
    """A decision that was never logged at all."""
    import dataclasses

    never_written = dataclasses.replace(decisions[0], bank_txn_id="TXN-NEVER-LOGGED")
    with pytest.raises(AuditLogError, match="missing from the log"):
        log.verify_log([never_written])


def test_rewriting_the_same_decision_raises_through_the_public_api(log, decisions):
    """`write_all` must not swallow a rejection -- re-appending a decision
    already on the ledger is an error, loudly."""
    with pytest.raises(AuditLogError, match="EXISTS"):
        log.write_all([decisions[0]])


# ---------------------------------------------------------------------------
# The exception path
#
# Every decision `data/` produces resolves, so that ledger never posts to the
# exceptions account. `stress_test/` is the adversarial set Tier 1 is supposed
# to decline wholesale -- real decisions, real UNMATCHED verdicts, and the
# only way to exercise the other half of `_to_transfer` against real data.
# It gets its own ledger so its balances don't mix with `data/`'s.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def declined_decisions():
    settlements = parse_razorpay_settlements(STRESS / "razorpay_settlements.csv")
    credits = parse_camt053_file(STRESS / "bank_statement.camt053.xml")
    return match_all(credits, settlements)


@pytest.fixture(scope="module")
def declined_log(cluster_factory, declined_decisions):
    with AuditLog(address=cluster_factory("exceptions")) as audit:
        audit.ensure_accounts()
        audit.write_all(declined_decisions)
        yield audit


def test_declined_decisions_are_really_declined(declined_decisions):
    assert declined_decisions
    assert all(d.resolution not in RESOLVED for d in declined_decisions)


def test_unresolved_decisions_post_to_the_exceptions_account(
    declined_log, declined_decisions
):
    """An audit log that quietly filed exceptions as reconciled would be worse
    than no audit log."""
    records = declined_log.verify_log(declined_decisions)
    assert len(records) == len(declined_decisions)
    assert not any(r.resolved for r in records)

    posted = declined_log.balances()
    expected = sum(d.credit_amount_minor for d in declined_decisions)
    assert posted["exceptions"] == expected
    assert posted["reconciled"] == 0
    assert posted["suspense_debits"] == expected


# ---------------------------------------------------------------------------
# Id mapping
# ---------------------------------------------------------------------------


def test_decision_ids_are_deterministic_and_distinct(decisions):
    ids = [decision_id(d.bank_txn_id) for d in decisions]
    assert len(set(ids)) == len(ids), "id collision across real bank_txn_ids"
    assert ids == [decision_id(d.bank_txn_id) for d in decisions], "ids are not stable"
    assert all(0 < i < (1 << 128) - 1 for i in ids), "id outside TigerBeetle's legal range"
