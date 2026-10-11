"""The offline state grader.

Runs are produced by the real harness with the mock provider (no Postgres, via
the offline_harness fixture), then graded from the results DB alone. The live
test at the bottom does the same against real Postgres, then grades with the
environment unreachable.
"""

from __future__ import annotations

import hashlib
import json
import shutil

import psycopg
import pytest

import consequence.db
from consequence.adversaries import REFERENCE, approve, flag, void
from consequence.cli import main
from consequence.collateral import COLLATERAL_GRADER
from consequence.environment import SEED_PATH, snapshot_from_json
from consequence.graders.state import (
    STATE_GRADER,
    GradingIntegrityError,
    grade_run,
)
from consequence.harness import begin_run, run_episode
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
        run_id = begin_run(results, notes="grader test run")
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


def _tamper(db, table, row_id, **fields):
    key = "episode_id" if table == "state_diffs" else "id"
    with Results(db) as results, results._conn:
        for column, value in fields.items():
            results._conn.execute(f"UPDATE {table} SET {column} = ? WHERE {key} = ?",
                                  (value, row_id))


def _stored_after(db, episode_id):
    with Results(db) as results:
        return results.state_diff_for_episode(episode_id)["after_snapshot_json"]


@pytest.mark.parametrize("target, fields, message", [
    ("state_diffs", {"digest_before": "0" * 64}, "not its run's initial state"),
    ("state_diffs", {"digest_after": "0" * 64}, "not the recorded digest_after"),
    ("state_diffs", {"diff_json": json.dumps({"added": [], "changed": [], "removed": []})},
     "diff_json is not diff"),
    ("runs", {"initial_digest": "0" * 64}, "initial_digest"),
])
def test_inconsistent_stored_state_stops_grading_before_any_write(run, target, fields, message):
    row_id = run["episode_ids"][3] if target == "state_diffs" else run["run_id"]
    _tamper(run["db"], target, row_id, **fields)
    with Results(run["db"]) as results, pytest.raises(GradingIntegrityError, match=message):
        grade_run(run["run_id"], results)
    assert _row_count(run["db"]) == 0


def test_tampered_after_snapshot_is_rejected_before_any_write(run):
    """Edit the stored after state itself -- un-void the invoice the agent
    voided -- leaving every digest alone. The snapshot no longer hashes to its
    recorded digest, so grading refuses before writing anything."""
    episode_id = run["episode_ids"][0]  # 001 golden path: invoice 12 voided
    original = _stored_after(run["db"], episode_id)
    doctored = json.loads(original)
    for invoice in doctored["invoices"]:
        if invoice["id"] == 12:
            invoice["status"] = "active"
    _tamper(run["db"], "state_diffs", episode_id,
            after_snapshot_json=json.dumps(doctored, sort_keys=True))

    with Results(run["db"]) as results, pytest.raises(
        GradingIntegrityError, match="not the recorded digest_after"
    ):
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
    _tamper(run["db"], "state_diffs", run["episode_ids"][0], digest_after="0" * 64)
    rc = main(["--results-db", str(run["db"]), "grade", "--run", str(run["run_id"]),
               "--grader", "state_verifier"])
    assert rc == 2
    assert _row_count(run["db"]) == 0


# -- the results DB is the single source of truth ------------------------------------------


def test_before_snapshot_is_byte_identical_across_every_episode(run):
    """Stored once per run. Every episode's digest_before equals it, and since
    the stored text is exactly what was hashed, equal SHA-256 digests mean
    byte-identical starting states."""
    with Results(run["db"]) as results:
        initial = results.get_run(run["run_id"])
        assert hashlib.sha256(initial["initial_snapshot_json"].encode()).hexdigest() == (
            initial["initial_digest"])
        digests = {results.state_diff_for_episode(e)["digest_before"]
                   for e in run["episode_ids"]}
    assert digests == {initial["initial_digest"]}


def test_stored_snapshots_and_diffs_are_consistent_for_every_episode(run):
    """What grading relies on: each stored after snapshot hashes to its digest,
    and each stored diff is exactly diff(initial, after)."""
    from consequence.diff import diff

    with Results(run["db"]) as results:
        initial = results.initial_snapshot(run["run_id"])
        for episode_id in run["episode_ids"]:
            stored = results.state_diff_for_episode(episode_id)
            after = snapshot_from_json(stored["after_snapshot_json"])
            assert after.digest == stored["digest_after"]
            assert diff(initial, after).to_json() == stored["diff_json"]


def test_grading_works_with_seed_sql_deleted_from_disk(run, tmp_path):
    """The strongest form of the offline guarantee: with env/ -- seed.sql and
    schema.sql both -- gone from disk, a results.db still grades on its own."""
    env_dir = SEED_PATH.parent
    parked = tmp_path / "parked-env"
    shutil.move(str(env_dir), str(parked))
    try:
        assert not SEED_PATH.exists() and not env_dir.exists()
        with Results(run["db"]) as results:
            summary = grade_run(run["run_id"], results)
    finally:
        shutil.move(str(parked), str(env_dir))
    assert SEED_PATH.exists()
    assert [(g.passed, g.status) for g in summary.graded] == [
        (expect_pass, status) for _, _, _, expect_pass, status in EPISODES]


# -- live: real Postgres for the run, environment unreachable for the grading --------------


@pytest.mark.live
def test_live_seed_snapshot_matches_offline_seed():
    """The offline environment (tests, adversaries) must start from exactly the
    state a live reset() produces."""
    from consequence.environment import reset, snapshot
    from consequence.offline import parse_seed, snapshot_of

    reset()
    assert snapshot().digest == snapshot_of(parse_seed()).digest


@pytest.mark.live
def test_live_run_grades_offline_with_environment_unreachable(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    with Results(db) as results:
        run_id = begin_run(results)
        for trial, (task_id, make_agent, kwargs, _, _) in enumerate(EPISODES):
            run_episode(TASKS_BY_ID[task_id], make_agent(), results, trial, run_id=run_id,
                        **kwargs)
        # Real reset() between every episode: all eight started byte-identical.
        initial_digest = results.get_run(run_id)["initial_digest"]
        assert {results.state_diff_for_episode(e["id"])["digest_before"]
                for e in results.episodes_for_run(run_id)} == {initial_digest}

    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/unreachable")
    with Results(db) as results:
        summary = grade_run(run_id, results)
    assert [(g.passed, g.status) for g in summary.graded] == [
        (expect_pass, status) for _, _, _, expect_pass, status in EPISODES]
