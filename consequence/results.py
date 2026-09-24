"""The results database: the durable record of every experiment run.

This is designed so grading can be re-run offline, from stored raw data,
without ever calling a model API again. A bug in a grader costs nothing to
fix and re-score -- see state_diffs and record_grading below.

Uses SQLite at RESULTS_DB_PATH (default results/results.db).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from consequence.config import load_config

DEFAULT_DB_PATH = "results/results.db"

# SQLite table order matters here only for readability; foreign keys are
# declared but SQLite enforces them only with PRAGMA foreign_keys = ON,
# which Results turns on for every connection.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    git_sha     TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES runs (id),
    task_id       TEXT NOT NULL,
    model_id      TEXT NOT NULL,
    trial_index   INTEGER NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    status        TEXT NOT NULL
                  CHECK (status IN ('completed', 'error', 'timeout', 'max_turns')),
    error         TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    latency_ms    INTEGER,
    turn_count    INTEGER,
    retry_count   INTEGER
);

CREATE TABLE IF NOT EXISTS transcripts (
    id            INTEGER PRIMARY KEY,
    episode_id    INTEGER NOT NULL REFERENCES episodes (id),
    turn_index    INTEGER NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT,
    tool_name     TEXT,
    tool_args     TEXT,
    tool_result   TEXT,
    raw_response  TEXT
);

-- The raw before/after state for an episode, stored verbatim so a grader
-- can be re-run offline against exactly what happened -- no need to replay
-- the episode or call a model API again. This is the whole point: a buggy
-- verifier costs nothing to fix and re-score, because the evidence it grades
-- already lives here.
CREATE TABLE IF NOT EXISTS state_diffs (
    id             INTEGER PRIMARY KEY,
    episode_id     INTEGER NOT NULL REFERENCES episodes (id),
    diff_json      TEXT NOT NULL,
    digest_before  TEXT NOT NULL,
    digest_after   TEXT NOT NULL
);

-- Multiple gradings per episode is by design -- that is the entire
-- experiment (comparing graders, re-scoring after a grader bugfix, running
-- both a state_verifier and an llm_judge over the same episode). A given
-- (episode_id, grader) pair is unique: re-grading replaces the prior verdict
-- rather than appending a new one, so offline re-scoring is safe to re-run.
CREATE TABLE IF NOT EXISTS gradings (
    id          INTEGER PRIMARY KEY,
    episode_id  INTEGER NOT NULL REFERENCES episodes (id),
    grader      TEXT NOT NULL CHECK (grader IN ('state_verifier', 'llm_judge')),
    passed      INTEGER NOT NULL CHECK (passed IN (0, 1)),
    detail_json TEXT,
    graded_at   TEXT NOT NULL,
    UNIQUE (episode_id, grader)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str | None:
    """Serialize to JSON unless it's already a string (or None)."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Results:
    """Owns the SQLite connection to the results database.

    Use as a context manager or call close() explicitly:

        with Results() as results:
            run_id = results.start_run()
            ...
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = load_config().results_db_path or DEFAULT_DB_PATH
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    # Every write below runs inside `with self._conn:`, sqlite3's idiom for
    # a transaction: it commits on clean exit and rolls back (re-raising) on
    # any exception, so a crashed write never leaves a partial row.

    def start_run(
        self,
        started_at: str | None = None,
        git_sha: str | None = None,
        notes: str | None = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (started_at, git_sha, notes) VALUES (?, ?, ?)",
                (started_at or _now(), git_sha, notes),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def finish_run(self, run_id: int, finished_at: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET finished_at = ? WHERE id = ?",
                (finished_at or _now(), run_id),
            )

    def record_episode(
        self,
        run_id: int,
        task_id: str,
        model_id: str,
        trial_index: int,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        error: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        turn_count: int | None = None,
        retry_count: int | None = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO episodes (
                    run_id, task_id, model_id, trial_index, started_at, finished_at,
                    status, error, input_tokens, output_tokens, cost_usd, latency_ms,
                    turn_count, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    model_id,
                    trial_index,
                    started_at,
                    finished_at,
                    status,
                    error,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    latency_ms,
                    turn_count,
                    retry_count,
                ),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def record_transcript(self, episode_id: int, turns: Iterable[Mapping[str, Any]]) -> None:
        """Write every turn of an episode's conversation in one transaction.

        `turns` is the full ordered conversation: each item needs
        turn_index, role, and optionally content, tool_name, tool_args,
        tool_result, raw_response (all JSON-encoded if not already a
        string). If any turn is malformed, none of them are written.
        """
        rows = [
            (
                episode_id,
                turn["turn_index"],
                turn["role"],
                turn.get("content"),
                turn.get("tool_name"),
                _json(turn.get("tool_args")),
                _json(turn.get("tool_result")),
                _json(turn.get("raw_response")),
            )
            for turn in turns
        ]
        with self._conn:
            self._conn.executemany(
                """
                INSERT INTO transcripts (
                    episode_id, turn_index, role, content, tool_name, tool_args, tool_result,
                    raw_response
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def record_state_diff(
        self,
        episode_id: int,
        diff_json: Any,
        digest_before: str,
        digest_after: str,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO state_diffs (episode_id, diff_json, digest_before, digest_after)
                VALUES (?, ?, ?, ?)
                """,
                (episode_id, _json(diff_json), digest_before, digest_after),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def record_grading(
        self,
        episode_id: int,
        grader: str,
        passed: bool,
        detail_json: Any = None,
        graded_at: str | None = None,
    ) -> int:
        """Insert or replace the verdict for (episode_id, grader).

        Idempotent per (episode_id, grader): re-grading replaces the prior
        row's values rather than appending a new one, so an offline
        re-scoring pass is safe to run as many times as needed.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO gradings (episode_id, grader, passed, detail_json, graded_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (episode_id, grader) DO UPDATE SET
                    passed = excluded.passed,
                    detail_json = excluded.detail_json,
                    graded_at = excluded.graded_at
                """,
                (episode_id, grader, int(passed), _json(detail_json), graded_at or _now()),
            )
        row = self._conn.execute(
            "SELECT id FROM gradings WHERE episode_id = ? AND grader = ?",
            (episode_id, grader),
        ).fetchone()
        return row["id"]

    # -- reads ---------------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
        return [_row(r) for r in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row(row)

    def gradings_for_episode(self, episode_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM gradings WHERE episode_id = ? ORDER BY grader", (episode_id,)
        ).fetchall()
        return [_row(r) for r in rows]

    def state_diff_for_episode(self, episode_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM state_diffs WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        return _row(row)

    def episodes_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM episodes WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [_row(r) for r in rows]

    def episodes_needing_grading(self, run_id: int | None = None) -> list[dict[str, Any]]:
        """Episodes with no grading row at all yet, from any grader."""
        query = """
            SELECT e.* FROM episodes e
            WHERE NOT EXISTS (SELECT 1 FROM gradings g WHERE g.episode_id = e.id)
        """
        params: tuple[Any, ...] = ()
        if run_id is not None:
            query += " AND e.run_id = ?"
            params = (run_id,)
        query += " ORDER BY e.id"
        rows = self._conn.execute(query, params).fetchall()
        return [_row(r) for r in rows]

    def ungraded_by(self, grader: str, run_id: int | None = None) -> list[dict[str, Any]]:
        """Episodes with no grading row yet from this specific grader.

        Distinct from episodes_needing_grading: an episode already scored by
        state_verifier but not yet by llm_judge is ungraded_by('llm_judge')
        but not episodes_needing_grading (it has been graded, just not by
        every grader).
        """
        query = """
            SELECT e.* FROM episodes e
            WHERE NOT EXISTS (
                SELECT 1 FROM gradings g WHERE g.episode_id = e.id AND g.grader = ?
            )
        """
        params: tuple[Any, ...] = (grader,)
        if run_id is not None:
            query += " AND e.run_id = ?"
            params = (grader, run_id)
        query += " ORDER BY e.id"
        rows = self._conn.execute(query, params).fetchall()
        return [_row(r) for r in rows]
