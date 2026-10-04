"""An offline environment: the exact seed state plus the tool layer's write
semantics, in pure Python. No Postgres.

Used to evaluate verifiers in CI (tests) and to measure adversarial agents
against them (consequence.adversaries) without a database. It is a mirror,
not a replacement: consequence/tools/impl.py remains the authoritative tool
layer, and the live tests run the same scenarios and adversaries through it.
To keep the mirror honest it reuses impl's own argument validation
(_validate_args) and VAT-format check, and implements the same business
rules and errors:
  void_invoice     refuses unknown, 'paid' and already-'void' invoices
  update_supplier  validates vat_number; refuses no-op calls and unknown suppliers
  create_approval  refuses unknown and 'void' invoices
  flag_for_review  refuses unknown invoices
Every successful write appends one audit_log row (actor 'agent'), as impl does.
Read tools are not simulated: they never change state, so they can't change a
verdict.

Cells have the shape a snapshot takes after a JSON round trip: NUMERIC as
normalized strings (like environment._normalize), dates and timestamps as ISO
strings. Verifiers accept that shape (see consequence/task.py).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from consequence.diff import diff
from consequence.environment import SEED_PATH, TABLES, Snapshot
from consequence.tools.impl import AGENT_ACTOR, ToolError, _validate_args, _validate_vat_number
from consequence.tools.schema import TOOLS_BY_NAME

_INSERT = re.compile(r"INSERT INTO (\w+) \(([^)]*)\) VALUES\n(.*?);\n", re.DOTALL)
_TOKEN = re.compile(r"'((?:[^']|'')*)'|(NULL)|(-?\d+\.\d+)|(-?\d+)")
_ROW = re.compile(r"^\s*\((.*)\),?\s*$")


def _parse_value(match: re.Match) -> object:
    quoted, null, decimal, integer = match.groups()
    if quoted is not None:
        return quoted.replace("''", "'")
    if null is not None:
        return None
    if decimal is not None:
        return format(Decimal(decimal).normalize(), "f")
    return int(integer)


def parse_seed(path: Path = SEED_PATH) -> dict[str, list[dict[str, Any]]]:
    """env/seed.sql parsed into {table: rows sorted by id}."""
    sql = "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    ) + "\n"
    structure: dict[str, list[dict[str, Any]]] = {t: [] for t in TABLES}
    for table, cols, body in _INSERT.findall(sql):
        columns = [c.strip() for c in cols.split(",")]
        for line in body.splitlines():
            row = _ROW.match(line)
            if row:
                values = [_parse_value(m) for m in _TOKEN.finditer(row.group(1))]
                if len(values) != len(columns):
                    raise ValueError(f"cannot parse seed row: {line}")
                structure[table].append(dict(zip(columns, values, strict=True)))
    for rows in structure.values():
        rows.sort(key=lambda r: r["id"])
    return structure


def snapshot_of(structure: dict[str, list[dict[str, Any]]]) -> Snapshot:
    canonical = json.dumps(structure, sort_keys=True, default=str)
    return Snapshot(
        structure=copy.deepcopy(structure),
        digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


class OfflineEnvironment:
    """Seed state plus the tool layer's write semantics."""

    AT = "2025-07-01T12:00:00+00:00"  # fixed, so offline runs are deterministic

    def __init__(self, seed: dict[str, list[dict[str, Any]]] | None = None):
        self._seed = seed if seed is not None else parse_seed()
        self.state = copy.deepcopy(self._seed)

    # -- the tool layer --------------------------------------------------------

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Same contract as consequence.tools.call_tool: never raises."""
        tool_def = TOOLS_BY_NAME.get(name)
        if tool_def is None:
            return {"ok": False, "data": None, "error": f"unknown tool: {name}"}
        handler = getattr(self, f"_tool_{name}", None)
        try:
            validated = _validate_args(tool_def, args or {})
            if handler is None:
                raise ToolError(f"{name} is a read tool and is not simulated offline")
            data = handler(validated)
        except ToolError as exc:
            return {"ok": False, "data": None, "error": str(exc)}
        return {"ok": True, "data": data, "error": None}

    def _find(self, table: str, row_id: int) -> dict[str, Any] | None:
        return next((r for r in self.state[table] if r["id"] == row_id), None)

    def _insert(self, table: str, **fields: Any) -> int:
        new_id = max((r["id"] for r in self.state[table]), default=0) + 1
        self.state[table].append({"id": new_id, **fields})
        return new_id

    def _audit(self, table: str, row_id: int, action: str) -> None:
        self._insert("audit_log", table_name=table, row_id=row_id, action=action,
                     actor=AGENT_ACTOR, at=self.AT)

    def _invoice(self, invoice_id: int) -> dict[str, Any]:
        invoice = self._find("invoices", invoice_id)
        if invoice is None:
            raise ToolError(f"invoice {invoice_id} not found")
        return invoice

    def _tool_void_invoice(self, args: dict[str, Any]) -> dict[str, Any]:
        invoice = self._invoice(args["invoice_id"])
        if invoice["status"] == "paid":
            raise ToolError(f"invoice {invoice['id']} is paid and cannot be voided")
        if invoice["status"] == "void":
            raise ToolError(f"invoice {invoice['id']} is already void")
        invoice["status"] = "void"
        self._audit("invoices", invoice["id"], "void_invoice")
        return {"invoice_id": invoice["id"], "status": "void"}

    def _tool_update_supplier(self, args: dict[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in args.items() if k != "supplier_id"}
        if "vat_number" in fields:
            fields["vat_number"] = _validate_vat_number(fields["vat_number"])
        if not fields:
            raise ToolError("update_supplier requires at least one of vat_number, brn, status")
        supplier = self._find("suppliers", args["supplier_id"])
        if supplier is None:
            raise ToolError(f"supplier {args['supplier_id']} not found")
        supplier.update(fields)
        self._audit("suppliers", supplier["id"], "update_supplier")
        return {"supplier_id": supplier["id"], "updated_fields": sorted(fields)}

    def _tool_create_approval(self, args: dict[str, Any]) -> dict[str, Any]:
        invoice = self._invoice(args["invoice_id"])
        if invoice["status"] == "void":
            raise ToolError(
                f"invoice {invoice['id']} is void and cannot be approved or rejected"
            )
        approval_id = self._insert(
            "approvals", invoice_id=invoice["id"], approver=AGENT_ACTOR,
            decision=args["decision"], reason=args["reason"], decided_at=self.AT,
        )
        self._audit("approvals", approval_id, "create_approval")
        return {"approval_id": approval_id, "invoice_id": invoice["id"],
                "decision": args["decision"]}

    def _tool_flag_for_review(self, args: dict[str, Any]) -> dict[str, Any]:
        invoice = self._invoice(args["invoice_id"])
        flag_id = self._insert("review_flags", invoice_id=invoice["id"],
                               reason=args["reason"], flagged_at=self.AT)
        self._audit("review_flags", flag_id, "flag_for_review")
        return {"review_flag_id": flag_id, "invoice_id": invoice["id"]}

    # -- grading ---------------------------------------------------------------

    def snapshots(self) -> tuple[Snapshot, Snapshot]:
        return snapshot_of(self._seed), snapshot_of(self.state)

    def verify(self, task):
        return self.grade(task)[0]

    def grade(self, task):
        """(VerifyResult, CollateralResult) for the current state."""
        from consequence.collateral import check_collateral

        before, after = self.snapshots()
        state_diff = diff(before, after)
        result = task.verify(before, after, state_diff)
        return result, check_collateral(task, state_diff, result)
