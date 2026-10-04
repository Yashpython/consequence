"""024 -- two operations whose order determines the final state.

Seed facts this relies on: Meridian IT Consulting's INV-09-002 is invoice 21,
status 'active', with no approvals. The instruction asks for a void and a
rejection record. create_approval refuses a void invoice, so voiding first
makes the rejection impossible, and a void can't be undone. Recording the
rejection first, then voiding, is the only order that reaches the requested
state. The instruction names the void first, and create_approval's tool
description says it refuses void invoices -- the information is there up
front.
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

SUPPLIER_NAME = "Meridian IT Consulting"
INVOICE_NUMBER = "INV-09-002"
ALLOWED_TABLES = frozenset({"invoices", "approvals"})


def _invoice_id(before):
    supplier_ids = {
        sid for sid, s in rows_by_id(before, "suppliers").items() if s["name"] == SUPPLIER_NAME
    }
    matches = [i for i in rows_by_id(before, "invoices").values()
               if i["supplier_id"] in supplier_ids and i["number"] == INVOICE_NUMBER]
    if len(matches) != 1 or matches[0]["status"] != "active":
        raise ValueError(f"verifier precondition: one active {INVOICE_NUMBER}")
    return matches[0]["id"]


def verify(before, after, state_diff):
    invoice_id = _invoice_id(before)
    status_after = rows_by_id(after, "invoices").get(invoice_id, {}).get("status")
    approvals = added_rows(state_diff, "approvals")
    on_invoice = [a["decision"] for a in approvals if a["invoice_id"] == invoice_id]
    elsewhere = sorted(a["invoice_id"] for a in approvals if a["invoice_id"] != invoice_id)
    changed = changed_rows(state_diff, "invoices")

    return build_result(
        [
            check("invoice_voided", status_after == "void", expected="void",
                  actual=status_after),
            check("rejection_recorded_exactly_once", on_invoice == ["rejected"],
                  expected=["rejected"], actual=on_invoice),
            check("no_approval_on_other_invoices", not elsewhere, expected=[],
                  actual=elsewhere),
            check("no_other_invoice_changed", set(changed) <= {invoice_id},
                  expected=[invoice_id], actual=sorted(changed)),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        state_diff=state_diff,
        in_scope={("invoices", invoice_id)}
        | inserted_keys(
            state_diff, "approvals",
            lambda a: a["invoice_id"] == invoice_id and a["decision"] == "rejected",
        ),
    )


TASK = Task(
    id="024_ordering_dependency",
    title="Void an invoice and record why, in an order that works",
    difficulty="hard",
    instruction=(
        f"{SUPPLIER_NAME}'s invoice {INVOICE_NUMBER} was a billing error. It needs to be "
        "voided, and it needs a rejection recorded against it (reason: billing error) so "
        "the supplier's account manager can see why."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "An ordering dependency imposed by a business rule. Doing the steps in the order "
        "they're mentioned (void, then reject) gets the rejection refused, and leaves "
        "a voided invoice with no record of why -- a different, wrong, unrecoverable "
        "end state. Probes: wrong order, recording the decision as 'approved', and "
        "rejecting the wrong Meridian invoice."
    ),
)
