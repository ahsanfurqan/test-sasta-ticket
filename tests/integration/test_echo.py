"""The echo route proves the container talks to Postgres and Redis. That is all it proves.

Needs the stack up: `make up`, then `make test`.
"""

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_healthz_is_unauthenticated(base_url):
    response = httpx.get(f"{base_url}/healthz", timeout=5)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_reports_both_dependencies(base_url):
    response = httpx.get(f"{base_url}/readyz", timeout=5)
    assert response.status_code == 200
    assert response.json()["checks"] == {"postgres": "ok", "redis": "ok"}


def test_echo_requires_an_api_key(base_url):
    response = httpx.get(f"{base_url}/v1/echo", timeout=5)
    assert response.status_code == 401


def test_echo_rejects_a_wrong_api_key(base_url):
    response = httpx.get(
        f"{base_url}/v1/echo", headers={"X-API-Key": "not-the-key"}, timeout=5
    )
    assert response.status_code == 401


def test_echo_answers_and_reaches_postgres_and_redis(base_url, api_key):
    response = httpx.get(
        f"{base_url}/v1/echo",
        params={"message": "hello"},
        headers={"X-API-Key": api_key},
        timeout=5,
    )
    assert response.status_code == 200

    body = response.json()
    assert body["message"] == "hello"
    assert body["dependencies"] == {"postgres": "ok", "redis": "ok"}
