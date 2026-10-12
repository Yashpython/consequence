"""Round-trip tests for the results database. Pure SQLite, no Postgres needed."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from helpers import record_episode_with_state, seed_run

from consequence.diff import diff
from consequence.environment import SEED_PATH, TABLES, Snapshot, snapshot_from_json
from consequence.results import (
    SCHEMA_VERSION,
    Results,
    ResultsIntegrityError,
    ResultsSchemaError,
)


@pytest.fixture
def results(tmp_path):
    with Results(tmp_path / "results.db") as r:
        yield r


def _make_episode(results: Results, run_id: int, **overrides) -> int:
    fields = {
        "task_id": "invoice-duplicate-flag",
        "model_id": "claude-sonnet-5",
        "trial_index": 0,
        "status": "completed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": 0.015,
        "latency_ms": 4200,
        "turn_count": 6,
    }
    fields.update(overrides)
    return results.record_episode(run_id, **fields)


def test_round_trip_runs(results):
    run_id = seed_run(results, git_sha="abc123", notes="smoke run")
    results.finish_run(run_id, finished_at="2026-01-01T01:00:00+00:00")

    row = results._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["git_sha"] == "abc123"
    assert row["notes"] == "smoke run"
    assert row["finished_at"] == "2026-01-01T01:00:00+00:00"


def test_round_trip_episodes(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)

    [row] = results.episodes_for_run(run_id)
    assert row["id"] == episode_id
    assert row["task_id"] == "invoice-duplicate-flag"
    assert row["model_id"] == "claude-sonnet-5"
    assert row["status"] == "completed"
    assert row["input_tokens"] == 1000
    assert row["turn_count"] == 6


def test_round_trip_transcripts(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)

    turns = [
        {"turn_index": 0, "role": "user", "content": "Flag the duplicate invoice."},
        {
            "turn_index": 1,
            "role": "assistant",
            "content": "Looking for duplicates.",
            "tool_name": "query_invoices",
            "tool_args": {"supplier_id": 1},
            "tool_result": {"rows": [{"id": 1}, {"id": 12}]},
        },
    ]
    results.record_transcript(episode_id, turns)

    rows = results._conn.execute(
        "SELECT * FROM transcripts WHERE episode_id = ? ORDER BY turn_index", (episode_id,)
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert rows[1]["tool_name"] == "query_invoices"
    assert json.loads(rows[1]["tool_args"]) == {"supplier_id": 1}
    assert json.loads(rows[1]["tool_result"]) == {"rows": [{"id": 1}, {"id": 12}]}


def test_round_trip_state_diffs(results):
    run_id = seed_run(results)
    episode_id = record_episode_with_state(
        results, run_id, [("void_invoice", {"invoice_id": 12, "reason": "duplicate"})]
    )
    row = results.state_diff_for_episode(episode_id)
    initial = results.initial_snapshot(run_id)
    after = snapshot_from_json(row["after_snapshot_json"])

    assert row["digest_before"] == initial.digest
    assert row["digest_after"] == after.digest  # the stored text IS what was hashed
    assert row["diff_json"] == diff(initial, after).to_json()
    assert json.loads(row["diff_json"])["changed"] == [
        ["invoices", 12, {"status": ["active", "void"]}]
    ]


def test_round_trip_gradings(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)

    results.record_grading(
        episode_id, "state_verifier", passed=True, detail_json={"reason": "flag set"}
    )

    row = results._conn.execute(
        "SELECT * FROM gradings WHERE episode_id = ? AND grader = ?",
        (episode_id, "state_verifier"),
    ).fetchone()
    assert row["passed"] == 1
    assert json.loads(row["detail_json"]) == {"reason": "flag set"}
    assert (row["status"], row["verifier_verdict_raw"], row["error_traceback"]) == (
        "graded", None, None)


def test_passed_and_the_raw_verifier_verdict_are_separate_columns(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id, status="error")
    results.record_grading(episode_id, "state_verifier", passed=False,
                           verifier_verdict_raw=True)
    [row] = results.gradings_for_episode(episode_id)
    assert (row["passed"], row["verifier_verdict_raw"]) == (0, 1)


def test_grading_error_has_no_verdict_and_keeps_the_traceback(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    results.record_grading(episode_id, "state_verifier", passed=True)
    results.record_grading_error(episode_id, "state_verifier", "Traceback ...\nKeyError: 'x'")

    [row] = results.gradings_for_episode(episode_id)  # replaced, not appended
    assert row["status"] == "grading_error"
    assert row["passed"] is None and row["verifier_verdict_raw"] is None
    assert row["error_traceback"].endswith("KeyError: 'x'")

    results.record_grading(episode_id, "state_verifier", passed=True)  # retried, fixed
    [row] = results.gradings_for_episode(episode_id)
    assert (row["status"], row["passed"], row["error_traceback"]) == ("graded", 1, None)


@pytest.mark.parametrize("status, passed, traceback_text", [
    ("graded", None, None),               # a verdict row must have a verdict
    ("grading_error", 0, "Traceback"),    # an error row can't also be a failure
    ("grading_error", None, None),        # an error row must keep its traceback
    ("graded", 1, "Traceback"),           # a verdict row has no traceback
])
def test_gradings_constraints_keep_verdicts_and_errors_apart(results, status, passed,
                                                             traceback_text):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        results._upsert_grading(episode_id, "state_verifier", status=status, passed=passed,
                                error_traceback=traceback_text)


@pytest.mark.parametrize("columns", [
    {"status": "judge_error"},                                   # an error needs its reason
    {"status": "judge_error", "error_reason": "x", "passed": 0},  # and is never also a fail
    {"status": "graded", "passed": 1, "error_reason": "x"},      # a verdict has no error
    {"status": "graded", "passed": 1, "judge_temperature": 0.7,
     "judge_api_agreement": 1.0},           # API agreement exists only at temperature 0
    {"status": "graded", "passed": 1, "judge_temperature": 0.0,
     "judge_vote_agreement": 1.0},          # vote agreement exists only above 0
    {"status": "graded", "passed": 1, "behaviour": "probably_fine"},  # closed vocabulary
    {"status": "graded", "passed": 1, "confidence": 1.5},        # confidence is 0..1
])
def test_judge_row_constraints(results, columns):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        results._upsert_grading(episode_id, "llm_judge", "judge-x", **columns)


def test_judge_rows_need_a_judge_model_and_other_rows_must_not_have_one(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        results._upsert_grading(episode_id, "llm_judge", status="graded", passed=1)
    with pytest.raises(sqlite3.IntegrityError):
        results._upsert_grading(episode_id, "state_verifier", "judge-x", status="graded",
                                passed=1)


def test_two_judges_on_one_episode_are_two_rows_and_regrading_replaces(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    for judge in ("judge-a", "judge-b", "judge-a"):
        results.record_judge_grading(episode_id, "llm_judge", judge, passed=True,
                                     behaviour="completed", confidence=0.8, temperature=0.0,
                                     agreement=1.0, input_tokens=1, output_tokens=1,
                                     cost_usd=0.0)
    rows = results.gradings_for_episode(episode_id)
    assert [(r["grader"], r["judge_model"]) for r in rows] == [
        ("llm_judge", "judge-a"), ("llm_judge", "judge-b")]


def test_regrading_is_idempotent_and_replaces(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)

    first_id = results.record_grading(episode_id, "state_verifier", passed=False)
    second_id = results.record_grading(
        episode_id, "state_verifier", passed=True, detail_json={"fixed": "grader bug"}
    )

    count = results._conn.execute(
        "SELECT COUNT(*) AS n FROM gradings WHERE episode_id = ? AND grader = ?",
        (episode_id, "state_verifier"),
    ).fetchone()["n"]
    assert count == 1
    assert first_id == second_id  # same row, updated in place

    row = results._conn.execute(
        "SELECT * FROM gradings WHERE id = ?", (second_id,)
    ).fetchone()
    assert row["passed"] == 1
    assert json.loads(row["detail_json"]) == {"fixed": "grader bug"}

    # a second grader on the same episode is a separate row, not a replacement
    results.record_judge_grading(episode_id, "llm_judge", "judge-x", passed=True, behaviour="completed",
                                 confidence=0.9, temperature=0.0, agreement=1.0,
                                 input_tokens=10, output_tokens=5, cost_usd=0.001)
    count = results._conn.execute(
        "SELECT COUNT(*) AS n FROM gradings WHERE episode_id = ?", (episode_id,)
    ).fetchone()["n"]
    assert count == 2


def test_failed_episode_write_rolls_back_cleanly(results):
    run_id = seed_run(results)
    with pytest.raises(sqlite3.IntegrityError):
        # 'bogus' violates the CHECK constraint on status.
        _make_episode(results, run_id, status="bogus")

    count = results._conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]
    assert count == 0


def test_failed_transcript_batch_rolls_back_cleanly(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)

    turns = [
        {"turn_index": 0, "role": "user", "content": "fine"},
        {"turn_index": 1, "role": None, "content": "role is NOT NULL, this must fail"},
    ]
    with pytest.raises(sqlite3.IntegrityError):
        results.record_transcript(episode_id, turns)

    count = results._conn.execute(
        "SELECT COUNT(*) AS n FROM transcripts WHERE episode_id = ?", (episode_id,)
    ).fetchone()["n"]
    assert count == 0


def test_episodes_needing_grading_and_ungraded_by(results):
    run_id = seed_run(results)
    graded_by_both = _make_episode(results, run_id, trial_index=0)
    graded_by_one = _make_episode(results, run_id, trial_index=1)
    graded_by_none = _make_episode(results, run_id, trial_index=2)

    results.record_grading(graded_by_both, "state_verifier", passed=True)
    results.record_judge_grading(graded_by_both, "llm_judge", "judge-x", passed=True, behaviour="completed",
                                 confidence=0.9, temperature=0.0, agreement=1.0,
                                 input_tokens=10, output_tokens=5, cost_usd=0.001)
    results.record_grading(graded_by_one, "state_verifier", passed=True)

    needing = {row["id"] for row in results.episodes_needing_grading(run_id)}
    assert needing == {graded_by_none}

    ungraded_llm = {row["id"] for row in results.ungraded_by("llm_judge", run_id)}
    assert ungraded_llm == {graded_by_one, graded_by_none}

    ungraded_verifier = {row["id"] for row in results.ungraded_by("state_verifier", run_id)}
    assert ungraded_verifier == {graded_by_none}


# -- results.db is the single source of truth: snapshots, schema, write-time integrity --


def test_run_stores_its_initial_state_and_seed_once(results):
    run_id = seed_run(results)
    row = results.get_run(run_id)
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["seed_sql_text"] == SEED_PATH.read_text(encoding="utf-8")
    assert row["seed_sha256"] == hashlib.sha256(row["seed_sql_text"].encode()).hexdigest()
    assert snapshot_from_json(row["initial_snapshot_json"]).digest == row["initial_digest"]


def test_schema_version_mismatch_refuses_to_open(tmp_path):
    path = tmp_path / "results.db"
    Results(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    with pytest.raises(ResultsSchemaError, match="delete .*results.db and re-run"):
        Results(path)


def test_database_from_before_versioning_refuses_to_open(tmp_path):
    """A results.db written by the previous code: tables present, no version."""
    path = tmp_path / "results.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL)")
    with pytest.raises(ResultsSchemaError, match="schema version 0"):
        Results(path)


def _void_12_state():
    from consequence.offline import OfflineEnvironment

    env = OfflineEnvironment()
    assert env.call_tool("void_invoice", {"invoice_id": 12, "reason": "dup"})["ok"]
    before, after = env.snapshots()
    return before, after, diff(before, after)


def _state_rows(results):
    return results._conn.execute("SELECT COUNT(*) FROM state_diffs").fetchone()[0]


def test_write_rejects_an_episode_that_did_not_start_from_the_run_state(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    _, after, state_diff = _void_12_state()
    with pytest.raises(ResultsIntegrityError, match="not deterministic"):
        results.record_state_diff(episode_id, state_diff.to_json(), "0" * 64, after.digest,
                                  after)
    assert _state_rows(results) == 0


def test_write_rejects_an_after_snapshot_that_does_not_match_its_digest(results):
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    before, after, state_diff = _void_12_state()
    tampered = Snapshot(structure=after.structure, digest="f" * 64)
    with pytest.raises(ResultsIntegrityError, match="digest_after"):
        results.record_state_diff(episode_id, state_diff.to_json(), before.digest,
                                  tampered.digest, tampered)
    assert _state_rows(results) == 0


def test_write_rejects_a_diff_that_drops_audit_log_rows(results):
    """The specific way the persisted diff could go wrong: touched() excludes
    audit_log, and if that exclusion leaked into what's stored, the stored diff
    would silently lose the audit trail. It must be rejected at write time."""
    run_id = seed_run(results)
    episode_id = _make_episode(results, run_id)
    before, after, state_diff = _void_12_state()
    without_audit = json.loads(state_diff.to_json())
    without_audit["added"] = [a for a in without_audit["added"] if a[0] != "audit_log"]
    with pytest.raises(ResultsIntegrityError, match="not diff"):
        results.record_state_diff(episode_id, json.dumps(without_audit, sort_keys=True),
                                  before.digest, after.digest, after)
    assert _state_rows(results) == 0


def test_stored_diff_round_trips_every_table_including_audit_log(results):
    """Regression guard. Which tables changed is computed here independently of
    the diff module, by comparing the stored before/after rows directly, and
    the persisted diff_json must cover exactly those tables -- audit_log
    included. diff.touched() excludes audit_log; that must never leak into
    what's persisted."""
    actions = [
        ("update_supplier", {"supplier_id": 8, "status": "suspended"}),   # suppliers
        ("void_invoice", {"invoice_id": 44, "reason": "draft"}),          # invoices
        ("flag_for_review", {"invoice_id": 8, "reason": "active"}),       # review_flags
        ("create_approval", {"invoice_id": 20, "decision": "rejected", "reason": "x"}),
    ]
    run_id = seed_run(results)
    episode_id = record_episode_with_state(results, run_id, actions)
    stored = results.state_diff_for_episode(episode_id)
    before = results.initial_snapshot(run_id).structure
    after = json.loads(stored["after_snapshot_json"])

    changed_tables = {
        table for table in TABLES
        if {r["id"]: r for r in before[table]} != {r["id"]: r for r in after[table]}
    }
    payload = json.loads(stored["diff_json"])
    tables_in_diff = {entry[0] for kind in ("added", "removed", "changed")
                      for entry in payload[kind]}

    assert changed_tables == {"suppliers", "invoices", "review_flags", "approvals", "audit_log"}
    assert tables_in_diff == changed_tables
    audit_added = [row for table, _, row in payload["added"] if table == "audit_log"]
    assert len(audit_added) == len(actions)
    assert audit_added == after["audit_log"]  # every audit row, verbatim
