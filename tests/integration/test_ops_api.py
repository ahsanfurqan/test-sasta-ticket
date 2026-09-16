"""The pipeline operations API. Owned by pipeline.

Closing a month was CLI-only, which meant a walkthrough had to shell into a container
mid-demo and an incident had to do the same. These endpoints call the SAME functions the
scheduled loops call, so what a demo exercises is the production path.

It runs on the worker, not the customer API, because `meter.api` may not import
`meter.pipeline` -- the trigger belongs with the owner of the orchestration. That is why
these tests use a different base URL.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.integration

OPS_URL = os.environ.get("TEST_OPS_URL", "http://worker:8001")


@pytest.fixture(scope="module")
def ops() -> str:
    try:
        httpx.get(f"{OPS_URL}/healthz", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"ops api not reachable at {OPS_URL}: {exc}")
    return OPS_URL


def _customer_with_traffic(base_url: str, requests: int = 5) -> dict:
    customer = httpx.post(
        f"{base_url}/admin/customers",
        json={"name": f"ops-close-{id(object())}", "plan": "Growth"},
        timeout=20,
    ).json()
    headers = {"X-API-Key": customer["api_key"]}
    for _ in range(requests):
        httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10)
    return customer


class TestClosingAMonthOverHttp:
    def test_it_reconciles_before_it_issues(self, ops, base_url):
        """ADR-0010's whole point. The response carries the reconciliation number AND the
        SQL behind it, so an invoice is never issued without showing what was checked."""
        customer = _customer_with_traffic(base_url)

        body = httpx.post(
            f"{ops}/ops/close-customer",
            json={"customer_id": customer["customer_id"]},
            timeout=180,
        ).json()

        assert body["converged"] is True
        assert body["invoice"] is not None
        assert "reconciliation for customer" in body["explain"]
        assert "unexplained" in body["explain"]
        assert "FROM usage_events" in body["explain"], "the query must travel with the number"

    def test_re_closing_returns_the_same_invoice_rather_than_a_second(self, ops, base_url):
        """An issued invoice is immutable, so regeneration must agree with it or fail
        loudly. It must never quietly produce a second document."""
        customer = _customer_with_traffic(base_url)
        payload = {"customer_id": customer["customer_id"]}

        first = httpx.post(f"{ops}/ops/close-customer", json=payload, timeout=180).json()
        second = httpx.post(f"{ops}/ops/close-customer", json=payload, timeout=180).json()

        assert first["invoice"]["invoice_number"] == second["invoice"]["invoice_number"]
        assert first["invoice"]["total_paisa"] == second["invoice"]["total_paisa"]
        assert second["invoice"]["already_existed"] is True

    def test_the_customer_can_then_fetch_it_with_their_own_key(self, ops, base_url):
        """The two halves of the demo meet: closed over the ops API, read over the
        customer API with nothing but an API key."""
        customer = _customer_with_traffic(base_url)
        closed = httpx.post(
            f"{ops}/ops/close-customer",
            json={"customer_id": customer["customer_id"]},
            timeout=180,
        ).json()
        number = closed["invoice"]["invoice_number"]

        fetched = httpx.get(
            f"{base_url}/v1/invoices/{number}",
            headers={"X-API-Key": customer["api_key"]},
            timeout=20,
        ).json()

        assert fetched["invoice_number"] == number
        assert fetched["total_paisa"] == closed["invoice"]["total_paisa"]
        assert fetched["lines_sum_to_total"] is True

    def test_a_malformed_month_is_rejected_rather_than_guessed(self, ops, base_url):
        response = httpx.post(
            f"{ops}/ops/close-customer",
            json={"customer_id": "00000000-0000-0000-0000-000000000000", "month": "September"},
            timeout=60,
        )
        assert response.status_code == 400
        assert "2026-09" in response.json()["detail"]


class TestThereIsNoShortcutToAnInvoice:
    def test_no_endpoint_issues_an_invoice_without_reconciling(self, ops):
        """A bare 'generate the invoice' trigger existed once as a CLI subcommand. Used on a
        customer whose requests were still draining, it issued an immutable invoice
        Rs. 22,049.65 short, which the database then refused to let anyone repair. It must
        not reappear as an HTTP route."""
        paths = httpx.get(f"{ops}/openapi.json", timeout=20).json()["paths"]
        for path in paths:
            assert "invoice" not in path.lower(), (
                f"{path} looks like a way to issue an invoice without draining and "
                "reconciling first (ADR-0010)"
            )


class TestTheOtherStagesAreReachable:
    @pytest.mark.parametrize("stage", ["drain", "aggregate", "thresholds"])
    def test_each_stage_can_be_triggered(self, ops, stage):
        response = httpx.post(f"{ops}/ops/{stage}", timeout=180)
        assert response.status_code == 200, response.text

    def test_reconcile_reports_the_number_that_must_be_zero(self, ops, base_url):
        customer = _customer_with_traffic(base_url)
        httpx.post(f"{ops}/ops/drain", timeout=180)
        httpx.post(f"{ops}/ops/aggregate", timeout=180)

        body = httpx.get(
            f"{ops}/ops/reconcile/{customer['customer_id']}", timeout=60
        ).json()

        assert "unexplained" in body["explain"]
        assert body["redis_counter"] >= 0
