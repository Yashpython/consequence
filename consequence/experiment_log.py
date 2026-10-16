"""Records served-model drift in docs/experiment-log.md.

When `consequence grade` finds that a configured model (or judge) was served
by more than one model version within a run, it says so in its output and
records it here, inside a marked block, one line per (run, model). Re-grading
updates that line rather than adding another. Grading is never blocked: the
drift is a fact about the run to report, not a reason to discard it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from consequence.metrics import ServedModels

LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "experiment-log.md"
BLOCK_START = "<!-- served-model-drift:start -->"
BLOCK_END = "<!-- served-model-drift:end -->"
EMPTY = "None recorded."


def drift_line(run_id: int, drift: ServedModels) -> str:
    served = ", ".join(f"`{name}` x{n}" for name, n in drift.served.items())
    return (f"- **Run {run_id}, `{drift.configured}`** (configured `{drift.api_model}`): "
            f"served {served}")


def _key(line: str) -> str:
    return line.split("**")[1] if line.count("**") >= 2 else line


def record_drift(run_id: int, drifts: Iterable[ServedModels],
                 path: Path | None = None) -> None:
    """Add or update one line per drifting model for this run."""
    drifts = list(drifts)
    if not drifts:
        return
    path = path or LOG_PATH  # looked up at call time, so tests can redirect it
    text = path.read_text(encoding="utf-8")
    if text.count(BLOCK_START) != 1 or text.count(BLOCK_END) != 1:
        raise ValueError(f"{path} must contain exactly one served-model-drift block")
    head, rest = text.split(BLOCK_START)
    body, tail = rest.split(BLOCK_END)
    lines = [ln for ln in body.strip().splitlines() if ln.strip() and ln.strip() != EMPTY]
    stamp = datetime.now(UTC).date().isoformat()
    for drift in drifts:
        new = f"{drift_line(run_id, drift)} (recorded {stamp} by `consequence grade`)"
        lines = [ln for ln in lines if _key(ln) != _key(new)]
        lines.append(new)
    path.write_text(f"{head}{BLOCK_START}\n" + "\n".join(lines) + f"\n{BLOCK_END}{tail}",
                    encoding="utf-8")
