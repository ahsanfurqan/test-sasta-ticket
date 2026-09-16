"""ORM models -- the source of schema truth. Owned by data-model. See docs/adr/0003.

House rules, enforced by review and by the database itself:

  * Money columns are BigInteger paisa. Never Numeric, never Float, never Money.
    See docs/adr/0004-money-as-integer-paisa.md.
  * Timestamps are ``DateTime(timezone=True)`` and stored UTC. Billing day and month
    boundaries are evaluated in Asia/Karachi (ADR-0009) and the *resolved* boundary is
    persisted, never re-derived per query.
  * Anything on a write path carries an idempotency key, so a retry after an ambiguous
    failure cannot double-count.

What is NOT expressible here, and therefore lives hand-written in
``migrations/versions/0002_schema.py`` instead:

  * declarative range partitioning of ``usage_events`` and the partition helper functions,
  * the triggers that make an issued invoice and a published price list version immutable,
  * the trigger that validates a price list's band ladder at the moment it is published,
  * the trigger that validates an invoice's totals at the moment it is issued.

Those are the invariants the database defends when application code has a bug. This module
declares the shape; the migration declares the defences. Both are hand-checked.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, UUID, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------------------
# Column helpers. Every money column in this file is BigInteger and every money column
# name ends in _paisa, so "is there a Numeric or a Float in a money path?" is one grep.
# ---------------------------------------------------------------------------------------

_UTC_NOW = func.now()


def _uuid_pk() -> Mapped[str]:
    return mapped_column(
        UUID(as_uuid=False), primary_key=True, server_default=func.gen_random_uuid()
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=_UTC_NOW)


class Base(DeclarativeBase):
    """Declarative base. Alembic autogenerate targets Base.metadata."""


# ---------------------------------------------------------------------------------------
# Customers and API keys
# ---------------------------------------------------------------------------------------


class Customer(Base):
    """A paying customer.

    ``billing_timezone`` carries Asia/Karachi from day one even though ADR-0009 makes it
    the only value in use. The ADR is explicit that storing it now is the cheap half of the
    eventual per-customer-zone migration, and adding the column later means backfilling
    rows whose invoices were already issued under an assumed zone.
    """

    __tablename__ = "customers"

    id: Mapped[str] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    billing_timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="Asia/Karachi"
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="ck_customers_name_not_blank"),
        CheckConstraint(
            "length(btrim(billing_timezone)) > 0", name="ck_customers_billing_timezone_not_blank"
        ),
    )


class ApiKey(Base):
    """One API key. Multiple live per customer at once (ADR-0015).

    The secret is never stored: only a SHA-256 hex digest and a short non-secret prefix for
    identifying the key in lists, logs and support conversations. Revocation is a timestamp,
    never a DELETE, so a charge from a key that no longer exists can still be traced.
    """

    __tablename__ = "api_keys"

    id: Mapped[str] = _uuid_pk()
    customer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.id", ondelete="RESTRICT", name="fk_api_keys_customer"),
        nullable=False,
    )
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Refuses to store anything that is not a hex digest -- including, specifically, a
        # plaintext key written by a bug. Changing hash algorithm is a migration, on purpose.
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="ck_api_keys_hash_is_sha256_hex"),
        CheckConstraint(
            "length(prefix) BETWEEN 4 AND 32", name="ck_api_keys_prefix_length"
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_api_keys_revoked_after_created",
        ),
        # QUERY: authentication. "SELECT customer_id, revoked_at FROM api_keys WHERE
        # key_hash = $1" -- one probe per auth cache miss. The unique index is also what
        # makes two customers sharing a key hash impossible.
        Index("uq_api_keys_key_hash", "key_hash", unique=True),
        # QUERY: key management. "SELECT * FROM api_keys WHERE customer_id = $1 AND
        # revoked_at IS NULL" -- listing a customer's live keys. Partial, because revoked
        # keys accumulate forever and nobody lists them.
        Index(
            "ix_api_keys_customer_active",
            "customer_id",
            postgresql_where="revoked_at IS NULL",
        ),
    )


# ---------------------------------------------------------------------------------------
# Pricing: price lists, versions, bands (ADR-0005)
# ---------------------------------------------------------------------------------------


class PriceList(Base):
    """A named price list. "Growth" is one; a negotiated deal with one customer on it is
    another. There is no override mechanism, so rating has exactly one kind of input."""

    __tablename__ = "price_lists"

    id: Mapped[str] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("name", name="uq_price_lists_name"),
        CheckConstraint("length(btrim(name)) > 0", name="ck_price_lists_name_not_blank"),
    )


class PriceListVersion(Base):
    """One immutable version of a price list.

    ADR-0005 says "immutable once referenced". The schema enforces something slightly
    stronger and far cheaper to check: **immutable once published**. A version is a draft
    until ``published_at`` is set; only a published version may be referenced by a plan
    assignment, a rollup or an invoice line; and a published version can never be updated
    or deleted. "Once referenced" would require the database to count references on every
    UPDATE; "once published" is a single-row test with the same guarantee, because a draft
    is unreferenceable by construction.

    ``name`` is duplicated from the parent list on purpose -- ADR-0005 asks for the
    human-readable name to live on the version so a historical charge can be labelled
    without joining to a row that may since have been renamed.
    """

    __tablename__ = "price_list_versions"

    id: Mapped[str] = _uuid_pk()
    price_list_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("price_lists.id", ondelete="RESTRICT", name="fk_price_list_versions_list"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    monthly_fee_paisa: Mapped[int] = mapped_column(BigInteger, nullable=False)
    included_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("price_list_id", "version", name="uq_price_list_versions_list_version"),
        CheckConstraint("version > 0", name="ck_price_list_versions_version_positive"),
        CheckConstraint(
            "monthly_fee_paisa >= 0", name="ck_price_list_versions_fee_non_negative"
        ),
        CheckConstraint(
            "included_quantity >= 0", name="ck_price_list_versions_included_non_negative"
        ),
        # QUERY: seeding and admin. "which version of 'Growth' is current?" --
        # ORDER BY version DESC LIMIT 1 for a given list, restricted to published.
        Index(
            "ix_price_list_versions_published",
            "price_list_id",
            "version",
            postgresql_where="published_at IS NOT NULL",
        ),
    )


class PriceBand(Base):
    """One marginal band of a price list version.

    ``up_to`` is the CUMULATIVE count of chargeable units through the end of this band,
    counting from the first unit beyond the included allowance; NULL means unbounded. This
    matches ``meter.domain.plans.Band`` exactly, so a row maps to the domain object with no
    reinterpretation.

    Row-level CHECKs cover what one row can know. The ladder-level rules -- exactly one
    unbounded band and it must be last, bounds strictly increasing -- are checked by a
    trigger at the moment the version is published, which is the only moment the whole
    ladder is knowable and the last moment it can still be fixed.
    """

    __tablename__ = "price_bands"

    id: Mapped[str] = _uuid_pk()
    price_list_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("price_list_versions.id", ondelete="CASCADE", name="fk_price_bands_version"),
        nullable=False,
    )
    band_index: Mapped[int] = mapped_column(Integer, nullable=False)
    up_to: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unit_price_paisa: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "price_list_version_id", "band_index", name="uq_price_bands_version_index"
        ),
        CheckConstraint("band_index >= 0", name="ck_price_bands_index_non_negative"),
        CheckConstraint("up_to IS NULL OR up_to > 0", name="ck_price_bands_up_to_positive"),
        # ADR-0008 requires the ladder to be monotonic in quantity, because the spending
        # limit inversion depends on it. A negative unit price would make the ladder
        # non-monotonic and the inversion ambiguous. Forbid it at the price list level,
        # which is where the ADR says it belongs.
        CheckConstraint(
            "unit_price_paisa >= 0", name="ck_price_bands_unit_price_non_negative"
        ),
    )


# ---------------------------------------------------------------------------------------
# Plan assignments (ADR-0006): who is on which price list version, over which time range
# ---------------------------------------------------------------------------------------


class PlanAssignment(Base):
    """A customer on a price list version over a half-open time range.

    ``effective`` is a ``tstzrange`` in canonical ``[from, to)`` form, with an open upper
    bound meaning "still current". Half-open is not a detail: it is what makes an instant
    exactly on a plan-change boundary belong to exactly one segment, which is the whole
    point of ADR-0006.

    Two assignments for the same customer may not overlap -- enforced by a GiST exclusion
    constraint, not by application code. "Resolve the plan in effect at instant T" must
    return at most one row, and a database that can return two is a database that will
    eventually price a request twice or not at all.

    Gaps are legal. A customer with no assignment covering an instant was not on a plan
    then (not yet signed up, or churned), which is a real state and not an error.
    """

    __tablename__ = "plan_assignments"

    id: Mapped[str] = _uuid_pk()
    customer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.id", ondelete="RESTRICT", name="fk_plan_assignments_customer"),
        nullable=False,
    )
    price_list_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey(
            "price_list_versions.id", ondelete="RESTRICT", name="fk_plan_assignments_version"
        ),
        nullable=False,
    )
    effective = mapped_column(TSTZRANGE, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "NOT isempty(effective) "
            "AND lower(effective) IS NOT NULL "
            "AND lower_inc(effective) "
            "AND NOT upper_inc(effective)",
            name="ck_plan_assignments_effective_is_half_open",
        ),
        # QUERY (and invariant): "which plan was customer C on at instant T?" ->
        # WHERE customer_id = $1 AND effective @> $2::timestamptz. The GiST index backing
        # this exclusion constraint serves that lookup, so segment resolution costs nothing
        # beyond the constraint we wanted anyway.
        ExcludeConstraint(
            ("customer_id", "="),
            ("effective", "&&"),
            name="ex_plan_assignments_no_overlap",
            using="gist",
        ),
    )


# ---------------------------------------------------------------------------------------
# Billing periods (ADR-0009, ADR-0010)
# ---------------------------------------------------------------------------------------

BILLING_PERIOD_STATUSES = ("open", "reconciling", "closed", "invoiced")


class BillingPeriod(Base):
    """One customer-month, with the resolved UTC boundaries persisted.

    ADR-0009 stores UTC and evaluates boundaries in Asia/Karachi. Deriving that conversion
    per query would put a timezone function between every aggregation and its index, so the
    resolved instants live here and everything downstream compares plain timestamps.

    ``status`` is where ADR-0010's "reconcile, then issue" records state:
    open -> reconciling -> closed -> invoiced. A closed period still ACCEPTS late usage
    rows -- they are recorded against the period they were incurred in, which is what makes
    roll-forward possible -- it simply no longer counts them toward the total that was
    invoiced. That exclusion is a query, not a constraint, because the rows must survive.

    ``discrepancy_requests`` is the loud part of ADR-0010's fallback: if the grace window
    elapses before reconciliation converges, the shortfall is written here rather than
    nowhere.
    """

    __tablename__ = "billing_periods"

    id: Mapped[str] = _uuid_pk()
    customer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.id", ondelete="RESTRICT", name="fk_billing_periods_customer"),
        nullable=False,
    )
    # The LOCAL first-of-month that names this period, e.g. 2026-09-01 for September in
    # Asia/Karachi. A label, never an instant.
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invoiced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discrepancy_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discrepancy_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("customer_id", "period_month", name="uq_billing_periods_customer_month"),
        CheckConstraint(
            "status IN ('open', 'reconciling', 'closed', 'invoiced')",
            name="ck_billing_periods_status",
        ),
        CheckConstraint("period_end > period_start", name="ck_billing_periods_range_ordered"),
        CheckConstraint(
            "EXTRACT(day FROM period_month) = 1", name="ck_billing_periods_month_is_first"
        ),
        CheckConstraint(
            "(status IN ('closed', 'invoiced')) = (closed_at IS NOT NULL)",
            name="ck_billing_periods_closed_at_matches_status",
        ),
        CheckConstraint(
            "(status = 'invoiced') = (invoiced_at IS NOT NULL)",
            name="ck_billing_periods_invoiced_at_matches_status",
        ),
        # QUERY: month close. "every period for month M not yet closed" --
        # WHERE period_month = $1 AND status <> 'invoiced'. Drives the close/invoice job,
        # which sweeps all customers for one month.
        Index("ix_billing_periods_month_status", "period_month", "status"),
    )


# ---------------------------------------------------------------------------------------
# Usage: per-request events (partitioned, 90 days) and rollups (long-term). ADR-0016.
# ---------------------------------------------------------------------------------------

# Native PG enum, deliberately, on this table only. Every other status column here is
# text + CHECK because those are easy to evolve and the tables are small. `usage_events`
# is the exception that justifies the cost: an enum is 4 bytes where 'unauthenticated'
# is 16, and at the design target that is ~1e9 rows in the retained window.
usage_outcome = Enum(
    "success",
    "client_error",
    "unauthenticated",
    "server_error",
    "limit_refused",
    name="usage_outcome",
    create_type=False,
)


class UsageEvent(Base):
    """One served request. The largest table in the system by three orders of magnitude.

    PARTITIONED BY RANGE on ``billing_period_start`` -- one partition per billing month, so
    ADR-0016's 90-day expiry is a ``DROP TABLE`` rather than a mass DELETE that would
    generate dead tuples faster than autovacuum can reclaim them on the hottest write path
    we have. The partitioning clause and the partitions themselves live in the migration.

    Deliberately absent, each for a stated reason:

    * **No foreign keys.** ``customer_id`` and ``api_key_id`` reference real rows but carry
      no FK. An FK is a per-row index probe plus a row-share lock on the parent, on a path
      that wants multi-row INSERT or COPY at a few thousand rows per second. Neither parent
      is ever deleted -- customers are not deleted and key revocation is a timestamp
      (ADR-0015) -- so the reference cannot dangle. This is the one place referential
      integrity is traded for write throughput, and it is traded knowingly.
    * **No plan or price list version.** Resolving the segment at capture time would mean a
      lookup on the request path, and would bake in an answer that a later backdated
      assignment correction should change. Attribution happens at aggregation time, against
      ``plan_assignments``; that is exactly what the exclusion constraint there is for.

    ``billable`` stores the DECISION made at capture time rather than deriving it from
    ``outcome``. ADR-0007 says the status-code list is maintained deliberately, which means
    it will change -- and a change to the rule must never retroactively re-bill history.
    The billability decision is part of the record, for the same reason the price list
    version is.
    """

    __tablename__ = "usage_events"

    # bigint from a shared sequence, not a random UUID: 8 bytes rather than 16, and
    # monotonic so the primary key index writes at its right-hand edge instead of dirtying
    # a random page per insert. (Identity columns on partitioned tables need PG17; the
    # explicit sequence is the PG16 spelling of the same thing.)
    id: Mapped[int] = mapped_column(
        BigInteger,
        Sequence("usage_events_id_seq"),
        server_default=Sequence("usage_events_id_seq").next_value(),
        nullable=False,
        primary_key=True,
    )
    # Partition key. The resolved start of the billing period this request falls in
    # (ADR-0009), constant for every row in a partition.
    billing_period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, primary_key=True
    )
    customer_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    api_key_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    # When we served it -- the instant that decides the period and the proration segment.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the row became durable. occurred_at .. received_at is the capture lag, and a
    # received_at after the period's closed_at is precisely ADR-0010's roll-forward case.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_UTC_NOW
    )
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    outcome: Mapped[str] = mapped_column(usage_outcome, nullable=False)
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status_code BETWEEN 100 AND 599", name="ck_usage_events_status_code_is_http"
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 8 AND 128",
            name="ck_usage_events_idempotency_key_length",
        ),
        # QUERY: idempotent capture. "INSERT ... ON CONFLICT (idempotency_key,
        # billing_period_start) DO NOTHING" -- the one thing standing between a retried
        # flush and a double-counted request. Unique indexes on a partitioned table must
        # contain the partition key, so uniqueness is per-period, not global. That is the
        # right grain: the period is derived from occurred_at, which is fixed when the
        # event is created, so every retry of one event resolves to the same partition.
        # The corollary the hot path must honour: the key is minted WITH the event, never
        # per attempt.
        UniqueConstraint(
            "idempotency_key", "billing_period_start", name="uq_usage_events_idempotency"
        ),
        # QUERY: aggregation and dispute. "SELECT ... WHERE customer_id = $1 AND
        # occurred_at >= $2 AND occurred_at < $3" against one partition -- both the rollup
        # pass and support's "which requests made up this line?". billing_period_start is
        # not in the key because it is constant within a partition and would be dead weight
        # in every entry.
        Index("ix_usage_events_customer_occurred", "customer_id", "occurred_at"),
        # No index on api_key_id. "Which of my keys caused this spike?" is answered from
        # usage_rollups, whose grain includes api_key_id precisely so this table does not
        # need a third index. ADR-0016 chose that grain generously for this reason.
        {"postgresql_partition_by": "RANGE (billing_period_start)"},
    )


class UsageRollup(Base):
    """Aggregated usage, retained long-term. ADR-0016.

    Grain: customer x local day x price list version x plan segment x API key. Everything a
    charge is a function of, plus the key, so "which of my keys caused March?" is still
    answerable in April. Written by ``pipeline`` at aggregation time, never derived from
    ``usage_events`` at query time -- because after 90 days there is nothing to derive from.

    ``usage_date`` is a LOCAL calendar date in the customer's billing timezone (ADR-0009),
    not a UTC date. They differ by five hours and the difference is a whole day's usage at
    the edges.

    ``invoice_id`` is how "no request goes unbilled" becomes checkable at the storage layer:
    a rollup with no invoice belongs to nobody's bill yet. It is also the mechanism behind
    ADR-0010's roll-forward -- late usage for a closed period is simply an unbilled rollup
    that the next invoice run picks up, still carrying its original price list version.
    """

    __tablename__ = "usage_rollups"

    id: Mapped[str] = _uuid_pk()
    customer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.id", ondelete="RESTRICT", name="fk_usage_rollups_customer"),
        nullable=False,
    )
    billing_period_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("billing_periods.id", ondelete="RESTRICT", name="fk_usage_rollups_period"),
        nullable=False,
    )
    usage_date: Mapped[date] = mapped_column(Date, nullable=False)
    price_list_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey(
            "price_list_versions.id", ondelete="RESTRICT", name="fk_usage_rollups_version"
        ),
        nullable=False,
    )
    plan_assignment_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey(
            "plan_assignments.id", ondelete="RESTRICT", name="fk_usage_rollups_assignment"
        ),
        nullable=False,
    )
    api_key_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("api_keys.id", ondelete="RESTRICT", name="fk_usage_rollups_api_key"),
        nullable=False,
    )
    billable_requests: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    # There is deliberately no non_billable_requests column (migration 0003). The hot path
    # does not stream non-billable outcomes -- they are counted in a Redis hash -- so such a
    # column could only ever read 0, and a column that always reads 0 misleads.
    invoice_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("invoices.id", ondelete="RESTRICT", name="fk_usage_rollups_invoice"),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "billable_requests >= 0",
            name="ck_usage_rollups_counts_non_negative",
        ),
        # QUERY: the aggregation upsert. "INSERT ... ON CONFLICT (customer_id, usage_date,
        # plan_assignment_id, price_list_version_id, api_key_id) DO UPDATE SET
        # billable_requests = ..." -- this index IS the rollup grain, and re-running an
        # aggregation pass over the same day must land on the same row rather than a
        # second one. Also serves "customer C's usage by day", the live usage endpoint's
        # long-term half.
        UniqueConstraint(
            "customer_id",
            "usage_date",
            "plan_assignment_id",
            "price_list_version_id",
            "api_key_id",
            name="uq_usage_rollups_grain",
        ),
        # QUERY: invoice generation. "every rollup for customer C in period P" --
        # WHERE billing_period_id = $1 AND customer_id = $2. Period leads because the
        # invoice run sweeps a period across all customers.
        Index("ix_usage_rollups_period_customer", "billing_period_id", "customer_id"),
        # QUERY: roll-forward and the unbilled-revenue alarm. "rollups for customer C not
        # yet on any invoice" -- WHERE customer_id = $1 AND invoice_id IS NULL. Partial,
        # because once a period is invoiced its rollups leave this index for good, so it
        # stays roughly the size of one open month no matter how long the system runs.
        Index(
            "ix_usage_rollups_unbilled",
            "customer_id",
            "billing_period_id",
            postgresql_where="invoice_id IS NULL",
        ),
    )


# ---------------------------------------------------------------------------------------
# Spending limits (ADR-0008, ADR-0012)
# ---------------------------------------------------------------------------------------


class SpendingLimit(Base):
    """A customer's cap on the total bill for one period (ADR-0012), plus the precomputed
    request-count threshold the hot path actually compares against (ADR-0008).

    ``threshold_requests`` is nullable because it is computed in the background and a limit
    exists the instant it is set. ``threshold_price_list_version_id`` and
    ``threshold_computed_at`` are not decoration: a threshold computed against a version the
    customer is no longer on is stale, and ADR-0008 calls a missed recomputation "the worst
    kind" of failure because everything looks fine. Recording what it was computed from is
    what makes staleness detectable instead of invisible.

    ADR-0012's "a limit below the monthly fee is rejected when it is set" is NOT a
    constraint here. It needs the customer's prorated fee for the period, which is a join
    plus a domain calculation -- and it is a validation error the customer must see with a
    message naming the fee, not a constraint violation. Application code owns it.
    """

    __tablename__ = "spending_limits"

    id: Mapped[str] = _uuid_pk()
    customer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.id", ondelete="RESTRICT", name="fk_spending_limits_customer"),
        nullable=False,
    )
    billing_period_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("billing_periods.id", ondelete="RESTRICT", name="fk_spending_limits_period"),
        nullable=False,
    )
    limit_paisa: Mapped[int] = mapped_column(BigInteger, nullable=False)
    threshold_requests: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    threshold_computed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    threshold_price_list_version_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey(
            "price_list_versions.id", ondelete="RESTRICT", name="fk_spending_limits_version"
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "customer_id", "billing_period_id", name="uq_spending_limits_customer_period"
        ),
        CheckConstraint("limit_paisa > 0", name="ck_spending_limits_limit_positive"),
        CheckConstraint(
            "threshold_requests IS NULL OR threshold_requests >= 0",
            name="ck_spending_limits_threshold_non_negative",
        ),
        CheckConstraint(
            "(threshold_requests IS NULL) = (threshold_computed_at IS NULL)",
            name="ck_spending_limits_threshold_has_timestamp",
        ),
        # QUERY: threshold refresh. "limits whose threshold is missing or older than the
        # last thing that could have moved it" -- the background recomputation sweep
        # ADR-0008 requires. Partial on the pathological case, which should be empty.
        Index(
            "ix_spending_limits_needs_threshold",
            "billing_period_id",
            postgresql_where="threshold_requests IS NULL",
        ),
    )


# ---------------------------------------------------------------------------------------
# Invoices (ADR-0010, ADR-0013)
# ---------------------------------------------------------------------------------------

INVOICE_STATUSES = ("draft", "issued")
INVOICE_LINE_KINDS = ("monthly_fee", "usage", "prior_period_usage")


class Invoice(Base):
    """One invoice per customer per period. Immutable once issued.

    There is no 'void'. ADR-0013 rejected void-and-reissue: the customer has already seen
    the number, so voiding does not undo it, it only removes our record of what they saw.
    A correction is a second document. The correction mechanism is not built in v1 -- the
    immutability that makes it the ONLY possible correction is, and it is a trigger, not a
    convention, because application code is not the last line of defence.
    """

    __tablename__ = "invoices"

    id: Mapped[str] = _uuid_pk()
    customer_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("customers.id", ondelete="RESTRICT", name="fk_invoices_customer"),
        nullable=False,
    )
    billing_period_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("billing_periods.id", ondelete="RESTRICT", name="fk_invoices_period"),
        nullable=False,
    )
    invoice_number: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    total_paisa: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "customer_id", "billing_period_id", name="uq_invoices_customer_period"
        ),
        UniqueConstraint("invoice_number", name="uq_invoices_number"),
        CheckConstraint("status IN ('draft', 'issued')", name="ck_invoices_status"),
        CheckConstraint(
            "(status = 'issued') = (issued_at IS NOT NULL)", name="ck_invoices_issued_at"
        ),
        CheckConstraint("total_paisa >= 0", name="ck_invoices_total_non_negative"),
        # QUERY: Finance's month view. "every invoice issued for period P" --
        # WHERE billing_period_id = $1 AND status = 'issued'.
        Index("ix_invoices_period_status", "billing_period_id", "status"),
    )


class InvoiceLine(Base):
    """One line of an invoice, carrying everything needed to re-derive it from scratch.

    quantity x unit_price_paisa = amount_paisa is a CHECK, not a comment. It is the
    cheapest possible test of "does this line's arithmetic hold?", it runs on every write,
    and it would have caught the class of bug where a rating change moves an amount without
    moving what it claims to be a multiplication of.

    ``price_list_version_id`` and ``plan_assignment_id`` make the line reproducible;
    ``band_index`` says which rung of the ladder it came from. ``usage_period_id`` is
    ADR-0010's roll-forward: a prior-period line names the period the usage was incurred in
    and is priced at that period's version, which is why an invoice cannot assume one
    period or one price list version.
    """

    __tablename__ = "invoice_lines"

    id: Mapped[str] = _uuid_pk()
    invoice_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("invoices.id", ondelete="CASCADE", name="fk_invoice_lines_invoice"),
        nullable=False,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price_paisa: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_paisa: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_list_version_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey(
            "price_list_versions.id", ondelete="RESTRICT", name="fk_invoice_lines_version"
        ),
        nullable=False,
    )
    plan_assignment_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey(
            "plan_assignments.id", ondelete="RESTRICT", name="fk_invoice_lines_assignment"
        ),
        nullable=True,
    )
    band_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_period_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("billing_periods.id", ondelete="RESTRICT", name="fk_invoice_lines_usage_period"),
        nullable=True,
    )

    __table_args__ = (
        # QUERY: rendering an invoice. "SELECT * FROM invoice_lines WHERE invoice_id = $1
        # ORDER BY line_number" -- served by this unique index, which is why there is no
        # separate index on invoice_id.
        UniqueConstraint("invoice_id", "line_number", name="uq_invoice_lines_number"),
        CheckConstraint(
            "kind IN ('monthly_fee', 'usage', 'prior_period_usage')",
            name="ck_invoice_lines_kind",
        ),
        CheckConstraint("line_number > 0", name="ck_invoice_lines_number_positive"),
        CheckConstraint("quantity >= 0", name="ck_invoice_lines_quantity_non_negative"),
        CheckConstraint(
            "unit_price_paisa >= 0", name="ck_invoice_lines_unit_price_non_negative"
        ),
        CheckConstraint(
            "amount_paisa = quantity * unit_price_paisa", name="ck_invoice_lines_amount_is_product"
        ),
        CheckConstraint(
            "(kind = 'prior_period_usage') = (usage_period_id IS NOT NULL)",
            name="ck_invoice_lines_prior_period_has_period",
        ),
        CheckConstraint(
            "band_index IS NULL OR band_index >= 0", name="ck_invoice_lines_band_index"
        ),
    )
