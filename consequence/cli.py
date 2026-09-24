"""The command-line interface: the single entry point for everything from
here on -- resetting the environment, running episodes, grading them, and
reporting on a run.

Nothing here computes a metric it doesn't also persist: `run` and `grade`
write to the results store before they print a summary of what they wrote;
`report` and `status` only ever read back what's already there. The one
exception is `run --dry-run`'s cost preview, which is explicitly a planning
estimate (see DRY_RUN_ESTIMATED_*_TOKENS below) -- it is labeled as such
and is never persisted.

Grading: no graders are registered by default yet -- consequence.tools,
consequence.harness, and this CLI are the plumbing; state_verifier and
llm_judge implementations (with their required reward-hacking-log entries,
per the standing rules) land in a later commit. `grade` is fully wired and
testable today via the `graders` parameter to main().
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from typing import Any

from consequence.environment import reset, snapshot
from consequence.harness import Task, run_episode
from consequence.providers.base import Provider
from consequence.providers.registry import compute_cost, load_model_registry, make_provider
from consequence.results import Results
from consequence.tasks import TASKS, get_task

# A rough, fixed per-episode token estimate used ONLY for the `run --dry-run`
# cost preview. This is a planning estimate, not a measurement: it is never
# written to the results store and is always printed labeled as an estimate.
DRY_RUN_ESTIMATED_INPUT_TOKENS = 8_000
DRY_RUN_ESTIMATED_OUTPUT_TOKENS = 1_500

GraderFn = Callable[[dict[str, Any], dict[str, Any]], tuple[bool, dict[str, Any]]]
ProviderFactory = Callable[..., Provider]

# Empty on purpose -- see the module docstring.
DEFAULT_GRADERS: dict[str, GraderFn] = {}

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

    report_parser = sub.add_parser("report", help="Report on a run")
    report_parser.add_argument("--run", type=int, required=True, dest="run_id")
    report_parser.add_argument("--format", choices=["md", "json"], default="md")

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
        run_id = args.run_id if args.run_id is not None else results.start_run(notes=args.run_notes)
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

            record = run_episode(task, provider, results, trial_index, run_id=run_id)
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


def cmd_grade(args: argparse.Namespace, graders: Mapping[str, GraderFn]) -> int:
    requested = set(ALL_GRADER_NAMES) if args.grader == "all" else {args.grader}
    missing = requested - graders.keys()
    if missing:
        raise CLIError(
            f"grader(s) not implemented yet: {sorted(missing)}. "
            f"registered: {sorted(graders) or 'none'}"
        )

    results = Results(args.results_db)
    try:
        if results.get_run(args.run_id) is None:
            raise CLIError(f"no run {args.run_id}")

        graded_count = 0
        for grader_name in sorted(requested):
            grader_fn = graders[grader_name]
            episodes = (
                results.episodes_for_run(args.run_id)
                if args.regrade
                else results.ungraded_by(grader_name, run_id=args.run_id)
            )
            for episode in episodes:
                diff_row = results.state_diff_for_episode(episode["id"])
                diff_json = (
                    json.loads(diff_row["diff_json"])
                    if diff_row is not None
                    else {"added": [], "removed": [], "changed": []}
                )
                passed, detail = grader_fn(episode, diff_json)
                results.record_grading(episode["id"], grader_name, passed=passed, detail_json=detail)
                graded_count += 1
                print(f"graded episode={episode['id']} grader={grader_name} passed={passed}")

        print(f"totals: graded={graded_count}")
        return 0
    finally:
        results.close()


# -- report / status -------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    results = Results(args.results_db)
    try:
        run = results.get_run(args.run_id)
        if run is None:
            raise CLIError(f"no run {args.run_id}")

        episodes = results.episodes_for_run(args.run_id)
        by_status: dict[str, int] = {}
        total_cost = 0.0
        total_turns = 0
        for e in episodes:
            by_status[e["status"]] = by_status.get(e["status"], 0) + 1
            total_cost += e["cost_usd"] or 0.0
            total_turns += e["turn_count"] or 0

        grading: dict[str, dict[str, int]] = {}
        for e in episodes:
            for g in results.gradings_for_episode(e["id"]):
                bucket = grading.setdefault(g["grader"], {"passed": 0, "failed": 0})
                bucket["passed" if g["passed"] else "failed"] += 1

        report = {
            "run_id": args.run_id,
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "episode_count": len(episodes),
            "by_status": by_status,
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "grading": grading,
        }

        if args.format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(f"# Run {report['run_id']} report")
            print(f"- started: {report['started_at']}")
            print(f"- finished: {report['finished_at']}")
            print(f"- episodes: {report['episode_count']}")
            print(f"- status breakdown: {report['by_status']}")
            print(f"- total cost: {_format_cost(report['total_cost_usd'])}")
            print(f"- total turns: {report['total_turns']}")
            if report["grading"]:
                print("- grading:")
                for grader_name, counts in report["grading"].items():
                    print(f"    {grader_name}: {counts}")
            else:
                print("- grading: none yet")
        return 0
    finally:
        results.close()


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
    graders: Mapping[str, GraderFn] | None = None,
) -> int:
    graders = DEFAULT_GRADERS if graders is None else graders
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
            return cmd_grade(args, graders)
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
