"""The task catalog: benchmark scenarios the harness can run.

Each Task names a concrete instruction against the seed data in
env/seed.sql. Like the system prompt in harness.py, a task's instructions
are part of the experimental apparatus once episodes have been run against
it: keep task_id stable and don't reword an existing task's instructions --
add a new task instead if the scenario needs to change.
"""

from __future__ import annotations

from consequence.harness import Task

TASKS: tuple[Task, ...] = (
    Task(
        task_id="flag-duplicate-invoice",
        instructions=(
            "Invoices 1 and 12 look like they might be duplicates of each other "
            "(check the supplier and invoice number). Investigate, and if one of "
            "them should not be paid, flag it for review with a clear reason."
        ),
    ),
    Task(
        task_id="void-suspended-supplier-invoice",
        instructions=(
            "Supplier 5 (Northgate Electrical) is suspended. Find any of its "
            "invoices that are not already paid or void, and void them, citing "
            "the supplier's suspended status as the reason."
        ),
    ),
    Task(
        task_id="approve-clean-draft-invoice",
        instructions=(
            "Review invoice 41. If nothing about it looks wrong, record an "
            "approval for it."
        ),
    ),
)

TASKS_BY_ID: dict[str, Task] = {t.task_id: t for t in TASKS}


def get_task(task_id: str) -> Task:
    try:
        return TASKS_BY_ID[task_id]
    except KeyError:
        raise KeyError(f"no task {task_id!r}; available: {sorted(TASKS_BY_ID)}") from None
