"""Checks docs/resume-evidence.md against the sources it cites.

The ledger is an aggregator, never a second source of truth. Every number in
it carries an HTML comment naming where it comes from, right after the value:

    | Task count | 25 <!-- fact:tasks.count --> | MEASURED | ... |

`check()` recomputes every cited fact and reports any that disagree. A fact is
either counted from the repository itself (the task catalog, the tool schema,
the model registry, pytest's own collection) or read out of a marked block in
the source document that owns the number:

    docs/reward-hacking-log.md  <!-- adversarial:summary:start --> ... end
    docs/methodology.md         <!-- planted-bugs:start --> ... end
    docs/experiment-log.md      <!-- smoke-runs:start --> ... end

When docs/findings.md or docs/failure-taxonomy.md land, their numbers get the
same treatment: give the document a marked block, add FACTS entries that parse
it, and cite those keys from the ledger -- never copy a number across by hand.

It also enforces the ledger's own rules:
  - a row marked MEASURED cites at least one fact; a NOT YET MEASURED row's
    value has no digits in it;
  - no em-dashes anywhere;
  - every candidate bullet in section 8 is marked READY or DRAFT, and a READY
    bullet contains no number that isn't one of the facts it cites.

    python -m consequence.ledger    # exit 1 and list the problems, if any
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = "docs/resume-evidence.md"

_MARKER = re.compile(r"(\S+)\s*<!-- fact:([\w.]+) -->")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _block(rel: str, name: str) -> str:
    text = _read(rel)
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"{rel} must contain exactly one {start} ... {end} block")
    return text[text.index(start):text.index(end)]


# -- facts counted from the repository ----------------------------------------------------


def _tasks():
    from consequence.tasks import TASKS
    return TASKS


def _difficulty(name: str) -> Callable[[], str]:
    return lambda: str(sum(t.difficulty == name for t in _tasks()))


def _registry() -> dict:
    from consequence.providers.registry import load_model_registry
    return load_model_registry()


def _providers() -> set[str]:
    return {m.provider for m in _registry().values()}


@cache
def _collected(marker: str) -> str:
    """How many tests pytest collects under `-m marker`, asked of pytest itself."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "-m", marker],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout
    lines = out.splitlines()
    if not lines or "error" in lines[-1].lower():  # a collection error, not a count
        raise RuntimeError(f"pytest could not collect -m {marker!r}:\n{out[-2000:]}")
    return str(sum("::" in line for line in lines))  # one node id per line


def _ci_jobs() -> str:
    text = _read(".github/workflows/ci.yml")
    jobs = text[text.index("\njobs:"):]
    return str(len(re.findall(r"^  [\w-]+:\s*$", jobs, re.MULTILINE)))


# -- facts read out of the document that owns them ---------------------------------------


def _adversarial(label: str) -> Callable[[], str]:
    """A row of the summary table in the reward-hacking log's generated block."""
    def get() -> str:
        block = _block("docs/reward-hacking-log.md", "adversarial:summary")
        m = re.search(rf"^\| {re.escape(label)} \| (\d+) \|$", block, re.MULTILINE)
        if m is None:
            raise ValueError(f"no row {label!r} in the adversarial:summary block")
        return m.group(1)
    return get


def _adversarial_strategies() -> str:
    block = _block("docs/reward-hacking-log.md", "adversarial:summary")
    return str(len(re.findall(r"^\| [a-z_]+ \| \d+ \| \d+ \| \d+ \| \d+ \|$", block,
                              re.MULTILINE)))


_STRATEGY_COLUMNS = ("attempted", "succeeded_at_baseline", "closed", "structural_limit")


def _strategy(strategy: str, column: str) -> Callable[[], str]:
    """One cell of the per-strategy table in the same generated block."""
    def get() -> str:
        block = _block("docs/reward-hacking-log.md", "adversarial:summary")
        m = re.search(rf"^\| {strategy} \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|$", block,
                      re.MULTILINE)
        if m is None:
            raise ValueError(f"no strategy row {strategy!r} in the adversarial:summary block")
        return m.group(_STRATEGY_COLUMNS.index(column) + 1)
    return get


def _dependencies() -> list[str]:
    import tomllib
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    return [*project["dependencies"],
            *(d for group in project.get("optional-dependencies", {}).values() for d in group)]


def _planted(key: str, suite: str) -> Callable[[], str]:
    def get() -> str:
        from consequence.planted_bugs import DOC_PATH, parse_block
        return str(parse_block(_read(DOC_PATH))[key][suite][0])
    return get


SMOKE_LOG = "docs/experiment-log.md"


def smoke_rows() -> dict[str, tuple[str, str]]:
    """{fact key: (smoke test id, outcome)} from the experiment log's block."""
    block = _block(SMOKE_LOG, "smoke-runs")
    return {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(
        r"^\| (\w+) \| `([^`]+)` \| (\w+) \|", block, re.MULTILINE)}


def _smoke(key: str) -> Callable[[], str]:
    def get() -> str:
        return smoke_rows()[key][1]
    return get


FACTS: dict[str, Callable[[], str]] = {
    "tasks.count": lambda: str(len(_tasks())),
    "tasks.easy": _difficulty("easy"),
    "tasks.medium": _difficulty("medium"),
    "tasks.hard": _difficulty("hard"),
    "tasks.trap": _difficulty("trap"),
    "tasks.normal": lambda: str(sum(t.difficulty != "trap" for t in _tasks())),
    "verifiers.count": lambda: str(sum(callable(t.verify) for t in _tasks())),
    "tools.count": lambda: str(len(__import__("consequence.tools.schema",
                                              fromlist=["TOOLS"]).TOOLS)),
    "models.count": lambda: str(len(_registry())),
    "metrics.count": lambda: str(len(__import__("consequence.metrics",
                                                fromlist=["METRICS"]).METRICS)),
    "judges.count": lambda: str(len(__import__("consequence.providers.registry",
                                               fromlist=["x"]).load_judge_registry())),
    "providers.count": lambda: str(len(_providers())),
    "tests.offline": lambda: _collected("not live and not smoke"),
    "tests.live": lambda: _collected("live"),
    "tests.smoke": lambda: _collected("smoke"),
    "ci.jobs": _ci_jobs,
    "adversarial.attempted": _adversarial("Adversarial strategies attempted"),
    "adversarial.succeeded_at_baseline": _adversarial("Succeeded against the baseline verifiers"),
    "adversarial.closed": _adversarial("Closed in this pass"),
    "adversarial.structural_limit": _adversarial(
        "Structural limits of state verification (judge-mitigated)"),
    "adversarial.rejected_from_start": _adversarial("Rejected by the verifiers from the start"),
    "adversarial.strategies": _adversarial_strategies,
}
for _strategy_name in ("do_nothing", "claim_without_acting", "opposite",
                       "task_plus_destruction", "letter_not_intent", "transient_write"):
    for _column in _STRATEGY_COLUMNS:
        FACTS[f"adversarial.{_strategy_name}.{_column}"] = _strategy(_strategy_name, _column)
FACTS["dependencies.count"] = lambda: str(len(_dependencies()))
FACTS["dependencies.pinned_exactly"] = lambda: str(sum("==" in d for d in _dependencies()))
for _key in smoke_rows():
    FACTS[f"smoke.{_key}"] = _smoke(_key)
for _key in (__import__("consequence.planted_bugs", fromlist=["x"]).PLANTED_BUGS_BY_KEY):
    for _suite in ("offline", "live"):
        FACTS[f"planted.{_key}.{_suite}"] = _planted(_key, _suite)


# -- the check ----------------------------------------------------------------------------


def _clean(value: str) -> str:
    return value.strip("*`_()[],;:")


def _sections(text: str) -> dict[str, str]:
    """{heading number: body} for the ledger's '## N. Title' sections."""
    parts = re.split(r"^## (\d+)\. .*$", text, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def check(text: str, facts: dict[str, Callable[[], str]] = FACTS) -> list[str]:
    """Every problem with the ledger text, or [] if it agrees with its sources."""
    problems = []
    cited: dict[str, str | None] = {}
    for value, key in _MARKER.findall(text):
        if key not in facts:
            problems.append(f"unknown fact {key!r} (cited with value {value!r})")
            continue
        if key not in cited:
            try:
                cited[key] = facts[key]()
            except (KeyError, ValueError, OSError) as exc:  # the source is missing or malformed
                problems.append(f"{key}: could not read its source ({type(exc).__name__}: {exc})")
                cited[key] = None
        if cited[key] is None:
            continue
        if _clean(value) != cited[key]:
            problems.append(f"{key}: ledger says {_clean(value)!r}, source says {cited[key]!r}")

    if "—" in text:
        problems.append("the ledger contains an em-dash")

    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if "MEASURED" in cells and "<!-- fact:" not in line:
            problems.append(f"MEASURED row cites no fact: {line[:90]}")
        # Tables with a status column put the value second: | Fact | Value | ...
        if "NOT YET MEASURED" in cells and len(cells) > 1 and _NUMBER.search(
                _COMMENT.sub("", cells[1])):
            problems.append(f"NOT YET MEASURED row has a number in its value: {line[:90]}")

    bullets = _sections(text).get("8", "")
    for line in bullets.splitlines():
        if not line.startswith("- "):
            continue
        visible = _COMMENT.sub("", line)
        if visible.startswith("- **READY**"):
            allowed = {cited.get(key) for _, key in _MARKER.findall(line)} - {None}
            stray = [n for n in _NUMBER.findall(visible.removeprefix("- **READY**"))
                     if n.rstrip("%").replace(",", "") not in allowed]
            if stray:
                problems.append(f"READY bullet has uncited number(s) {stray}: {visible[:90]}")
        elif not visible.startswith("- **DRAFT**"):
            problems.append(f"section 8 bullet is neither READY nor DRAFT: {visible[:90]}")
    return problems


def main() -> int:
    problems = check(_read(LEDGER_PATH))
    for problem in problems:
        print(f"ledger: {problem}", file=sys.stderr)
    if not problems:
        print(f"{LEDGER_PATH} agrees with every source it cites")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
