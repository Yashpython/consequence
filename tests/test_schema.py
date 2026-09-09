"""Static checks on env/schema.sql. No database required, so this runs in CI.

Live-DB tests arrive in the next commit and will be marked so CI can skip them.
"""

from __future__ import annotations

import re
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "env" / "schema.sql"


def _sql() -> str:
    """schema.sql with -- comment lines stripped."""
    lines = SCHEMA.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("--"))

EXPECTED: dict[str, set[str]] = {
    "suppliers": {"id", "name", "brn", "vat_number", "status"},
    "invoices": {
        "id", "supplier_id", "number", "invoice_date", "total", "vat",
        "status", "created_at",
    },
    "line_items": {"id", "invoice_id", "description", "qty", "unit_price"},
    "approvals": {"id", "invoice_id", "approver", "decision", "reason", "decided_at"},
    "review_flags": {"id", "invoice_id", "reason", "flagged_at"},
    "audit_log": {"id", "table_name", "row_id", "action", "actor", "at"},
}


def _table_bodies(sql: str) -> dict[str, str]:
    bodies: dict[str, str] = {}
    for match in re.finditer(
        r"CREATE TABLE\s+(\w+)\s*\((.*?)\n\);", sql, re.IGNORECASE | re.DOTALL
    ):
        bodies[match.group(1).lower()] = match.group(2)
    return bodies


def test_all_expected_tables_present() -> None:
    bodies = _table_bodies(_sql())
    assert set(bodies) == set(EXPECTED)


def test_all_expected_columns_present() -> None:
    bodies = _table_bodies(_sql())
    for table, columns in EXPECTED.items():
        body = bodies[table]
        defined = {
            line.strip().split()[0].strip('"')
            for line in body.splitlines()
            if line.strip()
            and not line.strip().upper().startswith(
                ("CHECK", "FOREIGN", "PRIMARY", "CONSTRAINT", "UNIQUE", "REFERENCES")
            )
        }
        missing = columns - defined
        assert not missing, f"{table} missing columns: {missing}"


def test_money_columns_are_numeric_not_float() -> None:
    sql = _sql().lower()
    assert "numeric(12,2)" in sql
    for bad in ("float", "double precision", " real"):
        assert bad not in sql, f"found {bad!r} in schema; money must be NUMERIC"


def test_foreign_keys_restrict_on_delete() -> None:
    sql = _sql().lower()
    assert sql.count("references") == sql.count("on delete restrict")
