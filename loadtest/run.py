"""Fire concurrent traffic, then prove the system billed exactly what it served.

Owned by test-engineer. Run with: make load-test N=5000 CONCURRENCY=100

A harness that reports throughput and never checks a total is a benchmark, and a benchmark
proves nothing about money. This one's real job is the assertion at the end: send exactly N
requests, then show that the Redis counter, the durable rows in Postgres, and the aggregated
rollups all equal the number the CLIENT observed being served. Not approximately -- exactly.
It exits non-zero if they disagree.

Three places a count can be wrong, checked separately because they fail differently:

  client 200s     what the customer believes they were served
  redis counter   what enforcement compares a spending limit against
  usage_events    the durable record an invoice is rated from
  usage_rollups   the aggregate that survives after per-request rows expire (ADR-0016)

The counter can legitimately lead the durable rows -- that is the buffer window ADR-0018
buys latency with -- so the harness WAITS for the drain to converge rather than sampling
once and declaring a discrepancy. What it will not tolerate is convergence to the wrong
number, or no convergence at all.

On latency: the target is a few thousand req/s BY DESIGN. This laptop will not produce that,
and an unqualified throughput number from a laptop misleads the reader, so the output always
states the concurrency it actually ran at. The per-request cost of capture is read from the
`x-usage-capture-us` header the middleware emits, which is the number ADR-0014's budget is
written against -- measured directly rather than inferred from end-to-end latency, which at
any real concurrency is dominated by queueing.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import text

from meter.config import get_settings
from meter.storage import cache, db
from meter.storage.repositories import usage as usage_repo

DRAIN_TIMEOUT_SECONDS = 120


@dataclass
class Outcome:
    latencies_ms: list[float] = field(default_factory=list)
    capture_us: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)

    @property
    def served(self) -> int:
        """What the CLIENT believes was served and billable: 2xx only, here."""
        return sum(count for status, count in self.statuses.items() if 200 <= status < 300)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * p / 100), len(ordered) - 1)]


async def _worker(client, url, headers, queue, outcome: Outcome) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        started = time.perf_counter()
        try:
            response = await client.get(url, headers=headers)
            status = response.status_code
            captured = response.headers.get("x-usage-capture-us")
            if captured:
                outcome.capture_us.append(float(captured))
        except httpx.HTTPError:
            status = 0
        outcome.latencies_ms.append((time.perf_counter() - started) * 1000)
        outcome.statuses[status] = outcome.statuses.get(status, 0) + 1


async def provision(base_url: str, plan: str) -> dict:
    """A FRESH customer, so the expected count starts at zero and the assertion is exact."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{base_url}/admin/customers",
            json={"name": f"loadtest-{int(time.time())}", "plan": plan},
        )
        response.raise_for_status()
        return response.json()


async def count_durable(engine, customer_id: str, period_start) -> tuple[int, int]:
    """(usage_events rows, usage_rollups total) for this customer and period."""
    async with engine.connect() as conn:
        events = await conn.scalar(
            text(
                "SELECT count(*) FROM usage_events "
                "WHERE customer_id = CAST(:c AS uuid) AND billing_period_start = :p "
                "AND billable"
            ),
            {"c": customer_id, "p": period_start},
        )
        rollups = await conn.scalar(
            text(
                "SELECT coalesce(sum(billable_requests), 0) FROM usage_rollups r "
                "JOIN billing_periods b ON b.id = r.billing_period_id "
                "WHERE r.customer_id = CAST(:c AS uuid) AND b.period_start = :p"
            ),
            {"c": customer_id, "p": period_start},
        )
    return int(events or 0), int(rollups or 0)


async def await_convergence(engine, redis, customer_id: str, period, expected: int):
    """Wait for the durable record to catch up with what the client saw.

    The counter leading the rows is normal -- it is the window ADR-0018 trades for latency.
    Sampling once and calling the difference a discrepancy would flag healthy lag as data
    loss, so this polls until the rows settle, and reports how long that took. A drain that
    never converges is a real failure and times out.
    """
    deadline = time.perf_counter() + DRAIN_TIMEOUT_SECONDS
    started = time.perf_counter()
    counter = events = rollups = 0
    while time.perf_counter() < deadline:
        counter = int(
            await redis.get(usage_repo.billable_counter_key(customer_id, period.label)) or 0
        )
        events, rollups = await count_durable(engine, customer_id, period.start)
        if events == expected and rollups == expected and counter == expected:
            return counter, events, rollups, time.perf_counter() - started
        await asyncio.sleep(0.5)
    return counter, events, rollups, time.perf_counter() - started


async def main(total: int, concurrency: int, base_url: str, plan: str) -> int:
    settings = get_settings()
    customer = await provision(base_url, plan)
    customer_id = customer["customer_id"]
    headers = {"X-API-Key": customer["api_key"]}
    url = f"{base_url}/v1/echo"

    queue: asyncio.Queue = asyncio.Queue()
    for _ in range(total):
        queue.put_nowait(1)
    outcome = Outcome()

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    began = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=60.0) as client:
        await asyncio.gather(
            *(_worker(client, url, headers, queue, outcome) for _ in range(concurrency))
        )
    elapsed = time.perf_counter() - began

    print(f"\ncustomer      {customer_id}  ({plan})")
    print(f"sent          {total} requests at concurrency {concurrency}")
    print(f"elapsed       {elapsed:.2f}s")
    print(f"throughput    {total / elapsed:,.0f} req/s  (this machine, at this concurrency)")
    print(f"statuses      {dict(sorted(outcome.statuses.items()))}")
    print(
        f"latency ms    p50 {percentile(outcome.latencies_ms, 50):.2f}   "
        f"p95 {percentile(outcome.latencies_ms, 95):.2f}   "
        f"p99 {percentile(outcome.latencies_ms, 99):.2f}   "
        f"mean {statistics.mean(outcome.latencies_ms):.2f}"
    )
    if outcome.capture_us:
        p99_ms = percentile(outcome.capture_us, 99) / 1000
        budget = "WITHIN" if p99_ms <= 1.0 else "OVER"
        print(
            f"capture ms    p50 {percentile(outcome.capture_us, 50) / 1000:.3f}   "
            f"p99 {p99_ms:.3f}   <- {budget} ADR-0014's 1ms p99 budget"
        )

    engine = db.create_engine(settings)
    redis = cache.create_client(settings)
    try:
        period = usage_repo.period_for(datetime.now(UTC))
        expected = outcome.served
        print(f"\nwaiting for the drain to converge on {expected:,}...")
        counter, events, rollups, took = await await_convergence(
            engine, redis, customer_id, period, expected
        )
    finally:
        await engine.dispose()
        await redis.aclose()

    print(f"\n  client saw served   {expected:,}")
    print(f"  redis counter       {counter:,}")
    print(f"  usage_events rows   {events:,}")
    print(f"  usage_rollups total {rollups:,}")
    print(f"  converged in        {took:.1f}s")

    disagreements = {
        "redis counter": counter,
        "usage_events": events,
        "usage_rollups": rollups,
    }
    wrong = {name: value for name, value in disagreements.items() if value != expected}
    if wrong or expected != total:
        print("\nFAILED")
        if expected != total:
            print(f"  {total - expected} of {total} requests were not served with 2xx")
        for name, value in wrong.items():
            print(f"  {name} says {value:,}, the client saw {expected:,} "
                  f"(difference {value - expected:+,})")
        return 1

    print(f"\nPASSED: {expected:,} served, {expected:,} billed, in three independent places.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--plan", default="Growth")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.requests, args.concurrency, args.base_url, args.plan)))
