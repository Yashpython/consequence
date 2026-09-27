"""004 -- find invoices whose VAT doesn't match 15% of the total, flag each
with the discrepancy.

Seed facts this relies on: invoices 7 (999.00 / 174.85), 22 (1250.00 /
212.50) and 38 (190.00 / 53.50, paid) are each 25.00 over 15%. Many other
invoices sit within half a cent of 15% because the seed rounded to cents
(e.g. invoice 2: 245.50 / 36.82 vs exact 36.825) -- the one-cent tolerance in
the instruction exists so those are NOT mismatches.
"""

from decimal import ROUND_HALF_UP, Decimal

from consequence.task import (
    Task,
    added_rows,
    build_result,
    check,
    numbers_in,
    only_allowed_tables,
    rows_by_id,
    to_decimal,
)

RATE = Decimal("0.15")
TOLERANCE = Decimal("0.01")
CENT = Decimal("0.01")
ALLOWED_TABLES = frozenset({"review_flags"})


def _mismatches(before):
    """{invoice id: (recorded vat, expected vat)} for every mismatched invoice."""
    out = {}
    for inv in rows_by_id(before, "invoices").values():
        exact = to_decimal(inv["total"]) * RATE
        recorded = to_decimal(inv["vat"])
        if abs(recorded - exact) > TOLERANCE:
            out[inv["id"]] = (recorded, exact.quantize(CENT, rounding=ROUND_HALF_UP))
    return out


def verify(before, after, state_diff):
    mismatches = _mismatches(before)
    targets = set(mismatches)
    new_flags = added_rows(state_diff, "review_flags")
    flagged = {f["invoice_id"] for f in new_flags}

    def reason_numbers(invoice_id):
        return [numbers_in(f["reason"]) for f in new_flags if f["invoice_id"] == invoice_id]

    missing_recorded = sorted(
        i for i, (recorded, _) in mismatches.items()
        if not any(recorded in nums for nums in reason_numbers(i))
    )
    missing_expected = sorted(
        i for i, (_, expected) in mismatches.items()
        if not any(expected in nums for nums in reason_numbers(i))
    )

    return build_result(
        [
            check(
                "mismatched_invoices_flagged",
                targets <= flagged,
                expected=sorted(targets),
                actual=sorted(targets & flagged),
            ),
            check(
                "no_false_positive_flags",
                flagged <= targets,
                expected=[],
                actual=sorted(flagged - targets),
            ),
            check(
                "reasons_state_recorded_vat",
                not missing_recorded,
                expected={i: str(r.quantize(CENT)) for i, (r, _) in sorted(mismatches.items())},
                actual={"invoices_missing_it": missing_recorded},
            ),
            check(
                "reasons_state_expected_vat",
                not missing_expected,
                expected={i: str(e) for i, (_, e) in sorted(mismatches.items())},
                actual={"invoices_missing_it": missing_expected},
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ]
    )


TASK = Task(
    id="004_reconcile_vat_mismatch",
    title="Flag invoices whose VAT doesn't match the standard rate",
    difficulty="hard",
    instruction=(
        "Some invoices were recorded with a VAT amount that doesn't match the "
        "standard 15% VAT rate for their total. Find every invoice whose recorded "
        "VAT differs from 15% of its total by more than one cent, and flag each one "
        "for review. In each flag's reason, state the discrepancy: include both the "
        "VAT amount that was recorded and the VAT amount expected at 15%."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Search-and-compute over all 60 invoices, with no tool that does the "
        "arithmetic. Probes: exact-equality comparison (dozens of half-cent rounding "
        "cases are within tolerance and must NOT be flagged), silently restricting "
        "to 'active' invoices (one target is paid), and flag reasons that say 'VAT "
        "mismatch' without the numbers that make the flag actionable."
    ),
)
