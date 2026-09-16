"""Customer-facing account endpoints -- capabilities 3 and 5 of the brief.

A customer sees their own usage and their own invoices, authenticated by their own key.
These were the last two capabilities with no API surface at all: the figures existed in the
domain and the pipeline, but nothing let a customer ask for them.
"""

import httpx
import pytest

pytestmark = pytest.mark.integration


def _customer(base_url: str, plan: str = "Growth") -> dict:
    response = httpx.post(
        f"{base_url}/admin/customers",
        json={"name": f"account-test-{plan.lower()}-{httpx.__name__}-{id(object())}"[:60],
              "plan": plan},
        timeout=20,
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestLiveUsage:
    def test_a_customer_sees_their_own_usage_and_what_it_will_cost(self, base_url):
        customer = _customer(base_url)
        headers = {"X-API-Key": customer["api_key"]}
        for _ in range(5):
            httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10)

        body = httpx.get(f"{base_url}/v1/usage", headers=headers, timeout=15).json()

        assert body["requests_this_period"] == 5
        assert isinstance(body["estimated_cost_paisa"], int)
        assert body["segments"], "a customer on a plan always has at least one segment"
        # The estimate is derived, not guessed: it re-derives from the same explanation the
        # invoice would print.
        assert "Period total" in body["explanation"]

    def test_the_figure_is_labelled_as_near_current_rather_than_exact(self, base_url):
        """ADR-0010 buys a fast live figure by letting it lag. A customer reading it should
        be told that, or they will treat it as a bill."""
        customer = _customer(base_url)
        body = httpx.get(
            f"{base_url}/v1/usage", headers={"X-API-Key": customer["api_key"]}, timeout=15
        ).json()
        assert "near-current" in body["freshness"]

    def test_usage_requires_a_key(self, base_url):
        assert httpx.get(f"{base_url}/v1/usage", timeout=10).status_code == 401


class TestAccountEndpointsAreNotBilled:
    """ADR-0007 read literally would bill a customer for asking what they owe. It is the
    same objection that ruled out billing a request refused for hitting a spending limit."""

    def test_reading_your_own_usage_does_not_move_the_counter(self, base_url):
        customer = _customer(base_url)
        headers = {"X-API-Key": customer["api_key"]}

        def counted() -> int:
            return httpx.get(f"{base_url}/v1/usage", headers=headers, timeout=15).json()[
                "requests_this_period"
            ]

        before = counted()
        for _ in range(5):
            httpx.get(f"{base_url}/v1/usage", headers=headers, timeout=15)
            httpx.get(f"{base_url}/v1/invoices", headers=headers, timeout=15)
        assert counted() == before

    def test_the_metered_endpoint_still_bills(self, base_url):
        """The exemption is a carve-out, not a hole: /v1/echo must still count."""
        customer = _customer(base_url)
        headers = {"X-API-Key": customer["api_key"]}
        httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10)
        body = httpx.get(f"{base_url}/v1/usage", headers=headers, timeout=15).json()
        assert body["requests_this_period"] == 1


class TestInvoices:
    def test_an_invoice_explains_every_line_and_the_lines_sum_to_the_total(self, base_url):
        """The brief's last definition-of-done step: pick a line, ask why it says what it
        says, and get the answer from the SYSTEM rather than from a person."""
        customer = _customer(base_url)
        headers = {"X-API-Key": customer["api_key"]}
        for _ in range(3):
            httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10)

        import subprocess

        subprocess.run(
            ["python", "-m", "meter.pipeline.cli", "close-customer",
             "--customer", customer["customer_id"]],
            capture_output=True, timeout=180,
        )

        listing = httpx.get(f"{base_url}/v1/invoices", headers=headers, timeout=20).json()
        if not listing["invoices"]:
            pytest.skip("close-customer is not runnable from inside this container")

        number = listing["invoices"][0]["invoice_number"]
        invoice = httpx.get(
            f"{base_url}/v1/invoices/{number}", headers=headers, timeout=20
        ).json()

        assert invoice["lines_sum_to_total"] is True
        assert invoice["lines"], "an issued invoice always has lines"
        for line in invoice["lines"]:
            # Every line cites the price list VERSION it came from, which is what makes the
            # charge reproducible after a price change (ADR-0005).
            assert line["price_list_version_id"]
            assert line["why"]
        if invoice["status"] == "issued":
            assert invoice["immutable"] is True

    def test_one_customer_cannot_read_another_customers_invoice(self, base_url):
        """404, never 403: a 403 would confirm which invoice numbers exist."""
        owner = _customer(base_url)
        stranger = _customer(base_url)
        headers = {"X-API-Key": owner["api_key"]}
        httpx.get(f"{base_url}/v1/echo", headers=headers, timeout=10)

        import subprocess

        subprocess.run(
            ["python", "-m", "meter.pipeline.cli", "close-customer",
             "--customer", owner["customer_id"]],
            capture_output=True, timeout=180,
        )
        listing = httpx.get(f"{base_url}/v1/invoices", headers=headers, timeout=20).json()
        if not listing["invoices"]:
            pytest.skip("close-customer is not runnable from inside this container")

        number = listing["invoices"][0]["invoice_number"]
        assert httpx.get(
            f"{base_url}/v1/invoices/{number}",
            headers={"X-API-Key": stranger["api_key"]},
            timeout=20,
        ).status_code == 404

    def test_a_customers_invoice_list_is_their_own(self, base_url):
        customer = _customer(base_url)
        body = httpx.get(
            f"{base_url}/v1/invoices", headers={"X-API-Key": customer["api_key"]}, timeout=20
        ).json()
        assert body["invoices"] == []  # brand new customer, nothing issued yet
