"""Usage capture: the ordering, the billability rule, and what happens when it fails.

ADR-0018's claim is an ORDERING claim -- the usage event reaches Redis before the response
reaches the customer -- so most of this file drives the ASGI middleware directly, with a
fake Redis and a hand-rolled `send`, both recording into one shared log. Asserting on the
order of that log is the only way to prove "before" rather than "shortly after"; a test that
makes a request and then looks for the event proves something weaker.

The live-stack tests at the bottom prove the same property end to end in the weaker but
real form: by the time the client is holding the response, the event is already in Redis.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from meter.api.context import HotPathContext
from meter.api.metering import UsageMeteringMiddleware, classify
from meter.api.settings import HotPathSettings
from meter.config import get_settings
from meter.storage.repositories import keys as keys_repo
from meter.storage.repositories import usage as usage_repo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------------------
# A fake Redis that records WHEN each command ran, relative to the response being sent.
# ---------------------------------------------------------------------------------------


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple]] = []

    def get(self, key):
        self._queued.append(("get", (key,)))
        return self

    def xlen(self, key):
        self._queued.append(("xlen", (key,)))
        return self

    def xadd(self, key, fields):
        self._queued.append(("xadd", (key, fields)))
        return self

    def incr(self, key):
        self._queued.append(("incr", (key,)))
        return self

    def set(self, key, value):
        self._queued.append(("set", (key, value)))
        return self

    async def execute(self):
        results = []
        for name, args in self._queued:
            results.append(await getattr(self._redis, name)(*args))
        self._queued.clear()
        return results


class FakeRedis:
    """Enough Redis for the hot path, plus a log of what happened in what order."""

    def __init__(self, store: dict | None = None, *, fail_on: set[str] | None = None) -> None:
        self.store: dict[str, str] = dict(store or {})
        self.stream: list[dict] = []
        self.hashes: dict[str, dict[str, int]] = {}
        self.log: list[str] = []
        self.fail_on = fail_on or set()

    def _maybe_fail(self, command: str) -> None:
        if command in self.fail_on:
            raise RedisConnectionError(f"fake redis: {command} is down")

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    async def get(self, key):
        self._maybe_fail("get")
        self.log.append(f"get {key}")
        return self.store.get(key)

    async def mget(self, *keys):
        self._maybe_fail("mget")
        self.log.append("mget " + ",".join(keys))
        return [self.store.get(key) for key in keys]

    async def set(self, key, value, ex=None, nx=False):
        self._maybe_fail("set")
        self.log.append(f"set {key}")
        self.store[key] = str(value)
        return True

    async def incr(self, key):
        self._maybe_fail("incr")
        self.log.append(f"incr {key}")
        self.store[key] = str(int(self.store.get(key, 0)) + 1)
        return int(self.store[key])

    async def xadd(self, key, fields):
        self._maybe_fail("xadd")
        self.log.append("xadd")
        self.stream.append(dict(fields))
        return f"0-{len(self.stream)}"

    async def xlen(self, key):
        self._maybe_fail("xlen")
        return len(self.stream)

    async def hincrby(self, key, field, amount):
        self._maybe_fail("hincrby")
        self.log.append(f"hincrby {field}")
        self.hashes.setdefault(key, {})
        self.hashes[key][field] = self.hashes[key].get(field, 0) + amount
        return self.hashes[key][field]


CUSTOMER = "11111111-1111-1111-1111-111111111111"
KEY_ID = "22222222-2222-2222-2222-222222222222"
SECRET = "test-key-material"


def authorised_store(**extra) -> dict:
    """A Redis keyspace in which SECRET is a live key and the counters are trustworthy."""
    digest = keys_repo.hash_key(SECRET)
    store = {
        keys_repo.auth_cache_key(digest): f"{CUSTOMER}|{KEY_ID}|{digest}",
        usage_repo.COUNTERS_AUTHORITATIVE: "1",
    }
    store.update(extra)
    return store


def make_context(redis: FakeRedis) -> HotPathContext:
    return HotPathContext(
        settings=get_settings(),
        hot=HotPathSettings(),
        redis=redis,
        session_factory=_exploding_session_factory,
    )


def _exploding_session_factory():  # pragma: no cover - called only if the test is wrong
    raise AssertionError("the hot path reached Postgres; it must not (invariant #1)")


def app_returning(status_code: int, *, raises: bool = False):
    async def app(scope, receive, send):
        if raises:
            raise RuntimeError("handler exploded")
        body = json.dumps({"served": True}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


async def call(middleware, *, key: str | None = SECRET, path: str = "/v1/echo"):
    """Drive the middleware as a server would, recording every ASGI message sent."""
    headers = [(b"host", b"test")]
    if key is not None:
        headers.append((b"x-api-key", key.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "scheme": "http",
    }
    sent: list[dict] = []

    async def send(message):
        if message["type"] == "http.response.start":
            middleware.context.cache.log.append("RESPONSE SENT")
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware(scope, receive, send)
    return sent


def status_of(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def header_of(sent: list[dict], name: bytes) -> str | None:
    start = next(m for m in sent if m["type"] == "http.response.start")
    for key, value in start["headers"]:
        if key.lower() == name:
            return value.decode()
    return None


def body_of(sent: list[dict]) -> dict:
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return json.loads(raw)


# ---------------------------------------------------------------------------------------
# ADR-0007: the billability rule, every row of the table
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "outcome", "billable"),
    [
        (200, "success", True),
        (204, "success", True),
        (400, "client_error", True),
        (404, "client_error", True),
        (422, "client_error", True),
        (429, "client_error", True),
        (401, "unauthenticated", False),
        (403, "unauthenticated", False),
        (402, "limit_refused", False),
        (500, "server_error", False),
        (503, "server_error", False),
    ],
)
def test_billability_matches_adr_0007(status_code, outcome, billable):
    decision = classify(status_code)
    assert (decision.name, decision.billable) == (outcome, billable)


# ---------------------------------------------------------------------------------------
# ADR-0018: the ordering. This is the point of the whole design.
# ---------------------------------------------------------------------------------------


async def test_the_event_is_written_before_the_response_is_sent():
    redis = FakeRedis(authorised_store())
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware)

    assert status_of(sent) == 200
    assert "xadd" in redis.log and "RESPONSE SENT" in redis.log
    assert redis.log.index("xadd") < redis.log.index("RESPONSE SENT"), (
        "the usage event must reach Redis BEFORE the response reaches the customer "
        "(ADR-0018); capture-after-send would make 'no request goes unbilled' false"
    )


async def test_a_dead_redis_at_capture_time_refuses_the_answer_we_computed():
    """The other half of the ordering guarantee: we would rather not answer than answer
    without recording. Nothing has been sent when the XADD fails, so we can still refuse."""
    redis = FakeRedis(authorised_store(), fail_on={"xadd"})
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware)

    assert status_of(sent) == 503
    assert header_of(sent, b"retry-after") is not None
    assert body_of(sent)["reason"] == "usage_capture_failed"
    assert body_of(sent)["billed"] is False
    assert "served" not in body_of(sent), "the handler's answer must not leak out"


async def test_one_xadd_per_request_and_no_batching():
    """ADR-0018 §2 forbids in-process batching, so N requests are N stream entries and N
    counter increments -- never one flush of N."""
    redis = FakeRedis(authorised_store())
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    for _ in range(7):
        await call(middleware)

    assert len(redis.stream) == 7
    assert redis.log.count("xadd") == 7
    assert redis.store[usage_repo.billable_counter_key(CUSTOMER, _period())] == "7"


def _period() -> str:
    return usage_repo.current_period().label


async def test_the_event_carries_everything_the_drain_needs():
    redis = FakeRedis(authorised_store())
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware)
    event = redis.stream[0]

    assert set(event) >= {
        "idempotency_key",
        "customer_id",
        "api_key_id",
        "occurred_at",
        "billing_period_start",
        "status_code",
        "outcome",
        "billable",
    }
    assert event["customer_id"] == CUSTOMER
    assert event["api_key_id"] == KEY_ID
    assert event["billable"] == "1"
    assert 8 <= len(event["idempotency_key"]) <= 128  # the schema's CHECK constraint
    # The key is minted WITH the event and handed to the customer, so a support request
    # about one response can be traced to exactly one usage row.
    assert header_of(sent, b"x-request-id") == event["idempotency_key"]


async def test_idempotency_keys_are_unique_per_request():
    redis = FakeRedis(authorised_store())
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    for _ in range(20):
        await call(middleware)

    assert len({event["idempotency_key"] for event in redis.stream}) == 20


async def test_a_client_error_is_billed_and_a_server_error_is_not():
    redis = FakeRedis(authorised_store())
    client = UsageMeteringMiddleware(app_returning(422), make_context(redis))
    await call(client)
    assert len(redis.stream) == 1
    assert redis.stream[0]["outcome"] == "client_error"

    server = UsageMeteringMiddleware(app_returning(500), make_context(redis))
    await call(server)
    assert len(redis.stream) == 1, "our own failure is never billed (ADR-0007)"
    assert redis.hashes[usage_repo.nonbillable_key(_period())][f"{CUSTOMER}:server_error"] == 1


async def test_a_handler_that_raises_is_counted_but_never_billed():
    redis = FakeRedis(authorised_store())
    middleware = UsageMeteringMiddleware(app_returning(200, raises=True), make_context(redis))

    with pytest.raises(RuntimeError):
        await call(middleware)

    assert redis.stream == []
    assert redis.hashes[usage_repo.nonbillable_key(_period())][f"{CUSTOMER}:server_error"] == 1


async def test_an_unauthenticated_request_never_reaches_the_stream():
    # Negative results are cached too, so a client spraying bad keys costs one Postgres
    # probe per 30 seconds rather than one per request.
    unknown = keys_repo.auth_cache_key(keys_repo.hash_key("not-a-key"))
    redis = FakeRedis(authorised_store(**{unknown: "-"}))
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware, key="not-a-key")

    assert status_of(sent) == 401
    assert redis.stream == [], "billing unauthenticated traffic is an attack vector"


async def test_unmetered_paths_are_not_touched():
    redis = FakeRedis(authorised_store())
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware, key=None, path="/healthz")

    assert status_of(sent) == 200
    assert [entry for entry in redis.log if entry != "RESPONSE SENT"] == []
    assert redis.stream == []


async def test_capture_disabled_is_the_adr_0014_baseline():
    """The mode that produces the 'without capture' number, and nothing else."""
    redis = FakeRedis(authorised_store())
    context = make_context(redis)
    context.hot = HotPathSettings(capture_enabled=False)
    middleware = UsageMeteringMiddleware(app_returning(200), context)

    sent = await call(middleware)

    assert status_of(sent) == 200
    assert redis.stream == [], "capture off means no event and no counter"
    assert redis.log == [f"get {keys_repo.auth_cache_key(keys_repo.hash_key(SECRET))}",
                         "RESPONSE SENT"]


# ---------------------------------------------------------------------------------------
# The same guarantee against the running stack
# ---------------------------------------------------------------------------------------

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")


def admin(method: str, path: str, **kwargs) -> httpx.Response:
    return httpx.request(method, f"{BASE_URL}{path}", timeout=15, **kwargs)


@pytest.fixture(scope="module")
def live_customer() -> dict:
    """A real customer, provisioned through the admin surface, on Starter."""
    response = admin(
        "POST",
        "/admin/customers",
        json={"name": f"capture-tests-{os.getpid()}-{id(object())}", "plan": "Starter"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def usage_of(customer_id: str) -> dict:
    response = admin("GET", f"/admin/customers/{customer_id}/usage")
    assert response.status_code == 200, response.text
    return response.json()


def test_a_served_request_is_in_the_stream_by_the_time_the_client_has_the_answer(
    base_url, live_customer
):
    """End-to-end ordering: the client is holding the response, so the XADD has happened.

    Not a race -- if capture ran after the send, this would be flaky by construction, and
    that is exactly what it is here to catch.
    """
    import redis as sync_redis

    response = httpx.get(
        f"{base_url}/v1/echo", headers={"X-API-Key": live_customer["api_key"]}, timeout=10
    )
    assert response.status_code == 200
    request_id = response.headers["x-request-id"]

    client = sync_redis.from_url(get_settings().redis_url, decode_responses=True)
    entries = client.xrevrange(get_settings().usage_stream_key, count=200)
    ids = {fields.get("idempotency_key") for _, fields in entries}
    assert request_id in ids, "the response was released before its usage reached Redis"


def test_the_counter_moves_once_per_served_request(base_url, live_customer):
    before = usage_of(live_customer["customer_id"])["billable_requests"]
    for _ in range(5):
        response = httpx.get(
            f"{base_url}/v1/echo", headers={"X-API-Key": live_customer["api_key"]}, timeout=10
        )
        assert response.status_code == 200
    after = usage_of(live_customer["customer_id"])["billable_requests"]
    assert after - before == 5


def test_a_404_under_v1_is_billed_and_a_health_check_is_not(base_url, live_customer):
    headers = {"X-API-Key": live_customer["api_key"]}
    before = usage_of(live_customer["customer_id"])["billable_requests"]

    assert httpx.get(f"{base_url}/v1/nope", headers=headers, timeout=10).status_code == 404
    assert httpx.get(f"{base_url}/healthz", timeout=10).status_code == 200

    after = usage_of(live_customer["customer_id"])["billable_requests"]
    assert after - before == 1, "a client's mistyped path is work we did; a healthcheck is not"


def test_concurrent_traffic_is_counted_exactly_once_each(base_url, live_customer):
    """No request goes unbilled, and none is billed twice, at the only concurrency this
    laptop can produce."""
    requests = 60

    async def fire() -> list[int]:
        headers = {"X-API-Key": live_customer["api_key"]}
        limits = httpx.Limits(max_connections=30)
        async with httpx.AsyncClient(limits=limits, timeout=20) as client:
            responses = await asyncio.gather(
                *(client.get(f"{base_url}/v1/echo", headers=headers) for _ in range(requests))
            )
        return [response.status_code for response in responses]

    before = usage_of(live_customer["customer_id"])["billable_requests"]
    statuses = asyncio.run(fire())
    after = usage_of(live_customer["customer_id"])["billable_requests"]

    assert statuses.count(200) == requests
    assert after - before == requests
