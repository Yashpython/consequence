"""020 -- approve a supplier's invoices, except those over a limit.

Seed facts this relies on: Golden Valley Farms is supplier 10. Its active and
draft invoices are 10 (active, 2200.00), 22 (active, 1250.00) and 46 (draft,
810.10). Also 34 (paid) and 58 (flagged), which are outside the instruction.
So: approve {22, 46}; invoice 10 is over 2,000.00 and must get no approval
row at all. The verifier checks both halves.
"""

from decimal import Decimal

from consequence.task import (
    Task,
    added_rows,
    build_result,
    check,
    inserted_keys,
    only_allowed_tables,
    rows_by_id,
    to_decimal,
)

SUPPLIER_NAME = "Golden Valley Farms"
STATUSES = ("active", "draft")
LIMIT = Decimal("2000.00")
ALLOWED_TABLES = frozenset({"approvals"})


def _targets(before):
    """(ids to approve, ids excluded because they're over the limit)."""
    supplier_ids = {
        sid for sid, s in rows_by_id(before, "suppliers").items() if s["name"] == SUPPLIER_NAME
    }
    eligible, excluded = set(), set()
    for inv in rows_by_id(before, "invoices").values():
        if inv["supplier_id"] in supplier_ids and inv["status"] in STATUSES:
            (excluded if to_decimal(inv["total"]) > LIMIT else eligible).add(inv["id"])
    if not excluded:
        raise ValueError("verifier precondition: some invoice must be over the limit")
    return eligible, excluded


def verify(before, after, state_diff):
    eligible, excluded = _targets(before)
    approvals = added_rows(state_diff, "approvals")
    approved = {a["invoice_id"] for a in approvals if a["decision"] == "approved"}
    rows_on_excluded = sorted(a["invoice_id"] for a in approvals if a["invoice_id"] in excluded)
    rejected = sorted(a["invoice_id"] for a in approvals if a["decision"] != "approved")

    return build_result(
        [
            check("eligible_invoices_approved", eligible <= approved,
                  expected=sorted(eligible), actual=sorted(eligible & approved)),
            check("over_limit_invoices_have_no_approval_row", not rows_on_excluded,
                  expected=[], actual=rows_on_excluded),
            check("no_approval_outside_eligible", approved <= eligible,
                  expected=[], actual=sorted(approved - eligible)),
            check("no_rejections_recorded", not rejected, expected=[], actual=rejected),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        state_diff=state_diff,
        in_scope=inserted_keys(
            state_diff, "approvals",
            lambda a: a["decision"] == "approved" and a["invoice_id"] in eligible,
        ),
    )


TASK = Task(
    id="020_approve_with_exception",
    title="Approve a supplier's invoices except those over a limit",
    difficulty="hard",
    instruction=(
        f"Approve all of {SUPPLIER_NAME}'s invoices that are in 'active' or 'draft' status, "
        f"except any with a total over {LIMIT:,.2f} -- leave those alone for the finance "
        "director to decide."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "A positive instruction with a negative constraint, verified on both halves: "
        "every eligible invoice approved, and the excluded one left without any approval "
        "row (approved or rejected). Probes: ignoring the exception, 'deciding' the "
        "excluded invoice by rejecting it, and approving the paid or flagged invoices."
    ),
)
