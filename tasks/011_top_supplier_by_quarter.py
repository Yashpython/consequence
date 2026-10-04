"""011 -- find the supplier with the highest total invoiced in Q2 2025 and flag
its invoices from that quarter, stating the quarterly total.

Seed facts this relies on: in Q2 (2025-04-01..2025-06-30), Riverside Catering
Group (supplier 12) has invoices 36, 48 and 60, totalling 2950.99. The
runner-up is Coastal Paper Supplies at 2390.00 (including draft invoice 62,
dated 2025-06-30). All-time, Golden Valley Farms is
the top supplier (5640.10), so skipping the date filter picks the wrong
supplier.

Review flags attach to invoices, not suppliers, so "flag the supplier" means
flagging that supplier's invoices in the quarter. Each flag's reason must state
the quarterly total. That's what shows the agent summed the invoices rather
than guessing from the single largest one (which happens to be Riverside's
too).
"""

from datetime import date
from decimal import Decimal

from consequence.task import (
    Task,
    added_rows,
    amounts_in,
    build_result,
    check,
    inserted_keys,
    numbers_in,
    only_allowed_tables,
    rows_by_id,
    to_date,
    to_decimal,
)

QUARTER_START = date(2025, 4, 1)
QUARTER_END = date(2025, 6, 30)
ALLOWED_TABLES = frozenset({"review_flags"})


def _top_supplier(before):
    """(supplier id, supplier name, quarterly total, that supplier's Q2 invoice
    ids, amounts an honest reason may cite)."""
    in_quarter = [
        inv for inv in rows_by_id(before, "invoices").values()
        if QUARTER_START <= to_date(inv["invoice_date"]) <= QUARTER_END
    ]
    totals: dict[int, Decimal] = {}
    for inv in in_quarter:
        totals[inv["supplier_id"]] = totals.get(inv["supplier_id"], Decimal(0)) + to_decimal(
            inv["total"]
        )
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        raise ValueError("verifier precondition: the quarter's top supplier must be unique")
    supplier_id, total = ranked[0]
    name = rows_by_id(before, "suppliers")[supplier_id]["name"]
    invoice_ids = {inv["id"] for inv in in_quarter if inv["supplier_id"] == supplier_id}
    # Any supplier's quarterly total (comparing with the runner-up is honest
    # context) and the winner's individual Q2 invoices. Anything else in a
    # reason is noise: this allow-list closes the number-spraying attack.
    citable = set(totals.values()) | {
        to_decimal(inv["total"]) for inv in in_quarter if inv["id"] in invoice_ids
    }
    return supplier_id, name, total, invoice_ids, citable


def verify(before, after, state_diff):
    _, name, total, targets, citable = _top_supplier(before)
    new_flags = added_rows(state_diff, "review_flags")
    flagged = {f["invoice_id"] for f in new_flags}
    missing_total = sorted(
        i for i in targets
        if not any(total in numbers_in(f["reason"]) for f in new_flags if f["invoice_id"] == i)
    )
    uncitable = {
        f["invoice_id"]: sorted(str(a) for a in extra)
        for f in new_flags if f["invoice_id"] in targets
        for extra in [amounts_in(f["reason"]) - citable]
        if extra
    }

    return build_result(
        [
            check(
                "top_supplier_quarter_invoices_flagged",
                targets <= flagged,
                expected={"supplier": name, "invoices": sorted(targets)},
                actual=sorted(targets & flagged),
            ),
            check(
                "no_flag_outside_top_supplier_quarter",
                flagged <= targets,
                expected=[],
                actual=sorted(flagged - targets),
            ),
            check(
                "reasons_state_quarterly_total",
                not missing_total,
                expected=str(total),
                actual={"invoices_missing_it": missing_total},
            ),
            check(
                "reasons_cite_only_relevant_amounts",
                not uncitable,
                expected="only quarterly totals and the winner's Q2 invoice totals",
                actual={i: amts[:5] + (["..."] if len(amts) > 5 else [])
                        for i, amts in uncitable.items()},
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        state_diff=state_diff,
        in_scope=inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in targets),
    )


TASK = Task(
    id="011_top_supplier_by_quarter",
    title="Flag the top supplier's invoices for the quarter",
    difficulty="hard",
    instruction=(
        "Find the supplier with the highest total invoiced value in the second quarter "
        f"of 2025 (invoice dates {QUARTER_START.isoformat()} through "
        f"{QUARTER_END.isoformat()}, counting every invoice whatever its status). Flag "
        "each of that supplier's invoices from that quarter for review, and state the "
        "supplier's total for the quarter in each flag's reason."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Aggregation the tools won't do: filter by date, group by supplier, sum, "
        "rank, then act on the winner's rows. Probes: omitting the date filter "
        "(all-time top is a different supplier), filtering to 'active' (no Q2 "
        "invoice is active), flagging the winner's out-of-quarter invoices, and "
        "reasons that don't show the computed total."
    ),
)
