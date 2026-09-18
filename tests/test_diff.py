"""Pure unit tests for the diff engine. No DB needed -- Snapshots are built by hand."""

from __future__ import annotations

from decimal import Decimal

from consequence.diff import diff
from consequence.environment import Snapshot


def _snap(structure: dict) -> Snapshot:
    # digest is irrelevant to diff(); only .structure is read.
    return Snapshot(structure=structure, digest="irrelevant")


def test_no_change_produces_empty_diff() -> None:
    structure = {
        "invoices": [{"id": 1, "total": "100.00", "status": "active"}],
    }
    before = _snap(structure)
    after = _snap({k: [dict(row) for row in v] for k, v in structure.items()})

    result = diff(before, after)

    assert result.is_empty()
    assert result.added == []
    assert result.removed == []
    assert result.changed == []


def test_single_column_update_detected_with_old_and_new() -> None:
    before = _snap({"invoices": [{"id": 1, "total": "100.00", "status": "draft"}]})
    after = _snap({"invoices": [{"id": 1, "total": "100.00", "status": "active"}]})

    result = diff(before, after)

    assert not result.is_empty()
    assert result.added == []
    assert result.removed == []
    assert result.changed == [("invoices", 1, {"status": ("draft", "active")})]


def test_added_and_removed_rows_classified_correctly() -> None:
    before = _snap({"invoices": [{"id": 1, "status": "active"}]})
    after = _snap(
        {
            "invoices": [
                {"id": 2, "status": "draft"},
            ]
        }
    )

    result = diff(before, after)

    assert result.removed == [("invoices", 1, {"id": 1, "status": "active"})]
    assert result.added == [("invoices", 2, {"id": 2, "status": "draft"})]
    assert result.changed == []


def test_numerically_equal_but_textually_different_numeric_is_not_a_change() -> None:
    before = _snap({"invoices": [{"id": 1, "total": "205.50"}]})
    after = _snap({"invoices": [{"id": 1, "total": "205.5"}]})

    result = diff(before, after)

    assert result.is_empty()


def test_numerically_equal_decimal_instances_are_not_a_change() -> None:
    before = _snap({"invoices": [{"id": 1, "total": Decimal("205.50")}]})
    after = _snap({"invoices": [{"id": 1, "total": Decimal("205.5")}]})

    result = diff(before, after)

    assert result.is_empty()


def test_touched_excludes_audit_log_by_default_and_includes_when_asked() -> None:
    before = _snap(
        {
            "invoices": [{"id": 1, "status": "draft"}],
            "audit_log": [],
        }
    )
    after = _snap(
        {
            "invoices": [{"id": 1, "status": "active"}],
            "audit_log": [{"id": 1, "table_name": "invoices", "row_id": 1}],
        }
    )

    result = diff(before, after)

    assert result.touched() == {("invoices", 1)}
    assert result.touched(include_audit=True) == {("invoices", 1), ("audit_log", 1)}


def test_render_is_stable_and_sorted() -> None:
    before = _snap(
        {
            "invoices": [
                {"id": 2, "status": "active"},
                {"id": 5, "status": "active", "total": "10.00"},
            ],
            "suppliers": [{"id": 1, "status": "active"}],
        }
    )
    after = _snap(
        {
            "invoices": [
                {"id": 2, "status": "void"},
                {"id": 5, "status": "active", "total": "10.00"},
                {"id": 9, "status": "draft"},
            ],
            "suppliers": [],
        }
    )

    result = diff(before, after)
    rendered = result.render()

    expected = (
        "+ invoices#9 {'id': 9, 'status': 'draft'}\n"
        "- suppliers#1 {'id': 1, 'status': 'active'}\n"
        "~ invoices#2.status: 'active' -> 'void'"
    )
    assert rendered == expected

    # Running it twice against the same inputs must be byte-identical.
    assert diff(before, after).render() == rendered
