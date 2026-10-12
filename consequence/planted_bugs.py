"""Planted-bug validation: does the test suite catch the bugs it exists to catch?

Each planted bug is one exact source edit that reintroduces a real failure
mode. For each: apply it, run the offline suite (as CI's check job does,
with no DATABASE_URL) and the live suite (needs `make up`), count the
failing tests, and restore the file -- in a `finally`, so an interrupted run
can't leave a bug in the tree. A bug that no test catches is a hole in the
suite.

    python -m consequence.planted_bugs                # print results
    python -m consequence.planted_bugs --write-doc    # also update docs/methodology.md

--write-doc rewrites only the block between the planted-bugs markers in
docs/methodology.md, so the counts there are always output of this command,
never typed by hand. tests/test_planted_bugs.py checks every anchor below
still matches the source exactly once, so a refactor can't silently turn a
planted bug into a no-op.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = "docs/methodology.md"
BLOCK_START = "<!-- planted-bugs:start -->"
BLOCK_END = "<!-- planted-bugs:end -->"


@dataclass(frozen=True)
class PlantedBug:
    key: str
    description: str
    path: str  # relative to the repo root
    anchor: str  # must occur exactly once in the current source
    replacement: str


PLANTED_BUGS = (
    PlantedBug(
        key="reads_live_environment",
        description="The grader takes `before` from the live environment instead of the "
                    "run's stored initial snapshot.",
        path="consequence/graders/state.py",
        anchor="    initial = load_initial(results, run_id)\n",
        replacement=(
            "    from consequence.environment import snapshot as _live_snapshot\n"
            "    initial = _live_snapshot()  # PLANTED\n"
        ),
    ),
    PlantedBug(
        key="skips_errored_episodes",
        description="The grader silently skips episodes that errored, timed out or ran "
                    "out of turns.",
        path="consequence/graders/state.py",
        anchor=(
            "    for episode in results.episodes_for_run(run_id):\n"
            "        if not regrade and _fully_graded(results, episode[\"id\"]):\n"
        ),
        replacement=(
            "    for episode in [e for e in results.episodes_for_run(run_id)\n"
            "                    if e[\"status\"] == COMPLETED]:  # PLANTED\n"
            "        if not regrade and _fully_graded(results, episode[\"id\"]):\n"
        ),
    ),
    PlantedBug(
        key="substring_supplier_filter",
        description="query_invoices' supplier_name filter as of a39b99d: an unescaped "
                    "substring ILIKE, so '%' matches every supplier.",
        path="consequence/tools/impl.py",
        anchor=(
            "        if \"supplier_name\" in args:\n"
            "            cur.execute(\n"
            "                \"SELECT id, name FROM suppliers WHERE name ILIKE %s ORDER BY id\",\n"
            "                (f\"%{_like_escape(args['supplier_name'])}%\",),\n"
            "            )\n"
            "            supplier_id = pick_supplier(args[\"supplier_name\"], cur.fetchall())\n"
            "            if supplier_id is None:\n"
            "                return {\"invoices\": [], \"total_count\": 0}\n"
            "            conditions.append(\"i.supplier_id = %s\")\n"
            "            params.append(supplier_id)\n"
            "            where = f\"WHERE {' AND '.join(conditions)}\"\n"
        ),
        replacement=(
            "        if \"supplier_name\" in args:  # PLANTED\n"
            "            conditions.append(\"s.name ILIKE %s\")\n"
            "            params.append(f\"%{args['supplier_name']}%\")\n"
            "            where = f\"WHERE {' AND '.join(conditions)}\"\n"
        ),
    ),
)
PLANTED_BUGS_BY_KEY = {b.key: b for b in PLANTED_BUGS}

_SUMMARY = re.compile(r"(\d+) (passed|failed|errors?)")


@dataclass(frozen=True)
class SuiteResult:
    failed: tuple[str, ...]  # node ids of failing or erroring tests
    passed: int

    @property
    def total(self) -> int:
        return len(self.failed) + self.passed


# Left out of the measured runs, because they test this harness and the docs,
# not the code a bug is planted in. The anchor test fails whenever its own bug
# is planted (the anchor is gone), which would add one fake catch to every
# count; the ledger and doc-completeness tests read the files this harness
# regenerates.
DOC_CONSISTENCY_TESTS = (
    "--ignore=tests/test_ledger.py",
    "--ignore=tests/test_planted_bugs.py",
)


def run_suite(suite: str) -> SuiteResult:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE",
           *DOC_CONSISTENCY_TESTS]
    env = dict(os.environ)
    if suite == "offline":
        env["DATABASE_URL"] = ""  # as in CI's check job: no database at all
    else:
        cmd += ["-m", "live"]
    # check=False: a non-zero exit (tests failing) is the measurement, not an error.
    out = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                         check=False).stdout
    failed = sorted({line.split(" - ")[0].split(" ", 1)[1] for line in out.splitlines()
                     if line.startswith(("FAILED ", "ERROR "))})
    counts = {kind: int(n) for n, kind in _SUMMARY.findall(out.splitlines()[-1] if out else "")}
    return SuiteResult(failed=tuple(failed), passed=counts.get("passed", 0))


def measure(bug: PlantedBug, suites: tuple[str, ...]) -> dict[str, SuiteResult]:
    path = ROOT / bug.path
    original = path.read_text(encoding="utf-8")
    if original.count(bug.anchor) != 1:
        raise ValueError(f"{bug.key}: anchor no longer matches {bug.path} exactly once")
    path.write_text(original.replace(bug.anchor, bug.replacement), encoding="utf-8")
    try:
        return {suite: run_suite(suite) for suite in suites}
    finally:
        path.write_text(original, encoding="utf-8")


def render_block(results: dict[str, dict[str, SuiteResult]]) -> str:
    lines = [
        BLOCK_START,
        "| Planted bug | What it does | Offline tests failing | Live tests failing |",
        "|---|---|---|---|",
    ]
    for key, by_suite in results.items():
        bug = PLANTED_BUGS_BY_KEY[key]
        cells = [f"{len(r.failed)} of {r.total}" for r in (by_suite["offline"],
                                                             by_suite["live"])]
        lines.append(f"| `{key}` | {bug.description} | {cells[0]} | {cells[1]} |")
    lines += ["", f"Measured {_provenance()} by `python -m consequence.planted_bugs --write-doc`.",
              BLOCK_END]
    return "\n".join(lines)


def _provenance() -> str:
    """When and against what the counts were taken. Totals change with every
    new test, so a table is only meaningful next to its commit."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()

    dirty = " plus uncommitted changes" if git("status", "--porcelain") else ""
    when = git("log", "-1", "--format=%cd", "--date=short")
    head = git("rev-parse", "--short", "HEAD")
    return (f"on {datetime.now(UTC).date().isoformat()}, at commit `{head}`{dirty} "
            f"(last commit {when})")


def parse_block(text: str) -> dict[str, dict[str, tuple[int, int]]]:
    """{bug key: {"offline"|"live": (failing, total)}} from the doc's block."""
    if text.count(BLOCK_START) != 1 or text.count(BLOCK_END) != 1:
        raise ValueError(f"{DOC_PATH} must contain exactly one planted-bugs block")
    block = text[text.index(BLOCK_START):text.index(BLOCK_END)]
    out = {}
    for row in re.finditer(r"^\| `(\w+)` \|.*\| (\d+) of (\d+) \| (\d+) of (\d+) \|$", block,
                           re.MULTILINE):
        key, off_f, off_t, live_f, live_t = row.groups()
        out[key] = {"offline": (int(off_f), int(off_t)), "live": (int(live_f), int(live_t))}
    return out


def write_doc(block: str) -> None:
    path = ROOT / DOC_PATH
    text = path.read_text(encoding="utf-8")
    start, end = text.index(BLOCK_START), text.index(BLOCK_END) + len(BLOCK_END)
    path.write_text(text[:start] + block + text[end:], encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m consequence.planted_bugs")
    parser.add_argument("bugs", nargs="*",
                        help=f"default: all of them ({', '.join(PLANTED_BUGS_BY_KEY)})")
    parser.add_argument("--write-doc", action="store_true",
                        help=f"rewrite the planted-bugs block in {DOC_PATH}")
    args = parser.parse_args(argv)
    unknown = [k for k in args.bugs if k not in PLANTED_BUGS_BY_KEY]
    if unknown:
        parser.error(f"unknown planted bug(s): {', '.join(unknown)}")
    keys = args.bugs or list(PLANTED_BUGS_BY_KEY)
    if args.write_doc and keys != list(PLANTED_BUGS_BY_KEY):
        parser.error("--write-doc needs every planted bug, so the table is complete")

    results = {}
    for key in keys:
        results[key] = measure(PLANTED_BUGS_BY_KEY[key], ("offline", "live"))
        for suite, r in results[key].items():
            print(f"== {key} / {suite}: {len(r.failed)} of {r.total} failing")
            for node in r.failed:
                print(f"    {node}")
        sys.stdout.flush()
    if args.write_doc:
        write_doc(render_block(results))
        print(f"wrote {DOC_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
