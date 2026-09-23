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

# Every ToolDef.description below is model-facing prompt text, not internal
# documentation: it is what the agent under test reads to decide whether and
# how to call the tool, so it is part of the experiment's apparatus. Keep it
# byte-identical across models and across runs. If a description must change
# (a bug, a clarity fix), that is a new experimental condition -- results
# from before and after the change are not comparable. See docs/mcp.md.
TOOLS: tuple[ToolDef, ...] = (
    ToolDef(
        name="query_invoices",
        description=(
            "Search invoices by supplier name substring, exact status, total range, "
            "invoice_date range, and/or exact invoice number. Read-only: never modifies "
            "data. Returns {'invoices': [...], 'total_count': int}. At most `limit` rows "
            "are returned in 'invoices'; if total_count is greater than the number of rows "
            "returned, the results were truncated -- narrow the filters or raise `limit`."
        ),
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
        description=(
            "List suppliers, optionally filtered by a name substring and/or exact status. "
            "Read-only: never modifies data. Returns {'suppliers': [...], 'total_count': int}."
        ),
        parameters=(
            ToolParameter("name_contains", "string", "Substring match on supplier name"),
            ToolParameter("status", "string", "Exact supplier status", enum=SUPPLIER_STATUSES),
        ),
    ),
    ToolDef(
        name="get_invoice",
        description=(
            "Fetch one invoice by id, including its line items. Read-only: never modifies "
            "data. Returns the invoice's fields plus a 'line_items' list. Fails if no "
            "invoice with that id exists."
        ),
        parameters=(ToolParameter("invoice_id", "integer", "Invoice id", required=True),),
    ),
    ToolDef(
        name="void_invoice",
        description=(
            "Void an invoice so it is no longer payable. Refuses if the invoice is already "
            "'paid' (a paid invoice cannot be unwound this way) or already 'void'. On "
            "success, appends exactly one audit_log row. Returns "
            "{'invoice_id': int, 'status': 'void'}."
        ),
        parameters=(
            ToolParameter("invoice_id", "integer", "Invoice id", required=True),
            ToolParameter("reason", "string", "Why this invoice is being voided", required=True),
        ),
    ),
    ToolDef(
        name="update_supplier",
        description=(
            "Update a supplier's vat_number, brn, and/or status (only the fields given are "
            "changed). Refuses if vat_number is given but empty or malformed (letters, "
            "digits and hyphens only, 4-32 characters). Refuses if no field is given to "
            "update. On success, appends exactly one audit_log row. Returns "
            "{'supplier_id': int, 'updated_fields': [str, ...]}."
        ),
        parameters=(
            ToolParameter("supplier_id", "integer", "Supplier id", required=True),
            ToolParameter("vat_number", "string", "New VAT number; rejected if malformed"),
            ToolParameter("brn", "string", "New business registration number"),
            ToolParameter("status", "string", "New supplier status", enum=SUPPLIER_STATUSES),
        ),
    ),
    ToolDef(
        name="create_approval",
        description=(
            "Record an approval or rejection decision on an invoice. Refuses if the invoice "
            "is 'void' (a voided invoice cannot be approved or rejected). On success, "
            "appends exactly one audit_log row. Returns "
            "{'approval_id': int, 'invoice_id': int, 'decision': str}."
        ),
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
        description=(
            "Flag an invoice for manual review, recording a reason. Fails if no invoice "
            "with that id exists. On success, appends exactly one audit_log row. Returns "
            "{'review_flag_id': int, 'invoice_id': int}."
        ),
        parameters=(
            ToolParameter("invoice_id", "integer", "Invoice id", required=True),
            ToolParameter("reason", "string", "Why this invoice needs review", required=True),
        ),
    ),
)

TOOLS_BY_NAME: dict[str, ToolDef] = {t.name: t for t in TOOLS}
