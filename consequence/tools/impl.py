"""Implementations of the typed tools -- the only write path to the DB.

Every tool call goes through call_tool(): arguments are validated against
the tool's typed schema *before* any SQL runs, every value that reaches the
database goes through a parameterized query placeholder (never string
interpolation of a model-supplied value), and every successful write
appends exactly one audit_log row. A bad call never raises up through
call_tool() -- it comes back as {"ok": False, "error": ...}, a normal tool
result the model can read and react to.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from consequence.db import connect
from consequence.tools.schema import TOOLS_BY_NAME, ToolDef

DEFAULT_LIMIT = 50
AGENT_ACTOR = "agent"

# Matches the seed data's vat_number style (e.g. "VAT-556677"): letters,
# digits and hyphens, 4-32 characters, not starting with a hyphen.
_VAT_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{2,31}$")

_TYPE_CHECKERS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


class ToolError(Exception):
    """A bad argument or a business-rule refusal.

    Raised by validation and by the tool implementations below, and always
    caught inside call_tool() -- it never propagates out and kills a run.
    """


def _validate_args(tool_def: ToolDef, args: dict[str, Any]) -> dict[str, Any]:
    """Check args against tool_def's typed schema. Touches no state at all.

    Only known parameters, of the declared type, satisfying enum/min/max,
    survive. Missing-but-required raises. A present value of None is
    treated as "not supplied" (that's how every WRITE tool's optional
    fields signal "leave unchanged").
    """
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")

    known = {p.name for p in tool_def.parameters}
    unknown = set(args) - known
    if unknown:
        raise ToolError(f"unknown argument(s): {', '.join(sorted(unknown))}")

    validated: dict[str, Any] = {}
    for param in tool_def.parameters:
        if param.name not in args or args[param.name] is None:
            if param.required:
                raise ToolError(f"missing required argument: {param.name}")
            continue

        value = args[param.name]
        if not _TYPE_CHECKERS[param.type](value):
            raise ToolError(
                f"argument '{param.name}' must be of type {param.type}, "
                f"got {type(value).__name__}"
            )
        if param.enum is not None and value not in param.enum:
            raise ToolError(
                f"argument '{param.name}' must be one of {list(param.enum)}, got {value!r}"
            )
        if param.minimum is not None and value < param.minimum:
            raise ToolError(f"argument '{param.name}' must be >= {param.minimum}, got {value}")
        if param.maximum is not None and value > param.maximum:
            raise ToolError(f"argument '{param.name}' must be <= {param.maximum}, got {value}")

        validated[param.name] = value

    return validated


def _json_safe(value: Any) -> Any:
    """Make one cell JSON-serializable: Decimal/date/datetime -> str."""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


def _row_dict(columns: list[str], row: tuple) -> dict[str, Any]:
    return {col: _json_safe(val) for col, val in zip(columns, row, strict=True)}


def _next_id(conn, table: str) -> int:
    """The next id for a table with no sequence (ids are assigned explicitly).

    `table` is always one of our own fixed constants below, never a
    model-supplied value, so building this one query string is not the
    "string interpolation of a model-supplied value" the write tools must
    avoid -- every value that came from the model still goes through a
    parameter placeholder.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}")
        return cur.fetchone()[0]


def _record_audit(conn, table_name: str, row_id: int, action: str) -> None:
    audit_id = _next_id(conn, "audit_log")
    conn.execute(
        "INSERT INTO audit_log (id, table_name, row_id, action, actor, at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (audit_id, table_name, row_id, action, AGENT_ACTOR, datetime.now(UTC)),
    )


# -- READ ------------------------------------------------------------------


def _query_invoices(args: dict[str, Any]) -> dict[str, Any]:
    limit = args.get("limit", DEFAULT_LIMIT)

    conditions: list[str] = []
    params: list[Any] = []
    if "supplier_name" in args:
        conditions.append("s.name ILIKE %s")
        params.append(f"%{args['supplier_name']}%")
    if "status" in args:
        conditions.append("i.status = %s")
        params.append(args["status"])
    if "min_total" in args:
        conditions.append("i.total >= %s")
        params.append(args["min_total"])
    if "max_total" in args:
        conditions.append("i.total <= %s")
        params.append(args["max_total"])
    if "date_from" in args:
        conditions.append("i.invoice_date >= %s")
        params.append(args["date_from"])
    if "date_to" in args:
        conditions.append("i.invoice_date <= %s")
        params.append(args["date_to"])
    if "number" in args:
        conditions.append("i.number = %s")
        params.append(args["number"])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    select_cols = (
        "i.id, i.supplier_id, s.name AS supplier_name, i.number, i.invoice_date, "
        "i.total, i.vat, i.status, i.created_at"
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM invoices i JOIN suppliers s ON s.id = i.supplier_id {where}",
            params,
        )
        total_count = cur.fetchone()[0]

        cur.execute(
            f"SELECT {select_cols} FROM invoices i JOIN suppliers s ON s.id = i.supplier_id "
            f"{where} ORDER BY i.id LIMIT %s",
            [*params, limit],
        )
        columns = [d.name for d in cur.description]
        rows = [_row_dict(columns, r) for r in cur.fetchall()]

    return {"invoices": rows, "total_count": total_count}


def _list_suppliers(args: dict[str, Any]) -> dict[str, Any]:
    conditions: list[str] = []
    params: list[Any] = []
    if "name_contains" in args:
        conditions.append("name ILIKE %s")
        params.append(f"%{args['name_contains']}%")
    if "status" in args:
        conditions.append("status = %s")
        params.append(args["status"])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT * FROM suppliers {where} ORDER BY id", params)
        columns = [d.name for d in cur.description]
        rows = [_row_dict(columns, r) for r in cur.fetchall()]

    return {"suppliers": rows, "total_count": len(rows)}


def _get_invoice(args: dict[str, Any]) -> dict[str, Any]:
    invoice_id = args["invoice_id"]
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE id = %s", (invoice_id,))
        row = cur.fetchone()
        if row is None:
            raise ToolError(f"invoice {invoice_id} not found")
        columns = [d.name for d in cur.description]
        invoice = _row_dict(columns, row)

        cur.execute("SELECT * FROM line_items WHERE invoice_id = %s ORDER BY id", (invoice_id,))
        li_columns = [d.name for d in cur.description]
        invoice["line_items"] = [_row_dict(li_columns, r) for r in cur.fetchall()]

    return invoice


# -- WRITE -------------------------------------------------------------


def _void_invoice(args: dict[str, Any]) -> dict[str, Any]:
    invoice_id = args["invoice_id"]
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,))
        row = cur.fetchone()
        if row is None:
            raise ToolError(f"invoice {invoice_id} not found")
        status = row[0]
        if status == "paid":
            raise ToolError(f"invoice {invoice_id} is paid and cannot be voided")
        if status == "void":
            raise ToolError(f"invoice {invoice_id} is already void")

        cur.execute("UPDATE invoices SET status = %s WHERE id = %s", ("void", invoice_id))
        _record_audit(conn, "invoices", invoice_id, "void_invoice")

    return {"invoice_id": invoice_id, "status": "void"}


def _validate_vat_number(vat_number: str) -> str:
    stripped = vat_number.strip()
    if not stripped:
        raise ToolError("vat_number cannot be empty")
    if not _VAT_NUMBER_RE.match(stripped):
        raise ToolError(f"vat_number is malformed: {vat_number!r}")
    return stripped


def _update_supplier(args: dict[str, Any]) -> dict[str, Any]:
    supplier_id = args["supplier_id"]

    sets: list[str] = []
    params: list[Any] = []
    if "vat_number" in args:
        sets.append("vat_number = %s")
        params.append(_validate_vat_number(args["vat_number"]))
    if "brn" in args:
        sets.append("brn = %s")
        params.append(args["brn"])
    if "status" in args:
        sets.append("status = %s")
        params.append(args["status"])

    if not sets:
        raise ToolError("update_supplier requires at least one of vat_number, brn, status")

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM suppliers WHERE id = %s FOR UPDATE", (supplier_id,))
        if cur.fetchone() is None:
            raise ToolError(f"supplier {supplier_id} not found")

        cur.execute(
            f"UPDATE suppliers SET {', '.join(sets)} WHERE id = %s", [*params, supplier_id]
        )
        _record_audit(conn, "suppliers", supplier_id, "update_supplier")

    return {"supplier_id": supplier_id, "updated_fields": sorted(args.keys() - {"supplier_id"})}


def _create_approval(args: dict[str, Any]) -> dict[str, Any]:
    invoice_id = args["invoice_id"]
    decision = args["decision"]
    reason = args["reason"]

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM invoices WHERE id = %s FOR UPDATE", (invoice_id,))
        row = cur.fetchone()
        if row is None:
            raise ToolError(f"invoice {invoice_id} not found")
        if row[0] == "void":
            raise ToolError(f"invoice {invoice_id} is void and cannot be approved or rejected")

        approval_id = _next_id(conn, "approvals")
        cur.execute(
            "INSERT INTO approvals (id, invoice_id, approver, decision, reason, decided_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (approval_id, invoice_id, AGENT_ACTOR, decision, reason, datetime.now(UTC)),
        )
        _record_audit(conn, "approvals", approval_id, "create_approval")

    return {"approval_id": approval_id, "invoice_id": invoice_id, "decision": decision}


def _flag_for_review(args: dict[str, Any]) -> dict[str, Any]:
    invoice_id = args["invoice_id"]
    reason = args["reason"]

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM invoices WHERE id = %s", (invoice_id,))
        if cur.fetchone() is None:
            raise ToolError(f"invoice {invoice_id} not found")

        flag_id = _next_id(conn, "review_flags")
        cur.execute(
            "INSERT INTO review_flags (id, invoice_id, reason, flagged_at) VALUES (%s, %s, %s, %s)",
            (flag_id, invoice_id, reason, datetime.now(UTC)),
        )
        _record_audit(conn, "review_flags", flag_id, "flag_for_review")

    return {"review_flag_id": flag_id, "invoice_id": invoice_id}


_IMPLS = {
    "query_invoices": _query_invoices,
    "list_suppliers": _list_suppliers,
    "get_invoice": _get_invoice,
    "void_invoice": _void_invoice,
    "update_supplier": _update_supplier,
    "create_approval": _create_approval,
    "flag_for_review": _flag_for_review,
}


def call_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """The one entry point the agent harness calls.

    Always returns {"ok": bool, "data": ..., "error": str | None} and never
    raises: a bad argument or a business-rule refusal is a normal ok=False
    result, not an exception that kills the episode.
    """
    args = args or {}
    tool_def = TOOLS_BY_NAME.get(name)
    if tool_def is None:
        return {"ok": False, "data": None, "error": f"unknown tool: {name}"}

    try:
        validated = _validate_args(tool_def, args)
        data = _IMPLS[name](validated)
    except ToolError as exc:
        return {"ok": False, "data": None, "error": str(exc)}

    return {"ok": True, "data": data, "error": None}
