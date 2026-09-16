"""Fire concurrent traffic at /v1/echo and report latency.

Owned by test-engineer. Run with: make load-test N=5000 CONCURRENCY=100

SESSION-1 SCOPE: this measures, it does not yet verify. Once usage recording exists, the
harness's real job begins -- send exactly N requests, then prove the system billed exactly
N. A load harness that reports throughput but never checks a total is a benchmark, and a
benchmark proves nothing about money.

Note the target is a few thousand req/s *by design*. This laptop will not produce that,
and an unqualified throughput number from a laptop misleads the reader -- so the output
states the concurrency it actually ran at.
"""

import argparse
import asyncio
import statistics
import time

import httpx

from meter.config import get_settings


async def _worker(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    queue: asyncio.Queue,
    latencies: list[float],
    statuses: dict[int, int],
) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        started = time.perf_counter()
        try:
            response = await client.get(url, headers=headers)
            status = response.status_code
        except httpx.HTTPError:
            status = 0
        latencies.append((time.perf_counter() - started) * 1000)
        statuses[status] = statuses.get(status, 0) + 1


async def main(total: int, concurrency: int, base_url: str) -> int:
    settings = get_settings()
    url = f"{base_url}/v1/echo"
    headers = {"X-API-Key": settings.dev_api_key}

    queue: asyncio.Queue = asyncio.Queue()
    for _ in range(total):
        queue.put_nowait(1)

    latencies: list[float] = []
    statuses: dict[int, int] = {}

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    started = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
        await asyncio.gather(
            *(_worker(client, url, headers, queue, latencies, statuses) for _ in range(concurrency))
        )
    elapsed = time.perf_counter() - started

    latencies.sort()

    def percentile(p: float) -> float:
        if not latencies:
            return 0.0
        index = min(int(len(latencies) * p / 100), len(latencies) - 1)
        return latencies[index]

    ok = statuses.get(200, 0)
    print(f"\nsent          {total} requests at concurrency {concurrency}")
    print(f"elapsed       {elapsed:.2f}s")
    print(f"throughput    {total / elapsed:,.0f} req/s  (on this machine, not production)")
    print(f"statuses      {dict(sorted(statuses.items()))}")
    if latencies:
        print(f"latency ms    p50 {percentile(50):.2f}   p95 {percentile(95):.2f}   "
              f"p99 {percentile(99):.2f}   max {latencies[-1]:.2f}   "
              f"mean {statistics.mean(latencies):.2f}")

    if ok != total:
        print(f"\nFAILED: {total - ok} of {total} requests did not return 200")
        return 1

    print("\nall requests returned 200.")
    print("NOT YET VERIFIED: that every request was billed exactly once -- there is no "
          "usage recording yet. That check is what makes this a proof rather than a "
          "benchmark, and it lands with the usage pipeline.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.requests, args.concurrency, args.base_url)))
