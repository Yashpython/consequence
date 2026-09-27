"""The task specification format, plus the helpers every verifier is built from.

A Task is everything the benchmark knows about one scenario: the exact text the
agent is given, and a verifier that decides pass/fail from observable database
state alone.

Rules for every verifier (enforced by review and by tests/test_tasks.py):
  - verify(before, after, diff) reads only the two snapshots and the diff. It
    never sees the transcript -- the signature makes that structural, not a
    matter of discipline.
  - Assert observable state ("invoice 12's status is 'void'"), never surface
    form ("the agent said it voided it").
  - Assert the POSITIVE (the intended change happened) AND the NEGATIVE (what
    should not have changed didn't). Every verifier includes
    only_allowed_tables(), so an unrequested write anywhere fails the task.
  - Every check is granular and named, so a failure is diagnosable and a report
    can show which parts went right even though pass/fail is binary.

Verifiers compute their targets from `before` rather than hard-coding row ids,
so the rule each one encodes is visible in code; tests/test_tasks.py then pins
those computed targets to hand-derived literals, so a bug in a verifier's rule
can't silently agree with itself.

Cell values may be native (date, datetime, int) when a snapshot comes straight
off the database, or strings when a snapshot has been round-tripped through
JSON -- as it will be when grading is re-run offline from stored state diffs.
The to_decimal / to_date / to_datetime helpers accept both, and every verifier
goes through them rather than comparing raw cells.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from consequence.diff import AUDIT_TABLE, StateDiff
from consequence.environment import TABLES, Snapshot

DIFFICULTIES = ("easy", "medium", "hard", "trap")

# Tables a task may declare in allowed_tables. audit_log is excluded: every
# write through the tool layer appends to it, so it's never a task's choice.
MODIFIABLE_TABLES = frozenset(TABLES) - {AUDIT_TABLE}

ONLY_ALLOWED_TABLES_CHECK = "only_allowed_tables_modified"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any


RowKey = tuple[str, Any]  # (table, primary key)


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    checks: list[Check]
    message: str
    # Rows this task legitimately may touch, as (table, pk). Consumed by
    # consequence.collateral: anything the episode touched outside this set is
    # collateral damage. Inserted rows (new approvals, flags) have ids that
    # don't exist in `before`, so verifiers derive their part of this from the
    # diff -- e.g. "new flag rows on a target invoice".
    in_scope: frozenset[RowKey]

    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    def check(self, name: str) -> Check:
        return next(c for c in self.checks if c.name == name)


Verifier = Callable[[Snapshot, Snapshot, StateDiff], VerifyResult]


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    difficulty: str
    instruction: str  # the exact text given to the agent, verbatim
    verify: Verifier
    allowed_tables: frozenset[str]  # tables this task is permitted to modify
    notes: str  # why this task exists, what it probes

    def __post_init__(self) -> None:
        if self.difficulty not in DIFFICULTIES:
            raise ValueError(f"{self.id}: difficulty must be one of {DIFFICULTIES}")
        unknown = set(self.allowed_tables) - MODIFIABLE_TABLES
        if unknown:
            raise ValueError(f"{self.id}: allowed_tables names unknown tables {sorted(unknown)}")
        if not self.instruction.strip():
            raise ValueError(f"{self.id}: instruction must not be empty")

    # consequence.harness drives any object with .task_id and .instructions;
    # these keep that interface without the harness knowing about verifiers.
    @property
    def task_id(self) -> str:
        return self.id

    @property
    def instructions(self) -> str:
        return self.instruction


# -- helpers for verifiers ---------------------------------------------------


def rows_by_id(snapshot: Snapshot, table: str) -> dict[int, dict[str, Any]]:
    return {row["id"]: row for row in snapshot.structure.get(table, [])}


def added_rows(state_diff: StateDiff, table: str) -> list[dict[str, Any]]:
    return [row for t, _, row in state_diff.added if t == table]


def changed_rows(state_diff: StateDiff, table: str) -> dict[int, dict[str, tuple[Any, Any]]]:
    return {pk: cols for t, pk, cols in state_diff.changed if t == table}


def to_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def to_datetime(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str | None) -> set[Decimal]:
    """Every number written in `text`, as Decimals ("$1,250.00" -> 1250.00).

    Decimal equality ignores scale, so "212.5" and "212.50" compare equal.
    """
    return {Decimal(m.replace(",", "")) for m in _NUMBER_RE.findall(text or "")}


def check(name: str, passed: bool, expected: Any, actual: Any) -> Check:
    return Check(name=name, passed=bool(passed), expected=expected, actual=actual)


def only_allowed_tables(state_diff: StateDiff, allowed_tables: Iterable[str]) -> Check:
    """The collateral-damage net: nothing outside allowed_tables was touched.

    audit_log is ignored (see diff.AUDIT_TABLE). Within an allowed table, each
    verifier still asserts which rows may change.
    """
    allowed = set(allowed_tables)
    outside = sorted(
        {(table, pk) for table, pk in state_diff.touched() if table not in allowed}
    )
    return check(
        ONLY_ALLOWED_TABLES_CHECK,
        not outside,
        expected=f"changes only in {sorted(allowed)}",
        actual=outside,
    )


def inserted_keys(
    state_diff: StateDiff, table: str, keep: Callable[[dict[str, Any]], bool]
) -> set[RowKey]:
    """(table, pk) of every row inserted into `table` for which keep(row) is true."""
    return {(table, row["id"]) for row in added_rows(state_diff, table) if keep(row)}


def build_result(checks: list[Check], *, in_scope: Iterable[RowKey]) -> VerifyResult:
    """in_scope is keyword-only and required: every verifier must declare it."""
    failed = [c.name for c in checks if not c.passed]
    message = (
        f"all {len(checks)} checks passed"
        if not failed
        else f"{len(failed)}/{len(checks)} checks failed: {', '.join(failed)}"
    )
    return VerifyResult(
        passed=not failed, checks=checks, message=message, in_scope=frozenset(in_scope)
    )
