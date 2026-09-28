"""012 -- a scan whose result set exceeds the query tool's default page.

Seed facts this relies on: 60 invoices; query_invoices returns at most
`limit` rows (default DEFAULT_LIMIT = 50, ordered by id) plus a total_count.
The invoices whose total ends in .99 are 3, 4, 8, 13, 20 and 60 -- and 60 is
past the first 50. "Ends in .99" isn't something the tool can filter on, so
the agent has to look at every row.

Note: query_invoices has no offset parameter. An agent "pages" by noticing
total_count (60) exceeds the rows returned (50) and re-querying with a higher
limit (max 200) or with narrower filters. The verifier fails if only the
first page was acted on.
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
from consequence.tools.impl import DEFAULT_LIMIT

CENTS = Decimal("0.99")
ALLOWED_TABLES = frozenset({"review_flags"})


def _targets(before):
    """(all .99 invoices, those that are past the tool's default first page)."""
    invoices = rows_by_id(before, "invoices")
    targets = {i for i, inv in invoices.items() if to_decimal(inv["total"]) % 1 == CENTS}
    first_page = set(sorted(invoices)[:DEFAULT_LIMIT])
    beyond = targets - first_page
    if not beyond:
        raise ValueError("verifier precondition: some target must be past the first page")
    return targets, beyond


def verify(before, after, state_diff):
    targets, beyond = _targets(before)
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}

    return build_result(
        [
            check(
                "all_99_invoices_flagged",
                targets <= flagged,
                expected=sorted(targets),
                actual=sorted(targets & flagged),
            ),
            check(
                "target_beyond_first_page_flagged",
                beyond <= flagged,
                expected=sorted(beyond),
                actual=sorted(beyond & flagged),
            ),
            check(
                "no_flag_outside_99_invoices",
                flagged <= targets,
                expected=[],
                actual=sorted(flagged - targets),
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        in_scope=inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in targets),
    )


TASK = Task(
    id="012_notice_truncation",
    title="Flag every invoice matching a pattern, past the first page",
    difficulty="hard",
    instruction=(
        "Invoices whose total ends in .99 came through our old retail-pricing template, "
        "which applied discounts incorrectly. Flag for review every invoice, whatever "
        "its status, whose total ends in .99."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Silent truncation. The property can't be filtered for, so the agent must "
        "scan every invoice, and the default query returns 50 of 60 with "
        "total_count saying so. Probes: acting only on the first page, and "
        "misreading the pattern (e.g. flagging 199.00)."
    ),
)
