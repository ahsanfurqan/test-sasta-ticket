"""The drain: Redis stream -> Postgres, via a consumer group. ADR-0018 section 3.

This is the crash-safety-critical part of the system, so the ordering is written out
before the code:

    read a batch (XREADGROUP)      entries become PENDING for this consumer
    commit the rows (one INSERT)   ON CONFLICT (idempotency_key, billing_period_start)
    mark the dirty cells (SADD)    so aggregation knows what changed
    XACK                           only now are the entries the group's problem no longer

**Killed before the commit:** nothing is in Postgres, nothing is acked. The entries stay
pending, are redelivered, and are inserted exactly once. Nothing lost.

**Killed between the commit and the XACK:** the rows ARE in Postgres and the entries are
still pending. They are redelivered, the insert conflicts on every row and inserts none,
and the batch is acked the second time. Nothing lost, nothing double-counted. This is the
window the test suite kills into, and the `ON CONFLICT` is the whole reason it is safe.

**Killed between the SADD and the XACK:** as above, plus the dirty cells are marked twice.
Re-aggregating a cell assigns rather than adds (see the rollup upsert), so that is a no-op.

**Killed and never restarted:** another worker's `XAUTOCLAIM` reclaims the batch after
`drain_reclaim_idle_ms`. Redelivery to a *different* consumer is safe for exactly the same
reason redelivery to the same one is.

The idempotency key is minted by the hot path WITH the event and never per delivery
attempt. Every sentence above depends on that, which is why `events.parse` refuses an
entry without one rather than inventing one here.

One thing ADR-0018 leaves unsaid: `XACK` removes an entry from the pending list, NOT from
the stream. Nothing else in the system trims, so without `trim_drained()` below the stream
would grow by one entry per request forever and walk into `usage_stream_max_entries` -- at
which point the hot path fails closed, and a Postgres-outage safety valve would have
tripped on ordinary success. Trimming is anchored on the oldest entry any consumer might
still need, so a crashed worker's pending batch is never deleted out from under it.
"""

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from datetime import date
from time import monotonic

import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from meter import billing_calendar as clock
from meter.config import Settings
from meter.pipeline import keys
from meter.pipeline.events import MalformedEvent, UsageEventRecord, parse
from meter.storage.repositories import periods, rollups

logger = logging.getLogger("meter.pipeline.drain")

# Redis set of (customer, local day) cells whose rollup is behind the per-request rows.
DIRTY_CELLS_KEY = f"{keys.NAMESPACE}:agg:dirty"

_BUSYGROUP = "BUSYGROUP"
_NOGROUP = "NOGROUP"


@dataclass(slots=True)
class DrainResult:
    """What one batch did. Every field is a number a test can assert on."""

    entries_read: int = 0
    rows_inserted: int = 0
    duplicates_ignored: int = 0
    dead_lettered: int = 0
    acked: int = 0
    lag_ms: int | None = None

    def __bool__(self) -> bool:
        return self.entries_read > 0


@dataclass(slots=True)
class _Batch:
    ids: list[str] = field(default_factory=list)
    records: list[UsageEventRecord] = field(default_factory=list)
    dead: list[tuple[str, dict[str, str], str]] = field(default_factory=list)


def cell_member(customer_id: str, usage_date: date) -> str:
    return f"{customer_id}|{usage_date.isoformat()}"


def parse_cell_member(member: str) -> tuple[str, date]:
    customer_id, _, day = member.partition("|")
    return customer_id, date.fromisoformat(day)


class Drain:
    """One worker's consumer in the group. Owns its consumer name and its pending batch."""

    def __init__(
        self, settings: Settings, engine: AsyncEngine, redis: aioredis.Redis
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._redis = redis
        self._stream = settings.usage_stream_key
        self._group = settings.usage_stream_group
        self._consumer = settings.drain_consumer_name or socket.gethostname()
        self._dead_stream = keys.dead_letter_stream(self._stream)
        # Months whose partition we have already ensured. Bounded by the number of months
        # a worker lives through, so it never needs eviction.
        self._partitions: set[date] = set()
        # On startup, claim back anything this consumer name left pending in a previous
        # life before touching new entries. A restarted container keeps its hostname, so
        # this is the common case after `docker compose kill worker`.
        self._recovering = True
        # Far enough in the past that the first pass always looks for abandoned batches.
        self._last_reclaim_at = -float("inf")

    @property
    def consumer(self) -> str:
        return self._consumer

    async def ensure_group(self) -> None:
        """Create the consumer group at the START of the stream, not at its end.

        `$` would silently skip every entry captured before the group existed -- which on a
        first deploy is every request served so far. `0` costs a replay of whatever is
        still in the stream, and the replay is safe by construction.
        """
        try:
            await self._redis.xgroup_create(
                name=self._stream, groupname=self._group, id="0", mkstream=True
            )
            logger.info("created consumer group %s on %s", self._group, self._stream)
        except ResponseError as exc:
            if _BUSYGROUP not in str(exc):
                raise

    # -----------------------------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------------------------

    async def _read(self, block_ms: int) -> list[tuple[str, dict[str, str]]]:
        count = self._settings.drain_batch_size

        if self._recovering:
            entries = await self._read_own_pending(count)
            if entries:
                return entries
            self._recovering = False
            logger.info("recovered own pending entries; switching to new deliveries")

        # XAUTOCLAIM on every batch would be a Redis round trip per batch spent looking
        # for a dead consumer that, in the normal case, does not exist. Nothing can become
        # reclaimable faster than `drain_reclaim_idle_ms`, so checking more often than that
        # cannot find anything checking once per interval would miss.
        now = monotonic()
        if now - self._last_reclaim_at >= self._settings.drain_reclaim_idle_ms / 1000:
            self._last_reclaim_at = now
            reclaimed = await self._reclaim_abandoned(count)
            if reclaimed:
                return reclaimed

        try:
            response = await self._redis.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={self._stream: ">"},
                count=count,
                # BLOCK 0 means "block forever" in Redis, not "do not block". Anything
                # non-positive must become no BLOCK argument at all, or `drain_until_empty`
                # would hang on the empty read that is its termination condition.
                block=block_ms if block_ms > 0 else None,
            )
        except RedisTimeoutError:
            # The client's socket timeout fired while the server was holding the BLOCK.
            # On an idle stream those two deadlines race, and losing the race means
            # "nothing arrived" -- not an error, and certainly not a stack trace once a
            # second forever. `drain_block_ms` is kept below `redis_timeout_seconds` so
            # this is the rare case rather than the normal one, but the race is real and
            # unavoidable: the two timeouts are measured by different clocks.
            return []
        except ResponseError as exc:
            if _NOGROUP in str(exc):
                await self.ensure_group()
                return []
            raise
        if not response:
            return []
        return [(entry_id, fields) for _stream, entries in response for entry_id, fields in entries]

    async def _read_own_pending(self, count: int) -> list[tuple[str, dict[str, str]]]:
        """Entries delivered to this consumer name and never acked -- i.e. the batch a
        previous incarnation of this process was holding when it was killed."""
        response = await self._redis.xreadgroup(
            groupname=self._group,
            consumername=self._consumer,
            streams={self._stream: "0"},
            count=count,
        )
        if not response:
            return []
        return [(entry_id, fields) for _stream, entries in response for entry_id, fields in entries]

    async def _reclaim_abandoned(self, count: int) -> list[tuple[str, dict[str, str]]]:
        """Take over entries pending against a consumer that has gone quiet.

        The killed-and-never-restarted case, and the killed-and-came-back-with-a-different
        -hostname case. Both are safe for the same reason a same-consumer redelivery is.
        """
        try:
            _next, messages, _deleted = await self._redis.xautoclaim(
                name=self._stream,
                groupname=self._group,
                consumername=self._consumer,
                min_idle_time=self._settings.drain_reclaim_idle_ms,
                start_id="0-0",
                count=count,
            )
        except ResponseError as exc:
            if _NOGROUP in str(exc):
                await self.ensure_group()
                return []
            raise
        if messages:
            logger.warning(
                "reclaimed %d entries idle for more than %dms -- a consumer died holding "
                "them, and they were never acked because they were never committed",
                len(messages),
                self._settings.drain_reclaim_idle_ms,
            )
        return [(entry_id, fields) for entry_id, fields in messages if fields]

    # -----------------------------------------------------------------------------------
    # One batch
    # -----------------------------------------------------------------------------------

    async def run_once(self, *, block_ms: int | None = None) -> DrainResult:
        entries = await self._read(
            self._settings.drain_block_ms if block_ms is None else block_ms
        )
        if not entries:
            return DrainResult()

        batch = _Batch()
        vanished = 0
        for entry_id, fields in entries:
            batch.ids.append(entry_id)
            if not fields:
                # The entry was pending but its payload is gone from the stream -- only an
                # XTRIM or XDEL by hand can do that, since `trim_drained` never trims past
                # the oldest pending id. There is nothing to insert and nothing to recover;
                # acking it is all that is left, and it is a data-loss incident.
                vanished += 1
                continue
            try:
                batch.records.append(parse(fields))
            except MalformedEvent as exc:
                batch.dead.append((entry_id, fields, str(exc)))
        if vanished:
            logger.error(
                "%d pending entries have been deleted from the stream before they were "
                "drained -- that usage is GONE and cannot be recovered from Redis",
                vanished,
            )

        result = DrainResult(entries_read=len(entries), dead_lettered=len(batch.dead))

        if batch.dead:
            await self._dead_letter(batch.dead)

        if batch.records:
            await self._ensure_partitions(batch.records)
            result.rows_inserted = await self._commit(batch.records)
            result.duplicates_ignored = len(batch.records) - result.rows_inserted
            result.lag_ms = await self._mark_progress(batch.records)

        # Only now. Everything above is either durable in Postgres or safely repeatable.
        acked = await self._redis.xack(self._stream, self._group, *batch.ids)
        result.acked = int(acked)

        if result.duplicates_ignored:
            logger.info(
                "batch of %d: %d new rows, %d already present (redelivery after a crash "
                "is supposed to look exactly like this)",
                result.entries_read,
                result.rows_inserted,
                result.duplicates_ignored,
            )
        return result

    async def _commit(self, records: list[UsageEventRecord]) -> int:
        """One transaction, one statement. Committed or not; there is no third state."""
        rows = [record.as_row() for record in records]
        async with self._engine.begin() as conn:
            return await rollups.insert_usage_events(conn, rows)

    async def _ensure_partitions(self, records: list[UsageEventRecord]) -> None:
        """A partition per billing month, created before the rows that need it.

        The schema ships a DEFAULT partition so a missing one never loses a request, but a
        non-empty default blocks the next `CREATE TABLE ... PARTITION OF` under ACCESS
        EXCLUSIVE -- so landing there is an incident, not a fallback. Creating the
        partition eagerly, in its own transaction, keeps the DDL lock off the insert path.
        """
        months = {clock.period_month(record.occurred_at) for record in records}
        missing = months - self._partitions
        for month in sorted(missing):
            async with self._engine.begin() as conn:
                name = await periods.ensure_usage_partition(conn, month)
            self._partitions.add(month)
            logger.info("usage partition for %s is %s", month, name)

    async def _mark_progress(self, records: list[UsageEventRecord]) -> int:
        """Dirty cells, then the lag metric. Before the XACK, so a crash here replays."""
        members = {
            cell_member(record.customer_id, clock.local_date(record.occurred_at))
            for record in records
        }
        latest = max(record.occurred_at for record in records)
        lag_ms = int((clock.now() - latest).total_seconds() * 1000)

        pipe = self._redis.pipeline()
        pipe.sadd(DIRTY_CELLS_KEY, *members)
        pipe.set(keys.DRAIN_LAG_MS, lag_ms)
        pipe.set(keys.DRAIN_COMMITTED_AT, f"{clock.now().timestamp():.3f}")
        await pipe.execute()
        return lag_ms

    async def _dead_letter(
        self, dead: list[tuple[str, dict[str, str], str]]
    ) -> None:
        """An entry that cannot become a row is moved, never dropped.

        A dropped entry is a lost billable request, which is the one failure this whole
        design exists to prevent -- so it goes somewhere durable and loud, with the reason
        attached, and someone looks at it.
        """
        pipe = self._redis.pipeline()
        for entry_id, fields, reason in dead:
            logger.error(
                "dead-lettering stream entry %s: %s -- fields=%r", entry_id, reason, fields
            )
            pipe.xadd(
                self._dead_stream,
                {
                    **{key: str(value) for key, value in fields.items()},
                    "_source_id": entry_id,
                    "_reason": reason,
                    "_dead_lettered_at": clock.now().isoformat(),
                },
            )
        await pipe.execute()

    # -----------------------------------------------------------------------------------
    # Whole-stream helpers
    # -----------------------------------------------------------------------------------

    async def drain_until_empty(self, *, max_batches: int = 10_000) -> DrainResult:
        """Drain everything currently in the stream. Month close blocks on this."""
        total = DrainResult()
        for _ in range(max_batches):
            result = await self.run_once(block_ms=0)
            if not result:
                return total
            total.entries_read += result.entries_read
            total.rows_inserted += result.rows_inserted
            total.duplicates_ignored += result.duplicates_ignored
            total.dead_lettered += result.dead_lettered
            total.acked += result.acked
            total.lag_ms = result.lag_ms
        logger.error("drain_until_empty hit its batch cap with entries still outstanding")
        return total

    async def outstanding(self) -> int:
        """Entries the group has not finished with: delivered-but-unacked plus undelivered.

        This is the number that has to be zero before a period can be said to be complete
        (ADR-0010 step 2), and it is stream-wide rather than per customer -- a stronger and
        far cheaper condition than proving it customer by customer.
        """
        try:
            pending = await self._redis.xpending(self._stream, self._group)
        except ResponseError as exc:
            if _NOGROUP in str(exc):
                return 0
            raise
        delivered_unacked = int(pending["pending"]) if pending else 0

        undelivered = 0
        for group in await self._redis.xinfo_groups(self._stream):
            if group.get("name") != self._group:
                continue
            if group.get("lag") is not None:
                undelivered = int(group["lag"])
                break
            # Redis reports a NULL lag when it cannot account for entries that were
            # trimmed out from under the group. `entries-added - entries-read` is the same
            # quantity computed from two counters that trimming does not disturb -- and
            # reconciliation gates the invoice run on this number, so guessing zero here
            # would let a close proceed over an undrained buffer.
            read = group.get("entries-read")
            info = await self._redis.xinfo_stream(self._stream)
            added = info.get("entries-added")
            if read is not None and added is not None:
                undelivered = max(0, int(added) - int(read))
            break
        return delivered_unacked + undelivered

    async def trim_drained(self) -> int:
        """Delete entries the group is completely finished with. Returns how many.

        `XACK` removes an entry from the pending list; it does NOT remove it from the
        stream. Without this the stream grows by one entry per request forever, and since
        the hot path fails closed at `usage_stream_max_entries` (ADR-0018), the system
        would stop serving after that many requests in total -- a Postgres-outage safety
        valve tripping on ordinary success. ADR-0018 bounds the stream but never says who
        trims it; this is that answer.

        The trim point is the oldest entry any consumer might still need: the oldest
        pending entry if there is one, otherwise the group's last delivered id. Anything
        older has been committed to Postgres and acked. `MINID` keeps everything from that
        id onward, so a pending batch is never trimmed out from under a crashed worker --
        which is the one thing that would turn "redelivered on restart" into "lost".

        This assumes ONE consumer group on the stream, which is what the design has. A
        second group would need the minimum across groups, and getting that wrong would
        delete another consumer's backlog.
        """
        try:
            pending = await self._redis.xpending(self._stream, self._group)
        except ResponseError as exc:
            if _NOGROUP in str(exc):
                return 0
            raise

        min_id = pending.get("min") if pending and int(pending["pending"]) else None
        if min_id is None:
            for group in await self._redis.xinfo_groups(self._stream):
                if group.get("name") == self._group:
                    min_id = group.get("last-delivered-id")
                    break
        if not min_id or min_id == "0-0":
            return 0
        return int(await self._redis.xtrim(self._stream, minid=min_id, approximate=False))

    async def check_stream_bound(self) -> tuple[int, bool]:
        """ADR-0018's bound. Returns (length, alerting).

        The bound exists because an unbounded stream exhausts Redis memory, and by
        ADR-0011 an exhausted Redis is a total outage -- so a recoverable Postgres outage
        would become an unrecoverable one. The alert has to fire long before the bound,
        which is what the ratio is for.
        """
        length = int(await self._redis.xlen(self._stream))
        bound = self._settings.usage_stream_max_entries
        threshold = int(bound * self._settings.usage_stream_alert_ratio)
        alerting = length >= threshold
        if alerting:
            logger.error(
                "usage stream is at %d entries, past the %d alert threshold on a bound of "
                "%d -- Postgres is probably behind, and at the bound we fail closed",
                length,
                threshold,
                bound,
            )
        return length, alerting

    async def run_forever(self, stopping: asyncio.Event) -> None:
        await self.ensure_group()
        while not stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed batch was never acked, so it is still pending and will be
                # redelivered. Backing off is the only thing to do that is not worse.
                logger.exception("drain batch failed; entries stay pending for redelivery")
                await asyncio.sleep(1.0)


async def take_dirty_cells(redis: aioredis.Redis) -> list[tuple[str, date]]:
    """Read the dirty set WITHOUT removing it.

    Removal happens after the aggregation commits (`clear_dirty_cells`), so a worker killed
    mid-aggregation leaves the cells marked and the next pass redoes them. Popping first
    would lose a day's rollup to a crash, and ADR-0016 has no way to recompute a rollup
    once the per-request rows expire.
    """
    members = await redis.smembers(DIRTY_CELLS_KEY)
    return [parse_cell_member(member) for member in members]


async def clear_dirty_cells(redis: aioredis.Redis, members: list[str]) -> None:
    if members:
        await redis.srem(DIRTY_CELLS_KEY, *members)


async def lag_ms(redis: aioredis.Redis) -> int | None:
    """"How far behind is the live figure right now?" -- invariant 7, as a number."""
    raw = await redis.get(keys.DRAIN_LAG_MS)
    return None if raw is None else int(raw)
