"""The ladder against the numbers the brief published, and the boundaries around them.

Property tests prove the ladder is internally consistent for any input. These prove it
agrees with what the customer was promised. Both are needed: a ladder can be perfectly
self-consistent and still charge the wrong price.
"""

import pytest

from meter.domain.catalogue import GROWTH_V1, SCALE_V1, STARTER_V1
from meter.domain.plans import Band, PriceList
from meter.domain.rating import USAGE, max_quantity_within, rate
from meter.money import rupees


def test_the_briefs_worked_example():
    """Growth, 1,200,000 requests. The brief states Rs. 335,000."""
    charge = rate(1_200_000, GROWTH_V1)

    assert charge.total_paisa == rupees(335_000)

    usage = [line for line in charge.lines if line.kind == USAGE]
    assert [(line.quantity, line.unit_price_paisa, line.amount_paisa) for line in usage] == [
        (500_000, 50, rupees(250_000)),   # the next 500,000 at Rs. 0.50
        (200_000, 35, rupees(70_000)),    # the remaining 200,000 at Rs. 0.35
    ]


@pytest.mark.parametrize(
    "price_list, quantity, expected_rupees",
    [
        # Starter: Rs. 0/month, 10,000 included, Rs. 0.80 after
        (STARTER_V1, 0, 0),
        (STARTER_V1, 10_000, 0),            # exactly the allowance
        (STARTER_V1, 25_000, 12_000),       # 15,000 x 0.80
        # Growth: Rs. 15,000/month, 500,000 included
        (GROWTH_V1, 0, 15_000),             # the fee is owed even with no usage
        (GROWTH_V1, 500_000, 15_000),       # exactly the allowance
        (GROWTH_V1, 1_000_000, 265_000),    # 15,000 + 500,000 x 0.50
        (GROWTH_V1, 1_200_000, 335_000),    # the brief's example
        # Scale: Rs. 90,000/month, 5,000,000 included
        (SCALE_V1, 5_000_000, 90_000),
        (SCALE_V1, 12_000_000, 1_640_000),  # 90,000 + 5M x 0.25 + 2M x 0.15
    ],
)
def test_published_plans_charge_what_was_advertised(price_list, quantity, expected_rupees):
    assert rate(quantity, price_list).total_paisa == rupees(expected_rupees)


@pytest.mark.parametrize(
    "price_list, quantity, expected_paisa",
    [
        (STARTER_V1, 10_001, 80),                      # first chargeable request
        (GROWTH_V1, 500_001, rupees(15_000) + 50),     # first request past the allowance
        (GROWTH_V1, 1_000_001, rupees(265_000) + 35),  # first request in the cheaper band
        (SCALE_V1, 10_000_001, rupees(1_340_000) + 15),
    ],
)
def test_the_request_either_side_of_every_band_edge(price_list, quantity, expected_paisa):
    """Band edges are where ladder maths dies. One request past each boundary, exactly."""
    assert rate(quantity, price_list).total_paisa == expected_paisa


def test_crossing_into_a_cheaper_band_does_not_reprice_what_came_before():
    """The brief is explicit: the cheaper price applies only to requests in that band. It is
    not a discount applied to everything once the customer crosses the line."""
    at_boundary = rate(1_000_000, GROWTH_V1).total_paisa
    past_boundary = rate(1_000_001, GROWTH_V1).total_paisa

    assert past_boundary - at_boundary == 35          # only the new request is cheaper
    assert at_boundary == rupees(265_000)             # earlier requests still cost Rs. 0.50


def test_a_charge_can_be_read_aloud_to_a_customer():
    explanation = rate(1_200_000, GROWTH_V1).explain()

    assert "Growth monthly fee: Rs. 15,000.00" in explanation
    assert "requests 500,001-1,000,000 at Rs. 0.50 each" in explanation
    assert "requests 1,000,001-1,200,000 at Rs. 0.35 each" in explanation
    assert "Total: Rs. 335,000.00" in explanation


class TestSpendingLimitThreshold:
    """ADR-0008's inversion, against ADR-0012's semantics (the limit caps the TOTAL bill)."""

    def test_a_growth_customer_with_a_50000_limit(self):
        # Rs. 15,000 fee leaves Rs. 35,000 of headroom; at Rs. 0.50 that is 70,000 requests
        # on top of the 500,000 included.
        threshold = max_quantity_within(rupees(50_000), GROWTH_V1)

        assert threshold == 570_000
        assert rate(threshold, GROWTH_V1).total_paisa == rupees(50_000)      # exactly at it
        assert rate(threshold + 1, GROWTH_V1).total_paisa > rupees(50_000)   # one over

    def test_a_limit_below_the_monthly_fee_is_unsatisfiable(self):
        """ADR-0012 requires this be rejected when the customer sets it, not discovered when
        their traffic stops. The domain reports it; enforcement is the caller's job."""
        assert max_quantity_within(rupees(50_000), SCALE_V1) == 0
        assert rate(0, SCALE_V1).total_paisa == rupees(90_000)

    def test_a_limit_that_only_covers_the_fee_allows_the_included_allowance(self):
        threshold = max_quantity_within(rupees(15_000), GROWTH_V1)
        assert threshold == 500_000  # every included request, no chargeable ones

    def test_an_unbounded_free_band_is_rejected_rather_than_looping_forever(self):
        never_chargeable = PriceList(
            name="Broken", version=1, monthly_fee_paisa=0, included_quantity=0,
            bands=(Band(up_to=None, unit_price_paisa=0),),
        )
        with pytest.raises(ValueError, match="no spending limit can ever be reached"):
            max_quantity_within(rupees(1_000), never_chargeable)


class TestPriceListValidation:
    def test_money_must_be_integer_paisa(self):
        with pytest.raises(TypeError):
            Band(up_to=None, unit_price_paisa=0.8)   # Rs. 0.80 as a float
        with pytest.raises(TypeError):
            PriceList(name="x", version=1, monthly_fee_paisa=15_000.0, included_quantity=0,
                      bands=(Band(up_to=None, unit_price_paisa=80),))

    def test_the_final_band_must_be_unbounded(self):
        with pytest.raises(ValueError, match="final band must be unbounded"):
            PriceList(name="x", version=1, monthly_fee_paisa=0, included_quantity=0,
                      bands=(Band(up_to=100, unit_price_paisa=80),))

    def test_band_bounds_must_strictly_increase(self):
        with pytest.raises(ValueError, match="strictly increase"):
            PriceList(name="x", version=1, monthly_fee_paisa=0, included_quantity=0,
                      bands=(Band(up_to=500, unit_price_paisa=50),
                             Band(up_to=100, unit_price_paisa=35),
                             Band(up_to=None, unit_price_paisa=25)))

    def test_negative_money_is_refused(self):
        with pytest.raises(ValueError):
            Band(up_to=None, unit_price_paisa=-1)

    def test_a_negative_quantity_cannot_be_rated(self):
        with pytest.raises(ValueError):
            rate(-1, GROWTH_V1)
