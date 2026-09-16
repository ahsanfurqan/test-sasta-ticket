"""The usage stream entry: the wire format between `hot-path` and `pipeline` (ADR-0018).

The hot path does ONE `XADD` per request, after the handler and before the response is
sent. No in-process batching -- batching belongs here, in the drain, where a crash is
recoverable. This module is the schema of that `XADD`, and the tests in
`tests/integration/test_drain.py` write entries by hand in exactly this shape, which is
what keeps the contract honest while both sides are being built.

Fields (Redis stream fields are flat strings, so everything is stringified):

===================== ======== =================================================
field                 required meaning
===================== ======== =================================================
idempotency_key       yes      minted WITH the event, never per delivery attempt.
                               8-128 chars. This is the single thing that makes
                               redelivery after a mid-drain kill safe.
customer_id           yes      uuid
api_key_id            yes      uuid -- in the rollup grain (ADR-0016), so "which
                               of my keys caused March?" is answerable in April
occurred_at           yes      ISO-8601 with offset, when we served it
status_code           yes      the HTTP status the customer received
outcome               yes      success | client_error | unauthenticated |
                               server_error | limit_refused
billable              yes      "1"/"0" -- the DECISION made at capture time
                               (ADR-0007), not re-derived here, so changing the
                               rule never re-bills history
billing_period_start  no       the resolved period start. Derived from
                               occurred_at when absent; the derivation is
                               deterministic, so both routes agree.
===================== ======== =================================================

An entry that does not parse is NOT dropped. It goes to the dead-letter stream and stays
there, because a dropped entry is a lost billable request and the whole design exists to
prevent exactly that.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from meter import billing_calendar as clock

# Mirrors the `usage_outcome` enum in the schema. Kept as a tuple rather than imported
# from the ORM so a malformed value is rejected here, at the edge, with a useful message.
OUTCOMES = (
    "success",
    "client_error",
    "unauthenticated",
    "server_error",
    "limit_refused",
)

# ADR-0007: we authenticated it AND processed it. Used only as the fallback when the hot
# path did not send an explicit decision; the explicit one always wins.
BILLABLE_OUTCOMES = frozenset({"success", "client_error"})

IDEMPOTENCY_KEY_MIN = 8
IDEMPOTENCY_KEY_MAX = 128


class MalformedEvent(ValueError):
    """An entry that cannot become a usage row. Dead-lettered, never discarded."""


@dataclass(frozen=True, slots=True)
class UsageEventRecord:
    """One parsed stream entry, ready to become one `usage_events` row."""

    idempotency_key: str
    customer_id: str
    api_key_id: str
    occurred_at: datetime
    billing_period_start: datetime
    status_code: int
    outcome: str
    billable: bool

    def as_row(self) -> dict:
        return {
            "idempotency_key": self.idempotency_key,
            "customer_id": self.customer_id,
            "api_key_id": self.api_key_id,
            "occurred_at": self.occurred_at,
            "billing_period_start": self.billing_period_start,
            "status_code": self.status_code,
            "outcome": self.outcome,
            "billable": self.billable,
        }


def encode(record: UsageEventRecord) -> dict[str, str]:
    """A record as stream fields. Used by tests and by the load harness; the hot path
    builds the same dict without needing to import this module."""
    return {
        "idempotency_key": record.idempotency_key,
        "customer_id": record.customer_id,
        "api_key_id": record.api_key_id,
        "occurred_at": record.occurred_at.astimezone(UTC).isoformat(),
        "billing_period_start": record.billing_period_start.astimezone(UTC).isoformat(),
        "status_code": str(record.status_code),
        "outcome": record.outcome,
        "billable": "1" if record.billable else "0",
    }


def _require(fields: dict[str, str], name: str) -> str:
    value = fields.get(name)
    if value is None or value == "":
        raise MalformedEvent(f"missing field {name!r}")
    return value


def _parse_instant(raw: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise MalformedEvent(f"{field} is not ISO-8601: {raw!r}") from exc
    if parsed.tzinfo is None:
        # A naive timestamp is ambiguous by five hours here, which is a whole day's usage
        # at a month boundary. Refuse it rather than guess.
        raise MalformedEvent(f"{field} has no timezone offset: {raw!r}")
    return parsed.astimezone(UTC)


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "t", "yes", "y"}:
        return True
    if lowered in {"0", "false", "f", "no", "n"}:
        return False
    raise MalformedEvent(f"billable is not a boolean: {raw!r}")


def parse(fields: dict[str, str]) -> UsageEventRecord:
    """Stream fields -> a usage row, or `MalformedEvent`.

    Strict on everything the database is strict about, so a bad entry is rejected at the
    edge with a readable message instead of aborting a 500-row batch insert.
    """
    idempotency_key = _require(fields, "idempotency_key")
    if not IDEMPOTENCY_KEY_MIN <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX:
        raise MalformedEvent(
            f"idempotency_key must be {IDEMPOTENCY_KEY_MIN}-{IDEMPOTENCY_KEY_MAX} chars, "
            f"got {len(idempotency_key)}"
        )

    occurred_at = _parse_instant(_require(fields, "occurred_at"), "occurred_at")

    raw_period_start = fields.get("billing_period_start")
    if raw_period_start:
        billing_period_start = _parse_instant(raw_period_start, "billing_period_start")
    else:
        billing_period_start = clock.period_start_of(occurred_at)

    raw_status = _require(fields, "status_code")
    try:
        status_code = int(raw_status)
    except ValueError as exc:
        raise MalformedEvent(f"status_code is not an integer: {raw_status!r}") from exc
    if not 100 <= status_code <= 599:
        raise MalformedEvent(f"status_code is not an HTTP status: {status_code}")

    outcome = _require(fields, "outcome")
    if outcome not in OUTCOMES:
        raise MalformedEvent(f"outcome {outcome!r} is not one of {OUTCOMES}")

    billable = _parse_bool(fields.get("billable"), default=outcome in BILLABLE_OUTCOMES)

    return UsageEventRecord(
        idempotency_key=idempotency_key,
        customer_id=_require(fields, "customer_id"),
        api_key_id=_require(fields, "api_key_id"),
        occurred_at=occurred_at,
        billing_period_start=billing_period_start,
        status_code=status_code,
        outcome=outcome,
        billable=billable,
    )
