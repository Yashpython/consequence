"""The task catalog: loads every tasks/NNN_name.py file at the repo root.

Each task file defines a module-level TASK (a consequence.task.Task) whose id
must equal the file's stem. Task file names start with a number, so they're
loaded by path rather than imported as modules.

Once episodes have been recorded against a task, its instruction and verifier
are part of the experimental apparatus: don't reword or re-rule an existing
task -- add a new numbered task instead.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from consequence.task import Task

TASKS_DIR = Path(__file__).resolve().parent.parent / "tasks"
_TASK_FILE = re.compile(r"^\d{3}_[a-z0-9_]+\.py$")


def _load_task_file(path: Path) -> Task:
    spec = importlib.util.spec_from_file_location(f"consequence_task_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load task file {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    task = getattr(module, "TASK", None)
    if not isinstance(task, Task):
        raise TypeError(f"{path.name} must define TASK = Task(...)")
    if task.id != path.stem:
        raise ValueError(f"{path.name}: TASK.id is {task.id!r}, expected {path.stem!r}")
    return task


def load_tasks(directory: Path = TASKS_DIR) -> tuple[Task, ...]:
    paths = sorted(p for p in directory.glob("*.py") if _TASK_FILE.match(p.name))
    return tuple(_load_task_file(p) for p in paths)


TASKS: tuple[Task, ...] = load_tasks()
TASKS_BY_ID: dict[str, Task] = {t.id: t for t in TASKS}


def get_task(task_id: str) -> Task:
    try:
        return TASKS_BY_ID[task_id]
    except KeyError:
        raise KeyError(f"no task {task_id!r}; available: {sorted(TASKS_BY_ID)}") from None
