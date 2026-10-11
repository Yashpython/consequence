"""The deterministic reset/snapshot primitive every measurement rests on.

reset() drops and recreates the schema, then loads the seed data, all inside
one transaction. snapshot() returns a canonical, hashable view of the entire
database so two runs can be compared for exact equality.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from consequence.db import connect

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "env" / "schema.sql"
SEED_PATH = Path(__file__).resolve().parent.parent / "env" / "seed.sql"

# Order matters for two reasons: it must respect foreign keys on drop/create,
# and snapshot() iterates it to build a stable, table-by-table structure.
TABLES = (
    "suppliers",
    "invoices",
    "line_items",
    "approvals",
    "review_flags",
    "audit_log",
)


def reset() -> None:
    """Drop and recreate the schema, then apply the seed data.

    Runs as one transaction: either the environment ends up fully reset, or
    nothing changes. Idempotent -- safe to call repeatedly.
    """
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    seed_sql = SEED_PATH.read_text(encoding="utf-8")
    with connect() as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        conn.execute(schema_sql)
        conn.execute(seed_sql)


def _normalize(value: Any) -> Any:
    """Canonicalize a single cell value, so a snapshot is plain JSON.

    NUMERIC values come back as Decimal. Decimal preserves trailing zeros
    ("205.50" != "205.5" as Decimal reprs), so two databases that are equal in
    value but differ in scale would otherwise hash differently. Normalizing
    strips that: `format(value.normalize(), "f")` always yields the same
    plain-decimal string for equal values, without falling back to
    scientific notation the way plain str(normalize()) can for round values.

    Dates become ISO strings and timestamps become UTC ISO strings
    ("2025-01-03T09:00:00+00:00"), independent of the session time zone.
    That makes every snapshot cell JSON-native, which is what lets offline
    grading work: an episode's stored state diff round-trips losslessly, and
    the seed parsed from env/seed.sql (consequence.offline) hashes to exactly
    the digest a live snapshot of a fresh reset() records.
    """
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):  # before date: datetime is a date subclass
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class Snapshot:
    structure: dict[str, list[dict[str, Any]]]
    digest: str


def canonical_json(structure: dict[str, list[dict[str, Any]]]) -> str:
    """THE serialization of a snapshot: what gets hashed AND what gets stored.

    Because the stored text is exactly the hashed text, verifying a stored
    snapshot is just sha256(stored) == recorded digest -- no re-serialization
    that could drift. Strict on purpose (no default=str): every cell is
    JSON-native after _normalize, so anything else is a bug and should fail
    loudly here rather than be silently stringified.
    """
    return json.dumps(structure, sort_keys=True)


def digest_of(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_from_json(canonical: str) -> Snapshot:
    """Load a stored snapshot. Its digest is computed from the stored text
    itself; callers compare it to the digest that was recorded."""
    return Snapshot(structure=json.loads(canonical), digest=digest_of(canonical))


def snapshot() -> Snapshot:
    """Return every table's rows, ordered by primary key, plus a sha256 digest.

    NUMERIC columns are normalized before hashing so equal values always
    produce the same digest regardless of trailing zeros.
    """
    structure: dict[str, list[dict[str, Any]]] = {}
    with connect() as conn:
        for table in TABLES:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {table} ORDER BY id")
                columns = [desc.name for desc in cur.description]
                rows = []
                for record in cur.fetchall():
                    rows.append({col: _normalize(val) for col, val in zip(columns, record)})
                structure[table] = rows

    return Snapshot(structure=structure, digest=digest_of(canonical_json(structure)))
