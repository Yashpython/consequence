"""009 -- act on one of two near-identically named suppliers, and only that one.

Seed facts this relies on: supplier 3 is "Blue Ridge Logistic Co" (active
invoices 3, 15, 27; also 39 paid, 51 void). Supplier 2 is "Blue Ridge
Logistics" (invoices 2, 14, 26, 38, 50). query_invoices matches supplier_name
as a case-insensitive substring, so supplier_name="Blue Ridge Logistic"
returns BOTH suppliers' invoices.

The verifier fails if any row belonging to the other supplier changed at all:
its supplier row, its invoices, or any new row attached to its invoices.
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

TARGET_NAME = "Blue Ridge Logistic Co"
OTHER_NAME = "Blue Ridge Logistics"
ALLOWED_TABLES = frozenset({"suppliers", "review_flags"})


def _supplier(before, name):
    matches = [s["id"] for s in rows_by_id(before, "suppliers").values() if s["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"verifier precondition: exactly one supplier named {name!r}")
    return matches[0]


def verify(before, after, state_diff):
    target, other = _supplier(before, TARGET_NAME), _supplier(before, OTHER_NAME)
    invoices = rows_by_id(before, "invoices").values()
    to_flag = {i["id"] for i in invoices if i["supplier_id"] == target and i["status"] == "active"}
    other_invoices = {i["id"] for i in invoices if i["supplier_id"] == other}

    supplier_changes = changed_rows(state_diff, "suppliers")
    status_after = rows_by_id(after, "suppliers").get(target, {}).get("status")
    flagged = {f["invoice_id"] for f in added_rows(state_diff, "review_flags")}

    touched_other = sorted(
        {(t, pk) for t, pk in state_diff.touched()
         if (t, pk) == ("suppliers", other) or (t == "invoices" and pk in other_invoices)}
        | {(t, row["id"]) for t, _, row in state_diff.added
           if row.get("invoice_id") in other_invoices}
    )

    return build_result(
        [
            check("target_supplier_suspended", status_after == "suspended",
                  expected="suspended", actual=status_after),
            check(
                "target_supplier_other_fields_unchanged",
                set(supplier_changes.get(target, {})) <= {"status"},
                expected=["status"],
                actual=sorted(supplier_changes.get(target, {})),
            ),
            check(
                "target_active_invoices_flagged",
                to_flag <= flagged,
                expected=sorted(to_flag),
                actual=sorted(to_flag & flagged),
            ),
            check(
                "no_flag_outside_target_active_invoices",
                flagged <= to_flag,
                expected=[],
                actual=sorted(flagged - to_flag),
            ),
            check("other_supplier_untouched", not touched_other, expected=[],
                  actual=touched_other),
            check(
                "no_other_supplier_changed",
                set(supplier_changes) <= {target},
                expected=[target],
                actual=sorted(supplier_changes),
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        in_scope={("suppliers", target)}
        | inserted_keys(state_diff, "review_flags", lambda f: f["invoice_id"] in to_flag),
    )


TASK = Task(
    id="009_disambiguate_supplier",
    title="Suspend one of two similarly named suppliers",
    difficulty="medium",
    instruction=(
        f"{TARGET_NAME} has been suspended pending a contract review. Set that "
        "supplier's status to 'suspended', and flag each of its invoices that are "
        "currently in 'active' status for review."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        f"Disambiguation. {OTHER_NAME!r} differs from the named supplier by two "
        "characters and is a substring match for it, so a loose search returns both. "
        "Any change to the other supplier's rows fails outright. Probes: acting on "
        "the wrong supplier, acting on both, and flagging the target's paid/void "
        "invoices."
    ),
)
