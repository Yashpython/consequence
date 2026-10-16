"""The results database: the durable record of every experiment run.

This is designed so grading can be re-run offline, from stored raw data,
without ever calling a model API again. A bug in a grader costs nothing to
fix and re-score -- see state_diffs and record_grading below.

It is the single source of truth: a results.db is gradeable on its own, with
no other file present. Each run stores the environment's starting state once
(runs.initial_snapshot_json -- reset() is deterministic, so every episode in a
run starts from exactly this state), plus the seed SQL it came from. Each
episode stores the full state it left (state_diffs.after_snapshot_json). Both
are stored as the exact canonical JSON that was hashed, so verifying one is
sha256(stored text) == recorded digest. Integrity is checked when an episode
is written, not discovered later at grading: a digest or diff that doesn't
match raises ResultsIntegrityError and nothing is written.

Uses SQLite at RESULTS_DB_PATH (default results/results.db).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from consequence.config import load_config
from consequence.diff import diff
from consequence.environment import Snapshot, canonical_json, digest_of, snapshot_from_json

DEFAULT_DB_PATH = "results/results.db"

# Bump on any schema change. A results.db with a different version is refused
# outright. There are no migrations: no real runs exist yet, so the remedy is
# to delete the file and re-run.
SCHEMA_VERSION = 6


class ResultsSchemaError(RuntimeError):
    """The results.db was written by a different schema version."""


class ResultsIntegrityError(RuntimeError):
    """Stored state doesn't match its recorded digests, or its own diff."""

# SQLite table order matters here only for readability; foreign keys are
# declared but SQLite enforces them only with PRAGMA foreign_keys = ON,
# which Results turns on for every connection.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id                    INTEGER PRIMARY KEY,
    schema_version        INTEGER NOT NULL,
    started_at            TEXT NOT NULL,
    finished_at           TEXT,
    git_sha               TEXT,
    notes                 TEXT,
    -- Provenance: the exact seed this run's environment was built from.
    seed_sql_text         TEXT NOT NULL,
    seed_sha256           TEXT NOT NULL,
    -- The canonical starting state, stored once per run: reset() is
    -- deterministic, so it is identical for every episode (enforced on write).
    initial_snapshot_json TEXT NOT NULL,
    initial_digest        TEXT NOT NULL
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
    retry_count   INTEGER,
    -- What actually served the episode's calls, from each response (the
    -- adapters' served_model_from_raw), comma-joined if they differed. It can
    -- differ from the configured api_model; see metrics.served_models.
    served_model  TEXT
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

-- The state an episode left, stored verbatim so a grader can be re-run
-- offline against exactly what happened -- no need to replay the episode or
-- call a model API again. This is the whole point: a buggy verifier costs
-- nothing to fix and re-score, because the evidence it grades already lives
-- here. `before` is the run's initial_snapshot_json (digest_before must equal
-- the run's initial_digest). diff_json is kept for the trace viewer, and is
-- cross-checked on write against diff(before, after).
CREATE TABLE IF NOT EXISTS state_diffs (
    id                  INTEGER PRIMARY KEY,
    episode_id          INTEGER NOT NULL REFERENCES episodes (id),
    diff_json           TEXT NOT NULL,
    digest_before       TEXT NOT NULL,
    digest_after        TEXT NOT NULL,
    after_snapshot_json TEXT NOT NULL
);

-- Multiple gradings per episode is by design -- that is the entire
-- experiment (comparing graders, re-scoring after a grader bugfix, running
-- both a state_verifier and an llm_judge over the same episode). 'collateral'
-- is not a pass/fail judge of the task: its passed flag means "no rows outside
-- the task's scope were touched" (see consequence/collateral.py). A given
-- (episode_id, grader, judge_model) is unique: re-grading replaces the prior
-- verdict rather than appending a new one, so re-scoring is safe to re-run.
-- judge_model is '' for the deterministic graders and the judge's models.yaml
-- id for llm_judge / llm_judge_stability, so results always split by judge.
--
-- Two notions of success, kept in separate columns so later code can't mix
-- them up:
--   passed               the authoritative result. Every metric uses this and
--                        only this. An episode that didn't complete is never
--                        passed, whatever state it left.
--   verifier_verdict_raw what the task's verifier said about the final state,
--                        before the completion rule. Analysis only, never
--                        aggregated into a pass rate. NULL for graders that
--                        have no separate verifier verdict.
-- status 'grading_error' means the grader itself raised: there is no verdict
-- (passed is NULL, the traceback is stored). status 'judge_error' means the
-- judge couldn't render one (error_reason says why, e.g. 'context_overflow').
-- Neither is a pass or a fail.
--
-- Judge rows (see consequence/graders/judge.py): passed is the majority of
-- three votes; behaviour, confidence and the token/cost columns describe the
-- judging, never the episode. Agreement among the three votes is stored under
-- a name that says what it measures: judge_api_agreement only at temperature 0
-- (API-level nondeterminism), judge_vote_agreement only above 0 (sampling
-- stability). judge_cost_usd is judging spend, kept out of episodes.cost_usd.
CREATE TABLE IF NOT EXISTS gradings (
    id                   INTEGER PRIMARY KEY,
    episode_id           INTEGER NOT NULL REFERENCES episodes (id),
    grader               TEXT NOT NULL CHECK (grader IN ('state_verifier', 'llm_judge',
                                                         'llm_judge_stability',
                                                         'llm_judge_api_agreement',
                                                         'collateral')),
    judge_model          TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL
                         CHECK (status IN ('graded', 'grading_error', 'judge_error')),
    passed               INTEGER CHECK (passed IN (0, 1)),
    verifier_verdict_raw INTEGER CHECK (verifier_verdict_raw IN (0, 1)),
    behaviour            TEXT CHECK (behaviour IN ('refused_and_explained', 'silent_inaction',
                                                   'false_claim', 'acted_incorrectly',
                                                   'completed')),
    confidence           REAL CHECK (confidence BETWEEN 0 AND 1),
    judge_temperature    REAL,
    judge_api_agreement  REAL CHECK (judge_api_agreement BETWEEN 0 AND 1),
    judge_vote_agreement REAL CHECK (judge_vote_agreement BETWEEN 0 AND 1),
    judge_input_tokens   INTEGER,
    judge_output_tokens  INTEGER,
    judge_cost_usd       REAL,
    served_model         TEXT,  -- the judge model that served the votes
    error_reason         TEXT,
    error_traceback      TEXT,
    detail_json          TEXT,
    graded_at            TEXT NOT NULL,
    UNIQUE (episode_id, grader, judge_model),
    CHECK ((status = 'graded') = (passed IS NOT NULL)),
    CHECK (status != 'grading_error' OR error_traceback IS NOT NULL),
    CHECK (status != 'judge_error' OR error_reason IS NOT NULL),
    CHECK (status != 'graded' OR (error_traceback IS NULL AND error_reason IS NULL)),
    CHECK ((grader IN ('llm_judge', 'llm_judge_stability', 'llm_judge_api_agreement'))
           = (judge_model != '')),
    CHECK (judge_api_agreement IS NULL OR judge_temperature = 0),
    CHECK (judge_vote_agreement IS NULL OR judge_temperature > 0)
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
            run_id = consequence.harness.begin_run(results)
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
        self._initial_cache: dict[int, Snapshot] = {}
        has_tables = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        if not has_tables:  # a brand-new file: create it at the current version
            self._conn.executescript(SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            return
        found = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if found != SCHEMA_VERSION:
            self._conn.close()
            raise ResultsSchemaError(
                f"{self.db_path} has results schema version {found}, but this code "
                f"needs version {SCHEMA_VERSION}. There are no migrations: delete "
                f"{self.db_path} and re-run."
            )

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
        initial_snapshot: Snapshot,
        seed_sql_text: str,
        *,
        started_at: str | None = None,
        git_sha: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Begin a run, storing its starting state and seed once.

        Normally called via consequence.harness.begin_run, which takes the
        snapshot right after reset().
        """
        initial_json = canonical_json(initial_snapshot.structure)
        if digest_of(initial_json) != initial_snapshot.digest:
            raise ResultsIntegrityError("initial snapshot doesn't hash to its own digest")
        seed_sha256 = hashlib.sha256(seed_sql_text.encode("utf-8")).hexdigest()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO runs (schema_version, started_at, git_sha, notes, seed_sql_text,
                                  seed_sha256, initial_snapshot_json, initial_digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (SCHEMA_VERSION, started_at or _now(), git_sha, notes, seed_sql_text,
                 seed_sha256, initial_json, initial_snapshot.digest),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def initial_snapshot(self, run_id: int) -> Snapshot:
        """The run's stored starting state, digest-verified."""
        if run_id not in self._initial_cache:
            row = self._conn.execute(
                "SELECT initial_snapshot_json, initial_digest FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ResultsIntegrityError(f"no run {run_id}")
            snap = snapshot_from_json(row["initial_snapshot_json"])
            if snap.digest != row["initial_digest"]:
                raise ResultsIntegrityError(
                    f"run {run_id}: stored initial snapshot hashes to {snap.digest}, "
                    f"not its recorded initial_digest {row['initial_digest']}"
                )
            self._initial_cache[run_id] = snap
        return self._initial_cache[run_id]

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
        served_model: str | None = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO episodes (
                    run_id, task_id, model_id, trial_index, started_at, finished_at,
                    status, error, input_tokens, output_tokens, cost_usd, latency_ms,
                    turn_count, retry_count, served_model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    served_model,
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

    def verify_state(
        self,
        run_id: int,
        diff_json: str,
        digest_before: str,
        digest_after: str,
        after_snapshot: Snapshot,
    ) -> str:
        """Check an episode's state against its run, before anything is
        written. Returns the after snapshot's canonical JSON to store.

        Raises ResultsIntegrityError if the episode didn't start from the
        run's initial state, if the after snapshot doesn't hash to
        digest_after, or if diff_json isn't exactly diff(before, after).
        """
        initial = self.initial_snapshot(run_id)
        if digest_before != initial.digest:
            raise ResultsIntegrityError(
                f"episode started from digest {digest_before}, not run {run_id}'s initial "
                f"state {initial.digest} -- reset() was not deterministic (standing rule 2)"
            )
        after_json = canonical_json(after_snapshot.structure)
        if not digest_of(after_json) == after_snapshot.digest == digest_after:
            raise ResultsIntegrityError(
                f"after snapshot hashes to {digest_of(after_json)}, but the recorded "
                f"digest_after is {digest_after}"
            )
        expected = diff(initial, snapshot_from_json(after_json)).to_json()
        if diff_json != expected:
            raise ResultsIntegrityError(
                "diff_json is not diff(before, after) of the stored snapshots"
            )
        return after_json

    def record_state_diff(
        self,
        episode_id: int,
        diff_json: str,
        digest_before: str,
        digest_after: str,
        after_snapshot: Snapshot,
    ) -> int:
        """Store an episode's final state. Verified first (verify_state); a
        mismatch raises and writes nothing."""
        row = self._conn.execute(
            "SELECT run_id FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if row is None:
            raise ResultsIntegrityError(f"no episode {episode_id}")
        after_json = self.verify_state(
            row["run_id"], diff_json, digest_before, digest_after, after_snapshot
        )
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO state_diffs (episode_id, diff_json, digest_before, digest_after,
                                         after_snapshot_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (episode_id, diff_json, digest_before, digest_after, after_json),
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
        *,
        verifier_verdict_raw: bool | None = None,
    ) -> int:
        """Insert or replace the verdict for (episode_id, grader).

        Idempotent per (episode_id, grader): re-grading replaces the prior
        row's values rather than appending a new one, so an offline
        re-scoring pass is safe to run as many times as needed.
        """
        raw = None if verifier_verdict_raw is None else int(verifier_verdict_raw)
        return self._upsert_grading(episode_id, grader, status="graded", passed=int(passed),
                                    verifier_verdict_raw=raw, detail_json=_json(detail_json),
                                    graded_at=graded_at)

    def record_grading_error(
        self,
        episode_id: int,
        grader: str,
        error_traceback: str,
        detail_json: Any = None,
        graded_at: str | None = None,
    ) -> int:
        """Record that the grader raised on this episode: no verdict, so
        passed is NULL -- not a failure -- and the traceback is kept.
        Replaces any prior row for (episode_id, grader), like record_grading."""
        return self._upsert_grading(episode_id, grader, status="grading_error",
                                    error_traceback=error_traceback,
                                    detail_json=_json(detail_json), graded_at=graded_at)

    def record_judge_grading(
        self,
        episode_id: int,
        grader: str,
        judge_model: str,
        *,
        passed: bool,
        behaviour: str | None,
        confidence: float | None,
        temperature: float,
        agreement: float | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None,
        detail_json: Any = None,
        served_model: str | None = None,
    ) -> int:
        """A judge's majority verdict. `agreement` is stored as
        judge_api_agreement at temperature 0 and as judge_vote_agreement above
        it: the same arithmetic measures different things (see
        docs/judge-methodology.md), so it never shares a column. It is None
        for a single-vote verdict, which measures no agreement at all."""
        at_zero = temperature == 0
        return self._upsert_grading(
            episode_id, grader, judge_model, status="graded", passed=int(passed),
            behaviour=behaviour, confidence=confidence, judge_temperature=temperature,
            judge_api_agreement=agreement if at_zero else None,
            judge_vote_agreement=None if at_zero else agreement,
            judge_input_tokens=input_tokens, judge_output_tokens=output_tokens,
            judge_cost_usd=cost_usd, served_model=served_model,
            detail_json=_json(detail_json),
        )

    def record_judge_error(
        self,
        episode_id: int,
        grader: str,
        judge_model: str,
        *,
        reason: str,
        temperature: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = 0.0,
        error_traceback: str | None = None,
        detail_json: Any = None,
        served_model: str | None = None,
    ) -> int:
        """The judge rendered no verdict. Tokens already spent are still
        recorded, so judging cost is complete even when judging fails."""
        return self._upsert_grading(
            episode_id, grader, judge_model, status="judge_error", error_reason=reason,
            error_traceback=error_traceback, judge_temperature=temperature,
            judge_input_tokens=input_tokens, judge_output_tokens=output_tokens,
            judge_cost_usd=cost_usd, served_model=served_model,
            detail_json=_json(detail_json),
        )

    _GRADING_COLUMNS = (
        "status", "passed", "verifier_verdict_raw", "behaviour", "confidence",
        "judge_temperature", "judge_api_agreement", "judge_vote_agreement",
        "judge_input_tokens", "judge_output_tokens", "judge_cost_usd", "served_model",
        "error_reason",
        "error_traceback", "detail_json", "graded_at",
    )

    def _upsert_grading(self, episode_id: int, grader: str, judge_model: str = "",
                        **values: Any) -> int:
        """Insert or replace the row for (episode_id, grader, judge_model).
        Every column not given is reset to NULL, so a replaced row never keeps
        a stale field from the verdict it replaces."""
        unknown = set(values) - set(self._GRADING_COLUMNS)
        if unknown:
            raise ValueError(f"unknown gradings column(s): {sorted(unknown)}")
        values["graded_at"] = values.get("graded_at") or _now()
        row = [values.get(c) for c in self._GRADING_COLUMNS]
        columns = ", ".join(self._GRADING_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in self._GRADING_COLUMNS)
        placeholders = ", ".join("?" * len(row))
        with self._conn:
            self._conn.execute(
                f"""
                INSERT INTO gradings (episode_id, grader, judge_model, {columns})
                VALUES (?, ?, ?, {placeholders})
                ON CONFLICT (episode_id, grader, judge_model) DO UPDATE SET {updates}
                """,
                (episode_id, grader, judge_model, *row),
            )
        found = self._conn.execute(
            "SELECT id FROM gradings WHERE episode_id = ? AND grader = ? AND judge_model = ?",
            (episode_id, grader, judge_model),
        ).fetchone()
        return found["id"]

    # -- reads ---------------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        """Run metadata only -- not the stored seed or snapshot payloads."""
        rows = self._conn.execute(
            "SELECT id, schema_version, started_at, finished_at, git_sha, notes, seed_sha256, "
            "initial_digest FROM runs ORDER BY id"
        ).fetchall()
        return [_row(r) for r in rows]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return _row(row)

    def gradings_for_episode(self, episode_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM gradings WHERE episode_id = ? ORDER BY grader, judge_model",
            (episode_id,),
        ).fetchall()
        return [_row(r) for r in rows]

    def episodes_graded_by(self, run_id: int, grader: str,
                           judge_model: str = "") -> set[int]:
        """Ids of the run's episodes with a row (any status) from this grader.
        Two graders' pass rates are only comparable over the same set."""
        rows = self._conn.execute(
            """
            SELECT g.episode_id FROM gradings g JOIN episodes e ON e.id = g.episode_id
            WHERE e.run_id = ? AND g.grader = ? AND g.judge_model = ?
            """,
            (run_id, grader, judge_model),
        ).fetchall()
        return {r["episode_id"] for r in rows}

    def transcript_for_episode(self, episode_id: int) -> list[dict[str, Any]]:
        """Every stored turn of the episode, in order, exactly as stored."""
        rows = self._conn.execute(
            "SELECT * FROM transcripts WHERE episode_id = ? ORDER BY turn_index",
            (episode_id,),
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
