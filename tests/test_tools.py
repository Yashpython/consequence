"""Live-DB tests for the typed tool layer -- the only write path to the DB.

Requires the docker-compose postgres (`make up`); run with `make test-live`.
Every test resets the environment to the seed data first, so tests don't
depend on each other's ordering or leftover state.
"""

from __future__ import annotations

import pytest

from consequence.db import connect
from consequence.diff import diff
from consequence.environment import reset, snapshot
from consequence.tools import call_tool

pytestmark = pytest.mark.live

INJECTION = "'; DROP TABLE invoices; --"


@pytest.fixture(autouse=True)
def _reset_env():
    reset()


def _audit_rows(table_name: str, row_id: int) -> list[tuple]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, row_id, action, actor FROM audit_log "
            "WHERE table_name = %s AND row_id = %s",
            (table_name, row_id),
        )
        return cur.fetchall()


# -- READ tools --------------------------------------------------------


def test_query_invoices_filters_by_supplier_name():
    result = call_tool("query_invoices", {"supplier_name": "Acme"})
    assert result["ok"] is True
    ids = {row["id"] for row in result["data"]["invoices"]}
    # Acme (supplier 1) has the duplicate pair: invoices 1 and 12.
    assert ids == {1, 12}
    assert result["data"]["total_count"] == 2


def test_query_invoices_filters_by_status_and_respects_limit():
    result = call_tool("query_invoices", {"status": "paid", "limit": 3})
    assert result["ok"] is True
    assert result["data"]["total_count"] == 10  # invoices 31-40 are 'paid'
    assert len(result["data"]["invoices"]) == 3
    assert all(row["status"] == "paid" for row in result["data"]["invoices"])


def test_query_invoices_filters_by_total_range():
    result = call_tool("query_invoices", {"min_total": 499.99, "max_total": 500.01})
    assert result["ok"] is True
    ids = {row["id"] for row in result["data"]["invoices"]}
    assert ids == {4, 17, 29}  # totals 499.99, 500.00, 500.01


def test_list_suppliers_filters_by_status():
    result = call_tool("list_suppliers", {"status": "suspended"})
    assert result["ok"] is True
    assert [row["name"] for row in result["data"]["suppliers"]] == ["Northgate Electrical"]
    assert result["data"]["total_count"] == 1


def test_get_invoice_includes_line_items():
    result = call_tool("get_invoice", {"invoice_id": 3})
    assert result["ok"] is True
    assert result["data"]["id"] == 3
    assert result["data"]["supplier_id"] == 3
    assert len(result["data"]["line_items"]) == 4
    assert all(li["invoice_id"] == 3 for li in result["data"]["line_items"])


def test_get_invoice_not_found():
    result = call_tool("get_invoice", {"invoice_id": 999999})
    assert result["ok"] is False
    assert result["data"] is None
    assert "not found" in result["error"]


# -- WRITE tools: happy path, exactly one row changed + one audit row --


def test_void_invoice_mutates_exactly_one_row_and_one_audit_row():
    before = snapshot()
    result = call_tool("void_invoice", {"invoice_id": 2, "reason": "cancelled by supplier"})
    after = snapshot()
    assert result["ok"] is True

    d = diff(before, after)
    assert d.changed == [("invoices", 2, {"status": ("active", "void")})]
    assert d.removed == []
    assert len(d.added) == 1  # exactly one audit_log row
    assert d.added[0][0] == "audit_log"
    assert d.touched() == {("invoices", 2)}

    rows = _audit_rows("invoices", 2)
    assert len(rows) == 1
    assert rows[0] == ("invoices", 2, "void_invoice", "agent")


def test_create_approval_mutates_exactly_one_row_and_one_audit_row():
    before = snapshot()
    result = call_tool(
        "create_approval", {"invoice_id": 41, "decision": "approved", "reason": "looks fine"}
    )
    after = snapshot()
    assert result["ok"] is True

    d = diff(before, after)
    assert d.changed == []
    assert d.removed == []
    added_tables = sorted(t for t, _, _ in d.added)
    assert added_tables == ["approvals", "audit_log"]
    assert d.touched() == {("approvals", result["data"]["approval_id"])}

    rows = _audit_rows("approvals", result["data"]["approval_id"])
    assert len(rows) == 1
    assert rows[0][2] == "create_approval"


def test_flag_for_review_mutates_exactly_one_row_and_one_audit_row():
    before = snapshot()
    result = call_tool("flag_for_review", {"invoice_id": 3, "reason": "needs manual check"})
    after = snapshot()
    assert result["ok"] is True

    d = diff(before, after)
    assert d.changed == []
    assert d.removed == []
    added_tables = sorted(t for t, _, _ in d.added)
    assert added_tables == ["audit_log", "review_flags"]
    assert d.touched() == {("review_flags", result["data"]["review_flag_id"])}


def test_update_supplier_mutates_exactly_one_row_and_one_audit_row():
    before = snapshot()
    result = call_tool("update_supplier", {"supplier_id": 6, "vat_number": "VAT-999000"})
    after = snapshot()
    assert result["ok"] is True

    d = diff(before, after)
    assert d.changed == [("suppliers", 6, {"vat_number": ("VAT-112233", "VAT-999000")})]
    assert d.removed == []
    assert len(d.added) == 1
    assert d.added[0][0] == "audit_log"
    assert d.touched() == {("suppliers", 6)}


# -- business rules: reject, state unchanged -----------------------------


def test_void_invoice_rejects_paid_invoice_and_leaves_state_unchanged():
    before = snapshot()
    result = call_tool("void_invoice", {"invoice_id": 31, "reason": "attempt"})  # 'paid'
    after = snapshot()

    assert result["ok"] is False
    assert "paid" in result["error"]
    assert diff(before, after).is_empty()


def test_void_invoice_rejects_already_void_invoice_and_leaves_state_unchanged():
    before = snapshot()
    result = call_tool("void_invoice", {"invoice_id": 50, "reason": "attempt"})  # already 'void'
    after = snapshot()

    assert result["ok"] is False
    assert "void" in result["error"]
    assert diff(before, after).is_empty()


def test_create_approval_rejects_void_invoice_and_leaves_state_unchanged():
    before = snapshot()
    result = call_tool(
        "create_approval", {"invoice_id": 50, "decision": "approved", "reason": "attempt"}
    )
    after = snapshot()

    assert result["ok"] is False
    assert "void" in result["error"]
    assert diff(before, after).is_empty()


@pytest.mark.parametrize("bad_vat", ["", "   ", "not valid!", "-leading-hyphen"])
def test_update_supplier_rejects_malformed_vat_number_and_leaves_state_unchanged(bad_vat):
    before = snapshot()
    result = call_tool("update_supplier", {"supplier_id": 6, "vat_number": bad_vat})
    after = snapshot()

    assert result["ok"] is False
    assert "vat_number" in result["error"]
    assert diff(before, after).is_empty()


# -- malformed arguments never raise, always ok=False ---------------------


def test_unknown_tool_returns_ok_false():
    result = call_tool("drop_all_tables", {})
    assert result["ok"] is False
    assert result["data"] is None


def test_missing_required_argument_returns_ok_false():
    result = call_tool("void_invoice", {"invoice_id": 2})  # missing reason
    assert result["ok"] is False
    assert "reason" in result["error"]


def test_wrong_type_argument_returns_ok_false():
    result = call_tool("get_invoice", {"invoice_id": "two"})
    assert result["ok"] is False
    assert "type" in result["error"]


def test_unknown_argument_returns_ok_false():
    result = call_tool("get_invoice", {"invoice_id": 2, "as_admin": True})
    assert result["ok"] is False
    assert "unknown argument" in result["error"]


def test_out_of_range_limit_returns_ok_false():
    result = call_tool("query_invoices", {"limit": 0})
    assert result["ok"] is False


def test_invalid_enum_value_returns_ok_false():
    result = call_tool("create_approval", {"invoice_id": 2, "decision": "maybe", "reason": "x"})
    assert result["ok"] is False


# -- SQL-injection-shaped strings are literal text, never executed --------


def test_injection_shaped_supplier_name_is_treated_as_literal_text():
    result = call_tool("query_invoices", {"supplier_name": INJECTION})
    assert result["ok"] is True
    assert result["data"]["total_count"] == 0

    # the database is intact: all 60 invoices and 12 suppliers still exist.
    invoices = call_tool("query_invoices", {"limit": 200})
    assert invoices["data"]["total_count"] == 60
    suppliers = call_tool("list_suppliers", {})
    assert suppliers["data"]["total_count"] == 12


def test_injection_shaped_reason_is_stored_as_literal_text():
    result = call_tool("flag_for_review", {"invoice_id": 3, "reason": INJECTION})
    assert result["ok"] is True

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reason FROM review_flags WHERE id = %s", (result["data"]["review_flag_id"],)
        )
        (stored_reason,) = cur.fetchone()
    assert stored_reason == INJECTION

    # the invoices table still exists and is untouched.
    invoices = call_tool("query_invoices", {"limit": 200})
    assert invoices["data"]["total_count"] == 60


def test_injection_shaped_brn_is_stored_as_literal_text():
    result = call_tool("update_supplier", {"supplier_id": 6, "brn": INJECTION})
    assert result["ok"] is True

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT brn FROM suppliers WHERE id = 6")
        (stored_brn,) = cur.fetchone()
    assert stored_brn == INJECTION

    suppliers = call_tool("list_suppliers", {})
    assert suppliers["data"]["total_count"] == 12
