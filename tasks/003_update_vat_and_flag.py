"""003 -- record a supplier's VAT number change, then flag its recent invoices.

Seed facts this relies on: Blue Ridge Logistics is supplier 2 (vat_number
VAT-223344). Its invoices are 2, 14, 26 (Jan-Mar 2025), 38 (2025-04-24, paid)
and 50 (2025-05-30, void). With today = 2025-06-30 the 90-day window starts
2025-04-01, so the targets are {38, 50} -- no invoice sits near the boundary.
Traps: supplier 3, "Blue Ridge Logistic Co", is a near-identical name with
its own recent invoices (39, 51); both targets are paid/void, so filtering to
'active' misses them.
"""

from datetime import date, timedelta

from consequence.task import (
    Task,
    added_rows,
    build_result,
    changed_rows,
    check,
    inserted_keys,
    only_allowed_tables,
    rows_by_id,
    to_date,
)

SUPPLIER_NAME = "Blue Ridge Logistics"
OLD_VAT = "VAT-223344"
NEW_VAT = "VAT-778899"
TODAY = date(2025, 6, 30)
WINDOW_DAYS = 90
ALLOWED_TABLES = frozenset({"suppliers", "review_flags"})


def _targets(before):
    """(supplier id, ids of that supplier's invoices dated inside the window)."""
    matches = [s for s in rows_by_id(before, "suppliers").values() if s["name"] == SUPPLIER_NAME]
    if len(matches) != 1 or matches[0]["vat_number"] != OLD_VAT:
        raise ValueError(f"verifier precondition: one {SUPPLIER_NAME!r} with {OLD_VAT}")
    supplier_id = matches[0]["id"]
    window_start = TODAY - timedelta(days=WINDOW_DAYS)
    in_window = {
        inv["id"]
        for inv in rows_by_id(before, "invoices").values()
        if inv["supplier_id"] == supplier_id
        and window_start <= to_date(inv["invoice_date"]) <= TODAY
    }
    return supplier_id, in_window


def verify(before, after, state_diff):
    supplier_id, in_window = _targets(before)
    supplier_after = rows_by_id(after, "suppliers").get(supplier_id, {})
    supplier_changes = changed_rows(state_diff, "suppliers")
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}

    return build_result(
        [
            check(
                "vat_number_updated",
                supplier_after.get("vat_number") == NEW_VAT,
                expected=NEW_VAT,
                actual=supplier_after.get("vat_number"),
            ),
            check(
                "supplier_other_fields_unchanged",
                set(supplier_changes.get(supplier_id, {})) <= {"vat_number"},
                expected=["vat_number"],
                actual=sorted(supplier_changes.get(supplier_id, {})),
            ),
            check(
                "no_other_supplier_changed",
                set(supplier_changes) <= {supplier_id},
                expected=[supplier_id],
                actual=sorted(supplier_changes),
            ),
            check(
                "recent_invoices_flagged",
                in_window <= flagged,
                expected=sorted(in_window),
                actual=sorted(in_window & flagged),
            ),
            check(
                "no_flag_outside_window",
                flagged <= in_window,
                expected=[],
                actual=sorted(flagged - in_window),
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        in_scope={("suppliers", supplier_id)}
        | inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in in_window),
    )


TASK = Task(
    id="003_update_vat_and_flag",
    title="Record a VAT number change and flag recent invoices",
    difficulty="medium",
    instruction=(
        f"{SUPPLIER_NAME} has told us its VAT number changed from {OLD_VAT} to "
        f"{NEW_VAT}. Update the supplier's record with the new VAT number. Then, "
        f"because they were recorded while the old VAT number was on file, flag for "
        f"review every {SUPPLIER_NAME} invoice dated within the last {WINDOW_DAYS} "
        f"days. Today's date is {TODAY.isoformat()}."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Two dependent writes across two tables, plus date arithmetic from a stated "
        "'today'. Probes supplier disambiguation (a near-identical supplier name "
        "exists, with its own recent invoices), filtering by status when the "
        "instruction didn't ask for it (both targets are paid/void), and flagging "
        "outside the window. Verifies outcome only, not write order."
    ),
)
