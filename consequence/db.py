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


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Yield a psycopg3 connection built from DATABASE_URL.

    Commits on clean exit, rolls back on exception, always closes.
    """
    with psycopg.connect(_dsn()) as conn:
        yield conn


def run_sql_file(path: str | Path) -> None:
    """Execute every statement in a .sql file in a single transaction."""
    sql = Path(path).read_text(encoding="utf-8")
    with connect() as conn:
        conn.execute(sql)
