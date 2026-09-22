"""Round-trip tests for the results database. Pure SQLite, no Postgres needed."""

from __future__ import annotations

import json
import sqlite3

import pytest

from consequence.results import Results


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
    run_id = results.start_run(git_sha="abc123", notes="smoke run")
    results.finish_run(run_id, finished_at="2026-01-01T01:00:00+00:00")

    row = results._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["git_sha"] == "abc123"
    assert row["notes"] == "smoke run"
    assert row["finished_at"] == "2026-01-01T01:00:00+00:00"


def test_round_trip_episodes(results):
    run_id = results.start_run()
    episode_id = _make_episode(results, run_id)

    [row] = results.episodes_for_run(run_id)
    assert row["id"] == episode_id
    assert row["task_id"] == "invoice-duplicate-flag"
    assert row["model_id"] == "claude-sonnet-5"
    assert row["status"] == "completed"
    assert row["input_tokens"] == 1000
    assert row["turn_count"] == 6


def test_round_trip_transcripts(results):
    run_id = results.start_run()
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
    run_id = results.start_run()
    episode_id = _make_episode(results, run_id)

    diff = {"changed": [["invoices", 12, {"status": ["active", "flagged"]}]]}
    diff_id = results.record_state_diff(
        episode_id, diff, digest_before="deadbeef", digest_after="cafef00d"
    )

    row = results._conn.execute(
        "SELECT * FROM state_diffs WHERE id = ?", (diff_id,)
    ).fetchone()
    assert row["episode_id"] == episode_id
    assert json.loads(row["diff_json"]) == diff
    assert row["digest_before"] == "deadbeef"
    assert row["digest_after"] == "cafef00d"


def test_round_trip_gradings(results):
    run_id = results.start_run()
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


def test_regrading_is_idempotent_and_replaces(results):
    run_id = results.start_run()
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
    results.record_grading(episode_id, "llm_judge", passed=True)
    count = results._conn.execute(
        "SELECT COUNT(*) AS n FROM gradings WHERE episode_id = ?", (episode_id,)
    ).fetchone()["n"]
    assert count == 2


def test_failed_episode_write_rolls_back_cleanly(results):
    run_id = results.start_run()
    with pytest.raises(sqlite3.IntegrityError):
        # 'bogus' violates the CHECK constraint on status.
        _make_episode(results, run_id, status="bogus")

    count = results._conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]
    assert count == 0


def test_failed_transcript_batch_rolls_back_cleanly(results):
    run_id = results.start_run()
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
    run_id = results.start_run()
    graded_by_both = _make_episode(results, run_id, trial_index=0)
    graded_by_one = _make_episode(results, run_id, trial_index=1)
    graded_by_none = _make_episode(results, run_id, trial_index=2)

    results.record_grading(graded_by_both, "state_verifier", passed=True)
    results.record_grading(graded_by_both, "llm_judge", passed=True)
    results.record_grading(graded_by_one, "state_verifier", passed=True)

    needing = {row["id"] for row in results.episodes_needing_grading(run_id)}
    assert needing == {graded_by_none}

    ungraded_llm = {row["id"] for row in results.ungraded_by("llm_judge", run_id)}
    assert ungraded_llm == {graded_by_one, graded_by_none}

    ungraded_verifier = {row["id"] for row in results.ungraded_by("state_verifier", run_id)}
    assert ungraded_verifier == {graded_by_none}
