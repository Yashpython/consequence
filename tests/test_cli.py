"""CLI tests: each command invoked directly through main(), with a mock
provider and a temp SQLite results DB. Commands that only touch the
results store (run --dry-run, grade, report, status) need no live
Postgres. `run` without --dry-run and `env` do -- see the @pytest.mark.live
tests at the bottom.
"""

from __future__ import annotations

import json

import pytest

from consequence.cli import main
from consequence.providers.base import TurnResult
from consequence.providers.mock import MockProvider
from consequence.results import Results


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "results.db")


def _mock_factory_no_tools(model_id, **kwargs):
    """A provider that finishes immediately with no tool calls."""
    return MockProvider(model_id=model_id, script=[TurnResult(text="done", tool_calls=())])


def _counting_factory(calls: list):
    def factory(model_id, **kwargs):
        calls.append(model_id)
        raise AssertionError("provider_factory must not be called during --dry-run")

    return factory


# -- run --dry-run: the safety gate, must make zero provider calls --------


def test_dry_run_single_task_prints_plan_and_makes_no_calls(db_path, capsys):
    calls: list = []
    rc = main(
        ["--results-db", db_path, "run", "--task", "flag-duplicate-invoice",
         "--model", "mock-1", "--dry-run"],
        provider_factory=_counting_factory(calls),
    )
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "flag-duplicate-invoice" in out
    assert "mock-1" in out
    assert "estimated total cost" in out

    # nothing was persisted by a dry run
    with Results(db_path) as results:
        assert results.list_runs() == []


def test_dry_run_all_tasks_multiple_models_and_trials(db_path, capsys):
    calls: list = []
    rc = main(
        ["--results-db", db_path, "run", "--all", "--models", "mock-1,mock-2",
         "--trials", "2", "--dry-run"],
        provider_factory=_counting_factory(calls),
    )
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    # 3 tasks x 2 models x 2 trials = 12 planned episodes
    assert out.count("would run:") == 12


def test_run_requires_task_and_model_or_all(db_path):
    rc = main(["--results-db", db_path, "run"])
    assert rc == 2


def test_run_unknown_task_is_a_clean_error(db_path):
    rc = main(["--results-db", db_path, "run", "--task", "no-such-task",
               "--model", "mock-1", "--dry-run"])
    assert rc == 2


# -- grade -----------------------------------------------------------------


def _seed_two_episodes(db_path: str) -> tuple[int, int, int]:
    with Results(db_path) as results:
        run_id = results.start_run(notes="seeded for grading test")
        changed_ep = results.record_episode(
            run_id=run_id, task_id="t1", model_id="mock-1", trial_index=0,
            status="completed", input_tokens=10, output_tokens=5,
            cost_usd=0.01, latency_ms=50, turn_count=2, retry_count=0,
        )
        results.record_state_diff(
            changed_ep,
            diff_json={"added": [], "removed": [],
                       "changed": [["invoices", 2, {"status": ["active", "void"]}]]},
            digest_before="a", digest_after="b",
        )
        noop_ep = results.record_episode(
            run_id=run_id, task_id="t2", model_id="mock-1", trial_index=0,
            status="completed", input_tokens=1, output_tokens=1,
            cost_usd=0.001, latency_ms=10, turn_count=1, retry_count=0,
        )
        results.record_state_diff(
            noop_ep, diff_json={"added": [], "removed": [], "changed": []},
            digest_before="a", digest_after="a",
        )
    return run_id, changed_ep, noop_ep


def _trivial_verifier(episode, diff_json):
    passed = bool(diff_json["added"] or diff_json["removed"] or diff_json["changed"])
    return passed, {"reason": "state changed" if passed else "no-op"}


def test_grade_unimplemented_grader_is_a_clean_error(db_path):
    run_id, _, _ = _seed_two_episodes(db_path)
    rc = main(["--results-db", db_path, "grade", "--run", str(run_id), "--grader", "state_verifier"])
    assert rc == 2

    with Results(db_path) as results:
        assert results.episodes_needing_grading(run_id=run_id) != []


def test_grade_records_one_row_per_episode(db_path, capsys):
    run_id, changed_ep, noop_ep = _seed_two_episodes(db_path)

    rc = main(
        ["--results-db", db_path, "grade", "--run", str(run_id), "--grader", "state_verifier"],
        graders={"state_verifier": _trivial_verifier},
    )
    assert rc == 0
    assert "totals: graded=2" in capsys.readouterr().out

    with Results(db_path) as results:
        [g1] = results.gradings_for_episode(changed_ep)
        [g2] = results.gradings_for_episode(noop_ep)
    assert g1["passed"] == 1
    assert g2["passed"] == 0


def test_grade_regrade_replaces_rather_than_duplicates(db_path):
    run_id, changed_ep, _ = _seed_two_episodes(db_path)
    graders = {"state_verifier": _trivial_verifier}

    main(["--results-db", db_path, "grade", "--run", str(run_id), "--grader", "state_verifier"],
         graders=graders)
    main(["--results-db", db_path, "grade", "--run", str(run_id), "--grader", "state_verifier",
          "--regrade"], graders=graders)

    with Results(db_path) as results:
        assert len(results.gradings_for_episode(changed_ep)) == 1


def test_grade_unknown_run_is_a_clean_error(db_path):
    rc = main(["--results-db", db_path, "grade", "--run", "999", "--grader", "state_verifier"],
              graders={"state_verifier": _trivial_verifier})
    assert rc == 2


# -- report / status ---------------------------------------------------


def test_report_json_reflects_persisted_rows(db_path, capsys):
    run_id, _changed_ep, _noop_ep = _seed_two_episodes(db_path)
    main(["--results-db", db_path, "grade", "--run", str(run_id), "--grader", "state_verifier"],
         graders={"state_verifier": _trivial_verifier})
    capsys.readouterr()  # discard grade output

    rc = main(["--results-db", db_path, "report", "--run", str(run_id), "--format", "json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["episode_count"] == 2
    assert report["by_status"] == {"completed": 2}
    assert report["grading"]["state_verifier"] == {"passed": 1, "failed": 1}


def test_report_unknown_run_is_a_clean_error(db_path):
    rc = main(["--results-db", db_path, "report", "--run", "999"])
    assert rc == 2


def test_status_with_no_runs(db_path, capsys):
    rc = main(["--results-db", db_path, "status"])
    assert rc == 0
    assert "no runs yet" in capsys.readouterr().out


def test_status_reports_ungraded_count(db_path, capsys):
    run_id, _, _ = _seed_two_episodes(db_path)
    rc = main(["--results-db", db_path, "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"run {run_id}" in out
    assert "ungraded=2" in out


# -- live: env commands and a real (mocked-provider) run ------------------


@pytest.mark.live
def test_env_reset_and_snapshot(capsys):
    rc = main(["env", "reset"])
    assert rc == 0
    assert "reset" in capsys.readouterr().out

    rc = main(["env", "snapshot", "--digest-only"])
    assert rc == 0
    digest = capsys.readouterr().out.strip()
    assert len(digest) == 64  # sha256 hex


@pytest.mark.live
def test_run_single_task_records_an_episode(db_path, capsys):
    rc = main(
        ["--results-db", db_path, "run", "--task", "approve-clean-draft-invoice",
         "--model", "mock-1"],
        provider_factory=_mock_factory_no_tools,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "status=completed" in out
    assert "totals: episodes=1" in out

    with Results(db_path) as results:
        runs = results.list_runs()
        assert len(runs) == 1
        episodes = results.episodes_for_run(runs[0]["id"])
        assert len(episodes) == 1
        assert episodes[0]["task_id"] == "approve-clean-draft-invoice"
        assert episodes[0]["status"] == "completed"


@pytest.mark.live
def test_run_is_resumable_and_skips_already_recorded_combos(db_path, capsys):
    call_count = 0

    def counting_mock_factory(model_id, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockProvider(model_id=model_id, script=[TurnResult(text="done", tool_calls=())])

    rc = main(
        ["--results-db", db_path, "run", "--task", "approve-clean-draft-invoice",
         "--model", "mock-1", "--trials", "2"],
        provider_factory=counting_mock_factory,
    )
    assert rc == 0
    assert call_count == 2
    with Results(db_path) as results:
        run_id = results.list_runs()[0]["id"]
    capsys.readouterr()

    # Re-run against the same run id: both trials already exist, so the
    # provider must not be called again.
    rc = main(
        ["--results-db", db_path, "run", "--task", "approve-clean-draft-invoice",
         "--model", "mock-1", "--trials", "2", "--run-id", str(run_id)],
        provider_factory=counting_mock_factory,
    )
    assert rc == 0
    assert call_count == 2  # unchanged
    out = capsys.readouterr().out
    assert out.count("skip") == 2

    with Results(db_path) as results:
        assert len(results.episodes_for_run(run_id)) == 2  # not duplicated
