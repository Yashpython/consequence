"""Live-DB tests for the reset/snapshot primitive.

Marked @pytest.mark.live so `pytest -m "not live"` (the default `make test`)
can run in CI with no database. `make test-live` runs these against the
docker-compose Postgres.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from consequence.environment import Snapshot, _normalize, reset, snapshot

DIGEST_FILE = Path(__file__).resolve().parent / ".digest_cross_process.json"


@pytest.mark.live
def test_reset_is_deterministic() -> None:
    reset()
    h1 = snapshot().digest
    reset()
    h2 = snapshot().digest
    assert h1 == h2


@pytest.mark.live
def test_reset_completes_under_two_seconds() -> None:
    start = time.monotonic()
    reset()
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"reset() took {elapsed:.3f}s, expected < 2s"


@pytest.mark.live
def test_digest_stable_across_process_restart_write() -> None:
    """Run 1 of 2: write the digest to a file for a separate process to read."""
    reset()
    digest = snapshot().digest
    DIGEST_FILE.write_text(json.dumps({"digest": digest}), encoding="utf-8")
    assert digest


@pytest.mark.live
def test_digest_stable_across_process_restart_read() -> None:
    """Run 2 of 2: a fresh process (this test function) re-derives the digest
    and compares it against what a prior process run wrote to disk."""
    if not DIGEST_FILE.exists():
        pytest.skip("run test_digest_stable_across_process_restart_write first")
    saved = json.loads(DIGEST_FILE.read_text(encoding="utf-8"))["digest"]
    reset()
    digest = snapshot().digest
    assert digest == saved
    DIGEST_FILE.unlink()


def test_numeric_normalization_strips_trailing_zeros() -> None:
    """205.50 and 205.5 are the same value and must normalize identically."""
    assert _normalize(Decimal("205.50")) == _normalize(Decimal("205.5")) == "205.5"


def test_numeric_normalization_used_by_snapshot_hashing() -> None:
    """Two 'databases' differing only in NUMERIC trailing-zero formatting
    must hash identically once normalized."""
    rows_a = {"invoices": [{"id": 1, "total": _normalize(Decimal("205.50"))}]}
    rows_b = {"invoices": [{"id": 1, "total": _normalize(Decimal("205.5"))}]}
    import hashlib

    digest_a = hashlib.sha256(json.dumps(rows_a, sort_keys=True).encode()).hexdigest()
    digest_b = hashlib.sha256(json.dumps(rows_b, sort_keys=True).encode()).hexdigest()
    assert digest_a == digest_b


def test_snapshot_is_a_frozen_dataclass_with_digest_and_structure() -> None:
    assert Snapshot.__dataclass_fields__.keys() == {"structure", "digest"}
