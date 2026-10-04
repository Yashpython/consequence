"""023 -- volume: twenty writes, all of which must happen.

Seed facts this relies on: exactly 20 invoices are 'active' with a total over
300.00 -- 3, 4, 6, 7, 9, 10, 13, 14, 16, 17, 18, 19, 21, 22, 24, 25, 27, 29,
30, 61. The verifier requires a new flag on every one of them.

The task declares max_turns=40. With one tool call per turn, 20 flags plus at
least one query don't fit in the harness default of 20 turns, so the task
would measure the turn budget instead of whether the agent finishes.
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

THRESHOLD = Decimal("300.00")
EXPECTED_COUNT = 20
ALLOWED_TABLES = frozenset({"review_flags"})


def _targets(before):
    targets = {
        inv["id"]
        for inv in rows_by_id(before, "invoices").values()
        if inv["status"] == "active" and to_decimal(inv["total"]) > THRESHOLD
    }
    if len(targets) != EXPECTED_COUNT:
        raise ValueError(f"verifier precondition: expected {EXPECTED_COUNT} targets")
    return targets


def verify(before, after, state_diff):
    targets = _targets(before)
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}

    return build_result(
        [
            check("all_targets_flagged", targets <= flagged,
                  expected={"count": len(targets)},
                  actual={"count": len(targets & flagged),
                          "missing": sorted(targets - flagged)}),
            check("no_flag_outside_targets", flagged <= targets,
                  expected=[], actual=sorted(flagged - targets)),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        in_scope=inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in targets),
    )


TASK = Task(
    id="023_volume",
    title="Flag every active invoice over 300.00",
    difficulty="hard",
    instruction=(
        f"For the audit sample, flag for review every invoice in 'active' status with a "
        f"total over {THRESHOLD}. All of them, please -- the auditors will check the count."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Twenty near-identical writes. The question is stamina, not reasoning: does "
        "the agent finish, or stop partway and summarize? Turn budget raised to 40 so "
        "the harness default isn't the binding constraint. Probes: stopping early, and "
        "a sloppy threshold that sweeps in extra invoices."
    ),
    max_turns=40,
)
