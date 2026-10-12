"""Database access helpers.

The connection string is read from DATABASE_URL. Example for the local
docker-compose environment:

    postgresql://consequence:consequence@localhost:5433/consequence
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

from consequence.config import load_config


def _dsn() -> str:
    dsn = load_config().database_url
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


# Every connection's session time zone is pinned to UTC. Every timestamp
# column is timestamptz, and the session zone decides both how a literal
# without an offset is read and how a value is rendered. snapshot() converts
# to UTC anyway, but nothing that touches the environment should depend on
# the server's default zone -- CI's postgres and a dev box needn't agree.
# Passed as a libpq option, so it applies from the first statement and
# survives a rollback (a SET inside a transaction would not).
SESSION_TIMEZONE = "UTC"


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Yield a psycopg3 connection built from DATABASE_URL, session time zone
    pinned to UTC.

    Commits on clean exit, rolls back on exception, always closes.
    """
    with psycopg.connect(_dsn(), options=f"-c TimeZone={SESSION_TIMEZONE}") as conn:
        yield conn


def run_sql_file(path: str | Path) -> None:
    """Execute every statement in a .sql file in a single transaction."""
    sql = Path(path).read_text(encoding="utf-8")
    with connect() as conn:
        conn.execute(sql)
