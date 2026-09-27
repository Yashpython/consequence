"""Shared test fixtures.

offline_env builds the exact seed state by parsing env/seed.sql in pure Python
-- no Postgres -- and lets a test apply the same row-level effects the tool
layer's write tools produce. It exists so every verifier is exercised in CI.
It is NOT a substitute for the tool layer: tests/test_tasks.py also builds
every ideal and wrong state through consequence.tools against a live database
(@pytest.mark.live), which is the authoritative check.

Cells come out as the snapshot would round-trip through JSON: NUMERIC as
normalized strings (like environment._normalize), dates and timestamps as ISO
strings. Verifiers must handle that shape anyway for offline re-grading.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from consequence.diff import diff
from consequence.environment import TABLES, Snapshot

SEED_SQL = Path(__file__).resolve().parents[1] / "env" / "seed.sql"

_INSERT = re.compile(r"INSERT INTO (\w+) \(([^)]*)\) VALUES\n(.*?);\n", re.DOTALL)
_TOKEN = re.compile(r"'((?:[^']|'')*)'|(NULL)|(-?\d+\.\d+)|(-?\d+)")
_ROW = re.compile(r"^\s*\((.*)\),?\s*$")


def _parse_value(match: re.Match) -> object:
    quoted, null, decimal, integer = match.groups()
    if quoted is not None:
        return quoted.replace("''", "'")
    if null is not None:
        return None
    if decimal is not None:
        return format(Decimal(decimal).normalize(), "f")
    return int(integer)


def parse_seed(path: Path = SEED_SQL) -> dict[str, list[dict]]:
    sql = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    ) + "\n"
    structure: dict[str, list[dict]] = {t: [] for t in TABLES}
    for table, cols, body in _INSERT.findall(sql):
        columns = [c.strip() for c in cols.split(",")]
        for line in body.splitlines():
            row = _ROW.match(line)
            if row:
                values = [_parse_value(m) for m in _TOKEN.finditer(row.group(1))]
                assert len(values) == len(columns), line
                structure[table].append(dict(zip(columns, values, strict=True)))
    for rows in structure.values():
        rows.sort(key=lambda r: r["id"])
    return structure


def _snap(structure: dict) -> Snapshot:
    digest = hashlib.sha256(json.dumps(structure, sort_keys=True).encode()).hexdigest()
    return Snapshot(structure=copy.deepcopy(structure), digest=digest)


class OfflineEnv:
    """Seed state plus tool-shaped mutations, for verifier unit tests."""

    AT = "2025-07-01T12:00:00+00:00"

    def __init__(self, seed: dict):
        self._seed = seed
        self.state = copy.deepcopy(seed)

    def _row(self, table: str, row_id: int) -> dict:
        return next(r for r in self.state[table] if r["id"] == row_id)

    def _insert(self, table: str, **fields) -> int:
        new_id = max((r["id"] for r in self.state[table]), default=0) + 1
        self.state[table].append({"id": new_id, **fields})
        return new_id

    def _audit(self, table: str, row_id: int, action: str) -> None:
        self._insert("audit_log", table_name=table, row_id=row_id, action=action,
                     actor="agent", at=self.AT)

    def void_invoice(self, invoice_id: int, reason: str = "") -> None:
        self._row("invoices", invoice_id)["status"] = "void"
        self._audit("invoices", invoice_id, "void_invoice")

    def create_approval(self, invoice_id: int, decision: str = "approved",
                        reason: str = "ok") -> None:
        new_id = self._insert("approvals", invoice_id=invoice_id, approver="agent",
                              decision=decision, reason=reason, decided_at=self.AT)
        self._audit("approvals", new_id, "create_approval")

    def flag_for_review(self, invoice_id: int, reason: str = "review") -> None:
        new_id = self._insert("review_flags", invoice_id=invoice_id, reason=reason,
                              flagged_at=self.AT)
        self._audit("review_flags", new_id, "flag_for_review")

    def update_supplier(self, supplier_id: int, **fields) -> None:
        self._row("suppliers", supplier_id).update(fields)
        self._audit("suppliers", supplier_id, "update_supplier")

    def verify(self, task):
        before, after = _snap(self._seed), _snap(self.state)
        return task.verify(before, after, diff(before, after))


@pytest.fixture(scope="session")
def seed_structure() -> dict:
    return parse_seed()


@pytest.fixture
def offline_env(seed_structure) -> OfflineEnv:
    return OfflineEnv(seed_structure)
