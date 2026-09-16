"""Redis key shapes. The written-down half of the hot-path/pipeline contract.

`hot-path` and `pipeline` are siblings -- the import-linter layers contract puts them on
the same rung, so neither may import the other. That makes this module *documentation with
a test suite attached* rather than a shared library: the shapes below are the agreement,
and both sides build the same strings.

Everything here is a pure string function. No I/O, no clock.

    usage:events                      the stream (config.usage_stream_key)
    usage:events:dead                 entries the drain could not parse -- never dropped
    usage:count:<customer>:<YYYY-MM>   billable requests counted this period  (hot path INCRs,
                                       pipeline rebuilds -- ADR-0011)
    limit:threshold:<customer>:<YYYY-MM>  the precomputed request-count threshold (ADR-0008);
                                       absent means "no limit set"
    meter:counters:authoritative      "1" once counters have been rebuilt from Postgres and
                                       the hot path may serve (ADR-0011). ABSENT is the
                                       normal state of a Redis that restarted empty, which
                                       is precisely the state that must not serve.
    meter:counters:rebuilt_at         unix seconds of the last rebuild
    meter:counters:rebuild_seconds    how long that rebuild took -- ADR-0011's unmeasured risk
    meter:drain:lag_ms                occurred_at -> committed lag of the last drained batch
    meter:drain:committed_at          unix seconds of the last successful Postgres commit
    meter:threshold:oldest_age_seconds  staleness of the oldest threshold (ADR-0018's alert)
"""

from datetime import date

NAMESPACE = "meter"

USAGE_COUNT_PREFIX = "usage:count"
LIMIT_THRESHOLD_PREFIX = "limit:threshold"

COUNTERS_AUTHORITATIVE = f"{NAMESPACE}:counters:authoritative"
COUNTERS_REBUILT_AT = f"{NAMESPACE}:counters:rebuilt_at"
COUNTERS_REBUILD_SECONDS = f"{NAMESPACE}:counters:rebuild_seconds"
DRAIN_LAG_MS = f"{NAMESPACE}:drain:lag_ms"
DRAIN_COMMITTED_AT = f"{NAMESPACE}:drain:committed_at"
THRESHOLD_OLDEST_AGE_SECONDS = f"{NAMESPACE}:threshold:oldest_age_seconds"


def month_label(period_month: date) -> str:
    """A period's name in a key: the LOCAL first-of-month, as YYYY-MM (ADR-0009)."""
    return f"{period_month.year:04d}-{period_month.month:02d}"


def usage_count(customer_id: str, period_month: date) -> str:
    """The hot path's request counter for one customer-month. Billable requests only."""
    return f"{USAGE_COUNT_PREFIX}:{customer_id}:{month_label(period_month)}"


def limit_threshold(customer_id: str, period_month: date) -> str:
    """The precomputed request-count threshold the hot path compares against (ADR-0008)."""
    return f"{LIMIT_THRESHOLD_PREFIX}:{customer_id}:{month_label(period_month)}"


def dead_letter_stream(stream_key: str) -> str:
    """Where an unparseable stream entry goes. A dropped entry is a lost billable request,
    so nothing is ever discarded -- it is moved somewhere loud and left there."""
    return f"{stream_key}:dead"
