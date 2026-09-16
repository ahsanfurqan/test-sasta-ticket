"""Money representation. See docs/adr/0004-money-as-integer-paisa.md.

Session 1 has no pricing math to test -- these guard the representation itself, which is
the invariant everything else will rest on.
"""

import pytest

from meter.money import PAISA_PER_RUPEE, format_paisa, rupees


def test_rupees_converts_to_integer_paisa():
    assert rupees(15_000) == 1_500_000
    assert rupees(0) == 0
    assert isinstance(rupees(90_000), int)


def test_the_brief_plan_fees_are_exact_in_paisa():
    # Starter, Growth, Scale monthly fees. No rounding, no remainder.
    assert (rupees(0), rupees(15_000), rupees(90_000)) == (0, 1_500_000, 9_000_000)


@pytest.mark.parametrize(
    "price_rupees, expected_paisa",
    [("0.80", 80), ("0.50", 50), ("0.35", 35), ("0.25", 25), ("0.15", 15)],
)
def test_every_per_request_price_is_a_whole_number_of_paisa(price_rupees, expected_paisa):
    """The reason per-request rating needs no rounding at all: every band price in the
    brief lands exactly on a paisa. Division only enters through proration."""
    whole, _, fraction = price_rupees.partition(".")
    assert int(whole) * PAISA_PER_RUPEE + int(fraction) == expected_paisa


@pytest.mark.parametrize(
    "amount, expected",
    [
        (0, "Rs. 0.00"),
        (80, "Rs. 0.80"),
        (1_500_000, "Rs. 15,000.00"),
        (33_500_000, "Rs. 335,000.00"),  # the brief's worked example total
        (-50, "-Rs. 0.50"),
    ],
)
def test_format_paisa_renders_rupees_for_display(amount, expected):
    assert format_paisa(amount) == expected


@pytest.mark.parametrize("bad", [1.5, 0.0, "100", None, True])
def test_format_paisa_rejects_anything_that_is_not_int_paisa(bad):
    """A float reaching a money path is a defect, not a style preference -- including
    True, which is an int in Python and would otherwise format as Rs. 0.01."""
    with pytest.raises(TypeError):
        format_paisa(bad)
