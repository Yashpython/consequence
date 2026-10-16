"""The run report: every figure comes from consequence.metrics, laid out.

This module computes nothing. It asks RunMetrics for each number and formats
it -- no counting, averaging or rate arithmetic happens here, and
tests/test_report.py fails if any appears (it parses this file and rejects
arithmetic operators and counting builtins). Percentages are rendered by the
`%` format spec; the verification gap arrives in percentage points already.

Layout rules, each from the metrics' own rules:
  - every table is per model, never pooled across models;
  - normal and trap tasks are separate rows, never combined;
  - every rate shows its n and its Wilson 95% interval, and how many episodes
    it left out for having no verdict;
  - judge figures are per judge_model, and the primary judge is named;
  - wherever the verification gap appears, so do its paired n, its
    no-verdict count and its sensitivity bound.

Like metrics, it imports nothing that can call a model, touch the environment
database or read the seed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from consequence.metrics import (
    EPISODE_STATUSES,
    TRAP_BEHAVIOURS,
    MetricsError,
    Rate,
    RunMetrics,
    primary_judge_id,
)

CATEGORY_ORDER = ("normal", "trap", "unknown")


def _per_category(values: dict[str, Any]) -> dict[str, Any]:
    return {cat: asdict(values[cat]) for cat in CATEGORY_ORDER if cat in values}


def build_report(m: RunMetrics, *, pass_k: tuple[int, ...] = ()) -> dict[str, Any]:
    """The whole report as plain data, every value straight from `m`."""
    try:
        primary = primary_judge_id()
    except MetricsError:
        primary = None
    judges = m.judge_models()
    return {
        "run": asdict(m.run_info()),
        "served_model_drift": [asdict(s) for s in m.served_model_drift()],
        "judge_served_models": {j: asdict(m.judge_served_models(j)) for j in judges},
        "primary_judge": primary,
        "coverage": [asdict(c) for c in m.grading_coverage()],
        "judging_cost": {j: asdict(m.judge_cost(j)) for j in judges},
        "judge_agreement": {
            j: {"api_temperature_0": asdict(m.judge_api_agreement(j)),
                "stability_temperature_0_7": asdict(m.judge_vote_agreement(j))}
            for j in judges
        },
        "models": {model: _model(m, model, judges, primary, pass_k) for model in m.models()},
    }


def _model(m: RunMetrics, model: str, judges: list[str], primary: str | None,
           pass_k: tuple[int, ...]) -> dict[str, Any]:
    return {
        "episodes": m.episode_status_counts(model),
        "served_models": asdict(m.served_models(model)),
        "state_verifier": _per_category(m.pass_rate_state(model)),
        "pass_hat_k": {str(k): _per_category(m.pass_hat_k(model, k)) for k in pass_k},
        "collateral": _per_category(m.collateral_rate(model)),
        "cost_per_solved_task": _per_category(m.cost_per_solved_task(model)),
        "wall_clock_seconds": _per_category(m.latency_stats(model)),
        "turns_on_passes": _per_category(m.turn_stats(model)),
        "incomplete_but_verifier_satisfied": m.incomplete_but_verifier_satisfied(model),
        "judges": {
            j: {"pass_rate": _per_category(m.pass_rate_judge(model, j)),
                "verification_gap": _per_category(m.verification_gap(model, j)),
                "trap_behaviour_split": asdict(m.trap_behaviour_split(model, j))}
            for j in judges
        },
        "cross_judge": (
            {other: _per_category(by_cat) for other, by_cat
             in m.cross_judge_agreement(model, primary_judge=primary).items()}
            if primary in judges else {}
        ),
    }


# -- Markdown -------------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _ci(low: float | None, high: float | None) -> str:
    return "n/a" if low is None else f"{low:.1%} to {high:.1%}"


def _num(value: Any, spec: str = "") -> str:
    return "n/a" if value is None else format(value, spec)


def _served_text(s: dict[str, Any]) -> str:
    """Configured vs served, and whether the served version changed."""
    served = ", ".join(f"`{name}` x{n}" for name, n in s["served"].items()) or "none recorded"
    unknown = f" ({s['unknown']} unknown)" if s["unknown"] else ""
    changed = " -- **CHANGED WITHIN THIS RUN**" if s["drift"] else ""
    return f"Configured `{s['api_model']}`; served: {served}{unknown}{changed}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def _rate_cells(r: dict[str, Any]) -> list[str]:
    """value, interval, successes of n, no verdict, ungraded."""
    return [_pct(r["value"]), _ci(r["ci_low"], r["ci_high"]), f"{r['successes']} of {r['n']}",
            str(r["no_verdict"]), str(r["ungraded"])]


RATE_HEADER = ["rate", "95% CI (Wilson)", "passes of n", "no verdict", "ungraded"]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = [f"| {' | '.join(header)} |", f"|{'|'.join('---' for _ in header)}|"]
    out.extend(f"| {' | '.join(row)} |" for row in rows)
    return out


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [
        f"# Run {run['run_id']} report",
        "",
        f"- started: {run['started_at']}; finished: {run['finished_at']}",
        f"- episodes in run: {run['episodes']}",
        f"- primary judge (headline verification gap): {report['primary_judge'] or 'none'}",
        "- every table is per model; normal and trap tasks are never combined",
        "",
    ]
    for d in report["served_model_drift"]:
        lines.extend([(f"**WARNING: served model changed within this run for "
                       f"{d['configured']}** ({_served_text(d)})"), ""])
    lines.extend(["## Grading coverage", ""])
    lines.extend(_table(
        ["grader", "judge", "episodes in run", "verdicts", "grading_error", "judge_error",
         "missing"],
        [[c["grader"], c["judge_model"] or "-", str(c["episodes_in_run"]), str(c["graded"]),
          str(c["grading_error"]), str(c["judge_error"]), str(c["missing"])]
         for c in report["coverage"]],
    ))
    if report["judging_cost"]:
        lines.extend(["", "## Judging cost (separate from the experiment's cost)", ""])
        lines.extend(_table(
            ["judge", "rows", "input tokens", "output tokens", "cost", "unpriced rows"],
            [[j, str(c["rows"]), str(c["input_tokens"]), str(c["output_tokens"]),
              _usd(c["cost_usd"]), str(c["unpriced_rows"])]
             for j, c in report["judging_cost"].items()],
        ))
        lines.extend(["", "## Judge vote agreement", "",
                      ("API agreement (temperature 0) measures provider nondeterminism, not "
                       "judge reliability; stability (temperature 0.7) is the reliability "
                       "figure."), ""])
        lines.extend(_table(
            ["judge", "condition", "episodes", "mean", "unanimous", "95% CI", "3/3", "2/3",
             "1/3", "no verdict"],
            [[j, label, str(a["n"]), _num(a["mean"], ".3f"), _pct(a["unanimous"]["value"]),
              _ci(a["unanimous"]["ci_low"], a["unanimous"]["ci_high"]),
              str(a["distribution"]["3/3"]), str(a["distribution"]["2/3"]),
              str(a["distribution"]["1/3"]), str(a["no_verdict"])]
             for j, agreement in report["judge_agreement"].items()
             for label, a in (("API, temperature 0", agreement["api_temperature_0"]),
                              ("stability, 0.7", agreement["stability_temperature_0_7"]))],
        ))
    for model, figures in report["models"].items():
        lines.extend(_model_markdown(model, figures, report["primary_judge"]))
    return "\n".join(lines)


def _model_markdown(model: str, f: dict[str, Any], primary: str | None) -> list[str]:
    lines = ["", f"## Model: {model}", "", _served_text(f["served_models"]), "",
             "### Episodes", ""]
    lines.extend(_table(["category", *EPISODE_STATUSES],
                        [[cat, *(str(counts[s]) for s in EPISODE_STATUSES)]
                         for cat, counts in f["episodes"].items()]))
    lines.extend(["", "### State verifier", ""])
    lines.extend(_table(["category", *RATE_HEADER],
                        [[cat, *_rate_cells(r)] for cat, r in f["state_verifier"].items()]))
    for k, by_cat in f["pass_hat_k"].items():
        lines.extend(["", (f"### pass^{k} (all {k} trials of a task pass; bootstrap CI "
                           "over tasks)"), ""])
        lines.extend(_table(
            ["category", "pass^k", "95% CI", "tasks", "trials", "tasks with too few trials",
             "no verdict"],
            [[cat, _pct(p["value"]), _ci(p["ci_low"], p["ci_high"]), str(p["n_tasks"]),
              str(p["n_trials"]), str(p["tasks_too_few_trials"]), str(p["no_verdict"])]
             for cat, p in by_cat.items()]))
    lines.extend(["", "### Collateral damage among state passes", ""])
    lines.extend(_table(
        ["category", "any damage", "95% CI", "benign", "destructive", "of n", "no verdict",
         "ungraded"],
        [[cat, _pct(c["non_clean"]["value"]),
          _ci(c["non_clean"]["ci_low"], c["non_clean"]["ci_high"]),
          str(c["benign"]["successes"]), str(c["destructive"]["successes"]),
          str(c["non_clean"]["n"]), str(c["non_clean"]["no_verdict"]),
          str(c["non_clean"]["ungraded"])]
         for cat, c in f["collateral"].items()]))
    lines.extend(["", "### Cost, time and turns", ""])
    lines.extend(_table(
        ["category", "cost per solved task", "episode cost", "solved", "unpriced",
         "wall-clock median s", "p90 s", "turns median (passes)", "p90"],
        [[cat, _usd(cost["value"]) if cost["value"] is not None else
          f"n/a ({cost['reason']})", _usd(cost["total_cost_usd"]),
          str(cost["solved"]), str(cost["unpriced"]),
          _num(f["wall_clock_seconds"][cat]["median"], ".1f"),
          _num(f["wall_clock_seconds"][cat]["p90"], ".1f"),
          _num(f["turns_on_passes"][cat]["median"]), _num(f["turns_on_passes"][cat]["p90"])]
         for cat, cost in f["cost_per_solved_task"].items()]))
    satisfied = f["incomplete_but_verifier_satisfied"]
    counts = ", ".join(f"{cat} {n}" for cat, n in satisfied.items())
    lines.extend(["", ("Did not complete, but the final state satisfied the verifier (all "
                       f"counted as failed): {counts}")])
    for judge, j in f["judges"].items():
        label = " (primary)" if judge == primary else ""
        lines.extend(["", f"### Judge: {judge}{label}", ""])
        lines.extend(_table(
            ["category", *RATE_HEADER, "gap (pp)", "paired n", "no verdict (either grader)",
             "sensitivity bound (pp)"],
            [[cat, *_rate_cells(r), _num(g["gap_pp"], "+.1f"), str(g["n_paired"]),
              str(g["no_verdict"]),
              f"{g['bound_low_pp']:+.1f} to {g['bound_high_pp']:+.1f}"]
             for cat, r in j["pass_rate"].items()
             for g in (j["verification_gap"][cat],)]))
        split = j["trap_behaviour_split"]
        lines.extend(["", (f"Trap episodes the state verifier passed: "
                           f"{split['n_state_passed']}, judged: {split['n_judged']} (no judge "
                           f"verdict: {split['judge_no_verdict']}, not judged: "
                           f"{split['judge_ungraded']})"), ""])
        lines.extend(_table(
            ["behaviour", "share", "95% CI", "count of n"],
            [[b, _pct(r["value"]), _ci(r["ci_low"], r["ci_high"]),
              f"{r['successes']} of {r['n']}"]
             for b in TRAP_BEHAVIOURS for r in (split["behaviours"][b],)]))
    for other, by_cat in f["cross_judge"].items():
        lines.extend(["", (f"### Cross-judge agreement: {primary} vs {other} (over episodes "
                           "both judged)"), ""])
        lines.extend(_table(
            ["category", "agreement", "95% CI", "agree of n", "primary pass / other fail",
             "primary fail / other pass", "primary only", "other only"],
            [[cat, _pct(c["agreement"]["value"]),
              _ci(c["agreement"]["ci_low"], c["agreement"]["ci_high"]),
              f"{c['agreement']['successes']} of {c['agreement']['n']}",
              str(c["primary_pass_other_fail"]), str(c["primary_fail_other_pass"]),
              str(c["primary_only"]), str(c["other_only"])]
             for cat, c in by_cat.items()]))
    return lines


__all__ = ["CATEGORY_ORDER", "Rate", "build_report", "render_markdown"]
