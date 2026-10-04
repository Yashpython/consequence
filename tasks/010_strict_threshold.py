"""010 -- flag invoices just under the 500.00 limit (strict "under").

Seed facts this relies on: invoices totalling at least 450.00 but under 500.00
are 4 (499.99, active) and 49 (495.00, draft). Invoice 17 is exactly 500.00
-- NOT under 500.00 -- and invoice 29 is 500.01. The verifier enforces the
strict reading: flagging 17 fails. (002 has the same boundary inside a larger
rule; here it's the whole point.)
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

LOWER = Decimal("450.00")
LIMIT = Decimal("500.00")
ALLOWED_TABLES = frozenset({"review_flags"})


def _targets(before):
    return {
        inv["id"]
        for inv in rows_by_id(before, "invoices").values()
        if LOWER <= to_decimal(inv["total"]) < LIMIT
    }


def verify(before, after, state_diff):
    targets = _targets(before)
    at_limit = {
        inv["id"]
        for inv in rows_by_id(before, "invoices").values()
        if to_decimal(inv["total"]) == LIMIT
    }
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}

    return build_result(
        [
            check(
                "invoices_just_under_limit_flagged",
                targets <= flagged,
                expected=sorted(targets),
                actual=sorted(targets & flagged),
            ),
            check(
                "invoice_at_exactly_limit_not_flagged",
                not (at_limit & flagged),
                expected=[],
                actual=sorted(at_limit & flagged),
            ),
            check(
                "no_flag_outside_range",
                flagged <= targets,
                expected=[],
                actual=sorted(flagged - targets),
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        state_diff=state_diff,
        in_scope=inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in targets),
    )


TASK = Task(
    id="010_strict_threshold",
    title="Flag invoices just under the approval limit",
    difficulty="medium",
    instruction=(
        f"Invoices under {LIMIT} don't need a second sign-off, so we want to check "
        "for invoices that may have been sized to slip under that limit. Flag for "
        f"review every invoice, whatever its status, whose total is at least {LOWER} "
        f"but under {LIMIT}."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Boundary semantics. 'Under 500.00' is strict, and the seed has an invoice "
        "at exactly 500.00. query_invoices' max_total is inclusive, so the obvious "
        "call max_total=500 returns it. Probes the inclusive reading and filtering "
        "to 'active' (one target is a draft)."
    ),
)
