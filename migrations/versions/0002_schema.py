"""schema: customers, keys, versioned pricing, usage, limits, periods, invoices

Revision ID: 0002_schema
Revises: 0001_baseline
Create Date: 2026-09-16

Owner: data-model

Why: the whole billing schema, hand-written. Autogenerate cannot express any of the four
     things that actually matter here -- declarative range partitioning of `usage_events`,
     the GiST exclusion constraint that makes a customer's plan timeline unambiguous, the
     triggers that make an issued invoice and a published price list version immutable, and
     the validation triggers that fire at the moment a thing becomes immutable. Writing the
     ordinary tables by hand too keeps the migration readable as one SQL script rather than
     as a mix of generated calls and escape hatches.

     Named queries each index serves are in `src/meter/storage/models.py`, next to the
     column list, so an index and its justification cannot drift apart.

Locking: **Nothing, in practice.** This migration only CREATEs. Every ACCESS EXCLUSIVE lock
     it takes is on an object that did not exist a statement earlier, so there is nothing to
     contend with -- on a laptop or on a production cluster. `CREATE EXTENSION btree_gist`
     touches only the catalogue. Expected wall time is under a second at any scale, because
     no statement here reads or rewrites a row.

     The locking this design creates for LATER is the part worth stating, because it is
     recurring and it is on the hottest table in the system:

     * `meter_create_usage_partition()` -- run monthly, ahead of the boundary. Takes
       ACCESS EXCLUSIVE on `usage_events` for the catalogue update, AND scans
       `usage_events_default` to prove it holds no row belonging to the new partition.
       That scan is why the default partition must stay empty: empty, this is milliseconds;
       with a month of stray rows in it, it is an outage on the write path. Alert on
       `usage_events_default` being non-empty, and run the function a month early so a
       missing partition never routes traffic there in the first place.
     * `meter_drop_expired_usage_partitions()` -- run after retention elapses. `DROP TABLE`
       on a partition takes ACCESS EXCLUSIVE on the parent for the catalogue update. Brief,
       but it queues behind and ahead of inserts, so it belongs in a maintenance window. The
       lock-free variant is `ALTER TABLE ... DETACH PARTITION CONCURRENTLY` followed by a
       separate `DROP`, which cannot run inside a transaction and therefore cannot live in a
       PL/pgSQL function -- if the brief window ever hurts, that is the change to make.

     ADR-0016 chose partitioning exactly so retention never becomes a `DELETE` of hundreds
     of millions of rows, which would generate dead tuples faster than autovacuum reclaims
     them on the table with the highest write rate here.
"""

import re
from collections.abc import Iterator, Sequence

from alembic import op

revision: str = "0002_schema"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# =======================================================================================
# asyncpg speaks the extended query protocol, which refuses more than one command per
# statement -- so the readable SQL scripts below are split before execution. The split is
# dollar-quote and string aware, because half of this migration is PL/pgSQL function
# bodies full of semicolons and apostrophes. Naive splitting on ";" would shred them.
# =======================================================================================

_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _statements(script: str) -> Iterator[str]:
    """Yield the top-level statements of a SQL script, comments and all."""
    buffer: list[str] = []
    tag: str | None = None
    in_string = False
    in_comment = False
    index = 0
    length = len(script)

    while index < length:
        char = script[index]

        if in_comment:
            buffer.append(char)
            in_comment = char != "\n"
            index += 1
        elif tag is not None:
            if script.startswith(tag, index):
                buffer.append(tag)
                index += len(tag)
                tag = None
            else:
                buffer.append(char)
                index += 1
        elif in_string:
            if char == "'" and script.startswith("''", index):
                buffer.append("''")
                index += 2
            else:
                if char == "'":
                    in_string = False
                buffer.append(char)
                index += 1
        elif script.startswith("--", index):
            in_comment = True
            buffer.append(char)
            index += 1
        elif char == "'":
            in_string = True
            buffer.append(char)
            index += 1
        elif char == "$" and (match := _DOLLAR_TAG.match(script, index)):
            tag = match.group(0)
            buffer.append(tag)
            index += len(tag)
        elif char == ";":
            yield from _emit("".join(buffer))
            buffer = []
            index += 1
        else:
            buffer.append(char)
            index += 1

    yield from _emit("".join(buffer))


def _emit(statement: str) -> Iterator[str]:
    """Drop fragments that are only comments -- they carry nothing to execute."""
    executable = "\n".join(
        line for line in statement.splitlines() if not line.strip().startswith("--")
    )
    if executable.strip():
        yield statement.strip()


# =======================================================================================
# Extensions
# =======================================================================================

EXTENSIONS = """
-- btree_gist lets a GiST exclusion constraint mix an equality operator on a scalar
-- (customer_id) with an overlap operator on a range (effective). Without it,
-- plan_assignments cannot enforce "one customer, no overlapping plan windows".
CREATE EXTENSION IF NOT EXISTS btree_gist;
"""


# =======================================================================================
# Tables
# =======================================================================================

CUSTOMERS = """
CREATE TABLE customers (
    id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name             text        NOT NULL,
    -- ADR-0009: always 'Asia/Karachi' today. The column exists from the first migration
    -- because the ADR is explicit that adding it later means backfilling rows whose
    -- invoices were already issued under an assumed zone.
    billing_timezone text        NOT NULL DEFAULT 'Asia/Karachi',
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_customers_name_not_blank
        CHECK (length(btrim(name)) > 0),
    CONSTRAINT ck_customers_billing_timezone_not_blank
        CHECK (length(btrim(billing_timezone)) > 0)
);
COMMENT ON COLUMN customers.billing_timezone IS
    'IANA zone in which this customer''s billing month and day boundaries are evaluated. '
    'Always Asia/Karachi today (ADR-0009). Validity of the name is checked by application '
    'code; a CHECK cannot consult pg_timezone_names.';
"""

API_KEYS = """
CREATE TABLE api_keys (
    id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid        NOT NULL
        CONSTRAINT fk_api_keys_customer REFERENCES customers (id) ON DELETE RESTRICT,
    key_hash    text        NOT NULL,
    prefix      text        NOT NULL,
    label       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- ADR-0015: revocation is a timestamp, never a DELETE, so a disputed charge can still
    -- be traced to a key that no longer works.
    revoked_at  timestamptz,
    -- Refuses to store anything that is not a hex digest -- including, specifically, a
    -- plaintext key written by a bug. A database leak must not be a key leak.
    CONSTRAINT ck_api_keys_hash_is_sha256_hex
        CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_api_keys_prefix_length
        CHECK (length(prefix) BETWEEN 4 AND 32),
    CONSTRAINT ck_api_keys_revoked_after_created
        CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

-- QUERY: authentication on an auth-cache miss --
--   SELECT customer_id, revoked_at FROM api_keys WHERE key_hash = $1
-- Unique, which also makes two customers sharing a key hash impossible.
CREATE UNIQUE INDEX uq_api_keys_key_hash ON api_keys (key_hash);

-- QUERY: key management --
--   SELECT * FROM api_keys WHERE customer_id = $1 AND revoked_at IS NULL
-- Partial: revoked keys accumulate forever (they are never deleted) and nobody lists them.
CREATE INDEX ix_api_keys_customer_active
    ON api_keys (customer_id) WHERE revoked_at IS NULL;

COMMENT ON COLUMN api_keys.prefix IS
    'Non-secret leading fragment, safe to show in lists and logs. The secret itself is '
    'shown once at creation and never stored (ADR-0015).';
"""

PRICE_LISTS = """
CREATE TABLE price_lists (
    id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_price_lists_name UNIQUE (name),
    CONSTRAINT ck_price_lists_name_not_blank CHECK (length(btrim(name)) > 0)
);
COMMENT ON TABLE price_lists IS
    'ADR-0005. "Starter"/"Growth"/"Scale" are price lists many customers share; a '
    'negotiated deal is a price list with one customer on it. There is no override '
    'mechanism, so rating has exactly one kind of input.';

CREATE TABLE price_list_versions (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    price_list_id     uuid        NOT NULL
        CONSTRAINT fk_price_list_versions_list
        REFERENCES price_lists (id) ON DELETE RESTRICT,
    version           integer     NOT NULL,
    -- Denormalised from price_lists on purpose (ADR-0005): a historical charge must be
    -- labellable without joining to a row that may since have been renamed.
    name              text        NOT NULL,
    monthly_fee_paisa bigint      NOT NULL,
    included_quantity bigint      NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- The immutability switch. NULL = draft, editable. Non-NULL = published, frozen by
    -- trigger and the only state in which anything may reference it.
    published_at      timestamptz,
    CONSTRAINT uq_price_list_versions_list_version UNIQUE (price_list_id, version),
    CONSTRAINT ck_price_list_versions_version_positive
        CHECK (version > 0),
    CONSTRAINT ck_price_list_versions_fee_non_negative
        CHECK (monthly_fee_paisa >= 0),
    CONSTRAINT ck_price_list_versions_included_non_negative
        CHECK (included_quantity >= 0)
);

-- QUERY: "what is the current published version of this list?" --
--   SELECT * FROM price_list_versions WHERE price_list_id = $1 AND published_at IS NOT NULL
--   ORDER BY version DESC LIMIT 1
-- Used by seeding, admin tooling, and the "move these customers to v2" operation.
CREATE INDEX ix_price_list_versions_published
    ON price_list_versions (price_list_id, version) WHERE published_at IS NOT NULL;

CREATE TABLE price_bands (
    id                    uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    price_list_version_id uuid    NOT NULL
        CONSTRAINT fk_price_bands_version
        REFERENCES price_list_versions (id) ON DELETE CASCADE,
    band_index            integer NOT NULL,
    -- Cumulative chargeable units through the END of this band, counted from the first
    -- unit beyond included_quantity. NULL = unbounded. Mirrors meter.domain.plans.Band
    -- exactly, so a row maps to the domain object with no reinterpretation.
    up_to                 bigint,
    unit_price_paisa      bigint  NOT NULL,
    CONSTRAINT uq_price_bands_version_index UNIQUE (price_list_version_id, band_index),
    CONSTRAINT ck_price_bands_index_non_negative CHECK (band_index >= 0),
    CONSTRAINT ck_price_bands_up_to_positive CHECK (up_to IS NULL OR up_to > 0),
    -- ADR-0008 needs the ladder monotonic in quantity, because the spending-limit
    -- inversion depends on it. A negative marginal price would make the inversion
    -- ambiguous. The ADR says forbid it at the price-list level; this is that.
    CONSTRAINT ck_price_bands_unit_price_non_negative CHECK (unit_price_paisa >= 0)
);
"""

PLAN_ASSIGNMENTS = """
CREATE TABLE plan_assignments (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id           uuid        NOT NULL
        CONSTRAINT fk_plan_assignments_customer
        REFERENCES customers (id) ON DELETE RESTRICT,
    price_list_version_id uuid        NOT NULL
        CONSTRAINT fk_plan_assignments_version
        REFERENCES price_list_versions (id) ON DELETE RESTRICT,
    -- Half-open [from, to), open upper bound = still current. Half-open is not a detail:
    -- it is what makes an instant exactly on a plan-change boundary belong to exactly one
    -- segment, which is the whole point of ADR-0006.
    effective             tstzrange   NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_plan_assignments_effective_is_half_open CHECK (
        NOT isempty(effective)
        AND lower(effective) IS NOT NULL
        AND lower_inc(effective)
        AND NOT upper_inc(effective)
    ),
    -- THE invariant of this table, and the reason it is a range and not two columns:
    -- one customer cannot have two overlapping plan windows. "Which plan was customer C
    -- on at instant T?" must return at most one row, and a database that can return two
    -- is a database that will eventually price a request twice or not at all.
    --
    -- QUERY: segment resolution --
    --   SELECT * FROM plan_assignments
    --    WHERE customer_id = $1 AND effective @> $2::timestamptz
    -- served by the GiST index this constraint creates, so resolution costs nothing
    -- beyond the constraint we wanted anyway.
    CONSTRAINT ex_plan_assignments_no_overlap
        EXCLUDE USING gist (customer_id WITH =, effective WITH &&)
);
COMMENT ON TABLE plan_assignments IS
    'ADR-0006. Gaps in a customer''s timeline are legal -- not yet signed up, or churned -- '
    'and are a real state, not an error. Overlaps are not legal and are refused by the '
    'database.';
"""

BILLING_PERIODS = """
CREATE TABLE billing_periods (
    id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id          uuid        NOT NULL
        CONSTRAINT fk_billing_periods_customer
        REFERENCES customers (id) ON DELETE RESTRICT,
    -- The LOCAL first-of-month naming this period (2026-09-01 = September in Asia/Karachi).
    -- A label, never an instant.
    period_month         date        NOT NULL,
    -- The RESOLVED UTC boundaries. ADR-0009 asks for these to be persisted rather than
    -- re-derived: a timezone conversion between an aggregation and its index is a
    -- sequential scan waiting to happen.
    period_start         timestamptz NOT NULL,
    period_end           timestamptz NOT NULL,
    -- ADR-0010's "reconcile, then issue" state machine.
    status               text        NOT NULL DEFAULT 'open',
    reconciled_at        timestamptz,
    closed_at            timestamptz,
    invoiced_at          timestamptz,
    -- ADR-0010's loud fallback: if the grace window elapses before reconciliation
    -- converges, the shortfall is recorded here rather than nowhere.
    discrepancy_requests bigint,
    discrepancy_note     text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_billing_periods_customer_month UNIQUE (customer_id, period_month),
    CONSTRAINT ck_billing_periods_status
        CHECK (status IN ('open', 'reconciling', 'closed', 'invoiced')),
    CONSTRAINT ck_billing_periods_range_ordered
        CHECK (period_end > period_start),
    CONSTRAINT ck_billing_periods_month_is_first
        CHECK (EXTRACT(day FROM period_month) = 1),
    CONSTRAINT ck_billing_periods_closed_at_matches_status
        CHECK ((status IN ('closed', 'invoiced')) = (closed_at IS NOT NULL)),
    CONSTRAINT ck_billing_periods_invoiced_at_matches_status
        CHECK ((status = 'invoiced') = (invoiced_at IS NOT NULL))
);

-- QUERY: the month-close sweep --
--   SELECT * FROM billing_periods WHERE period_month = $1 AND status <> 'invoiced'
-- One month across all customers, which is the shape of the close/invoice job.
CREATE INDEX ix_billing_periods_month_status ON billing_periods (period_month, status);

COMMENT ON COLUMN billing_periods.status IS
    'open -> reconciling -> closed -> invoiced (ADR-0010). A closed period still ACCEPTS '
    'late usage rows -- they are recorded against the period they were incurred in, which '
    'is what makes roll-forward possible -- it simply stops counting them toward the total '
    'that was invoiced. That exclusion is a query, not a constraint, because the rows must '
    'survive.';
"""

USAGE_EVENTS = """
-- Native enum on this table only. Every other status column in this schema is text+CHECK,
-- which is easier to evolve on a small table. usage_events is the exception that pays for
-- the rigidity: an enum is 4 bytes where 'unauthenticated' is 16, and at the design target
-- this table holds ~1e9 rows inside the 90-day retention window. Adding a value later is
-- ALTER TYPE ... ADD VALUE, which rewrites nothing.
CREATE TYPE usage_outcome AS ENUM (
    'success',          -- 2xx: billable (ADR-0007)
    'client_error',     -- 4xx caused by the customer's request: billable
    'unauthenticated',  -- 401/403: never billable; billing it is an attack vector
    'server_error',     -- our 5xx: never billable
    'limit_refused'     -- refused for hitting the spending limit: never billable
);

-- bigint from a shared sequence rather than a random uuid: 8 bytes not 16, and monotonic,
-- so the primary key index writes at its right-hand edge instead of dirtying a random leaf
-- page per insert. (Identity columns on partitioned tables need PG17; an explicit sequence
-- is the PG16 spelling of the same thing.)
CREATE SEQUENCE usage_events_id_seq AS bigint;

CREATE TABLE usage_events (
    id                   bigint      NOT NULL DEFAULT nextval('usage_events_id_seq'),
    -- PARTITION KEY. The resolved start of the billing period this request falls in
    -- (ADR-0009). Constant for every row in a partition, which is why no non-unique index
    -- below repeats it.
    billing_period_start timestamptz NOT NULL,
    -- No foreign keys on this table. See the comment below the CREATE for why.
    customer_id          uuid        NOT NULL,
    api_key_id           uuid        NOT NULL,
    -- When we served it: the instant that decides the period and the proration segment.
    occurred_at          timestamptz NOT NULL,
    -- When the row became durable. occurred_at..received_at is the capture lag, and a
    -- received_at past the period's closed_at is exactly ADR-0010's roll-forward case.
    received_at          timestamptz NOT NULL DEFAULT now(),
    status_code          smallint    NOT NULL,
    outcome              usage_outcome NOT NULL,
    -- The DECISION made at capture time, not derived from `outcome` at read time.
    -- ADR-0007 says the status-code list is maintained deliberately, which means it will
    -- change -- and a change to the rule must never retroactively re-bill history. The
    -- billability decision is part of the record, for the same reason the price list
    -- version is.
    billable             boolean     NOT NULL,
    idempotency_key      text        NOT NULL,

    CONSTRAINT pk_usage_events PRIMARY KEY (billing_period_start, id),

    -- QUERY: idempotent capture --
    --   INSERT ... ON CONFLICT (idempotency_key, billing_period_start) DO NOTHING
    -- The one thing standing between a retried flush and a double-counted request.
    -- A unique index on a partitioned table must contain the partition key, so
    -- uniqueness is PER PERIOD, not global. That is the right grain, and it is safe:
    -- the period is derived from occurred_at, which is fixed when the event is created,
    -- so every retry of one event resolves to the same partition. The corollary the hot
    -- path must honour is that the key is minted WITH the event, never per attempt.
    CONSTRAINT uq_usage_events_idempotency UNIQUE (idempotency_key, billing_period_start),

    CONSTRAINT ck_usage_events_status_code_is_http
        CHECK (status_code BETWEEN 100 AND 599),
    CONSTRAINT ck_usage_events_idempotency_key_length
        CHECK (length(idempotency_key) BETWEEN 8 AND 128)
) PARTITION BY RANGE (billing_period_start);

ALTER SEQUENCE usage_events_id_seq OWNED BY usage_events.id;

-- QUERY: aggregation and dispute --
--   SELECT ... FROM usage_events
--    WHERE customer_id = $1 AND occurred_at >= $2 AND occurred_at < $3
-- Both the rollup pass and support's "which requests made up this line?".
-- billing_period_start is deliberately NOT in the key: it is constant within a partition
-- and would be 8 dead bytes in every entry of the biggest index in the system.
CREATE INDEX ix_usage_events_customer_occurred ON usage_events (customer_id, occurred_at);

-- Deliberately ABSENT: an index on api_key_id. "Which of my keys caused this spike?" is
-- answered from usage_rollups, whose grain includes api_key_id precisely so that this
-- table does not need a third index (ADR-0016 chose that grain generously for this
-- reason). Also absent: an index on received_at for finding late arrivals -- the
-- reconciliation pass already scans by (customer_id, occurred_at) and can filter.

COMMENT ON TABLE usage_events IS
    'Per-request usage, retained 90 days (ADR-0016). Partitioned monthly by billing period '
    'so expiry is a DROP TABLE, never a mass DELETE -- a DELETE of hundreds of millions of '
    'rows generates dead tuples faster than autovacuum reclaims them, on the table with the '
    'highest write rate in the system. '
    'NO FOREIGN KEYS, on purpose: an FK is a per-row index probe plus a row-share lock on '
    'the parent, on a path that wants multi-row INSERT or COPY at a few thousand rows per '
    'second. Neither parent is ever deleted -- customers are not deleted and key revocation '
    'is a timestamp (ADR-0015) -- so the reference cannot dangle. This is the one place '
    'referential integrity is traded for write throughput, and it is traded knowingly. '
    'NO PLAN OR PRICE LIST VERSION either: resolving the segment at capture time would put '
    'a lookup on the request path and would freeze an answer that a later backdated '
    'assignment correction should change. Attribution happens at aggregation time against '
    'plan_assignments, which is what its exclusion constraint is for.';
"""

USAGE_PARTITION_FUNCTIONS = """
-- How partitions get created going forward.
--
-- This function is the ONLY sanctioned way to add a partition: it derives the bounds from
-- the Asia/Karachi month so they line up exactly with the billing_period_start values that
-- will be written into them, and it is idempotent so a scheduler can call it blindly.
-- `pipeline` calls it for next month as part of the month-close job; ops can also call it
-- by hand. Call it EARLY -- a month ahead is cheap, and see the Locking note above for why
-- being late is not.
CREATE OR REPLACE FUNCTION meter_create_usage_partition(p_month date)
RETURNS text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_tz    constant text := 'Asia/Karachi';  -- ADR-0009
    v_month date;
    v_start timestamptz;
    v_end   timestamptz;
    v_name  text;
BEGIN
    v_month := date_trunc('month', p_month)::date;
    v_start := (v_month::timestamp) AT TIME ZONE v_tz;
    v_end   := ((v_month + interval '1 month')::timestamp) AT TIME ZONE v_tz;
    v_name  := format('usage_events_%s', to_char(v_month, 'YYYY_MM'));

    IF to_regclass(format('public.%I', v_name)) IS NOT NULL THEN
        RETURN v_name;
    END IF;

    EXECUTE format(
        'CREATE TABLE %I PARTITION OF usage_events FOR VALUES FROM (%L) TO (%L)',
        v_name, v_start, v_end
    );
    RETURN v_name;
END;
$fn$;

-- ADR-0016's retention, as a partition drop. Never touches usage_events_default (which
-- does not match the name pattern) -- a row in the default partition is an incident to
-- investigate, not data to silently discard.
CREATE OR REPLACE FUNCTION meter_drop_expired_usage_partitions(p_retain_days integer DEFAULT 90)
RETURNS SETOF text
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_tz     constant text := 'Asia/Karachi';
    v_cutoff timestamptz := now() - make_interval(days => p_retain_days);
    v_month  date;
    v_end    timestamptz;
    r        record;
BEGIN
    FOR r IN
        SELECT child.relname AS name
          FROM pg_inherits i
          JOIN pg_class child  ON child.oid  = i.inhrelid
          JOIN pg_class parent ON parent.oid = i.inhparent
         WHERE parent.relname = 'usage_events'
           AND child.relname ~ '^usage_events_[0-9]{4}_[0-9]{2}$'
         ORDER BY child.relname
    LOOP
        v_month := to_date(right(r.name, 7), 'YYYY_MM');
        v_end   := ((v_month + interval '1 month')::timestamp) AT TIME ZONE v_tz;
        IF v_end <= v_cutoff THEN
            EXECUTE format('DROP TABLE %I', r.name);
            RETURN NEXT r.name;
        END IF;
    END LOOP;
END;
$fn$;
"""

USAGE_PARTITIONS = """
-- A DEFAULT partition, because on this table a failed INSERT is a lost billable request.
-- Without it, a row whose period has no partition raises "no partition of relation ...
-- found" and the request goes unbilled -- the one thing this schema exists to prevent.
-- It must stay EMPTY: a non-empty default makes the next CREATE TABLE ... PARTITION OF
-- scan it under ACCESS EXCLUSIVE. Treat any row here as a paging alert.
CREATE TABLE usage_events_default PARTITION OF usage_events DEFAULT;

-- This month and next, so the system is writable the moment the migration lands and stays
-- writable across the next boundary without operator action. Derived from the clock on
-- purpose: a migration that hard-codes 2026-09 is stale the day it is applied anywhere
-- else, and partition bounds are operational state, not schema.
SELECT meter_create_usage_partition((now() AT TIME ZONE 'Asia/Karachi')::date);
SELECT meter_create_usage_partition(
    ((now() AT TIME ZONE 'Asia/Karachi')::date + interval '1 month')::date
);
"""

INVOICES = """
CREATE TABLE invoices (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id       uuid        NOT NULL
        CONSTRAINT fk_invoices_customer REFERENCES customers (id) ON DELETE RESTRICT,
    billing_period_id uuid        NOT NULL
        CONSTRAINT fk_invoices_period REFERENCES billing_periods (id) ON DELETE RESTRICT,
    invoice_number    text        NOT NULL,
    -- There is no 'void'. ADR-0013 rejected void-and-reissue: the customer has already
    -- seen the number, so voiding does not undo it, it only removes our record of what
    -- they saw. A correction is a second document.
    status            text        NOT NULL DEFAULT 'draft',
    total_paisa       bigint      NOT NULL DEFAULT 0,
    issued_at         timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_invoices_customer_period UNIQUE (customer_id, billing_period_id),
    CONSTRAINT uq_invoices_number UNIQUE (invoice_number),
    CONSTRAINT ck_invoices_status CHECK (status IN ('draft', 'issued')),
    CONSTRAINT ck_invoices_issued_at CHECK ((status = 'issued') = (issued_at IS NOT NULL)),
    CONSTRAINT ck_invoices_total_non_negative CHECK (total_paisa >= 0)
);

-- QUERY: Finance's month view --
--   SELECT * FROM invoices WHERE billing_period_id = $1 AND status = 'issued'
CREATE INDEX ix_invoices_period_status ON invoices (billing_period_id, status);

CREATE TABLE invoice_lines (
    id                    uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id            uuid    NOT NULL
        CONSTRAINT fk_invoice_lines_invoice REFERENCES invoices (id) ON DELETE CASCADE,
    line_number           integer NOT NULL,
    kind                  text    NOT NULL,
    -- The sentence Support reads to the customer on the phone.
    description           text    NOT NULL,
    quantity              bigint  NOT NULL,
    unit_price_paisa      bigint  NOT NULL,
    amount_paisa          bigint  NOT NULL,
    -- Everything below is what makes the charge re-derivable from scratch rather than
    -- taken on trust (ADR-0005, ADR-0006).
    price_list_version_id uuid    NOT NULL
        CONSTRAINT fk_invoice_lines_version
        REFERENCES price_list_versions (id) ON DELETE RESTRICT,
    plan_assignment_id    uuid
        CONSTRAINT fk_invoice_lines_assignment
        REFERENCES plan_assignments (id) ON DELETE RESTRICT,
    band_index            integer,
    -- ADR-0010 roll-forward: a prior-period line names the period the usage was incurred
    -- in and is priced at THAT period's version. This is why an invoice cannot assume one
    -- period or one price list version.
    usage_period_id       uuid
        CONSTRAINT fk_invoice_lines_usage_period
        REFERENCES billing_periods (id) ON DELETE RESTRICT,

    -- QUERY: rendering an invoice --
    --   SELECT * FROM invoice_lines WHERE invoice_id = $1 ORDER BY line_number
    -- Served by this unique index, which is why there is no separate index on invoice_id.
    CONSTRAINT uq_invoice_lines_number UNIQUE (invoice_id, line_number),

    CONSTRAINT ck_invoice_lines_kind
        CHECK (kind IN ('monthly_fee', 'usage', 'prior_period_usage')),
    CONSTRAINT ck_invoice_lines_number_positive CHECK (line_number > 0),
    CONSTRAINT ck_invoice_lines_quantity_non_negative CHECK (quantity >= 0),
    CONSTRAINT ck_invoice_lines_unit_price_non_negative CHECK (unit_price_paisa >= 0),
    -- The cheapest possible test of "does this line's arithmetic hold?", run on every
    -- write. Catches the class of bug where a rating change moves an amount without
    -- moving what it claims to be a multiplication of.
    CONSTRAINT ck_invoice_lines_amount_is_product
        CHECK (amount_paisa = quantity * unit_price_paisa),
    CONSTRAINT ck_invoice_lines_prior_period_has_period
        CHECK ((kind = 'prior_period_usage') = (usage_period_id IS NOT NULL)),
    CONSTRAINT ck_invoice_lines_band_index CHECK (band_index IS NULL OR band_index >= 0)
);
"""

USAGE_ROLLUPS = """
CREATE TABLE usage_rollups (
    id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id           uuid        NOT NULL
        CONSTRAINT fk_usage_rollups_customer REFERENCES customers (id) ON DELETE RESTRICT,
    billing_period_id     uuid        NOT NULL
        CONSTRAINT fk_usage_rollups_period
        REFERENCES billing_periods (id) ON DELETE RESTRICT,
    -- LOCAL calendar date in the customer's billing timezone (ADR-0009), not a UTC date.
    -- They differ by five hours, and the difference is a whole day of usage at the edges.
    usage_date            date        NOT NULL,
    price_list_version_id uuid        NOT NULL
        CONSTRAINT fk_usage_rollups_version
        REFERENCES price_list_versions (id) ON DELETE RESTRICT,
    -- The proration segment (ADR-0006), resolved at aggregation time from plan_assignments.
    plan_assignment_id    uuid        NOT NULL
        CONSTRAINT fk_usage_rollups_assignment
        REFERENCES plan_assignments (id) ON DELETE RESTRICT,
    -- ADR-0016: "the grain should be chosen generously now, because widening it later only
    -- helps future periods". This is the generosity -- it is what keeps "which of my keys
    -- caused March?" answerable in April, after the per-request rows are gone.
    api_key_id            uuid        NOT NULL
        CONSTRAINT fk_usage_rollups_api_key REFERENCES api_keys (id) ON DELETE RESTRICT,
    billable_requests     bigint      NOT NULL DEFAULT 0,
    non_billable_requests bigint      NOT NULL DEFAULT 0,
    -- How "no request goes unbilled" becomes checkable at the storage layer: a rollup with
    -- no invoice belongs to nobody's bill yet. Also the mechanism behind ADR-0010's
    -- roll-forward -- late usage for a closed period is simply an unbilled rollup that the
    -- next invoice run picks up, still carrying its original price list version.
    invoice_id            uuid
        CONSTRAINT fk_usage_rollups_invoice REFERENCES invoices (id) ON DELETE RESTRICT,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),

    -- QUERY: the aggregation upsert --
    --   INSERT ... ON CONFLICT (customer_id, usage_date, plan_assignment_id,
    --                           price_list_version_id, api_key_id) DO UPDATE SET ...
    -- This index IS the rollup grain. Re-running an aggregation pass over the same day
    -- must land on the same row rather than create a second one. It also serves
    -- "customer C's usage by day", the long-term half of the live usage view.
    CONSTRAINT uq_usage_rollups_grain UNIQUE (
        customer_id, usage_date, plan_assignment_id, price_list_version_id, api_key_id
    ),
    CONSTRAINT ck_usage_rollups_counts_non_negative
        CHECK (billable_requests >= 0 AND non_billable_requests >= 0)
);

-- QUERY: invoice generation --
--   SELECT * FROM usage_rollups WHERE billing_period_id = $1 AND customer_id = $2
-- Period leads because the invoice run sweeps one period across all customers.
CREATE INDEX ix_usage_rollups_period_customer
    ON usage_rollups (billing_period_id, customer_id);

-- QUERY: roll-forward, and the unbilled-revenue alarm --
--   SELECT * FROM usage_rollups WHERE customer_id = $1 AND invoice_id IS NULL
-- Partial: once a period is invoiced its rollups leave this index for good, so it stays
-- roughly the size of one open month however long the system runs.
CREATE INDEX ix_usage_rollups_unbilled
    ON usage_rollups (customer_id, billing_period_id) WHERE invoice_id IS NULL;

COMMENT ON TABLE usage_rollups IS
    'ADR-0016. Retained long-term and written by pipeline at aggregation time -- never '
    'derived from usage_events at query time, because after 90 days there is nothing to '
    'derive from. A rollup bug found on day 91 is unrecoverable for that period.';
"""

SPENDING_LIMITS = """
CREATE TABLE spending_limits (
    id                              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id                     uuid        NOT NULL
        CONSTRAINT fk_spending_limits_customer REFERENCES customers (id) ON DELETE RESTRICT,
    billing_period_id               uuid        NOT NULL
        CONSTRAINT fk_spending_limits_period
        REFERENCES billing_periods (id) ON DELETE RESTRICT,
    -- ADR-0012: caps the WHOLE bill, monthly fee included.
    limit_paisa                     bigint      NOT NULL,
    -- ADR-0008's inversion: the request count at which the limit is first reached. NULL
    -- until the background job computes it; the limit exists the instant it is set.
    threshold_requests              bigint,
    threshold_computed_at           timestamptz,
    -- What the inversion was computed against. Not decoration: ADR-0008 calls a missed
    -- recomputation "the worst kind" of failure because everything looks fine. Recording
    -- the inputs is what makes staleness detectable instead of invisible.
    threshold_price_list_version_id uuid
        CONSTRAINT fk_spending_limits_version
        REFERENCES price_list_versions (id) ON DELETE RESTRICT,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_spending_limits_customer_period UNIQUE (customer_id, billing_period_id),
    CONSTRAINT ck_spending_limits_limit_positive CHECK (limit_paisa > 0),
    CONSTRAINT ck_spending_limits_threshold_non_negative
        CHECK (threshold_requests IS NULL OR threshold_requests >= 0),
    CONSTRAINT ck_spending_limits_threshold_has_timestamp
        CHECK ((threshold_requests IS NULL) = (threshold_computed_at IS NULL))
);

-- QUERY: the threshold-refresh sweep --
--   SELECT * FROM spending_limits WHERE billing_period_id = $1 AND threshold_requests IS NULL
-- Partial on the pathological case, which should always be empty; a non-empty result is a
-- silent enforcement failure in progress.
CREATE INDEX ix_spending_limits_needs_threshold
    ON spending_limits (billing_period_id) WHERE threshold_requests IS NULL;

COMMENT ON TABLE spending_limits IS
    'ADR-0012''s "a limit below the monthly fee is rejected when it is set" is NOT a '
    'constraint here. It needs the customer''s PRORATED fee for the period -- a join plus a '
    'domain calculation -- and it must reach the customer as a validation message naming '
    'the fee, not as a constraint violation. Application code owns that rule.';
"""


# =======================================================================================
# The defences: triggers the database enforces when application code has a bug.
# =======================================================================================

TRIGGERS = """
-- ---------------------------------------------------------------------------------------
-- TRUNCATE bypasses row-level triggers entirely. Anything protected below is also
-- protected from being emptied.
-- ---------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meter_refuse_truncate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION '% may not be truncated: it holds records that are immutable once '
                    'issued or published', TG_TABLE_NAME
        USING ERRCODE = '23514';
END;
$fn$;

-- ---------------------------------------------------------------------------------------
-- ADR-0013: an issued invoice is immutable. Application code is not the last line of
-- defence; this is. A correction is a credit note -- a second document -- never an edit.
-- ---------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meter_invoices_immutable()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.status = 'issued' THEN
        RAISE EXCEPTION
            'invoice % is issued and immutable; correct it with a credit note, never an '
            'edit (ADR-0013)', OLD.invoice_number
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER trg_invoices_immutable
    BEFORE UPDATE OR DELETE ON invoices
    FOR EACH ROW EXECUTE FUNCTION meter_invoices_immutable();

CREATE TRIGGER trg_invoices_no_truncate
    BEFORE TRUNCATE ON invoices
    FOR EACH STATEMENT EXECUTE FUNCTION meter_refuse_truncate();

CREATE OR REPLACE FUNCTION meter_invoice_lines_immutable()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_status text;
    v_number text;
BEGIN
    -- An UPDATE that moves a line between invoices must satisfy both ends.
    IF TG_OP <> 'INSERT' THEN
        SELECT status, invoice_number INTO v_status, v_number
          FROM invoices WHERE id = OLD.invoice_id;
        IF v_status = 'issued' THEN
            RAISE EXCEPTION
                'invoice % is issued; its lines are immutable (ADR-0013)', v_number
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        SELECT status, invoice_number INTO v_status, v_number
          FROM invoices WHERE id = NEW.invoice_id;
        IF v_status = 'issued' THEN
            RAISE EXCEPTION
                'invoice % is issued; a line cannot be added to or moved onto it '
                '(ADR-0013)', v_number
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$fn$;

CREATE TRIGGER trg_invoice_lines_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON invoice_lines
    FOR EACH ROW EXECUTE FUNCTION meter_invoice_lines_immutable();

CREATE TRIGGER trg_invoice_lines_no_truncate
    BEFORE TRUNCATE ON invoice_lines
    FOR EACH STATEMENT EXECUTE FUNCTION meter_refuse_truncate();

-- Issuing is the moment the number stops being changeable, so it is the moment to check
-- it. After this fires, nothing can fix a total that disagrees with its lines.
CREATE OR REPLACE FUNCTION meter_validate_invoice_on_issue()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_lines bigint;
    v_sum   bigint;
BEGIN
    SELECT count(*), COALESCE(sum(amount_paisa), 0)
      INTO v_lines, v_sum
      FROM invoice_lines WHERE invoice_id = NEW.id;

    IF v_lines = 0 THEN
        RAISE EXCEPTION 'invoice % cannot be issued with no lines', NEW.invoice_number
            USING ERRCODE = '23514';
    END IF;
    IF v_sum <> NEW.total_paisa THEN
        RAISE EXCEPTION
            'invoice % claims a total of % paisa but its lines sum to % paisa',
            NEW.invoice_number, NEW.total_paisa, v_sum
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER trg_invoices_validate_on_issue
    BEFORE UPDATE ON invoices
    FOR EACH ROW WHEN (OLD.status = 'draft' AND NEW.status = 'issued')
    EXECUTE FUNCTION meter_validate_invoice_on_issue();

-- An INSERT straight to 'issued' would otherwise skip the check above.
CREATE TRIGGER trg_invoices_validate_on_insert_issued
    BEFORE INSERT ON invoices
    FOR EACH ROW WHEN (NEW.status = 'issued')
    EXECUTE FUNCTION meter_validate_invoice_on_issue();

-- ---------------------------------------------------------------------------------------
-- ADR-0005: a price list version is immutable once referenced. Enforced here as
-- "immutable once PUBLISHED", which is the same guarantee with a single-row test:
-- a draft cannot be referenced (see meter_require_published_price_version below), so a
-- referenced version is necessarily a published one, and a published one is frozen.
-- The day someone "just fixes a typo" in a band price is the day a past invoice stops
-- being reproducible.
-- ---------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meter_price_list_versions_immutable()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.published_at IS NOT NULL THEN
        RAISE EXCEPTION
            'price list version "% v%" is published and immutable; change a price by '
            'creating a new version (ADR-0005)', OLD.name, OLD.version
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER trg_price_list_versions_immutable
    BEFORE UPDATE OR DELETE ON price_list_versions
    FOR EACH ROW EXECUTE FUNCTION meter_price_list_versions_immutable();

CREATE TRIGGER trg_price_list_versions_no_truncate
    BEFORE TRUNCATE ON price_list_versions
    FOR EACH STATEMENT EXECUTE FUNCTION meter_refuse_truncate();

CREATE OR REPLACE FUNCTION meter_price_bands_immutable()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_published timestamptz;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        SELECT published_at INTO v_published
          FROM price_list_versions WHERE id = OLD.price_list_version_id;
        IF v_published IS NOT NULL THEN
            RAISE EXCEPTION
                'the bands of a published price list version are immutable (ADR-0005)'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        SELECT published_at INTO v_published
          FROM price_list_versions WHERE id = NEW.price_list_version_id;
        IF v_published IS NOT NULL THEN
            RAISE EXCEPTION
                'a band cannot be added to or moved onto a published price list version '
                '(ADR-0005)'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    RETURN OLD;
END;
$fn$;

CREATE TRIGGER trg_price_bands_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON price_bands
    FOR EACH ROW EXECUTE FUNCTION meter_price_bands_immutable();

CREATE TRIGGER trg_price_bands_no_truncate
    BEFORE TRUNCATE ON price_bands
    FOR EACH STATEMENT EXECUTE FUNCTION meter_refuse_truncate();

-- Publishing is the last moment the ladder can be fixed, and the first moment it can be
-- checked as a whole. A per-row CHECK cannot see the other bands; this can. The rules
-- mirror meter.domain.plans.PriceList exactly, so a version the database accepts is a
-- version the rating function accepts.
CREATE OR REPLACE FUNCTION meter_validate_price_bands_on_publish()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    r           record;
    v_seen      integer := 0;
    v_prev_up   bigint  := NULL;
    v_unbounded boolean := false;
BEGIN
    FOR r IN
        SELECT band_index, up_to
          FROM price_bands
         WHERE price_list_version_id = NEW.id
         ORDER BY band_index
    LOOP
        IF v_unbounded THEN
            RAISE EXCEPTION
                'only the final band of "% v%" may be unbounded', NEW.name, NEW.version
                USING ERRCODE = '23514';
        END IF;
        IF r.band_index <> v_seen THEN
            RAISE EXCEPTION
                'band_index of "% v%" must run 0..n-1 with no gaps, found % at position %',
                NEW.name, NEW.version, r.band_index, v_seen
                USING ERRCODE = '23514';
        END IF;
        IF r.up_to IS NULL THEN
            v_unbounded := true;
        ELSE
            IF v_prev_up IS NOT NULL AND r.up_to <= v_prev_up THEN
                RAISE EXCEPTION
                    'band bounds of "% v%" must strictly increase, found % after %',
                    NEW.name, NEW.version, r.up_to, v_prev_up
                    USING ERRCODE = '23514';
            END IF;
            v_prev_up := r.up_to;
        END IF;
        v_seen := v_seen + 1;
    END LOOP;

    IF v_seen = 0 THEN
        RAISE EXCEPTION 'price list version "% v%" needs at least one band',
            NEW.name, NEW.version USING ERRCODE = '23514';
    END IF;
    IF NOT v_unbounded THEN
        RAISE EXCEPTION
            'the final band of "% v%" must be unbounded (up_to IS NULL), or usage beyond '
            'the last bound would be unpriced', NEW.name, NEW.version
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER trg_price_list_versions_validate_on_publish
    BEFORE UPDATE ON price_list_versions
    FOR EACH ROW WHEN (OLD.published_at IS NULL AND NEW.published_at IS NOT NULL)
    EXECUTE FUNCTION meter_validate_price_bands_on_publish();

-- A version inserted already-published would skip the check. It also has no bands yet, so
-- this correctly forces insert-draft / add-bands / publish.
CREATE TRIGGER trg_price_list_versions_validate_on_insert_published
    BEFORE INSERT ON price_list_versions
    FOR EACH ROW WHEN (NEW.published_at IS NOT NULL)
    EXECUTE FUNCTION meter_validate_price_bands_on_publish();

-- The other half of "immutable once referenced": nothing may reference a draft. Without
-- this, the freeze-on-publish rule has a hole -- reference a draft, then edit it, and a
-- past charge silently stops reproducing.
CREATE OR REPLACE FUNCTION meter_require_published_price_version()
RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_column    text := TG_ARGV[0];
    v_id        uuid;
    v_published timestamptz;
BEGIN
    v_id := (to_jsonb(NEW) ->> v_column)::uuid;
    IF v_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT published_at INTO v_published FROM price_list_versions WHERE id = v_id;
    IF v_published IS NULL THEN
        RAISE EXCEPTION
            'price list version % is an unpublished draft and cannot be referenced by '
            '%.% (ADR-0005)', v_id, TG_TABLE_NAME, v_column
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER trg_plan_assignments_require_published
    BEFORE INSERT OR UPDATE ON plan_assignments
    FOR EACH ROW EXECUTE FUNCTION meter_require_published_price_version('price_list_version_id');

CREATE TRIGGER trg_usage_rollups_require_published
    BEFORE INSERT OR UPDATE ON usage_rollups
    FOR EACH ROW EXECUTE FUNCTION meter_require_published_price_version('price_list_version_id');

CREATE TRIGGER trg_invoice_lines_require_published
    BEFORE INSERT OR UPDATE ON invoice_lines
    FOR EACH ROW EXECUTE FUNCTION meter_require_published_price_version('price_list_version_id');

CREATE TRIGGER trg_spending_limits_require_published
    BEFORE INSERT OR UPDATE ON spending_limits
    FOR EACH ROW
    EXECUTE FUNCTION meter_require_published_price_version('threshold_price_list_version_id');

-- ---------------------------------------------------------------------------------------
-- updated_at, maintained by the database so a raw SQL upsert cannot forget it.
-- ---------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meter_touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$fn$;

CREATE TRIGGER trg_usage_rollups_touch_updated_at
    BEFORE UPDATE ON usage_rollups
    FOR EACH ROW EXECUTE FUNCTION meter_touch_updated_at();

CREATE TRIGGER trg_spending_limits_touch_updated_at
    BEFORE UPDATE ON spending_limits
    FOR EACH ROW EXECUTE FUNCTION meter_touch_updated_at();
"""


# Order matters: usage_rollups references invoices, invoice_lines references
# plan_assignments and billing_periods.
_UPGRADE_STEPS = (
    EXTENSIONS,
    CUSTOMERS,
    API_KEYS,
    PRICE_LISTS,
    PLAN_ASSIGNMENTS,
    BILLING_PERIODS,
    USAGE_EVENTS,
    USAGE_PARTITION_FUNCTIONS,
    USAGE_PARTITIONS,
    INVOICES,
    USAGE_ROLLUPS,
    SPENDING_LIMITS,
    TRIGGERS,
)


def upgrade() -> None:
    for step in _UPGRADE_STEPS:
        for statement in _statements(step):
            op.execute(statement)


def downgrade() -> None:
    """Drops the whole schema.

    This exists so the migration path is testable in both directions on a development
    database, and for no other reason. At production scale it is not a recovery mechanism:
    it destroys every invoice Finance has issued and every usage row inside the retention
    window, and no trigger in this file can stop a DROP TABLE. Migrations here are
    forward-only in practice (data-model invariant 7). Recovery from a bad schema change is
    a new forward revision, or a restore.

    The DROPs are ordered by dependency and CASCADE is used only for the partitioned parent,
    where the partitions have no independent existence.
    """
    op.execute("DROP TABLE IF EXISTS spending_limits")
    op.execute("DROP TABLE IF EXISTS usage_rollups")
    op.execute("DROP TABLE IF EXISTS invoice_lines")
    op.execute("DROP TABLE IF EXISTS invoices")
    op.execute("DROP TABLE IF EXISTS usage_events CASCADE")
    op.execute("DROP SEQUENCE IF EXISTS usage_events_id_seq")
    op.execute("DROP TYPE IF EXISTS usage_outcome")
    op.execute("DROP TABLE IF EXISTS billing_periods")
    op.execute("DROP TABLE IF EXISTS plan_assignments")
    op.execute("DROP TABLE IF EXISTS price_bands")
    op.execute("DROP TABLE IF EXISTS price_list_versions")
    op.execute("DROP TABLE IF EXISTS price_lists")
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("DROP TABLE IF EXISTS customers")
    for function in (
        "meter_create_usage_partition(date)",
        "meter_drop_expired_usage_partitions(integer)",
        "meter_refuse_truncate()",
        "meter_invoices_immutable()",
        "meter_invoice_lines_immutable()",
        "meter_validate_invoice_on_issue()",
        "meter_price_list_versions_immutable()",
        "meter_price_bands_immutable()",
        "meter_validate_price_bands_on_publish()",
        "meter_require_published_price_version()",
        "meter_touch_updated_at()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
    # btree_gist is left installed: it is cheap, and 0001_baseline owns extension lifecycle.
