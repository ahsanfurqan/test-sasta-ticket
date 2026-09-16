"""API key authentication: the cache, the constant-time compare, and the 30s window.

ADR-0015's guarantees are specific, so the tests are too:

* multiple live keys per customer, all of them working, because that is what makes rotation
  something a customer will actually do;
* keys stored only as a digest and shown exactly once;
* a cache HIT costs no Postgres call -- proved by handing the hot path a session factory
  that raises if it is touched, rather than by reading the code and believing it;
* revocation effective within the auth cache TTL, which is the stated window and is
  asserted against the actual TTL on the actual key rather than against the constant.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest
import redis as sync_redis

from meter.api import auth
from meter.api.metering import UsageMeteringMiddleware
from meter.config import get_settings
from meter.storage.repositories import keys as keys_repo
from tests.integration.test_capture import (
    CUSTOMER,
    KEY_ID,
    SECRET,
    FakeRedis,
    app_returning,
    authorised_store,
    call,
    make_context,
    status_of,
)

pytestmark = pytest.mark.integration

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="module")
def customer() -> dict:
    response = httpx.post(
        f"{BASE_URL}/admin/customers",
        json={"name": f"auth-tests-{os.getpid()}-{time.time()}", "plan": "Starter"},
        timeout=15,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def redis_client():
    client = sync_redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    client.close()


# ---------------------------------------------------------------------------------------
# Resolution, in process: what the cache costs and what it does not
# ---------------------------------------------------------------------------------------


async def test_a_cache_hit_never_touches_postgres():
    """Hot-path invariant #1. `make_context` hands over a session factory that raises."""
    redis = FakeRedis(authorised_store())
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(redis))

    sent = await call(middleware)

    assert status_of(sent) == 200


async def test_a_tampered_cache_entry_is_refused():
    """The digest travels in the cached value and is compared with `compare_digest`, so an
    entry that does not match the key presented resolves to nobody."""
    digest = keys_repo.hash_key(SECRET)
    store = authorised_store()
    store[keys_repo.auth_cache_key(digest)] = (
        f"{CUSTOMER}|{KEY_ID}|{'0' * 64}|{time.time() + 3600:.0f}"
    )
    middleware = UsageMeteringMiddleware(app_returning(200), make_context(FakeRedis(store)))

    assert status_of(await call(middleware)) == 401


def test_the_cache_encoding_holds_a_digest_and_never_a_key():
    record = keys_repo.KeyRecord(
        key_id=KEY_ID, customer_id=CUSTOMER, key_hash=keys_repo.hash_key(SECRET), revoked_at=None
    )
    encoded = auth.encode(record, time.time() + 30)
    assert SECRET not in encoded
    assert auth.decode(encoded, record.key_hash) == auth.Caller(CUSTOMER, KEY_ID)
    assert auth.decode(encoded, keys_repo.hash_key("another-key")) is None


def test_a_key_is_stored_only_as_a_sha256_digest():
    key, prefix = keys_repo.generate_key()
    assert key.startswith(prefix + "_")
    assert prefix not in keys_repo.hash_key(key)
    assert len(keys_repo.hash_key(key)) == 64


# ---------------------------------------------------------------------------------------
# Against the running stack
# ---------------------------------------------------------------------------------------


def test_no_key_and_a_wrong_key_are_both_refused_and_neither_is_billed(base_url):
    without = httpx.get(f"{base_url}/v1/echo", timeout=10)
    assert without.status_code == 401
    assert without.json()["billed"] is False
    assert without.headers["www-authenticate"] == "X-API-Key"

    wrong = httpx.get(f"{base_url}/v1/echo", headers={"X-API-Key": "nope"}, timeout=10)
    assert wrong.status_code == 401
    assert "x-request-id" not in wrong.headers


def test_a_provisioned_key_resolves_to_its_own_customer(base_url, customer):
    response = httpx.get(
        f"{base_url}/v1/echo", headers={"X-API-Key": customer["api_key"]}, timeout=10
    )
    assert response.status_code == 200
    assert response.json()["customer_id"] == customer["customer_id"]
    assert response.json()["api_key_id"] == customer["api_key_id"]


def test_a_customer_may_hold_several_live_keys(base_url, customer):
    second = httpx.post(
        f"{base_url}/admin/customers/{customer['customer_id']}/keys",
        json={"label": "rotation"},
        timeout=15,
    )
    assert second.status_code == 201, second.text
    for key in (customer["api_key"], second.json()["api_key"]):
        response = httpx.get(f"{base_url}/v1/echo", headers={"X-API-Key": key}, timeout=10)
        assert response.status_code == 200
        assert response.json()["customer_id"] == customer["customer_id"]

    listed = httpx.get(
        f"{base_url}/admin/customers/{customer['customer_id']}/keys", timeout=15
    ).json()["keys"]
    assert len(listed) >= 2
    assert all("api_key" not in entry and "key_hash" not in entry for entry in listed)
    assert all(entry["prefix"].startswith("mk_") for entry in listed)


def test_revocation_is_bounded_by_the_auth_cache_ttl(base_url, customer, redis_client):
    """The window is 30 seconds, asserted against the freshness deadline in the cached value.

    Since ADR-0019 the Redis TTL is the STALE CEILING, not the revocation window: a positive
    entry outlives its freshness so that it can be served stale during a Postgres outage. The
    window that ADR-0015 promises is the deadline carried inside the value, and that is what
    this asserts. Deleting the entry afterwards stands in for the deadline passing.
    """
    issued = httpx.post(
        f"{base_url}/admin/customers/{customer['customer_id']}/keys",
        json={"label": "to be revoked"},
        timeout=15,
    ).json()

    assert (
        httpx.get(
            f"{base_url}/v1/echo", headers={"X-API-Key": issued["api_key"]}, timeout=10
        ).status_code
        == 200
    )

    revoked = httpx.delete(f"{base_url}/admin/keys/{issued['api_key_id']}", timeout=15)
    assert revoked.status_code == 200
    assert revoked.json()["effective_within_seconds"] == 30

    cache_key = keys_repo.auth_cache_key(keys_repo.hash_key(issued["api_key"]))

    remaining = auth.fresh_until(redis_client.get(cache_key)) - time.time()
    assert 0 < remaining <= 30, "a revoked key must not stay FRESH beyond 30s (ADR-0015)"

    ttl = redis_client.ttl(cache_key)
    assert 30 < ttl <= 900, "the Redis TTL is the stale ceiling, not the window (ADR-0019)"

    redis_client.delete(cache_key)
    after = httpx.get(
        f"{base_url}/v1/echo", headers={"X-API-Key": issued["api_key"]}, timeout=10
    )
    assert after.status_code == 401


@pytest.mark.skipif(
    not os.environ.get("REVOCATION_WINDOW_TEST"),
    reason="waits out the real 30s TTL; run with REVOCATION_WINDOW_TEST=1",
)
def test_revocation_takes_effect_within_thirty_seconds_in_real_time(base_url, customer):
    issued = httpx.post(
        f"{base_url}/admin/customers/{customer['customer_id']}/keys",
        json={"label": "real time revocation"},
        timeout=15,
    ).json()
    headers = {"X-API-Key": issued["api_key"]}
    assert httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code == 200

    httpx.delete(f"{base_url}/admin/keys/{issued['api_key_id']}", timeout=15)
    started = time.monotonic()
    while time.monotonic() - started < 35:
        if httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10).status_code == 401:
            elapsed = time.monotonic() - started
            assert elapsed <= 31, f"revocation took {elapsed:.1f}s"
            return
        time.sleep(1)
    raise AssertionError("the key still worked 35 seconds after revocation")


def test_usage_served_before_revocation_is_still_billed(base_url, customer, redis_client):
    """ADR-0015: the charge reflects what we served, even if the key is gone by now."""
    issued = httpx.post(
        f"{base_url}/admin/customers/{customer['customer_id']}/keys",
        json={"label": "billed then revoked"},
        timeout=15,
    ).json()

    before = httpx.get(
        f"{base_url}/admin/customers/{customer['customer_id']}/usage", timeout=15
    ).json()["billable_requests"]
    assert (
        httpx.get(
            f"{base_url}/v1/echo", headers={"X-API-Key": issued["api_key"]}, timeout=10
        ).status_code
        == 200
    )
    httpx.delete(f"{base_url}/admin/keys/{issued['api_key_id']}", timeout=15)
    after = httpx.get(
        f"{base_url}/admin/customers/{customer['customer_id']}/usage", timeout=15
    ).json()["billable_requests"]

    assert after - before == 1


def test_the_development_key_is_an_ordinary_hashed_key(base_url, api_key):
    """The local key from .env goes through the same path a customer's does -- no second
    authentication branch that nothing else exercises."""
    response = httpx.get(f"{base_url}/v1/echo", headers={"X-API-Key": api_key}, timeout=10)
    assert response.status_code == 200
    assert response.json()["customer_id"], "the dev key resolves to a real customer row"

