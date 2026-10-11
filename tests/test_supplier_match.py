"""The supplier_name resolution rule in query_invoices, as a pure function --
no database. tests/test_tools.py checks the same rule end to end, live."""

from __future__ import annotations

import pytest

from consequence.tools.impl import ToolError, pick_supplier

BLUE_RIDGE = [(2, "Blue Ridge Logistics"), (3, "Blue Ridge Logistic Co")]


def test_single_candidate_resolves():
    assert pick_supplier("Acme", [(1, "Acme Manufacturing Ltd")]) == 1


def test_no_candidate_is_none():
    assert pick_supplier("nobody", []) is None


def test_several_candidates_are_refused_with_names():
    with pytest.raises(ToolError, match="matches 2 suppliers") as exc:
        pick_supplier("Blue Ridge Logistic", BLUE_RIDGE)
    assert "'Blue Ridge Logistics'" in str(exc.value)
    assert "'Blue Ridge Logistic Co'" in str(exc.value)


def test_exact_full_name_wins_over_longer_names_containing_it():
    """If one supplier's whole name is part of another's, the exact name still
    resolves -- otherwise that supplier could never be queried by name."""
    candidates = [(7, "Northgate"), (8, "Northgate Electrical")]
    assert pick_supplier("Northgate", candidates) == 7
    assert pick_supplier("  northgate ", candidates) == 7  # trimmed, case-insensitive


def test_duplicate_exact_names_are_refused():
    with pytest.raises(ToolError, match="matches 2 suppliers"):
        pick_supplier("Twin", [(1, "Twin"), (2, "twin")])
