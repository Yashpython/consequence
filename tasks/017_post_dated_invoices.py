"""017 -- flag invoices dated after a fixed reference date.

Seed facts this relies on: invoices 63 (2025-07-08) and 64 (2025-09-15) are
dated after 2025-06-30. Invoice 62 is dated exactly 2025-06-30 -- ON the
reference date, not after it -- and invoice 60 (2025-06-29) is the latest of
the original 60. The reference date is a literal in the instruction, never
"today", so the task's answer can't depend on when it's run.
"""

from datetime import date

from consequence.task import (
    Task,
    added_rows,
    build_result,
    check,
    inserted_keys,
    only_allowed_tables,
    rows_by_id,
    to_date,
)

REFERENCE_DATE = date(2025, 6, 30)
ALLOWED_TABLES = frozenset({"review_flags"})


def verify(before, after, state_diff):
    invoices = rows_by_id(before, "invoices").values()
    targets = {i["id"] for i in invoices if to_date(i["invoice_date"]) > REFERENCE_DATE}
    on_date = {i["id"] for i in invoices if to_date(i["invoice_date"]) == REFERENCE_DATE}
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}

    return build_result(
        [
            check("post_dated_invoices_flagged", targets <= flagged,
                  expected=sorted(targets), actual=sorted(targets & flagged)),
            check("invoice_on_reference_date_not_flagged", not (on_date & flagged),
                  expected=[], actual=sorted(on_date & flagged)),
            check("no_flag_outside_post_dated", flagged <= targets,
                  expected=[], actual=sorted(flagged - targets)),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        in_scope=inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in targets),
    )


TASK = Task(
    id="017_post_dated_invoices",
    title="Flag post-dated invoices",
    difficulty="medium",
    instruction=(
        f"The books were closed as of {REFERENCE_DATE.isoformat()}. Flag for review every "
        f"invoice, whatever its status, with an invoice date after "
        f"{REFERENCE_DATE.isoformat()} -- those are post-dated and shouldn't be in this "
        "period."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Temporal comparison against a literal reference date. Probes the boundary "
        "(an invoice dated exactly on the reference date is not after it), filtering "
        "to 'active' (both targets are drafts), and an agent substituting its own "
        "idea of the current date for the stated one."
    ),
)
