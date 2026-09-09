-- consequence: supplier-invoice back office schema.
--
-- Money is always NUMERIC(12,2), never float/double precision. Binary floating
-- point cannot represent most decimal fractions exactly, so sums and VAT
-- calculations would drift and comparisons in verifiers would be non-deterministic.
-- NUMERIC gives exact decimal arithmetic.
--
-- Every primary key is a plain integer with NO sequence / GENERATED / serial.
-- Seed data assigns ids explicitly so that a restore is byte-identical every
-- time and row ordering is deterministic. Do not convert these to identity
-- columns.
--
-- Foreign keys use ON DELETE RESTRICT explicitly: deleting a referenced row is
-- an error, not a silent cascade. No triggers yet -- audit_log rows are written
-- by the typed tool layer in a later commit.

CREATE TABLE suppliers (
    id          integer PRIMARY KEY,
    name        text    NOT NULL,
    brn         text,
    vat_number  text,
    status      text    NOT NULL CHECK (status IN ('active', 'suspended'))
);

CREATE TABLE invoices (
    id           integer       PRIMARY KEY,
    supplier_id  integer       NOT NULL REFERENCES suppliers (id) ON DELETE RESTRICT,
    number       text          NOT NULL,
    invoice_date date          NOT NULL,
    total        numeric(12,2) NOT NULL,
    vat          numeric(12,2) NOT NULL,
    status       text          NOT NULL
                 CHECK (status IN ('draft', 'active', 'void', 'paid', 'flagged')),
    created_at   timestamptz   NOT NULL
);

CREATE TABLE line_items (
    id          integer       PRIMARY KEY,
    invoice_id  integer       NOT NULL REFERENCES invoices (id) ON DELETE RESTRICT,
    description text          NOT NULL,
    qty         integer       NOT NULL,
    unit_price  numeric(12,2) NOT NULL
);

CREATE TABLE approvals (
    id          integer     PRIMARY KEY,
    invoice_id  integer     NOT NULL REFERENCES invoices (id) ON DELETE RESTRICT,
    approver    text        NOT NULL,
    decision    text        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason      text,
    decided_at  timestamptz NOT NULL
);

CREATE TABLE review_flags (
    id          integer     PRIMARY KEY,
    invoice_id  integer     NOT NULL REFERENCES invoices (id) ON DELETE RESTRICT,
    reason      text        NOT NULL,
    flagged_at  timestamptz NOT NULL
);

CREATE TABLE audit_log (
    id         integer     PRIMARY KEY,
    table_name text        NOT NULL,
    row_id     integer     NOT NULL,
    action     text        NOT NULL,
    actor      text        NOT NULL,
    at         timestamptz NOT NULL
);
