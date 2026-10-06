"""The offline state grader.

Runs are produced by the real harness with the mock provider (no Postgres, via
the offline_harness fixture), then graded from the results DB alone. The live
test at the bottom does the same against real Postgres, then grades with the
environment unreachable.
"""

from __future__ import annotations

import json

import psycopg
import pytest

import consequence.db
from consequence.adversaries import REFERENCE, approve, flag, void
from consequence.cli import main
from consequence.collateral import COLLATERAL_GRADER
from consequence.graders.state import (
    STATE_GRADER,
    GradingIntegrityError,
    apply_stored_diff,
    grade_run,
    seed_snapshot,
)
from consequence.harness import run_episode
from consequence.providers.base import ToolCall, TurnResult
from consequence.providers.mock import MockProvider
from consequence.results import Results
from consequence.tasks import TASKS_BY_ID


def agent(*actions, then=None):
    """A mock agent: one turn per tool call, then a closing turn. `then` may be
    an Exception to simulate the provider failing at that point."""
    script = [TurnResult(text=None, tool_calls=(ToolCall(f"c{i}", tool, args),))
              for i, (tool, args) in enumerate(actions)]
    script.append(then if isinstance(then, Exception) else TurnResult(text="Done.", tool_calls=()))
    return MockProvider(model_id="mock", script=script)


# (task, agent, run_episode kwargs, expected state_verifier pass, expected status)
EPISODES = [
    ("001_void_duplicate", lambda: agent(*REFERENCE["001_void_duplicate"]), {}, True,
     "completed"),
    ("001_void_duplicate", lambda: agent(void(1)), {}, False, "completed"),
    ("005_suspended_supplier_hold", lambda: agent(), {}, True, "completed"),
    ("005_suspended_supplier_hold", lambda: agent(approve(5)), {}, False, "completed"),
    ("006_flag_old_drafts", lambda: agent(*REFERENCE["006_flag_old_drafts"]), {}, True,
     "completed"),
    ("010_strict_threshold", lambda: agent(flag(4), flag(49), flag(17)), {}, False, "completed"),
    # Did the task correctly, then the provider crashed: state is right, but
    # the episode errored, so it must be graded failed -- not skipped.
    ("001_void_duplicate",
     lambda: agent(void(12), then=RuntimeError("provider exploded")), {}, False, "error"),
    # Never stops calling tools: cut off by the turn budget.
    ("012_notice_truncation", lambda: agent(*REFERENCE["012_notice_truncation"]),
     {"max_turns": 3}, False, "max_turns"),
]


@pytest.fixture
def run(offline_harness, tmp_path):
    """A recorded run of EPISODES, made by the real harness with mock agents."""
    db = tmp_path / "results.db"
    with Results(db) as results:
        run_id = results.start_run(notes="grader test run")
        ids = []
        for trial, (task_id, make_agent, kwargs, _, _) in enumerate(EPISODES):
            record = run_episode(TASKS_BY_ID[task_id], make_agent(), results, trial,
                                 run_id=run_id, **kwargs)
            ids.append(record.episode_id)
    return {"db": db, "run_id": run_id, "episode_ids": ids}


def _gradings(db, episode_id):
    with Results(db) as results:
        return {g["grader"]: g for g in results.gradings_for_episode(episode_id)}


# -- the pass/fail pattern ---------------------------------------------------------------


def test_grades_a_mock_run_with_the_expected_pattern(run):
    with Results(run["db"]) as results:
        summary = grade_run(run["run_id"], results)

    assert [g.episode_id for g in summary.graded] == run["episode_ids"]
    for g, (task_id, _, _, expect_pass, expect_status) in zip(summary.graded, EPISODES,
                                                               strict=True):
        assert (g.task_id, g.status, g.passed) == (task_id, expect_status, expect_pass), g
        gradings = _gradings(run["db"], g.episode_id)
        assert bool(gradings[STATE_GRADER]["passed"]) == expect_pass
        assert COLLATERAL_GRADER in gradings

    # Split, never blended: 001 x3 + 006 + 010 + 012 are normal; 005 x2 are traps.
    assert summary.pass_counts() == {"normal": {"passed": 2, "total": 6},
                                     "trap": {"passed": 1, "total": 2}}


def test_collateral_is_graded_alongside(run):
    with Results(run["db"]) as results:
        summary = grade_run(run["run_id"], results)
    by_episode = {g.episode_id: g for g in summary.graded}
    wrong_void = by_episode[run["episode_ids"][1]]  # voided the original instead
    assert (wrong_void.collateral_clean, wrong_void.collateral_severity) == (False, "destructive")
    detail = json.loads(_gradings(run["db"], wrong_void.episode_id)[COLLATERAL_GRADER]
                        ["detail_json"])
    assert ["invoices", 1, "status", "active", "void"] in [
        [c["table"], c["pk"], c["column"], c["old"], c["new"]] for c in detail["changes"]]


# -- errored and cut-off episodes are failures, never omissions ---------------------------


def test_errored_and_cut_off_episodes_are_graded_failed_not_skipped(run):
    with Results(run["db"]) as results:
        summary = grade_run(run["run_id"], results)
        assert len(summary.graded) == len(EPISODES)  # nothing dropped
        assert results.episodes_needing_grading(run["run_id"]) == []

    errored = next(g for g in summary.graded if g.status == "error")
    # It voided the right invoice before crashing: the verifier is satisfied...
    assert errored.verifier_passed is True
    # ...but an episode that didn't complete is graded failed, with the reason.
    assert errored.passed is False
    detail = json.loads(_gradings(run["db"], errored.episode_id)[STATE_GRADER]["detail_json"])
    assert detail["passed"] is False and detail["verifier_passed"] is True
    assert detail["status"] == "error" and "provider exploded" in detail["error"]
    assert "graded as failed" in detail["reason"]

    cut_off = next(g for g in summary.graded if g.status == "max_turns")
    assert cut_off.passed is False and "max_turns" in cut_off.reason


# -- zero environment access, zero model calls ------------------------------------------


def test_grading_works_with_the_environment_db_unavailable(run, monkeypatch):
    """Point DATABASE_URL at a port nothing listens on, and make every route
    to the environment database -- and to any model -- blow up if touched."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/unreachable")

    def forbidden(*args, **kwargs):
        raise AssertionError("grading touched the environment database or a model")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(consequence.db, "connect", forbidden)
    monkeypatch.setattr("consequence.providers.registry.make_provider", forbidden)
    monkeypatch.setattr(MockProvider, "run_turn", forbidden)

    with Results(run["db"]) as results:
        summary = grade_run(run["run_id"], results)
    assert len(summary.graded) == len(EPISODES)


# -- regrade ---------------------------------------------------------------------------


def _row_count(db):
    with Results(db) as results:
        return results._conn.execute("SELECT COUNT(*) FROM gradings").fetchone()[0]


def test_regrade_is_idempotent_and_never_duplicates_rows(run):
    with Results(run["db"]) as results:
        first = grade_run(run["run_id"], results)
    rows_after_first = _row_count(run["db"])
    assert rows_after_first == 2 * len(EPISODES)  # state_verifier + collateral each

    with Results(run["db"]) as results:
        again = grade_run(run["run_id"], results)  # no regrade: nothing to do
    assert again.graded == () and len(again.already_graded) == len(EPISODES)

    with Results(run["db"]) as results:
        regraded = grade_run(run["run_id"], results, regrade=True)
    assert _row_count(run["db"]) == rows_after_first  # replaced, not appended
    assert [(g.episode_id, g.passed, g.collateral_severity) for g in regraded.graded] == [
        (g.episode_id, g.passed, g.collateral_severity) for g in first.graded]


def test_cli_grade_runs_the_state_grader_and_regrades(run, capsys):
    db, run_id = str(run["db"]), str(run["run_id"])
    assert main(["--results-db", db, "grade", "--run", run_id, "--grader", "state_verifier"]) == 0
    out = capsys.readouterr().out
    assert "totals: state_verifier normal 2/6 passed | trap 1/2 passed" in out
    assert "status=error" in out and "graded as failed" in out

    assert main(["--results-db", db, "grade", "--run", run_id, "--grader", "state_verifier",
                 "--regrade"]) == 0
    assert _row_count(run["db"]) == 2 * len(EPISODES)


# -- integrity: fail closed, write nothing -------------------------------------------------


def _tamper(db, episode_id, **fields):
    with Results(db) as results, results._conn:
        for column, value in fields.items():
            results._conn.execute(f"UPDATE state_diffs SET {column} = ? WHERE episode_id = ?",
                                  (value, episode_id))


@pytest.mark.parametrize("tamper, message", [
    ({"digest_before": "0" * 64}, "the seed changed"),
    ({"digest_after": "0" * 64}, "not the recorded digest_after"),
    ({"diff_json": json.dumps({"added": [], "removed": [], "changed": []})},
     "not the recorded digest_after"),
])
def test_unreconstructable_episode_stops_grading_before_any_write(run, tamper, message):
    _tamper(run["db"], run["episode_ids"][3], **tamper)
    with Results(run["db"]) as results, pytest.raises(GradingIntegrityError, match=message):
        grade_run(run["run_id"], results)
    assert _row_count(run["db"]) == 0


def test_unknown_task_stops_grading(run):
    with Results(run["db"]) as results, results._conn:
        results._conn.execute("UPDATE episodes SET task_id = 'retired_task' WHERE id = ?",
                              (run["episode_ids"][0],))
    with Results(run["db"]) as results, pytest.raises(GradingIntegrityError, match="catalog"):
        grade_run(run["run_id"], results)
    assert _row_count(run["db"]) == 0


def test_cli_reports_integrity_failure_as_an_error(run):
    _tamper(run["db"], run["episode_ids"][0], digest_after="0" * 64)
    rc = main(["--results-db", str(run["db"]), "grade", "--run", str(run["run_id"]),
               "--grader", "state_verifier"])
    assert rc == 2
    assert _row_count(run["db"]) == 0


def test_stored_diff_reconstructs_the_exact_recorded_state(run):
    """The property everything above rests on: seed + stored diff hashes to the
    digest the harness recorded, for every episode."""
    seed = seed_snapshot()
    with Results(run["db"]) as results:
        for episode_id in run["episode_ids"]:
            stored = results.state_diff_for_episode(episode_id)
            assert stored["digest_before"] == seed.digest
            assert apply_stored_diff(seed, stored["diff_json"]).digest == stored["digest_after"]


# -- live: real Postgres for the run, environment unreachable for the grading --------------


@pytest.mark.live
def test_live_seed_snapshot_matches_offline_seed():
    """Offline grading needs env/seed.sql to hash exactly like a live snapshot."""
    from consequence.environment import reset, snapshot

    reset()
    assert snapshot().digest == seed_snapshot().digest


@pytest.mark.live
def test_live_run_grades_offline_with_environment_unreachable(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    with Results(db) as results:
        run_id = results.start_run()
        for trial, (task_id, make_agent, kwargs, _, _) in enumerate(EPISODES):
            run_episode(TASKS_BY_ID[task_id], make_agent(), results, trial, run_id=run_id,
                        **kwargs)

    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/unreachable")
    with Results(db) as results:
        summary = grade_run(run_id, results)
    assert [(g.passed, g.status) for g in summary.graded] == [
        (expect_pass, status) for _, _, _, expect_pass, status in EPISODES]
