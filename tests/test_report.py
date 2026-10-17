"""consequence.report: lays out what consequence.metrics computes, and
computes nothing itself."""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from dataclasses import asdict

from test_metrics import FORBIDDEN, J1, J2, N1, STANDARD, T, open_run

from consequence import report
from consequence.report import build_report, render_markdown

# -- report.py computes nothing --------------------------------------------------------------

ARITHMETIC_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
                  ast.MatMult, ast.LShift, ast.RShift, ast.BitOr, ast.BitAnd, ast.BitXor)
COUNTING_CALLS = {"sum", "len", "min", "max", "round", "abs", "divmod", "pow", "Counter",
                  "mean", "median", "fsum", "comb", "quantiles", "count"}
NUMERIC_MODULES = ("math", "statistics", "collections", "sqlite3", "fractions", "decimal",
                   "numpy", "random")


def _annotation_nodes(tree: ast.AST) -> set[int]:
    """Type hints: `float | None` is a BinOp in the syntax tree but not arithmetic."""
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef) and node.returns is not None:
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(n) for root in roots for n in ast.walk(root)}


def _computes(node: ast.AST) -> bool:
    """Arithmetic, counting, or reading the results DB directly."""
    arithmetic = (isinstance(node, ast.BinOp) and isinstance(node.op, ARITHMETIC_OPS)
                  or isinstance(node, ast.AugAssign)
                  or isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd))
    called = ""
    if isinstance(node, ast.Call):
        called = node.func.id if isinstance(node.func, ast.Name) else getattr(
            node.func, "attr", "")
    reads_db = isinstance(node, ast.Attribute) and node.attr in (
        "_conn", "execute", "fetchall", "fetchone", "_rows", "_episodes")
    return arithmetic or called in COUNTING_CALLS or reads_db


def test_report_contains_no_arithmetic_over_results():
    """It may format and lay out, nothing else: no arithmetic operators, no
    augmented assignment, no numeric negation, no counting builtins, no maths
    modules, and no access to the results DB itself."""
    source = inspect.getsource(report)
    tree = ast.parse(source)
    hints = _annotation_nodes(tree)
    found = [ast.get_source_segment(source, n) for n in ast.walk(tree)
             if id(n) not in hints and _computes(n)]
    assert found == []


def test_report_imports_only_metrics():
    tree = ast.parse(inspect.getsource(report))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imported == {"__future__", "dataclasses", "typing", "consequence.metrics"}
    assert not [m for m in imported if m.startswith(NUMERIC_MODULES)]


def test_report_import_isolation():
    """Mirrors metrics: imported alone, it loads nothing that can call a
    model, touch the environment database or read the seed."""
    out = subprocess.run(
        [sys.executable, "-c", "import sys, consequence.report; print('\\n'.join(sys.modules))"],
        capture_output=True, text=True, check=True).stdout.split()
    assert [m for m in out if m.startswith(FORBIDDEN)] == []
    assert "consequence.metrics" in out


# -- every figure is metrics' figure, laid out --------------------------------------------------


def test_every_figure_is_exactly_what_metrics_returns(tmp_path):
    m, _ = open_run(tmp_path, STANDARD)
    with m:
        data = build_report(m, pass_k=(2,))
        model = data["models"]["m-a"]
        assert model["state_verifier"] == {c: asdict(r) for c, r in
                                           m.pass_rate_state("m-a").items()}
        assert model["judges"][J1]["verification_gap"] == {
            c: asdict(g) for c, g in m.verification_gap("m-a", J1).items()}
        assert model["judges"][J1]["trap_behaviour_split"] == asdict(
            m.trap_behaviour_split("m-a", J1))
        assert model["pass_hat_k"]["2"] == {c: asdict(p) for c, p in
                                           m.pass_hat_k("m-a", 2).items()}
        assert model["collateral"] == {c: asdict(x) for c, x in m.collateral_rate("m-a").items()}
        assert model["cost_per_solved_task"] == {
            c: asdict(x) for c, x in m.cost_per_solved_task("m-a").items()}
        assert data["coverage"] == [asdict(c) for c in m.grading_coverage()]
        assert data["judge_agreement"][J1]["api_temperature_0"] == asdict(
            m.judge_api_agreement(J1))
        assert data["judge_agreement"][J1]["sampling_stability"] == asdict(
            m.judge_vote_agreement(J1))
        assert model["narration_chars"] == asdict(m.narration_chars("m-a"))


def test_agreement_is_labelled_with_its_condition_and_narration_is_shown(tmp_path):
    m, _ = open_run(tmp_path, [
        {"task": N1, "state": True, "default_judges": {J1: (True, "completed", 2 / 3)}}])
    with m:
        md = render_markdown(build_report(m))
    assert "sampling stability, provider_default" in md
    assert "Visible narration" in md and "median 0" in md


def test_tables_are_per_model_never_pooled(tmp_path):
    m, _ = open_run(tmp_path, [
        {"task": N1, "model": "m-a", "state": True}, {"task": N1, "model": "m-a", "state": True},
        {"task": N1, "model": "m-b", "state": False},
    ])
    with m:
        data = build_report(m)
    assert set(data["models"]) == {"m-a", "m-b"}
    assert data["models"]["m-a"]["state_verifier"]["normal"]["successes"] == 2
    assert data["models"]["m-b"]["state_verifier"]["normal"]["successes"] == 0
    md = render_markdown(data)
    assert "## Model: m-a" in md and "## Model: m-b" in md
    assert "2 of 3" not in md  # the pooled figure appears nowhere


def test_judges_are_split_and_the_primary_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "primary_judge_id", lambda: J1)
    m, _ = open_run(tmp_path, [{"task": N1, "state": True,
                                "judges": {J1: (True, "completed", 1.0),
                                           J2: (False, "acted_incorrectly", 1.0)}}])
    with m:
        data = build_report(m)
    judges = data["models"]["m-a"]["judges"]
    assert judges[J1]["pass_rate"]["normal"]["value"] == 1.0
    assert judges[J2]["pass_rate"]["normal"]["value"] == 0.0
    assert set(data["models"]["m-a"]["cross_judge"]) == {J2}
    md = render_markdown(data)
    assert f"### Judge: {J1} (primary)" in md and f"### Judge: {J2}\n" in md


def test_the_gap_always_shows_its_n_no_verdict_count_and_bound(tmp_path):
    eps = [{"task": N1, "state": i < 5, "judges": {J1: "error"}} for i in range(6)]
    eps[0]["judges"] = {J1: (True, "completed", 1.0)}
    m, _ = open_run(tmp_path, eps)
    with m:
        data = build_report(m)
    md = render_markdown(data)
    gap = data["models"]["m-a"]["judges"][J1]["verification_gap"]["normal"]
    header = next(line for line in md.splitlines() if "gap (pp)" in line)
    for column in ("paired n", "no verdict (either grader)", "sensitivity bound (pp)"):
        assert column in header
    row = f"{gap['bound_low_pp']:+.1f} to {gap['bound_high_pp']:+.1f}"
    assert row in md
    assert (gap["n_paired"], gap["no_verdict"]) == (1, 5)


def test_every_rate_shows_its_n_and_interval_and_traps_stay_apart(tmp_path):
    m, _ = open_run(tmp_path, STANDARD)
    with m:
        md = render_markdown(build_report(m))
    state = md.split("### State verifier")[1].split("###")[0]
    assert "| normal | 80.0% |" in state and "| 8 of 10 |" in state
    assert "| trap | 100.0% |" in state and "| 3 of 3 |" in state
    assert "95% CI (Wilson)" in state and " to " in state
    assert "11 of 13" not in md  # normal and trap never combined


def test_empty_and_judgeless_runs_render(tmp_path):
    m, _ = open_run(tmp_path, [{"task": T, "state": True}])
    with m:
        md = render_markdown(build_report(m))
    assert "## Model: m-a" in md and "Judging cost" not in md
