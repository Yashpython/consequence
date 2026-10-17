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
    PROVIDER_DEFAULT,
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
# The same kind of planning estimate for one judge call: it reads the judge
# prompt plus the episode's transcript (taken as the episode's whole input
# above, which over-counts, since the transcript is the conversation once
# rather than re-sent every turn), and writes one short verdict. Thinking is
# not in it: a judge that thinks bills its thinking as output, by an amount
# no run has measured yet, so the dry run prints what each extra
# DRY_RUN_OUTPUT_SENSITIVITY_TOKENS per call would add instead of a guess.
DRY_RUN_ESTIMATED_JUDGE_OUTPUT_TOKENS = 500
DRY_RUN_OUTPUT_SENSITIVITY_TOKENS = 1_000

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
    for flag, default, text in (
        ("--judge-votes", judge.VOTES, "votes per primary verdict"),
        ("--api-agreement-subset", 0,
         ("episodes given a temperature-0 triplicate (with --judge-votes 1; primary judges "
          "that take a temperature only)")),
        ("--judge-stability-subset", 0,
         ("episodes re-judged 3x at temperature 0.7 (primary judges that take a temperature "
          "only)")),
        ("--cross-family-subset", 0, "episodes graded by each non-primary judge"),
    ):
        run_parser.add_argument(flag, type=int, default=default, metavar="N",
                                help=f"Dry-run judging estimate: {text} (default {default})")

    grade_parser = sub.add_parser("grade", help="Grade the episodes in a run")
    grade_parser.add_argument("--run", type=int, required=True, dest="run_id")
    grade_parser.add_argument(
        "--grader", default=None, choices=[*ALL_GRADER_NAMES, "all"],
        help="Required unless --preflight",
    )
    grade_parser.add_argument(
        "--preflight", action="store_true",
        help="Report how many episodes would overflow each judge's context window; "
             "calls no model and writes nothing",
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
        help="Judge sampling temperature (default 0). Not for a judge whose models.yaml "
             "sampling is provider_default: no temperature is ever sent to it",
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
        "--judge-votes", type=int, default=None, metavar="N",
        help=f"Votes per primary verdict, odd (default {judge.VOTES}). With 1, every "
             "episode still gets exactly one primary verdict",
    )
    grade_parser.add_argument(
        "--api-agreement-subset", type=int, default=None, metavar="N",
        help=f"With --judge-votes 1: also judge N sampled episodes {judge.VOTES} times at "
             f"temperature 0, under grader '{judge.API_AGREEMENT_GRADER}'",
    )
    grade_parser.add_argument(
        "--judge-sample-seed", type=int, default=0,
        help="Seed for every --*-subset sample (default 0)",
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


def estimate_costs(
    combos: list[tuple[Task, str, int]], registry: Mapping[str, Any],
    judges: Mapping[str, Any], *, judge_votes: int = 3, api_agreement_subset: int = 0,
    stability_subset: int = 0, cross_family_subset: int = 0,
) -> dict[str, Any]:
    """The dry run's cost estimate, from models.yaml prices and the
    DRY_RUN_ESTIMATED_* token counts. A planning estimate, never persisted.

    Judging: the primary judge gives every episode one verdict of
    `judge_votes` votes (so it covers exactly the state verifier's
    episodes); the API-agreement and stability subsets add 3 votes each per
    sampled episode on the primary judge; each non-primary judge grades the
    cross-family subset with `judge_votes` votes. A judge at the provider's
    default sampling always takes 3 votes (they are its stability
    measurement). A subset can't exceed the episodes there are.
    """
    episodes = len(combos)
    experiment: dict[str, Any] = {}
    for model_id in sorted({m for _, m, _ in combos}):
        count = sum(1 for _, m, _ in combos if m == model_id)
        per_episode = compute_cost(model_id, DRY_RUN_ESTIMATED_INPUT_TOKENS,
                                   DRY_RUN_ESTIMATED_OUTPUT_TOKENS, registry=registry)
        extra = compute_cost(model_id, 0, DRY_RUN_OUTPUT_SENSITIVITY_TOKENS, registry=registry)
        experiment[model_id] = {"episodes": count, "per_episode": per_episode,
                                "cost": None if per_episode is None else per_episode * count,
                                "per_extra_output": None if extra is None else extra * count}
    judge_input = (DRY_RUN_ESTIMATED_INPUT_TOKENS
                   + judge.estimate_tokens(judge.build_messages(())))

    def call_cost(judge_id: str) -> float | None:
        return compute_cost(judge_id, judge_input, DRY_RUN_ESTIMATED_JUDGE_OUTPUT_TOKENS,
                            registry=dict(judges))

    judging = []
    if judges:
        primary = primary_judge(judges).id

        def votes_for(judge_id: str) -> int:
            return judge.VOTES if judges[judge_id].sampling == PROVIDER_DEFAULT else judge_votes

        def condition(judge_id: str) -> str:
            return ("provider default sampling" if judges[judge_id].sampling == PROVIDER_DEFAULT
                    else "temperature 0")

        lines = [(f"primary {primary} ({condition(primary)})", primary, episodes,
                  votes_for(primary))]
        if judges[primary].sampling != PROVIDER_DEFAULT:  # its 3 votes are its stability
            lines += [(f"API-agreement subset (temperature 0), {primary}", primary,
                       min(api_agreement_subset, episodes), judge.VOTES),
                      (f"stability subset (temperature {judge.STABILITY_TEMPERATURE}), {primary}",
                       primary, min(stability_subset, episodes), judge.VOTES)]
        lines += [(f"cross-family {other} ({condition(other)})", other,
                   min(cross_family_subset, episodes), votes_for(other))
                  for other in sorted(judges) if other != primary]
        for label, judge_id, sampled, votes in lines:
            per_call = call_cost(judge_id)
            calls = sampled * votes
            extra = compute_cost(judge_id, 0, DRY_RUN_OUTPUT_SENSITIVITY_TOKENS,
                                 registry=dict(judges))
            judging.append({"label": label, "episodes": sampled, "votes": votes,
                            "calls": calls,
                            "cost": None if per_call is None else per_call * calls,
                            "per_extra_output": None if extra is None else extra * calls})
    experiment_total = sum(e["cost"] or 0.0 for e in experiment.values())
    judging_total = sum(j["cost"] or 0.0 for j in judging)
    used = [registry[m] for m in experiment if m in registry]
    used += [judges[j] for j in judges] if judges else []
    return {"episodes": episodes, "experiment": experiment, "judging": judging,
            "judge_input_tokens": judge_input,
            "price_notes": {spec.id: spec.price_note for spec in used if spec.price_note},
            "unpriced": sorted([m for m, e in experiment.items() if e["cost"] is None]
                               + [j["label"] for j in judging if j["cost"] is None]),
            "experiment_total": experiment_total, "judging_total": judging_total,
            "grand_total": experiment_total + judging_total}


def _print_dry_run(combos: list[tuple[Task, str, int]], estimate: dict[str, Any]) -> None:
    print(f"DRY RUN -- {len(combos)} episode(s) planned, no API calls will be made")
    for task, model_id, trial_index in combos:
        print(f"  would run: task={task.task_id} model={model_id} trial={trial_index}")
    print("experiment (estimated):")
    for model_id, e in estimate["experiment"].items():
        if e["cost"] is None:
            print(f"  {model_id}: {e['episodes']} episode(s), cost unknown (not in models.yaml)")
        else:
            print(f"  {model_id}: {e['episodes']} episode(s) x ~{_format_cost(e['per_episode'])}"
                  f" = ~{_format_cost(e['cost'])}")
    print(f"estimated experiment cost: ~{_format_cost(estimate['experiment_total'])} (assumes ~"
          f"{DRY_RUN_ESTIMATED_INPUT_TOKENS} input / ~{DRY_RUN_ESTIMATED_OUTPUT_TOKENS} output "
          "tokens per episode -- a planning estimate, not a measurement, and not an upper "
          "bound: it does not assume the output cap is reached)")
    for model_id, e in estimate["experiment"].items():
        if e["per_extra_output"] is not None:
            print(f"  + {_format_cost(e['per_extra_output'])} for {model_id} per extra "
                  f"{DRY_RUN_OUTPUT_SENSITIVITY_TOKENS} output tokens per episode (thinking is "
                  "billed as output)")
    print(f"judging (estimated; ~{estimate['judge_input_tokens']} input / "
          f"~{DRY_RUN_ESTIMATED_JUDGE_OUTPUT_TOKENS} output tokens per judge call):")
    for j in estimate["judging"]:
        print(f"  {j['label']}: {j['episodes']} episode(s) x {j['votes']} vote(s) = "
              f"{j['calls']} call(s) = ~{_format_cost(j['cost'])}")
    print(f"estimated judging cost: ~{_format_cost(estimate['judging_total'])} (thinking "
          "tokens not included)")
    for j in estimate["judging"]:
        if j["calls"]:
            print(f"  + {_format_cost(j['per_extra_output'])} for {j['label']} per extra "
                  f"{DRY_RUN_OUTPUT_SENSITIVITY_TOKENS} output tokens per call (thinking is "
                  "billed as output)")
    print(f"estimated grand total: ~{_format_cost(estimate['grand_total'])}")
    for model_id, note in estimate["price_notes"].items():
        print(f"price note, {model_id}: {note}")
    if estimate["unpriced"]:
        print(f"no price on file for: {estimate['unpriced']}")


def _check_judge_votes(votes: int, api_subset: int | None) -> None:
    if votes < 1 or votes % 2 == 0:
        raise CLIError(f"--judge-votes must be odd and at least 1, got {votes}")
    if api_subset and votes != 1:
        raise CLIError("--api-agreement-subset needs --judge-votes 1: with more votes every "
                       "episode already has its own temperature-0 agreement figure")


def _check_dry_run_judging(args: argparse.Namespace, judges: Mapping[str, Any]) -> None:
    _check_judge_votes(args.judge_votes, args.api_agreement_subset)
    if not judges:
        return
    primary = primary_judge(judges)
    if primary.sampling == PROVIDER_DEFAULT and (args.api_agreement_subset
                                                 or args.judge_stability_subset
                                                 or args.judge_votes != judge.VOTES):
        raise CLIError(f"the primary judge {primary.id} runs at the provider's default "
                       f"sampling: it always takes {judge.VOTES} votes, which are its stability "
                       "measurement, so --judge-votes, --api-agreement-subset and "
                       "--judge-stability-subset don't apply to it")


def cmd_run(args: argparse.Namespace, provider_factory: ProviderFactory) -> int:
    combos = _resolve_combos(args)
    registry = load_model_registry()

    if args.dry_run:
        judges = load_judge_registry()
        _check_dry_run_judging(args, judges)
        _print_dry_run(combos, estimate_costs(
            combos, registry, judges, judge_votes=args.judge_votes,
            api_agreement_subset=args.api_agreement_subset,
            stability_subset=args.judge_stability_subset,
            cross_family_subset=args.cross_family_subset))
        return 0
    if (args.judge_votes, args.api_agreement_subset, args.judge_stability_subset,
            args.cross_family_subset) != (judge.VOTES, 0, 0, 0):
        raise CLIError("the judging options on `run` only shape the --dry-run estimate; "
                       "judging itself is `consequence grade`")

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
    votes = args.judge_votes if args.judge_votes is not None else judge.VOTES
    if stability and args.judge_votes is not None:
        raise CLIError(f"--judge-stability-subset always takes {judge.VOTES} votes; drop "
                       "--judge-votes")
    _check_judge_votes(votes, args.api_agreement_subset)
    if args.api_agreement_subset is not None and (stability or args.judge_subset is not None
                                                  or args.judge_temperature not in (None, 0)):
        raise CLIError("--api-agreement-subset goes with a full temperature-0 primary pass, "
                       "not with --judge-subset, --judge-stability-subset or a non-zero "
                       "--judge-temperature")
    temperature = (judge.STABILITY_TEMPERATURE if stability
                   else args.judge_temperature if args.judge_temperature is not None
                   else judge.default_temperature(spec))
    grader = judge.STABILITY_GRADER if stability else judge.JUDGE_GRADER
    try:
        judge.check_config(spec)
        judge.check_sampling(spec, temperature, grader, judge.VOTES if stability else votes)
        if args.api_agreement_subset is not None:
            judge.check_sampling(spec, 0.0, judge.API_AGREEMENT_GRADER, judge.VOTES)
        provider = factory(spec.id, registry=registry, temperature=temperature)
    except (judge.JudgeConfigError, KeyError, ImportError, ValueError) as exc:
        raise CLIError(f"the judge can't run, so nothing was graded: {exc}") from exc
    return {"spec": spec, "provider": provider, "temperature": temperature,
            "grader": grader,
            "subset": args.judge_stability_subset if stability else args.judge_subset,
            "votes": judge.VOTES if stability else votes,
            "api_subset": args.api_agreement_subset}


def _grade_judge(args: argparse.Namespace, results: Results, setup: dict[str, Any]) -> int:
    """Judge the run and print the results. Returns the number of judge errors."""
    spec = setup["spec"]
    episode_ids, detail = _sample(args, results, setup["subset"])
    summary = judge.grade_run_with_judge(
        args.run_id, results, setup["provider"], spec, grader=setup["grader"],
        temperature=setup["temperature"], episode_ids=episode_ids, regrade=args.regrade,
        votes=setup["votes"], detail=detail,
    )
    errors = _print_judging(summary, setup["temperature"])
    if setup["api_subset"] is not None:
        episode_ids, detail = _sample(args, results, setup["api_subset"])
        errors += _print_judging(judge.grade_run_with_judge(
            args.run_id, results, setup["provider"], spec, grader=judge.API_AGREEMENT_GRADER,
            temperature=0.0, episode_ids=episode_ids, regrade=args.regrade,
            votes=judge.VOTES, detail=detail), 0.0)
    return errors


def _sample(args: argparse.Namespace, results: Results,
            size: int | None) -> tuple[list[int] | None, dict[str, int]]:
    if size is None:
        return None, {}
    try:
        ids = judge.sample_episodes(results, args.run_id, size, args.judge_sample_seed)
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    return ids, {"sample_seed": args.judge_sample_seed, "sample_size": size}


def _condition_text(temperature: float | None) -> str:
    return "provider default sampling" if temperature is None else f"temperature {temperature}"


def _print_judging(summary: judge.JudgeSummary, temperature: float | None) -> int:
    grader, spec_id = summary.grader, summary.judge_model
    for g in summary.graded:
        if g.status == judge.JUDGE_ERROR:
            print(f"JUDGE ERROR episode={g.episode_id} task={g.task_id} ({g.error_reason})")
        else:
            print(f"judged episode={g.episode_id} task={g.task_id} status={g.episode_status} "
                  f"{grader}={'pass' if g.passed else 'FAIL'} "
                  f"behaviour={g.behaviour or 'no_majority'} agreement="
                  f"{'n/a (one vote)' if g.agreement is None else f'{g.agreement:.2f}'}")
    if summary.already_graded:
        print(f"skipped {len(summary.already_graded)} already-judged episode(s); "
              "pass --regrade to re-judge them")
    counts = summary.pass_counts()
    rates = " | ".join(f"{c} {counts[c]['passed']}/{counts[c]['total']} passed"
                       for c in CATEGORY_ORDER if c in counts)
    print(f"totals: {grader} [{spec_id}, {_condition_text(temperature)}] "
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


def cmd_preflight(args: argparse.Namespace) -> int:
    """How many episodes each judge could not take whole. No model, no writes."""
    results = Results(args.results_db)
    try:
        try:
            checks = judge.preflight(results, args.run_id, load_judge_registry())
        except judge.JudgeConfigError as exc:
            raise CLIError(str(exc)) from exc
    finally:
        results.close()
    for c in checks:
        if c.context_window_tokens is None:
            print(f"{c.judge_model}: context_window_tokens not set; this judge can't run")
            continue
        print(f"{c.judge_model} (window {c.context_window_tokens} tokens): "
              f"{c.would_overflow} of {c.episodes} episode(s) would overflow"
              + (f"; {c.missing_transcript} have no transcript" if c.missing_transcript else ""))
    return 0


def cmd_grade(args: argparse.Namespace, judge_provider_factory: ProviderFactory) -> int:
    if args.preflight:
        if args.grader is not None:
            raise CLIError("--preflight grades nothing; drop --grader")
        return cmd_preflight(args)
    if args.grader is None:
        raise CLIError("--grader is required (or --preflight)")
    requested = set(ALL_GRADER_NAMES) if args.grader == "all" else {args.grader}
    judge_flags = (args.judge_model, args.judge_temperature, args.judge_subset,
                   args.judge_stability_subset, args.judge_votes, args.api_agreement_subset)
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
        _warn_served_drift(args)
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


def _results_path(args: argparse.Namespace) -> str:
    return args.results_db or load_config().results_db_path or DEFAULT_DB_PATH


def _warn_served_drift(args: argparse.Namespace) -> None:
    """If any configured model or judge was served by more than one model
    version in this run, say so loudly and record it in the experiment log.
    Never blocks grading."""
    from consequence import experiment_log
    from consequence.metrics import RunMetrics

    with RunMetrics(_results_path(args), args.run_id) as m:
        drifts = m.served_model_drift()
    for d in drifts:
        served = ", ".join(f"{name} x{n}" for name, n in d.served.items())
        banner = (f"WARNING: SERVED MODEL CHANGED WITHIN RUN {args.run_id} for "
                  f"{d.configured} (configured {d.api_model}): {served}")
        print(banner)
        print(banner, file=sys.stderr)
    experiment_log.record_drift(args.run_id, drifts)
    if drifts:
        print(f"recorded in {experiment_log.LOG_PATH.relative_to(experiment_log.LOG_PATH.parents[1])}")


def cmd_report(args: argparse.Namespace) -> int:
    """Every figure comes from consequence.metrics via consequence.report;
    nothing is computed here."""
    from consequence import report
    from consequence.metrics import MetricsError, RunMetrics

    path = _results_path(args)
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
