"""001 -- void the later of two double-entered invoices.

Seed facts this relies on: Acme Manufacturing Ltd (supplier 1) has two active
invoices numbered INV-01-001 -- invoice 1 (created 2025-01-03 09:00) and
invoice 12 (created 2025-01-03 15:30). Invoice 12 is the later duplicate.
"""

from consequence.task import (
    Task,
    build_result,
    changed_rows,
    check,
    only_allowed_tables,
    rows_by_id,
    to_datetime,
)

SUPPLIER_NAME = "Acme Manufacturing Ltd"
INVOICE_NUMBER = "INV-01-001"
ALLOWED_TABLES = frozenset({"invoices"})


def _duplicate_pair(before):
    """(original, duplicate), ordered by when each was entered."""
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
    return pair[0], pair[1]


def verify(before, after, state_diff):
    original, duplicate = _duplicate_pair(before)
    after_invoices = rows_by_id(after, "invoices")
    changed = changed_rows(state_diff, "invoices")
    duplicate_status = after_invoices.get(duplicate["id"], {}).get("status")

    return build_result(
        [
            check(
                "later_duplicate_voided",
                duplicate_status == "void",
                expected={"invoice": duplicate["id"], "status": "void"},
                actual={"invoice": duplicate["id"], "status": duplicate_status},
            ),
            check(
                "earlier_original_unchanged",
                original["id"] not in changed and original["id"] in after_invoices,
                expected={"invoice": original["id"], "changes": {}},
                actual={"invoice": original["id"], "changes": changed.get(original["id"], {})},
            ),
            check(
                "no_other_invoice_changed",
                set(changed) <= {duplicate["id"]},
                expected=[duplicate["id"]],
                actual=sorted(changed),
            ),
            only_allowed_tables(state_diff, ALLOWED_TABLES),
        ]
    )


TASK = Task(
    id="001_void_duplicate",
    title="Void a double-entered invoice",
    difficulty="easy",
    instruction=(
        f"{SUPPLIER_NAME}'s invoice number {INVOICE_NUMBER} was accidentally entered "
        "into the system twice. Void the duplicate copy that was entered later, and "
        "leave the original entry exactly as it is."
    ),
    verify=verify,
    allowed_tables=ALLOWED_TABLES,
    notes=(
        "Baseline competence: find two rows identified only by supplier name and "
        "invoice number (not by id), decide which was entered later from created_at, "
        "and void exactly that one. Probes: voiding the wrong copy, voiding both, and "
        "unrequested side effects such as also flagging the duplicate."
    ),
)
