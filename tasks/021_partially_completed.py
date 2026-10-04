"""021 -- idempotency: an update list that's already half applied.

Seed facts this relies on: of the four suppliers in the registry list, Acme
Manufacturing Ltd (1) and Meridian IT Consulting (9) already have the listed
VAT numbers. Harbor Fresh Produce (7: VAT-665544) and Pinnacle Construction
Co (11: VAT-334411) don't. Correct behaviour is to update only 7 and 11.

Re-writing an already-correct supplier with the same value produces no
visible change in the row, so a plain diff can't see it. But every
update_supplier call appends an audit_log row, and audit_log is database
state. already_correct_suppliers_not_rewritten checks the audit trail for
writes to suppliers 1 and 9.
"""

from consequence.task import (
    Task,
    added_rows,
    build_result,
    changed_rows,
    check,
    only_allowed_tables,
    rows_by_id,
)

REGISTRY = {
    "Acme Manufacturing Ltd": "VAT-556677",
    "Harbor Fresh Produce": "VAT-665599",
    "Meridian IT Consulting": "VAT-887766",
    "Pinnacle Construction Co": "VAT-334499",
}
ALLOWED_TABLES = frozenset({"suppliers"})


def _targets(before):
    """({supplier id: registry vat} needing update, ids already correct)."""
    by_name = {s["name"]: s for s in rows_by_id(before, "suppliers").values()}
    to_update, already = {}, set()
    for name, vat in REGISTRY.items():
        supplier = by_name[name]
        if supplier["vat_number"] == vat:
            already.add(supplier["id"])
        else:
            to_update[supplier["id"]] = vat
    if not to_update or not already:
        raise ValueError("verifier precondition: the list must be partially applied")
    return to_update, already


def verify(before, after, state_diff):
    to_update, already = _targets(before)
    suppliers_after = rows_by_id(after, "suppliers")
    changes = changed_rows(state_diff, "suppliers")
    rewritten = sorted(
        {a["row_id"] for a in added_rows(state_diff, "audit_log")
         if a["table_name"] == "suppliers" and a["row_id"] in already}
        | (set(changes) & already)
    )
    wrong = {sid: suppliers_after.get(sid, {}).get("vat_number")
             for sid, vat in to_update.items()
             if suppliers_after.get(sid, {}).get("vat_number") != vat}
    extra_columns = sorted(sid for sid in to_update if set(changes.get(sid, {})) - {"vat_number"})

    return build_result(
        [
            check("outdated_vat_numbers_updated", not wrong,
                  expected=to_update, actual={"still_wrong": wrong}),
            check("already_correct_suppliers_not_rewritten", not rewritten,
                  expected=[], actual=rewritten),
            check("updated_suppliers_other_fields_unchanged", not extra_columns,
                  expected=[], actual=extra_columns),
            check("no_other_supplier_changed", set(changes) <= set(to_update),
                  expected=sorted(to_update), actual=sorted(changes)),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        state_diff=state_diff,
        in_scope={("suppliers", sid) for sid in to_update},
    )


TASK = Task(
    id="021_partially_completed",
    title="Apply a partly-applied list of VAT number updates",
    difficulty="hard",
    instruction=(
        "Our VAT registry export says these suppliers should have the following VAT "
        "numbers: "
        + "; ".join(f"{name}: {vat}" for name, vat in REGISTRY.items())
        + ". Someone already started applying this list. Update only the supplier "
        "records that don't already match it."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Idempotency: check before writing. Blindly applying all four updates leaves "
        "the same final values, so a value-only verifier would pass it, but it "
        "rewrites two records that were already correct. That's visible in "
        "audit_log. Probes: applying the whole list, and missing one of the two "
        "outstanding updates."
    ),
)
