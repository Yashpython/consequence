"""Timestamp canonicalization: the cells most likely to make a digest
machine-dependent, or to make a real change invisible to the diff.

Offline tests run in CI's check job. The @live tests at the bottom need the
docker-compose Postgres; CI's live job runs them on a different machine than
the dev box, which is the point of the time zone tests.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from consequence.diff import diff
from consequence.environment import (
    SCHEMA_PATH,
    SEED_PATH,
    Snapshot,
    _normalize,
    canonical_json,
    digest_of,
)
from consequence.offline import parse_seed

# -- every timestamp column has the same type ------------------------------------------

# Every spelling Postgres accepts, mapped to one name.
_TIMESTAMP_TYPE = re.compile(
    r"^\s*(\w+)\s+(timestamptz|timestamp\s+with\s+time\s+zone"
    r"|timestamp\s+without\s+time\s+zone|timestamp)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _timestamp_columns() -> dict[str, str]:
    """{column: 'timestamptz' | 'timestamp'} for every timestamp column in the DDL."""
    found = {}
    for column, spelling in _TIMESTAMP_TYPE.findall(SCHEMA_PATH.read_text(encoding="utf-8")):
        spelling = " ".join(spelling.lower().split())
        with_zone = spelling in ("timestamptz", "timestamp with time zone")
        found[column] = "timestamptz" if with_zone else "timestamp"
    return found


def test_every_timestamp_column_has_the_same_type():
    """A mix would mean some cells come back aware and some naive, and
    _normalize renders the two differently (with and without an offset)."""
    columns = _timestamp_columns()
    assert columns, "no timestamp columns found -- has the DDL format changed?"
    assert len(set(columns.values())) == 1, columns


def test_the_ddl_parser_sees_a_mix(tmp_path, monkeypatch):
    """The check above can fail: feed it DDL with one of each."""
    ddl = tmp_path / "schema.sql"
    ddl.write_text("CREATE TABLE t (\n    a timestamptz NOT NULL,\n"
                   "    b timestamp without time zone\n);\n", encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.SCHEMA_PATH", ddl)
    assert _timestamp_columns() == {"a": "timestamptz", "b": "timestamp"}


def test_the_columns_are_timestamptz():
    """Which of the two matters for the live tests below: with timestamptz,
    the session time zone is in play, so connect() pins it to UTC."""
    assert set(_timestamp_columns().values()) == {"timestamptz"}


# -- sub-second precision is preserved -----------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    (datetime(2025, 1, 3, 9, 0, 0, 123456, tzinfo=UTC), "2025-01-03T09:00:00.123456+00:00"),
    (datetime(2025, 1, 3, 9, 0, 0, 1, tzinfo=UTC), "2025-01-03T09:00:00.000001+00:00"),
    # A non-UTC offset is converted, and the microseconds survive the conversion.
    (datetime(2025, 1, 3, 14, 30, 0, 999999, tzinfo=timezone(timedelta(hours=5, minutes=30))),
     "2025-01-03T09:00:00.999999+00:00"),
    # Whole seconds: no fractional part, which is still one form per instant.
    (datetime(2025, 1, 3, 9, 0, 0, tzinfo=UTC), "2025-01-03T09:00:00+00:00"),
])
def test_canonical_form_keeps_microseconds(value, expected):
    assert _normalize(value) == expected


def _one_cell(at) -> Snapshot:
    structure = {"audit_log": [{"id": 1, "at": at}]}
    return Snapshot(structure=structure, digest=digest_of(canonical_json(structure)))


def test_a_one_microsecond_change_is_visible_to_the_diff():
    t = datetime(2025, 1, 3, 9, 0, 0, 500000, tzinfo=UTC)
    before = _one_cell(_normalize(t))
    after = _one_cell(_normalize(t + timedelta(microseconds=1)))
    assert before.digest != after.digest
    assert diff(before, after).changed == [
        ("audit_log", 1, {"at": ("2025-01-03T09:00:00.500000+00:00",
                                 "2025-01-03T09:00:00.500001+00:00")})]


def test_offline_seed_parser_keeps_microseconds_and_converts_to_utc(tmp_path):
    """The offline environment parses seed literals itself; it must produce
    the same canonical form a live snapshot does, fraction included."""
    seed = tmp_path / "seed.sql"
    seed.write_text(
        "INSERT INTO audit_log (id, table_name, row_id, action, actor, at) VALUES\n"
        "  (1, 'invoices', 1, 'void_invoice', 'agent', '2025-01-03 14:30:00.123456+05:30');\n",
        encoding="utf-8",
    )
    [row] = parse_seed(seed)["audit_log"]
    assert row["at"] == "2025-01-03T09:00:00.123456+00:00"


def test_every_seed_timestamp_literal_carries_an_explicit_offset():
    """A literal without an offset is read in the session time zone, so the
    stored instant would depend on the machine. Every one must say +00 (or
    another explicit offset)."""
    literals = re.findall(r"'(\d{4}-\d\d-\d\d[ T]\d\d:\d\d[^']*)'",
                          SEED_PATH.read_text(encoding="utf-8"))
    assert literals
    missing = [lit for lit in literals if not re.search(r"([+-]\d\d(:?\d\d)?|Z)$", lit)]
    assert missing == []


# -- NULL ----------------------------------------------------------------------------------


def test_null_canonicalizes_to_json_null_on_every_path(tmp_path):
    assert _normalize(None) is None  # live path: psycopg returns None
    seed = tmp_path / "seed.sql"
    seed.write_text(
        "INSERT INTO audit_log (id, table_name, row_id, action, actor, at) VALUES\n"
        "  (1, 'invoices', 1, 'void_invoice', 'agent', NULL);\n",
        encoding="utf-8",
    )
    assert parse_seed(seed)["audit_log"][0]["at"] is None  # offline path
    assert '"at": null' in canonical_json({"audit_log": [{"id": 1, "at": None}]})


@pytest.mark.parametrize("real", [
    "", "null", "None", "NULL",  # strings a careless serializer might produce for NULL
    _normalize(datetime(1970, 1, 1, tzinfo=UTC)),  # the epoch, a classic sentinel
    _normalize(datetime(1, 1, 1, tzinfo=UTC)),  # datetime.min
])
def test_null_never_collides_with_a_real_value(real):
    """A real timestamp always canonicalizes to a string; NULL to JSON null.
    No value can hash like NULL, and a NULL <-> value change shows in the diff."""
    assert canonical_json({"t": [{"at": None}]}) != canonical_json({"t": [{"at": real}]})
    assert _one_cell(None).digest != _one_cell(real).digest
    assert diff(_one_cell(None), _one_cell(real)).changed == [
        ("audit_log", 1, {"at": (None, real)})]


# -- live: session time zone ---------------------------------------------------------------


@pytest.mark.live
def test_every_environment_connection_is_pinned_to_utc():
    from consequence.db import connect

    with connect() as conn:
        assert conn.execute("SHOW TimeZone").fetchone()[0] == "UTC"


@pytest.mark.live
@pytest.mark.parametrize("zone", ["Pacific/Chatham", "America/St_Johns", "Asia/Kathmandu"])
def test_snapshot_cells_do_not_depend_on_the_session_time_zone(zone):
    """Read the same rows through a session in an odd-offset zone: the driver
    hands back different tz-aware datetimes, and every one canonicalizes to
    exactly what snapshot() recorded under UTC."""
    import psycopg

    from consequence.db import _dsn
    from consequence.environment import reset, snapshot

    reset()
    expected = {r["id"]: r["created_at"] for r in snapshot().structure["invoices"]}
    with psycopg.connect(_dsn(), options=f"-c TimeZone={zone}") as conn:
        rows = conn.execute("SELECT id, created_at FROM invoices ORDER BY id").fetchall()
    assert rows[0][1].utcoffset() != timedelta(0)  # the session really was in another zone
    assert {row_id: _normalize(at) for row_id, at in rows} == expected


@pytest.mark.live
def test_a_one_microsecond_change_survives_postgres_and_the_driver():
    """End to end: Postgres keeps microseconds, psycopg returns them, and
    the snapshot and diff show the change."""
    from consequence.db import connect
    from consequence.environment import reset, snapshot

    reset()
    try:
        before = snapshot()
        with connect() as conn:
            conn.execute("UPDATE invoices SET created_at = created_at + interval '1 microsecond' "
                         "WHERE id = 1")
        after = snapshot()
        [(table, pk, changes)] = diff(before, after).changed
        assert (table, pk, list(changes)) == ("invoices", 1, ["created_at"])
        old, new = changes["created_at"]
        assert old == "2025-01-03T09:00:00+00:00"
        assert new == "2025-01-03T09:00:00.000001+00:00"
    finally:
        reset()
