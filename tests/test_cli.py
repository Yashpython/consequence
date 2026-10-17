"""CLI tests: each command invoked directly through main(), with a mock
provider and a temp SQLite results DB. Commands that only touch the
results store (run --dry-run, grade, report, status) need no live
Postgres. `run` without --dry-run and `env` do -- see the @pytest.mark.live
tests at the bottom.
"""

from __future__ import annotations

import json

import pytest
from helpers import (
    DEFAULT_SAMPLING_JUDGES,
    TEST_JUDGES,
    record_episode_with_state,
    seed_run,
    stub_judge_factory,
    verdict,
)

from consequence.cli import main
from consequence.diff import diff
from consequence.providers.base import TurnResult
from consequence.providers.mock import MockProvider
from consequence.results import Results
from consequence.tasks import TASKS


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
        ["--results-db", db_path, "run", "--task", "001_void_duplicate",
         "--model", "mock-1", "--dry-run"],
        provider_factory=_counting_factory(calls),
    )
    assert rc == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "001_void_duplicate" in out
    assert "mock-1" in out
    assert "estimated experiment cost" in out
    assert "estimated judging cost" in out and "estimated grand total" in out

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
    # 25 tasks x 2 models x 2 trials = 100 planned episodes
    assert out.count("would run:") == 100


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
        run_id = seed_run(results, notes="seeded for grading test")
        changed_ep = record_episode_with_state(
            results, run_id, [("void_invoice", {"invoice_id": 2, "reason": "test"})],
            task_id="t1", input_tokens=10, output_tokens=5, cost_usd=0.01, latency_ms=50,
            turn_count=2, retry_count=0,
        )
        noop_ep = record_episode_with_state(
            results, run_id, [], task_id="t2", input_tokens=1, output_tokens=1,
            cost_usd=0.001, latency_ms=10, turn_count=1, retry_count=0,
        )
    return run_id, changed_ep, noop_ep


def _changed_state_passes(user_message, vote):
    """Stub judge: passes an episode whose transcript shows a write."""
    wrote = "tool: void_invoice" in user_message
    return verdict(wrote, "completed" if wrote else "silent_inaction")


@pytest.fixture
def judges(monkeypatch):
    """A configured judge registry. The real one has no context windows set,
    so it refuses to run, by design (see the test below)."""
    monkeypatch.setattr("consequence.cli.load_judge_registry", lambda: TEST_JUDGES)
    return TEST_JUDGES


def _grade(db_path, run_id, *extra, factory=None):
    return main(["--results-db", db_path, "grade", "--run", str(run_id), *extra],
                judge_provider_factory=factory or stub_judge_factory(_changed_state_passes))


def _rows(db_path):
    with Results(db_path) as results:
        return results._conn.execute("SELECT COUNT(*) FROM gradings").fetchone()[0]


def test_a_judge_without_a_context_window_refuses_to_run(db_path, capsys, monkeypatch):
    """Without context_window_tokens the overflow check can't work, so
    grading refuses and writes nothing. (models.yaml's judges now have theirs,
    cited from vendor documentation; see test_providers.)"""
    from dataclasses import replace
    monkeypatch.setattr("consequence.cli.load_judge_registry", lambda: {
        TEST_JUDGES["judge-test"].id: replace(TEST_JUDGES["judge-test"],
                                              context_window_tokens=None)})
    run_id, _, _ = _seed_two_episodes(db_path)
    assert _grade(db_path, run_id, "--grader", "llm_judge") == 2
    assert "context_window_tokens" in capsys.readouterr().err
    assert _rows(db_path) == 0


def test_grade_all_fails_closed_when_the_judge_cannot_run(db_path, capsys, judges):
    """--grader all must not run the state grader and then discover the
    judge can't run: that would pass off a partial grading as complete."""
    run_id, _, _ = _seed_two_episodes(db_path)

    def broken_factory(model_id, registry=None, temperature=None):
        raise ImportError("No module named 'anthropic'")

    assert _grade(db_path, run_id, "--grader", "all", factory=broken_factory) == 2
    err = capsys.readouterr().err
    assert "the judge can't run, so nothing was graded" in err and "anthropic" in err
    assert _rows(db_path) == 0  # fail closed: not even the state grader ran


def test_grade_all_runs_both_graders(db_path, judges, monkeypatch):
    run_id, changed_ep, _ = _seed_two_episodes(db_path)
    ran = []
    monkeypatch.setattr("consequence.cli._grade_state",
                        lambda args, results: ran.append("state_verifier") or 0)
    assert _grade(db_path, run_id, "--grader", "all") == 0
    assert ran == ["state_verifier"]
    with Results(db_path) as results:
        [row] = results.gradings_for_episode(changed_ep)
    assert (row["grader"], row["judge_model"]) == ("llm_judge", "judge-test")


def test_grade_records_one_judge_row_per_episode(db_path, capsys, judges):
    run_id, changed_ep, noop_ep = _seed_two_episodes(db_path)
    assert _grade(db_path, run_id, "--grader", "llm_judge") == 0
    out = capsys.readouterr().out
    assert "totals: llm_judge [judge-test, temperature 0.0] unknown 1/2 passed" in out
    assert "judging cost (not part of the experiment's cost)" in out
    with Results(db_path) as results:
        [g1] = results.gradings_for_episode(changed_ep)
        [g2] = results.gradings_for_episode(noop_ep)
    assert (g1["passed"], g1["behaviour"]) == (1, "completed")
    assert (g2["passed"], g2["behaviour"]) == (0, "silent_inaction")


def test_grade_regrade_replaces_rather_than_duplicates(db_path, judges):
    run_id, changed_ep, _ = _seed_two_episodes(db_path)
    _grade(db_path, run_id, "--grader", "llm_judge")
    _grade(db_path, run_id, "--grader", "llm_judge", "--regrade")
    with Results(db_path) as results:
        assert len(results.gradings_for_episode(changed_ep)) == 1


def test_grade_unknown_run_is_a_clean_error(db_path, judges):
    assert _grade(db_path, 999, "--grader", "llm_judge") == 2


@pytest.mark.parametrize("flags, message", [
    (["--grader", "state_verifier", "--judge-model", "judge-test"], "apply only to"),
    (["--grader", "llm_judge", "--judge-model", "nope"], "no judge 'nope'"),
    (["--grader", "llm_judge", "--judge-subset", "1"], "for second judges only"),
    (["--grader", "llm_judge", "--judge-stability-subset", "1", "--judge-temperature", "0"],
     "always runs at temperature"),
    (["--grader", "llm_judge", "--judge-model", "judge-other-family", "--judge-subset", "9"],
     "cannot sample 9"),
])
def test_judge_flags_are_validated_before_anything_is_written(db_path, capsys, judges, flags,
                                                              message):
    run_id, _, _ = _seed_two_episodes(db_path)
    assert _grade(db_path, run_id, *flags) == 2
    assert message in capsys.readouterr().err
    assert _rows(db_path) == 0


def test_judge_temperature_reaches_the_judge_provider(db_path, judges):
    run_id, _, _ = _seed_two_episodes(db_path)
    made = []
    _grade(db_path, run_id, "--grader", "llm_judge", "--judge-temperature", "0.3",
           factory=stub_judge_factory(_changed_state_passes, made))
    assert [j.temperature for j in made] == [0.3]
    with Results(db_path) as results:
        temps = {r[0] for r in results._conn.execute("SELECT judge_temperature FROM gradings")}
    assert temps == {0.3}


# -- dry-run judging estimate ---------------------------------------------------------


def _estimate(**kwargs):
    from consequence.cli import (
        DRY_RUN_ESTIMATED_INPUT_TOKENS,
        DRY_RUN_ESTIMATED_JUDGE_OUTPUT_TOKENS,
        DRY_RUN_ESTIMATED_OUTPUT_TOKENS,
        estimate_costs,
    )
    from consequence.graders.judge import build_messages, estimate_tokens
    from consequence.harness import Task
    from consequence.providers.registry import ModelSpec

    models = {"m": ModelSpec(id="m", provider="stub", api_model="m",
                             input_price_per_million=1.0, output_price_per_million=2.0)}
    combos = [(Task(task_id=f"t{i}", instructions="x"), "m", 0) for i in range(10)]
    judge_in = DRY_RUN_ESTIMATED_INPUT_TOKENS + estimate_tokens(build_messages(()))
    per_call = {j: (judge_in * s.input_price_per_million
                    + DRY_RUN_ESTIMATED_JUDGE_OUTPUT_TOKENS * s.output_price_per_million) / 1e6
                for j, s in TEST_JUDGES.items()}
    episode = (DRY_RUN_ESTIMATED_INPUT_TOKENS * 1.0 + DRY_RUN_ESTIMATED_OUTPUT_TOKENS * 2.0) / 1e6
    return estimate_costs(combos, models, TEST_JUDGES, **kwargs), per_call, episode


def test_dry_run_estimates_experiment_judging_and_grand_totals():
    est, per_call, episode = _estimate(judge_votes=1, api_agreement_subset=4,
                                       stability_subset=3, cross_family_subset=2)
    calls = {j["label"].split(" ")[0]: (j["episodes"], j["votes"], j["calls"])
             for j in est["judging"]}
    # The primary judge: one verdict for EVERY episode (parity), here one vote each.
    assert calls["primary"] == (10, 1, 10)
    assert calls["API-agreement"] == (4, 3, 12)
    assert calls["stability"] == (3, 3, 9)
    assert calls["cross-family"] == (2, 1, 2)
    assert est["experiment_total"] == pytest.approx(10 * episode)
    primary, second = per_call["judge-test"], per_call["judge-other-family"]
    assert est["judging_total"] == pytest.approx((10 + 12 + 9) * primary + 2 * second)
    assert est["grand_total"] == pytest.approx(est["experiment_total"] + est["judging_total"])


def test_dry_run_subsets_cannot_exceed_the_episodes():
    est, _, _ = _estimate(stability_subset=500, cross_family_subset=500)
    assert {j["episodes"] for j in est["judging"] if "subset" in j["label"]
            or "cross" in j["label"]} <= {0, 10}


@pytest.mark.parametrize("flags, message", [
    (["--judge-votes", "2"], "must be odd"),
    (["--api-agreement-subset", "5"], "needs --judge-votes 1"),
])
def test_dry_run_judging_options_are_validated(db_path, capsys, flags, message):
    rc = main(["--results-db", db_path, "run", "--task", "001_void_duplicate", "--model",
               "mock-1", "--dry-run", *flags], provider_factory=_counting_factory([]))
    assert rc == 2 and message in capsys.readouterr().err


def test_dry_run_gives_a_provider_default_primary_judge_three_votes():
    """A judge that rejects a temperature always takes three votes (they are
    its stability measurement) and has no temperature-0 or 0.7 subsets."""
    from consequence.cli import DRY_RUN_OUTPUT_SENSITIVITY_TOKENS, estimate_costs
    from consequence.harness import Task
    from consequence.providers.registry import ModelSpec

    models = {"m": ModelSpec(id="m", provider="stub", api_model="m",
                             input_price_per_million=1.0, output_price_per_million=2.0)}
    combos = [(Task(task_id=f"t{i}", instructions="x"), "m", 0) for i in range(10)]
    est = estimate_costs(combos, models, DEFAULT_SAMPLING_JUDGES, judge_votes=1,
                         cross_family_subset=2)
    lines = {j["label"]: j for j in est["judging"]}
    primary = lines["primary judge-no-temperature (provider default sampling)"]
    assert (primary["episodes"], primary["votes"], primary["calls"]) == (10, 3, 30)
    assert lines["cross-family judge-other-family (temperature 0)"]["votes"] == 1
    assert not any("subset" in label for label in lines)  # no 0 or 0.7 subsets for it
    # What thinking would add, stated per extra output tokens rather than guessed.
    assert primary["per_extra_output"] == pytest.approx(
        30 * DRY_RUN_OUTPUT_SENSITIVITY_TOKENS * 20.0 / 1e6)


@pytest.mark.parametrize("flags", [["--judge-votes", "1"],
                                   ["--judge-stability-subset", "5"],
                                   ["--judge-votes", "1", "--api-agreement-subset", "5"]])
def test_dry_run_refuses_temperature_options_for_a_provider_default_primary(
        db_path, capsys, monkeypatch, flags):
    monkeypatch.setattr("consequence.cli.load_judge_registry", lambda: DEFAULT_SAMPLING_JUDGES)
    rc = main(["--results-db", db_path, "run", "--task", "001_void_duplicate", "--model",
               "mock-1", "--dry-run", *flags], provider_factory=_counting_factory([]))
    assert rc == 2 and "provider's default sampling" in capsys.readouterr().err


def test_judging_options_on_run_need_dry_run(db_path, capsys):
    rc = main(["--results-db", db_path, "run", "--task", "001_void_duplicate", "--model",
               "mock-1", "--cross-family-subset", "5"], provider_factory=_counting_factory([]))
    assert rc == 2 and "only shape the --dry-run estimate" in capsys.readouterr().err


# -- report / status ---------------------------------------------------


def test_report_json_reflects_persisted_rows(db_path, capsys, judges):
    run_id, _changed_ep, _noop_ep = _seed_two_episodes(db_path)
    _grade(db_path, run_id, "--grader", "llm_judge")
    capsys.readouterr()  # discard grade output

    rc = main(["--results-db", db_path, "report", "--run", str(run_id), "--format", "json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run"]["episodes"] == 2
    model = report["models"]["mock-1"]  # per model, never pooled
    # t1/t2 aren't catalog tasks, so they report under "unknown".
    assert model["episodes"] == {"unknown": {"completed": 2, "error": 0, "timeout": 0,
                                             "max_turns": 0}}
    judged = model["judges"]["judge-test"]["pass_rate"]["unknown"]  # per judge
    assert (judged["successes"], judged["n"]) == (1, 2)
    assert judged["ci_low"] is not None  # every rate carries its interval
    # Experiment cost is the episodes' own; judging is reported apart.
    assert model["cost_per_solved_task"]["unknown"]["total_cost_usd"] == pytest.approx(0.011)
    assert report["judging_cost"]["judge-test"]["cost_usd"] > 0


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
        ["--results-db", db_path, "run", "--task", "005_suspended_supplier_hold",
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
        assert episodes[0]["task_id"] == "005_suspended_supplier_hold"
        assert episodes[0]["status"] == "completed"


@pytest.mark.live
def test_run_is_resumable_and_skips_already_recorded_combos(db_path, capsys):
    call_count = 0

    def counting_mock_factory(model_id, **kwargs):
        nonlocal call_count
        call_count += 1
        return MockProvider(model_id=model_id, script=[TurnResult(text="done", tool_calls=())])

    rc = main(
        ["--results-db", db_path, "run", "--task", "005_suspended_supplier_hold",
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
        ["--results-db", db_path, "run", "--task", "005_suspended_supplier_hold",
         "--model", "mock-1", "--trials", "2", "--run-id", str(run_id)],
        provider_factory=counting_mock_factory,
    )
    assert rc == 0
    assert call_count == 2  # unchanged
    out = capsys.readouterr().out
    assert out.count("skip") == 2

    with Results(db_path) as results:
        assert len(results.episodes_for_run(run_id)) == 2  # not duplicated


# -- do-nothing agent: trap and normal pass rates are never blended ----------------


def test_do_nothing_agent_is_reported_split_not_blended(db_path, offline_env, capsys):
    """Every trap task passes on an empty diff, so an agent that does nothing
    passes all 6 traps and none of the 19 normal tasks. A blended headline
    would call that 6/25 (24%) for doing nothing; the report must never show one."""
    before, after = offline_env.snapshots()
    nothing = diff(before, after)
    with Results(db_path) as results:
        run_id = seed_run(results, notes="do-nothing agent")
        for task in TASKS:
            episode_id = results.record_episode(
                run_id=run_id, task_id=task.id, model_id="do-nothing", trial_index=0,
                status="completed",
            )
            verdict = task.verify(before, after, nothing)
            results.record_grading(episode_id, "state_verifier", passed=verdict.passed)

    assert main(["--results-db", db_path, "report", "--run", str(run_id), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    state = report["models"]["do-nothing"]["state_verifier"]
    assert set(state) == {"normal", "trap"}  # no combined row
    assert (state["normal"]["successes"], state["normal"]["n"]) == (0, 19)
    assert (state["trap"]["successes"], state["trap"]["n"]) == (6, 6)

    assert main(["--results-db", db_path, "report", "--run", str(run_id)]) == 0
    md = capsys.readouterr().out
    assert "| normal | 0.0% |" in md and "| 0 of 19 |" in md
    assert "| trap | 100.0% |" in md and "| 6 of 6 |" in md
    for blended in ("6/25", "/25", "of 25", "24.0%"):  # no figure over all 25 tasks
        assert blended not in md


# -- per-task turn budget ---------------------------------------------------------


def test_run_passes_the_tasks_own_turn_budget_to_the_harness(db_path, monkeypatch):
    """023 needs ~21 single-call turns; the CLI must hand the harness its
    max_turns, not the default. begin_run and run_episode are stubbed, so no
    DB is needed."""
    from types import SimpleNamespace

    from consequence import cli
    from consequence.harness import DEFAULT_MAX_TURNS
    from consequence.tasks import TASKS_BY_ID

    seen = {}

    def fake_run_episode(task, provider, results, trial_index, *, run_id, max_turns):
        seen[task.id] = max_turns
        return SimpleNamespace(turn_count=0, status="completed", latency_ms=0, cost_usd=None)

    monkeypatch.setattr(cli, "begin_run", seed_run)  # the real one resets Postgres
    monkeypatch.setattr(cli, "run_episode", fake_run_episode)
    for task_id in ("023_volume", "001_void_duplicate"):
        assert main(["--results-db", db_path, "run", "--task", task_id, "--model", "mock-1"],
                    provider_factory=_mock_factory_no_tools) == 0

    assert seen == {"023_volume": 40, "001_void_duplicate": DEFAULT_MAX_TURNS}
    assert TASKS_BY_ID["023_volume"].max_turns == 40
