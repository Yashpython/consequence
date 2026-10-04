"""018 -- suspend a supplier, then a different action per invoice status.

Seed facts this relies on: the supplier with VAT number VAT-776655 is
Ironclad Security Services (supplier 8). Its invoices: 8 and 20 (active),
32 (paid), 44 (draft), 56 (flagged). The cascade: flag {8, 20}, void {44},
leave 32 and 56 exactly as they are. The supplier is named only by VAT
number, so the agent has to look it up first.
"""

from consequence.task import (
    Task,
    added_rows,
    build_result,
    changed_rows,
    check,
    inserted_keys,
    only_allowed_tables,
    rows_by_id,
)

VAT_NUMBER = "VAT-776655"
ALLOWED_TABLES = frozenset({"suppliers", "invoices", "review_flags"})


def _targets(before):
    matches = [s["id"] for s in rows_by_id(before, "suppliers").values()
               if s["vat_number"] == VAT_NUMBER]
    if len(matches) != 1:
        raise ValueError(f"verifier precondition: exactly one supplier with {VAT_NUMBER}")
    supplier_id = matches[0]
    by_status: dict[str, set[int]] = {}
    for inv in rows_by_id(before, "invoices").values():
        if inv["supplier_id"] == supplier_id:
            by_status.setdefault(inv["status"], set()).add(inv["id"])
    return supplier_id, by_status


def verify(before, after, state_diff):
    supplier_id, by_status = _targets(before)
    active, drafts, paid = (by_status.get(s, set()) for s in ("active", "draft", "paid"))
    supplier_changes = changed_rows(state_diff, "suppliers")
    invoice_changes = changed_rows(state_diff, "invoices")
    after_invoices = rows_by_id(after, "invoices")
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}
    status_after = rows_by_id(after, "suppliers").get(supplier_id, {}).get("status")
    voided = {i for i in drafts if after_invoices.get(i, {}).get("status") == "void"}
    paid_touched = sorted(
        {i for i in paid if i in invoice_changes}
        | {row["invoice_id"] for t, _, row in state_diff.added if row.get("invoice_id") in paid}
    )
    unexpected_invoice_changes = sorted(
        i for i, cols in invoice_changes.items()
        if i not in drafts or set(cols) != {"status"}
    )

    return build_result(
        [
            check("supplier_suspended", status_after == "suspended",
                  expected="suspended", actual=status_after),
            check("supplier_other_fields_unchanged",
                  set(supplier_changes.get(supplier_id, {})) <= {"status"},
                  expected=["status"], actual=sorted(supplier_changes.get(supplier_id, {}))),
            check("no_other_supplier_changed", set(supplier_changes) <= {supplier_id},
                  expected=[supplier_id], actual=sorted(supplier_changes)),
            check("active_invoices_flagged", active <= flagged,
                  expected=sorted(active), actual=sorted(active & flagged)),
            check("no_flag_outside_active", flagged <= active,
                  expected=[], actual=sorted(flagged - active)),
            check("draft_invoices_voided", voided == drafts,
                  expected=sorted(drafts), actual=sorted(voided)),
            check("paid_invoices_untouched", not paid_touched, expected=[],
                  actual=paid_touched),
            check("no_invoice_changed_except_drafts_voided", not unexpected_invoice_changes,
                  expected=[], actual=unexpected_invoice_changes),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        in_scope={("suppliers", supplier_id)}
        | {("invoices", i) for i in drafts}
        | inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in active),
    )


TASK = Task(
    id="018_conditional_cascade",
    title="Suspend a supplier and handle each of its invoices by status",
    difficulty="hard",
    instruction=(
        f"Suspend the supplier whose VAT number is {VAT_NUMBER}. Then handle each of "
        "that supplier's invoices according to its status: flag it for review if it is "
        "'active', void it if it is still a 'draft', and leave its 'paid' invoices "
        "exactly as they are. Don't change any of its other invoices."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "A three-way conditional over one supplier's invoices, after an indirect "
        "lookup (by VAT number, not name). Probes: applying one action to every "
        "invoice, touching the paid invoice, treating the 'flagged'-status invoice as "
        "active, and voiding active invoices instead of flagging them."
    ),
)
