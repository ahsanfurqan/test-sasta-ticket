"""The Redis keyspace the hot path reads and writes, and the period label inside it.

This is one half of the hot-path/pipeline contract (ADR-0018). The other half is
`meter.pipeline.keys`, which spells the same strings for the drain, the counter rebuild and
the threshold sweep. The two MUST agree character for character: a hot path that INCRs
`usage:count:C:2026-09` while the drain rebuilds `usage:counts:C:2026-09` is a system that
counts, enforces and invoices normally right up until a Redis restart quietly zeroes a
month of enforcement.

They are duplicated rather than shared because `meter.api` and `meter.pipeline` are
siblings in the import-linter layers contract and may not import each other. This module
sits in `meter.storage`, UNDER both of them, which is the one place a shared spelling could
live -- so the cleanup, when someone gets to it, is for `meter.pipeline.keys` to import
these rather than restate them. Until then, changing a key here means changing it there in
the same commit.

    usage:events                          the stream (Settings.usage_stream_key)
    usage:count:<customer>:<YYYY-MM>      billable requests this period; hot path INCRs
    limit:threshold:<customer>:<YYYY-MM>  precomputed request threshold (ADR-0008);
                                          ABSENT means no limit is set
    meter:counters:authoritative          "1" once a rebuild has happened; ABSENT is the
                                          state a Redis that restarted empty is in, and is
                                          precisely the state that must not serve
    meter:usage:nonbillable:<YYYY-MM>     hash of outcomes nobody is billed for

Owned by data-model in the long run (their directory); written by hot-path, which is the
side that cannot serve a request without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

#: ADR-0009. Storage is UTC; boundaries are evaluated here.
BILLING_TIMEZONE = ZoneInfo("Asia/Karachi")

USAGE_COUNT_PREFIX = "usage:count"
LIMIT_THRESHOLD_PREFIX = "limit:threshold"

#: Written last by a counter rebuild, and by nothing else (meter.pipeline.counters). The
#: hot path serves on this key existing -- never on "Redis answered a PING".
COUNTERS_AUTHORITATIVE = "meter:counters:authoritative"


@dataclass(frozen=True, slots=True)
class Period:
    """The billing period an instant falls in.

    ``label`` names it in a Redis key ("2026-09" = September in Asia/Karachi); ``start`` is
    the resolved UTC instant it begins, which is what ``usage_events.billing_period_start``
    -- the partition key -- stores.
    """

    label: str
    start: datetime


_period_cache: dict[tuple[int, int], Period] = {}


def period_for(now: datetime) -> Period:
    """Resolve the billing period containing `now`.

    Cached per month because the hot path calls this on every request and a timezone
    conversion per request is a real cost against a 1ms budget (ADR-0014). The cache holds
    one entry per calendar month and is never invalidated -- the mapping cannot change.
    """
    local = now.astimezone(BILLING_TIMEZONE)
    key = (local.year, local.month)
    cached = _period_cache.get(key)
    if cached is not None:
        return cached
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0, fold=0)
    period = Period(
        label=f"{local.year:04d}-{local.month:02d}", start=start_local.astimezone(UTC)
    )
    _period_cache[key] = period
    return period


def current_period() -> Period:
    return period_for(datetime.now(UTC))


def billable_counter_key(customer_id: str, period_label: str) -> str:
    """Billable requests this customer has made this period. INCR on the hot path, SET by
    a rebuild. Mirrors `meter.pipeline.keys.usage_count`."""
    return f"{USAGE_COUNT_PREFIX}:{customer_id}:{period_label}"


def threshold_key(customer_id: str, period_label: str) -> str:
    """The request count at which this customer reaches their spending limit (ADR-0008).

    Absent means no limit. The hot path compares this integer with the counter and does
    nothing else -- no ladder, no price list, no money arithmetic.
    Mirrors `meter.pipeline.keys.limit_threshold`.
    """
    return f"{LIMIT_THRESHOLD_PREFIX}:{customer_id}:{period_label}"


def nonbillable_key(period_label: str) -> str:
    """Hash of `<customer_id>:<outcome>` -> count, for outcomes that are NOT billed.

    Hot-path only: nothing downstream may charge from it. It exists so a refusal or a 5xx
    is visible somewhere, without putting a non-billable entry into the stream that carries
    the billable ones -- where, at the stream's bound, it could displace usage we are owed
    money for.
    """
    return f"meter:usage:nonbillable:{period_label}"
