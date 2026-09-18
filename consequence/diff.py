"""Compares two Snapshots and reports what changed.

Every verifier and the collateral-damage check are built on this, so it has
to be precise about what counts as a change: two NUMERIC values that are
numerically equal but textually different (205.50 vs 205.5) must never show
up as a diff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from consequence.environment import Snapshot

Row = dict[str, Any]
PrimaryKey = Any
Added = tuple[str, PrimaryKey, Row]
Removed = tuple[str, PrimaryKey, Row]
Changed = tuple[str, PrimaryKey, dict[str, tuple[Any, Any]]]

# audit_log rows are an expected side effect of any legitimate write (every
# write via the tool layer appends one), so a diff that included them would
# flag "changed the database" on every single episode regardless of what the
# agent actually did. touched() ignores audit_log by default; pass
# include_audit=True when the audit trail itself is what's under test.
AUDIT_TABLE = "audit_log"


def _comparable(value: Any) -> Any:
    """Canonicalize a value for equality/display so NUMERIC scale doesn't matter.

    Mirrors consequence.environment._normalize, but also accepts values that
    arrive as plain strings (e.g. a Snapshot built by hand in a test) rather
    than Decimal instances straight out of psycopg.
    """
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, str):
        try:
            return format(Decimal(value).normalize(), "f")
        except InvalidOperation:
            return value
    return value


def _rows_by_pk(rows: list[Row]) -> dict[PrimaryKey, Row]:
    return {row["id"]: row for row in rows}


@dataclass(frozen=True)
class StateDiff:
    added: list[Added] = field(default_factory=list)
    removed: list[Removed] = field(default_factory=list)
    changed: list[Changed] = field(default_factory=list)

    def touched(self, include_audit: bool = False) -> set[tuple[str, PrimaryKey]]:
        """Return the set of (table, primary_key) touched in any way.

        audit_log is excluded by default -- see the module comment on
        AUDIT_TABLE.
        """
        keys = {(table, pk) for table, pk, _ in self.added}
        keys |= {(table, pk) for table, pk, _ in self.removed}
        keys |= {(table, pk) for table, pk, _ in self.changed}
        if not include_audit:
            keys = {(table, pk) for table, pk in keys if table != AUDIT_TABLE}
        return keys

    def is_empty(self) -> bool:
        return not self.added and not self.removed and not self.changed

    def render(self) -> str:
        """A compact, deterministic, human-readable report: one line per
        added/removed row and one line per changed cell."""
        lines: list[str] = []

        for table, pk, row in self.added:
            lines.append(f"+ {table}#{pk} {row}")

        for table, pk, row in self.removed:
            lines.append(f"- {table}#{pk} {row}")

        for table, pk, columns in self.changed:
            for column in sorted(columns):
                old, new = columns[column]
                lines.append(f"~ {table}#{pk}.{column}: {old!r} -> {new!r}")

        return "\n".join(lines)


def diff(before: Snapshot, after: Snapshot) -> StateDiff:
    """Compare two Snapshots table by table, row by row, column by column.

    Every list is sorted deterministically by (table, primary_key[, column]).
    """
    added: list[Added] = []
    removed: list[Removed] = []
    changed: list[Changed] = []

    tables = sorted(set(before.structure) | set(after.structure))
    for table in tables:
        before_rows = _rows_by_pk(before.structure.get(table, []))
        after_rows = _rows_by_pk(after.structure.get(table, []))

        for pk in sorted(set(before_rows) - set(after_rows)):
            removed.append((table, pk, before_rows[pk]))

        for pk in sorted(set(after_rows) - set(before_rows)):
            added.append((table, pk, after_rows[pk]))

        for pk in sorted(set(before_rows) & set(after_rows)):
            before_row = before_rows[pk]
            after_row = after_rows[pk]
            columns: dict[str, tuple[Any, Any]] = {}
            for column in sorted(set(before_row) | set(after_row)):
                old = before_row.get(column)
                new = after_row.get(column)
                if _comparable(old) != _comparable(new):
                    columns[column] = (old, new)
            if columns:
                changed.append((table, pk, columns))

    return StateDiff(added=added, removed=removed, changed=changed)
