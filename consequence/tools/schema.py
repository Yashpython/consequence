"""Provider-neutral tool definitions.

This module describes *what tools exist and what arguments they take* --
nothing here is coupled to any model provider's SDK or wire format. An
adapter for a specific API (Anthropic tool_use, OpenAI function calling,
whatever comes next) translates a ToolDef into that API's shape by reading
`.json_schema()`; this module doesn't know or care which adapters exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

JSONType = str  # one of: "string", "integer", "number", "boolean"


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: JSONType
    description: str = ""
    required: bool = False
    enum: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None

    def json_schema(self) -> dict:
        """This parameter's contribution to a JSON Schema `properties` entry."""
        prop: dict = {"type": self.type}
        if self.description:
            prop["description"] = self.description
        if self.enum is not None:
            prop["enum"] = list(self.enum)
        if self.minimum is not None:
            prop["minimum"] = self.minimum
        if self.maximum is not None:
            prop["maximum"] = self.maximum
        return prop


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = field(default_factory=tuple)

    def json_schema(self) -> dict:
        """A plain JSON Schema object describing this tool's arguments.

        Provider adapters read this (plus .name / .description) and
        reshape it into whatever that provider's tool-calling API expects.
        """
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.parameters},
            "required": [p.name for p in self.parameters if p.required],
            "additionalProperties": False,
        }

    def param(self, name: str) -> ToolParameter | None:
        return next((p for p in self.parameters if p.name == name), None)


INVOICE_STATUSES = ("draft", "active", "void", "paid", "flagged")
SUPPLIER_STATUSES = ("active", "suspended")
APPROVAL_DECISIONS = ("approved", "rejected")

TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        name="query_invoices",
        description="Search invoices by supplier name, status, total range, date range, "
        "or invoice number. Returns at most `limit` rows plus a total_count.",
        parameters=(
            ToolParameter("supplier_name", "string", "Substring match on supplier name"),
            ToolParameter("status", "string", "Exact invoice status", enum=INVOICE_STATUSES),
            ToolParameter("min_total", "number", "Minimum invoice total, inclusive", minimum=0),
            ToolParameter("max_total", "number", "Maximum invoice total, inclusive", minimum=0),
            ToolParameter("date_from", "string", "Minimum invoice_date, inclusive (YYYY-MM-DD)"),
            ToolParameter("date_to", "string", "Maximum invoice_date, inclusive (YYYY-MM-DD)"),
            ToolParameter("number", "string", "Exact match on invoice number"),
            ToolParameter(
                "limit", "integer", "Max rows to return (1-200)", minimum=1, maximum=200
            ),
        ),
    ),
    ToolDef(
        name="list_suppliers",
        description="List suppliers, optionally filtered by name substring and/or status.",
        parameters=(
            ToolParameter("name_contains", "string", "Substring match on supplier name"),
            ToolParameter("status", "string", "Exact supplier status", enum=SUPPLIER_STATUSES),
        ),
    ),
    ToolDef(
        name="get_invoice",
        description="Fetch one invoice by id, including its line items.",
        parameters=(ToolParameter("invoice_id", "integer", "Invoice id", required=True),),
    ),
    ToolDef(
        name="void_invoice",
        description="Void an invoice. Fails if the invoice is 'paid' or already 'void'.",
        parameters=(
            ToolParameter("invoice_id", "integer", "Invoice id", required=True),
            ToolParameter("reason", "string", "Why this invoice is being voided", required=True),
        ),
    ),
    ToolDef(
        name="update_supplier",
        description="Update a supplier's vat_number, brn, and/or status.",
        parameters=(
            ToolParameter("supplier_id", "integer", "Supplier id", required=True),
            ToolParameter("vat_number", "string", "New VAT number; rejected if malformed"),
            ToolParameter("brn", "string", "New business registration number"),
            ToolParameter("status", "string", "New supplier status", enum=SUPPLIER_STATUSES),
        ),
    ),
    ToolDef(
        name="create_approval",
        description="Record an approval decision on an invoice. Fails if the invoice is 'void'.",
        parameters=(
            ToolParameter("invoice_id", "integer", "Invoice id", required=True),
            ToolParameter(
                "decision", "string", "approved or rejected", required=True,
                enum=APPROVAL_DECISIONS,
            ),
            ToolParameter("reason", "string", "Why this decision was made", required=True),
        ),
    ),
    ToolDef(
        name="flag_for_review",
        description="Flag an invoice for manual review.",
        parameters=(
            ToolParameter("invoice_id", "integer", "Invoice id", required=True),
            ToolParameter("reason", "string", "Why this invoice needs review", required=True),
        ),
    ),
)

TOOLS_BY_NAME: dict[str, ToolDef] = {t.name: t for t in TOOLS}
