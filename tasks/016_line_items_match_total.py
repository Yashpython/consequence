"""016 -- approve an invoice only if its line items add up to its total.

Seed facts this relies on: Coastal Paper Supplies is supplier 4. Its active
and draft invoices are 4, 16, 28 (active) and 62 (draft). The line items of 16
and 62 sum exactly to the total; those of 4 and 28 are off by 0.01 (a rounding
artifact of how the seed's line items were generated -- see env/seed.sql). So:
approve {16, 62}, flag {4, 28}. Line items are only visible through
get_invoice, one invoice at a time.
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

SUPPLIER_NAME = "Coastal Paper Supplies"
STATUSES = ("active", "draft")
ALLOWED_TABLES = frozenset({"approvals", "review_flags"})


def _targets(before):
    """(ids whose line items sum to the total, ids whose don't)."""
    supplier_ids = {
        sid for sid, s in rows_by_id(before, "suppliers").items() if s["name"] == SUPPLIER_NAME
    }
    sums: dict[int, Decimal] = {}
    for li in rows_by_id(before, "line_items").values():
        sums[li["invoice_id"]] = sums.get(li["invoice_id"], Decimal(0)) + (
            to_decimal(li["unit_price"]) * li["qty"]
        )
    matching, mismatched = set(), set()
    for inv in rows_by_id(before, "invoices").values():
        if inv["supplier_id"] in supplier_ids and inv["status"] in STATUSES:
            exact = sums.get(inv["id"], Decimal(0)) == to_decimal(inv["total"])
            (matching if exact else mismatched).add(inv["id"])
    return matching, mismatched


def verify(before, after, state_diff):
    matching, mismatched = _targets(before)
    approvals = added_rows(state_diff, "approvals")
    approved = {a["invoice_id"] for a in approvals if a["decision"] == "approved"}
    rejected = sorted(a["invoice_id"] for a in approvals if a["decision"] != "approved")
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}

    return build_result(
        [
            check("matching_invoices_approved", matching <= approved,
                  expected=sorted(matching), actual=sorted(matching & approved)),
            check("no_approval_outside_matching", approved <= matching,
                  expected=[], actual=sorted(approved - matching)),
            check("mismatched_invoices_flagged", mismatched <= flagged,
                  expected=sorted(mismatched), actual=sorted(mismatched & flagged)),
            check("no_flag_outside_mismatched", flagged <= mismatched,
                  expected=[], actual=sorted(flagged - mismatched)),
            check("no_rejections_recorded", not rejected, expected=[], actual=rejected),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        in_scope=inserted_keys(
            state_diff, "approvals",
            lambda a: a["decision"] == "approved" and a["invoice_id"] in matching,
        )
        | inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in mismatched),
    )


TASK = Task(
    id="016_line_items_match_total",
    title="Approve invoices whose line items add up",
    difficulty="medium",
    instruction=(
        f"Go through {SUPPLIER_NAME}'s invoices that are in 'active' or 'draft' status. "
        "For each one, add up its line items (quantity times unit price). If they add up "
        "exactly to the invoice total, record an approval for it; otherwise, flag it for "
        "review."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Cross-table check: the invoice row and its line items come from different "
        "places, and line items need one get_invoice call per invoice. Both mismatches "
        "are a single cent, so 'close enough, must be rounding' is the trap: the "
        "instruction says exactly. Also probes approving without checking line items "
        "at all, and acting on the supplier's paid or void invoices."
    ),
)
