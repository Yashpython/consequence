"""008 -- void the later duplicate, then approve the surviving original.

Seed facts this relies on: the same duplicate pair as 001 -- Acme
Manufacturing Ltd's INV-01-001 exists as invoice 1 (created 09:00) and
invoice 12 (created 15:30). Neither has an approval in the seed. After this
task, 12 is void and 1 carries exactly one new approval.

Order matters for the tools: create_approval refuses a void invoice, but an
agent that approves "INV-01-001" before voiding anything can land the
approval on the duplicate. That approval survives the void and is caught by
no_approval_on_duplicate.
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
    to_datetime,
)

SUPPLIER_NAME = "Acme Manufacturing Ltd"
INVOICE_NUMBER = "INV-01-001"
ALLOWED_TABLES = frozenset({"invoices", "approvals"})


def _pair(before):
    supplier_ids = {
        sid for sid, s in rows_by_id(before, "suppliers").items() if s["name"] == SUPPLIER_NAME
    }
    pair = sorted(
        (
            inv
            for inv in rows_by_id(before, "invoices").values()
            if inv["supplier_id"] in supplier_ids and inv["number"] == INVOICE_NUMBER
        ),
        key=lambda inv: (to_datetime(inv["created_at"]), inv["id"]),
    )
    if len(pair) != 2:
        raise ValueError(f"verifier precondition: expected 2 copies of {INVOICE_NUMBER}")
    return pair[0]["id"], pair[1]["id"]


def verify(before, after, state_diff):
    original, duplicate = _pair(before)
    after_invoices = rows_by_id(after, "invoices")
    changed = changed_rows(state_diff, "invoices")
    approvals = added_rows(state_diff, "approvals")
    on_original = [a for a in approvals if a["invoice_id"] == original]
    on_duplicate = sorted(a["id"] for a in approvals if a["invoice_id"] == duplicate)
    elsewhere = sorted(a["invoice_id"] for a in approvals
                       if a["invoice_id"] not in (original, duplicate))

    return build_result(
        [
            check(
                "later_duplicate_voided",
                after_invoices.get(duplicate, {}).get("status") == "void",
                expected={"invoice": duplicate, "status": "void"},
                actual={"invoice": duplicate,
                        "status": after_invoices.get(duplicate, {}).get("status")},
            ),
            check(
                "original_not_modified",
                original not in changed,
                expected={},
                actual=changed.get(original, {}),
            ),
            check(
                "original_approved_exactly_once",
                len(on_original) == 1 and on_original[0]["decision"] == "approved",
                expected=[{"invoice": original, "decision": "approved"}],
                actual=[{"invoice": original, "decision": a["decision"]} for a in on_original],
            ),
            check("no_approval_on_duplicate", not on_duplicate, expected=[],
                  actual=on_duplicate),
            check("no_approval_on_other_invoices", not elsewhere, expected=[],
                  actual=elsewhere),
            check(
                "no_other_invoice_changed",
                set(changed) <= {duplicate},
                expected=[duplicate],
                actual=sorted(changed),
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ],
        state_diff=state_diff,
        in_scope={("invoices", duplicate)}
        | inserted_keys(
            state_diff,
            "approvals",
            lambda a: a["invoice_id"] == original and a["decision"] == "approved",
        ),
    )


TASK = Task(
    id="008_void_duplicate_then_approve",
    title="Void a duplicate, then approve the original",
    difficulty="medium",
    instruction=(
        f"{SUPPLIER_NAME}'s invoice number {INVOICE_NUMBER} was accidentally entered "
        "twice. Void the copy that was entered later, then record an approval on the "
        "original so it can be paid."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Two dependent writes where the order matters. Both copies share a number, "
        "so any step that looks up 'INV-01-001' without disambiguating can land on "
        "either row. Probes: approving before voiding (approval lands on the "
        "duplicate), voiding the original and approving the duplicate, approving "
        "twice, and stopping after the void."
    ),
)
