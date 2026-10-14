"""The command-line interface: the single entry point for everything from
here on -- resetting the environment, running episodes, grading them, and
reporting on a run.

Nothing here computes a metric it doesn't also persist: `run` and `grade`
write to the results store before they print a summary of what they wrote;
`report` and `status` only ever read back what's already there. The one
exception is `run --dry-run`'s cost preview, which is explicitly a planning
estimate (see DRY_RUN_ESTIMATED_*_TOKENS below) -- it is labeled as such
and is never persisted.

Grading: `grade --grader state_verifier` runs consequence.graders.state, which
scores every episode with its task's verifier and the collateral check from
stored data alone -- no environment database, no model calls. `grade --grader
llm_judge` runs consequence.graders.judge, which reads only each episode's
transcript and calls the judge model (models.yaml `judges:`); its cost is
recorded on the gradings rows, never in the experiment's cost.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from typing import Any

from consequence.config import load_config
from consequence.environment import reset, snapshot
from consequence.graders import judge
from consequence.graders.state import GRADING_ERROR, GradingIntegrityError, grade_run
from consequence.harness import DEFAULT_MAX_TURNS, Task, begin_run, run_episode
from consequence.providers.base import Provider
from consequence.providers.registry import (
    compute_cost,
    load_judge_registry,
    load_model_registry,
    make_provider,
    primary_judge,
)
from consequence.results import DEFAULT_DB_PATH, Results
from consequence.tasks import TASKS, get_task

# A rough, fixed per-episode token estimate used ONLY for the `run --dry-run`
# cost preview. This is a planning estimate, not a measurement: it is never
# written to the results store and is always printed labeled as an estimate.
DRY_RUN_ESTIMATED_INPUT_TOKENS = 8_000
DRY_RUN_ESTIMATED_OUTPUT_TOKENS = 1_500

ProviderFactory = Callable[..., Provider]

ALL_GRADER_NAMES = ("state_verifier", "llm_judge")


class CLIError(Exception):
    """A user-facing error: bad arguments, unknown task/model/run/grader.

    Caught once in main() and printed to stderr with a non-zero exit code --
    never a raw traceback for something the user can just fix and rerun.
    """


# -- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="consequence")
    parser.add_argument(
        "--results-db",
        default=None,
        help="Path to the results SQLite DB (default: RESULTS_DB_PATH or results/results.db)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    env_parser = sub.add_parser("env", help="Manage the environment database")
    env_sub = env_parser.add_subparsers(dest="env_command", required=True)
    env_sub.add_parser("reset", help="Drop, recreate, and reseed the environment DB")
    snapshot_parser = env_sub.add_parser("snapshot", help="Print a snapshot of the environment DB")
    snapshot_parser.add_argument("--digest-only", action="store_true")

    run_parser = sub.add_parser("run", help="Run one or more episodes")
    run_parser.add_argument("--task", default=None, help="A single task id")
    run_parser.add_argument("--model", default=None, help="A single model id (from models.yaml)")
    run_parser.add_argument("--all", action="store_true", help="Run every task")
    run_parser.add_argument(
        "--models", default=None, help="Comma-separated model ids to use with --all (default: all)"
    )
    run_parser.add_argument("--trials", type=int, default=1)
    run_parser.add_argument("--run-notes", default=None)
    run_parser.add_argument(
        "--run-id", type=int, default=None, help="Resume an existing run instead of starting a new one"
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and an estimated cost, then exit without calling any API",
    )

    grade_parser = sub.add_parser("grade", help="Grade the episodes in a run")
    grade_parser.add_argument("--run", type=int, required=True, dest="run_id")
    grade_parser.add_argument(
        "--grader", required=True, choices=[*ALL_GRADER_NAMES, "all"]
    )
    grade_parser.add_argument(
        "--regrade", action="store_true", help="Regrade every episode, not just ungraded ones"
    )
    grade_parser.add_argument(
        "--judge-model", default=None,
        help="Judge id from models.yaml `judges:` (default: the primary judge)",
    )
    grade_parser.add_argument(
        "--judge-temperature", type=float, default=None,
        help="Judge sampling temperature (default 0)",
    )
    grade_parser.add_argument(
        "--judge-subset", type=int, default=None, metavar="N",
        help="Judge only N randomly sampled episodes. Non-primary judges only: the primary "
             "judge must grade exactly the episodes the state verifier grades",
    )
    grade_parser.add_argument(
        "--judge-stability-subset", type=int, default=None, metavar="N",
        help=f"Re-judge N randomly sampled episodes at temperature "
             f"{judge.STABILITY_TEMPERATURE}, {judge.VOTES} times each, under grader "
             f"'{judge.STABILITY_GRADER}'",
    )
    grade_parser.add_argument(
        "--judge-sample-seed", type=int, default=0,
        help="Seed for --judge-subset / --judge-stability-subset sampling (default 0)",
    )

    report_parser = sub.add_parser("report", help="Report on a run")
    report_parser.add_argument("--run", type=int, required=True, dest="run_id")
    report_parser.add_argument("--format", choices=["md", "json"], default="md")
    report_parser.add_argument(
        "--pass-k", type=int, nargs="*", default=[], metavar="K",
        help="Also report pass^k for each K (all K trials of a task pass)",
    )

    sub.add_parser("status", help="What has been run, and what is still ungraded")

    return parser


# -- env ---------------------------------------------------------------


def cmd_env_reset(args: argparse.Namespace) -> int:
    reset()
    print("environment reset to seed state")
    return 0


def cmd_env_snapshot(args: argparse.Namespace) -> int:
    snap = snapshot()
    if args.digest_only:
        print(snap.digest)
    else:
        print(json.dumps({"digest": snap.digest, "structure": snap.structure}, indent=2, default=str))
    return 0


# -- run -----------------------------------------------------------------


def _resolve_combos(args: argparse.Namespace) -> list[tuple[Task, str, int]]:
    trials = args.trials
    if trials < 1:
        raise CLIError("--trials must be at least 1")

    if args.all:
        tasks = list(TASKS)
        if args.models:
            model_ids = [m.strip() for m in args.models.split(",") if m.strip()]
        else:
            model_ids = sorted(load_model_registry())
    else:
        if not args.task or not args.model:
            raise CLIError("run requires either --all, or both --task and --model")
        try:
            tasks = [get_task(args.task)]
        except KeyError as exc:
            raise CLIError(str(exc)) from exc
        model_ids = [args.model]

    return [(task, model_id, i) for task in tasks for model_id in model_ids for i in range(trials)]


def max_turns_for(task: Task) -> int:
    """The task's own turn budget if it declares one, else the harness default."""
    return getattr(task, "max_turns", None) or DEFAULT_MAX_TURNS


def _format_cost(cost_usd: float | None) -> str:
    return f"${cost_usd:.4f}" if cost_usd is not None else "unknown"


def _print_dry_run(combos: list[tuple[Task, str, int]], registry: Mapping[str, Any]) -> None:
    print(f"DRY RUN -- {len(combos)} episode(s) planned, no API calls will be made")
    for task, model_id, trial_index in combos:
        print(f"  would run: task={task.task_id} model={model_id} trial={trial_index}")

    total_cost = 0.0
    unpriced: list[str] = []
    for model_id in sorted({m for _, m, _ in combos}):
        count = sum(1 for _, m, _ in combos if m == model_id)
        per_episode = compute_cost(
            model_id, DRY_RUN_ESTIMATED_INPUT_TOKENS, DRY_RUN_ESTIMATED_OUTPUT_TOKENS, registry=registry
        )
        if per_episode is None:
            unpriced.append(model_id)
            print(f"  {model_id}: {count} episode(s), cost unknown (not in models.yaml)")
        else:
            subtotal = per_episode * count
            total_cost += subtotal
            print(f"  {model_id}: {count} episode(s) x ~{_format_cost(per_episode)} (est.) = ~{_format_cost(subtotal)}")

    print(
        f"estimated total cost: ~{_format_cost(total_cost)} (assumes ~"
        f"{DRY_RUN_ESTIMATED_INPUT_TOKENS} input / ~{DRY_RUN_ESTIMATED_OUTPUT_TOKENS} output "
        "tokens per episode -- a planning estimate, not a measurement)"
    )
    if unpriced:
        print(f"no price on file for: {sorted(set(unpriced))}")


def cmd_run(args: argparse.Namespace, provider_factory: ProviderFactory) -> int:
    combos = _resolve_combos(args)
    registry = load_model_registry()

    if args.dry_run:
        _print_dry_run(combos, registry)
        return 0

    results = Results(args.results_db)
    try:
        run_id = (args.run_id if args.run_id is not None
                  else begin_run(results, notes=args.run_notes))
        print(f"run_id={run_id}")

        already_done = {
            (e["task_id"], e["model_id"], e["trial_index"])
            for e in results.episodes_for_run(run_id)
        }

        totals = {"episodes": 0, "turns": 0, "cost_usd": 0.0, "by_status": {}}
        for task, model_id, trial_index in combos:
            key = (task.task_id, model_id, trial_index)
            if key in already_done:
                print(
                    f"skip  task={task.task_id} model={model_id} trial={trial_index} "
                    f"(already recorded for run {run_id})"
                )
                continue

            try:
                provider = provider_factory(model_id)
            except KeyError as exc:
                raise CLIError(str(exc)) from exc

            record = run_episode(
                task, provider, results, trial_index, run_id=run_id,
                max_turns=max_turns_for(task),
            )
            print(
                f"task={task.task_id} model={model_id} trial={trial_index} "
                f"turns={record.turn_count} status={record.status} "
                f"latency={record.latency_ms}ms cost={_format_cost(record.cost_usd)}"
            )
            totals["episodes"] += 1
            totals["turns"] += record.turn_count
            if record.cost_usd is not None:
                totals["cost_usd"] += record.cost_usd
            totals["by_status"][record.status] = totals["by_status"].get(record.status, 0) + 1

        results.finish_run(run_id)
        print(
            f"totals: episodes={totals['episodes']} turns={totals['turns']} "
            f"cost={_format_cost(totals['cost_usd'])} by_status={totals['by_status']}"
        )
        return 0
    finally:
        results.close()


# -- grade -----------------------------------------------------------------


def _grade_state(args: argparse.Namespace, results: Results) -> int:
    """Grade with the state grader and print the results. Returns the number
    of grading errors (episodes whose verifier raised)."""
    summary = grade_run(args.run_id, results, regrade=args.regrade)
    for g in summary.graded:
        if g.grading_status == GRADING_ERROR:
            print(f"GRADING ERROR episode={g.episode_id} task={g.task_id} ({g.reason})")
            continue
        collateral = ("clean" if g.collateral_clean
                      else g.collateral_severity if g.collateral_clean is False
                      else GRADING_ERROR)
        line = (f"graded episode={g.episode_id} task={g.task_id} status={g.status} "
                f"state_verifier={'pass' if g.passed else 'FAIL'} collateral={collateral}")
        if g.status != "completed":
            line += f" ({g.reason})"
        print(line)
    if summary.already_graded:
        print(f"skipped {len(summary.already_graded)} already-graded episode(s); "
              "pass --regrade to re-grade them")
    counts = summary.pass_counts()
    rates = " | ".join(f"{c} {counts[c]['passed']}/{counts[c]['total']} passed"
                       for c in CATEGORY_ORDER if c in counts)
    with_verdict = [g for g in summary.graded if g.collateral_clean is not None]
    severities = [g.collateral_severity or "clean" for g in with_verdict]
    print(f"totals: state_verifier {rates or 'none graded'}; collateral "
          + ", ".join(f"{severities.count(k)} {k}" for k in ("clean", "benign", "destructive")))
    satisfied = summary.incomplete_but_verifier_satisfied()
    if satisfied:
        print("did not complete, but the final state satisfied the verifier (counted as failed): "
              + " | ".join(f"{c} {satisfied[c]}" for c in CATEGORY_ORDER if c in satisfied))
    errors = summary.grading_errors()
    if errors:
        print(f"grading errors: {len(errors)} episode(s) have no verdict and are excluded "
              "from the totals above; fix the verifier and grade again to retry them")
    return len(errors)


def _judge_setup(args: argparse.Namespace, factory: ProviderFactory) -> dict[str, Any]:
    """Everything the judge needs, validated before any episode is touched.
    Any problem here is a CLIError, so a run is never half-judged by a judge
    that couldn't be set up."""
    registry = load_judge_registry()
    if not registry:
        raise CLIError("models.yaml has no judges: section")
    model = args.judge_model or primary_judge(registry).id
    spec = registry.get(model)
    if spec is None:
        raise CLIError(f"no judge {model!r} in models.yaml; judges: {sorted(registry)}")
    stability = args.judge_stability_subset is not None
    if stability and args.judge_temperature is not None:
        raise CLIError(f"--judge-stability-subset always runs at temperature "
                       f"{judge.STABILITY_TEMPERATURE}; drop --judge-temperature")
    if stability and args.judge_subset is not None:
        raise CLIError("use either --judge-subset or --judge-stability-subset, not both")
    if args.judge_subset is not None and spec.primary:
        raise CLIError(f"--judge-subset is for second judges only: the primary judge "
                       f"({spec.id}) grades every episode, so its verdicts and the state "
                       "verifier's cover the same episodes")
    temperature = (judge.STABILITY_TEMPERATURE if stability
                   else args.judge_temperature if args.judge_temperature is not None else 0.0)
    try:
        judge.check_config(spec)
        provider = factory(spec.id, registry=registry, temperature=temperature)
    except (judge.JudgeConfigError, KeyError, ImportError, ValueError) as exc:
        raise CLIError(f"the judge can't run, so nothing was graded: {exc}") from exc
    return {"spec": spec, "provider": provider, "temperature": temperature,
            "grader": judge.STABILITY_GRADER if stability else judge.JUDGE_GRADER,
            "subset": args.judge_stability_subset if stability else args.judge_subset}


def _grade_judge(args: argparse.Namespace, results: Results, setup: dict[str, Any]) -> int:
    """Judge the run and print the results. Returns the number of judge errors."""
    spec, grader = setup["spec"], setup["grader"]
    episode_ids, detail = None, {}
    if setup["subset"] is not None:
        try:
            episode_ids = judge.sample_episodes(results, args.run_id, setup["subset"],
                                                args.judge_sample_seed)
        except ValueError as exc:
            raise CLIError(str(exc)) from exc
        detail = {"sample_seed": args.judge_sample_seed, "sample_size": setup["subset"]}
    summary = judge.grade_run_with_judge(
        args.run_id, results, setup["provider"], spec, grader=grader,
        temperature=setup["temperature"], episode_ids=episode_ids, regrade=args.regrade,
        detail=detail,
    )
    for g in summary.graded:
        if g.status == judge.JUDGE_ERROR:
            print(f"JUDGE ERROR episode={g.episode_id} task={g.task_id} ({g.error_reason})")
        else:
            print(f"judged episode={g.episode_id} task={g.task_id} status={g.episode_status} "
                  f"{grader}={'pass' if g.passed else 'FAIL'} "
                  f"behaviour={g.behaviour or 'no_majority'} agreement={g.agreement:.2f}")
    if summary.already_graded:
        print(f"skipped {len(summary.already_graded)} already-judged episode(s); "
              "pass --regrade to re-judge them")
    counts = summary.pass_counts()
    rates = " | ".join(f"{c} {counts[c]['passed']}/{counts[c]['total']} passed"
                       for c in CATEGORY_ORDER if c in counts)
    print(f"totals: {grader} [{spec.id}, temperature {setup['temperature']}] "
          f"{rates or 'none judged'}")
    for category, behaviours in summary.behaviour_counts().items():
        print(f"behaviour ({category}): "
              + ", ".join(f"{b} {n}" for b, n in sorted(behaviours.items())))
    cost = summary.cost_usd()
    print("judging cost (not part of the experiment's cost): "
          + ("unknown" if cost is None else f"${cost:.4f}"))
    errors = summary.judge_errors()
    if errors:
        print("judge errors, excluded from the totals above: "
              + ", ".join(f"{reason} {n}" for reason, n in sorted(errors.items())))
    return sum(errors.values())


def cmd_grade(args: argparse.Namespace, judge_provider_factory: ProviderFactory) -> int:
    requested = set(ALL_GRADER_NAMES) if args.grader == "all" else {args.grader}
    judge_flags = (args.judge_model, args.judge_temperature, args.judge_subset,
                   args.judge_stability_subset)
    if "llm_judge" not in requested and any(f is not None for f in judge_flags):
        raise CLIError("--judge-* options apply only to --grader llm_judge or all")
    # Fail closed, like every other integrity path: if any requested grader
    # can't run, run none of them. Set the judge up before grading anything.
    setup = _judge_setup(args, judge_provider_factory) if "llm_judge" in requested else None

    results = Results(args.results_db)
    try:
        if results.get_run(args.run_id) is None:
            raise CLIError(f"no run {args.run_id}")
        errors = 0
        if "state_verifier" in requested:  # first: free, and stops on bad stored state
            try:
                errors += _grade_state(args, results)
            except GradingIntegrityError as exc:
                raise CLIError(f"grading stopped, nothing written: {exc}") from exc
        if setup is not None:
            try:
                errors += _grade_judge(args, results, setup)
            except judge.JudgeConfigError as exc:
                raise CLIError(str(exc)) from exc
        if errors:
            # Every other episode is graded and recorded; the exit code says
            # the grading isn't complete.
            print(f"error: {errors} grading(s) have no verdict "
                  "(grading_error / judge_error)", file=sys.stderr)
            return 1
        return 0
    finally:
        results.close()


# -- report / status -------------------------------------------------------


# Pass rates are NEVER blended across these categories. Every trap task's
# correct outcome is an unchanged database, so an agent that does nothing at
# all passes every trap and fails every normal task. One headline rate over
# all tasks would give that agent a nonzero score for doing nothing. See
# docs/reward-hacking-log.md.
CATEGORY_ORDER = ("normal", "trap", "unknown")


def cmd_report(args: argparse.Namespace) -> int:
    """Every figure comes from consequence.metrics via consequence.report;
    nothing is computed here."""
    from consequence import report
    from consequence.metrics import MetricsError, RunMetrics

    path = args.results_db or load_config().results_db_path or DEFAULT_DB_PATH
    try:
        with RunMetrics(path, args.run_id) as m:
            data = report.build_report(m, pass_k=tuple(args.pass_k))
    except MetricsError as exc:
        raise CLIError(str(exc)) from exc
    print(json.dumps(data, indent=2) if args.format == "json" else report.render_markdown(data))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    results = Results(args.results_db)
    try:
        runs = results.list_runs()
        if not runs:
            print("no runs yet")
            return 0
        for run in runs:
            episodes = results.episodes_for_run(run["id"])
            ungraded = results.episodes_needing_grading(run_id=run["id"])
            by_status: dict[str, int] = {}
            for e in episodes:
                by_status[e["status"]] = by_status.get(e["status"], 0) + 1
            print(
                f"run {run['id']}: {len(episodes)} episode(s), "
                f"status={by_status}, ungraded={len(ungraded)}"
            )
        return 0
    finally:
        results.close()


# -- entry point -------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    provider_factory: ProviderFactory = make_provider,
    judge_provider_factory: ProviderFactory = make_provider,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "env":
            if args.env_command == "reset":
                return cmd_env_reset(args)
            return cmd_env_snapshot(args)
        if args.command == "run":
            return cmd_run(args, provider_factory)
        if args.command == "grade":
            return cmd_grade(args, judge_provider_factory)
        if args.command == "report":
            return cmd_report(args)
        if args.command == "status":
            return cmd_status(args)
        raise CLIError(f"unknown command: {args.command}")
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
