"""Property tests over the band ladder. Owned by test-engineer.

The worked example in the brief proves one input. These prove the ladder over arbitrary
price lists and arbitrary quantities -- including price lists nobody would design, because
a property that only holds for well-behaved input is not a property.

Every assertion here is exact. A tolerance in a money test hides the hole it was added for.
"""

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from meter.domain.catalogue import GROWTH_V1, LAUNCH_PRICE_LISTS, SCALE_V1, STARTER_V1
from meter.domain.plans import Band, PriceList
from meter.domain.rating import USAGE, max_quantity_within, rate


@st.composite
def price_lists(
    draw, max_bands: int = 4, max_included: int = 1_000_000, max_bound: int = 5_000_000
):
    """Arbitrary well-formed price lists.

    Band prices are deliberately NOT forced to decrease. Real catalogues get cheaper as
    volume grows, but the ladder's correctness must not depend on that, and a bug that only
    shows up on an unusual list is exactly what a property test is for.
    """
    band_count = draw(st.integers(min_value=1, max_value=max_bands))
    bounds = sorted(
        draw(
            st.lists(
                st.integers(min_value=1, max_value=max_bound),
                min_size=band_count - 1,
                max_size=band_count - 1,
                unique=True,
            )
        )
    )
    prices = draw(
        st.lists(
            st.integers(min_value=0, max_value=500),
            min_size=band_count - 1,
            max_size=band_count - 1,
        )
    )
    # The final band is unbounded and must cost something, or no spending limit is reachable.
    final_price = draw(st.integers(min_value=1, max_value=500))

    bands = tuple(Band(up_to=b, unit_price_paisa=p) for b, p in zip(bounds, prices, strict=True))
    bands += (Band(up_to=None, unit_price_paisa=final_price),)

    return PriceList(
        name=draw(st.sampled_from(["Starter", "Growth", "Scale", "Negotiated"])),
        version=draw(st.integers(min_value=1, max_value=9)),
        monthly_fee_paisa=draw(st.integers(min_value=0, max_value=50_000_000)),
        included_quantity=draw(st.integers(min_value=0, max_value=max_included)),
        bands=bands,
    )


quantities = st.integers(min_value=0, max_value=50_000_000)


@given(price_list=price_lists(), quantity=quantities)
def test_lines_sum_to_the_total_exactly(price_list, quantity):
    """The decomposition IS the total. If these can drift, no explanation is trustworthy."""
    charge = rate(quantity, price_list)
    assert sum(line.amount_paisa for line in charge.lines) == charge.total_paisa


@given(price_list=price_lists(), quantity=quantities)
def test_every_amount_is_a_non_negative_integer(price_list, quantity):
    charge = rate(quantity, price_list)
    for line in charge.lines:
        assert type(line.amount_paisa) is int  # not bool, not float, not Decimal
        assert line.amount_paisa >= 0
        assert line.quantity >= 0
    assert type(charge.total_paisa) is int
    assert charge.total_paisa >= 0


@given(price_list=price_lists(), a=quantities, b=quantities)
def test_total_is_monotonic_in_quantity(price_list, a, b):
    """More requests never costs less. This is also what makes the limit inversion valid."""
    low, high = sorted((a, b))
    assert rate(low, price_list).total_paisa <= rate(high, price_list).total_paisa


@given(price_list=price_lists(), quantity=quantities)
def test_usage_below_the_allowance_costs_only_the_monthly_fee(price_list, quantity):
    assume(quantity <= price_list.included_quantity)
    assert rate(quantity, price_list).total_paisa == price_list.monthly_fee_paisa


@given(price_list=price_lists(), quantity=quantities)
def test_charged_units_are_exactly_those_beyond_the_allowance(price_list, quantity):
    """No unit is charged twice, and no chargeable unit is skipped."""
    charge = rate(quantity, price_list)
    charged = sum(line.quantity for line in charge.lines if line.kind == USAGE)
    assert charged == max(0, quantity - price_list.included_quantity)


@given(price_list=price_lists(), quantity=st.integers(min_value=0, max_value=5_000_000))
def test_one_more_request_costs_exactly_one_band_price(price_list, quantity):
    """Marginal, not retroactive: the next request is priced at its own band, and crossing a
    boundary never re-prices what came before it."""
    before = rate(quantity, price_list).total_paisa
    after = rate(quantity + 1, price_list).total_paisa
    step = after - before

    if quantity + 1 <= price_list.included_quantity:
        assert step == 0
    else:
        assert step in {band.unit_price_paisa for band in price_list.bands}


@settings(max_examples=50)
@given(
    # Small lists on purpose: this property costs O(n) ratings, and band edges are where the
    # bugs are -- so the boundaries must fall INSIDE the quantity range being walked.
    price_list=price_lists(max_bands=3, max_included=400, max_bound=600),
    quantity=st.integers(min_value=0, max_value=800),
)
def test_rating_in_one_call_equals_summing_each_request(price_list, quantity):
    """Rating N at once must equal rating one request at a time across every boundary.
    This is the property that catches an off-by-one at a band edge."""
    whole = rate(quantity, price_list).total_paisa
    piecewise = price_list.monthly_fee_paisa + sum(
        rate(n + 1, price_list).total_paisa - rate(n, price_list).total_paisa
        for n in range(quantity)
    )
    assert whole == piecewise


@given(price_list=price_lists(), budget=st.integers(min_value=0, max_value=200_000_000))
def test_the_limit_threshold_is_the_exact_crossing_point(price_list, budget):
    """ADR-0008's inversion: the request count at which a spending limit is reached.

    Exactness matters in both directions -- one request under must be affordable, and one
    request over must not be, or the hot path stops the wrong customer at the wrong moment.
    """
    threshold = max_quantity_within(budget, price_list)

    if price_list.monthly_fee_paisa > budget:
        assert threshold == 0
        assert rate(0, price_list).total_paisa > budget
        return

    assert rate(threshold, price_list).total_paisa <= budget
    assert rate(threshold + 1, price_list).total_paisa > budget


@given(quantity=quantities, price_list=st.sampled_from(LAUNCH_PRICE_LISTS))
def test_the_published_plans_get_cheaper_per_request_not_dearer(quantity, price_list):
    """The brief's stated intent: 'the price per request gets cheaper the more the customer
    uses'. Holds for our catalogue; the ladder itself does not require it."""
    step_now = rate(quantity + 1, price_list).total_paisa - rate(quantity, price_list).total_paisa
    step_later = (
        rate(quantity + 2, price_list).total_paisa - rate(quantity + 1, price_list).total_paisa
    )
    if quantity + 1 > price_list.included_quantity:
        assert step_later <= step_now


@given(budget=st.integers(min_value=0, max_value=500_000_000))
def test_growth_threshold_never_lets_a_customer_exceed_their_limit(budget):
    """The same inversion, pinned to a real plan -- the case a customer would actually hit."""
    threshold = max_quantity_within(budget, GROWTH_V1)
    if budget >= GROWTH_V1.monthly_fee_paisa:
        assert rate(threshold, GROWTH_V1).total_paisa <= budget < rate(
            threshold + 1, GROWTH_V1
        ).total_paisa


@given(quantity=quantities)
def test_a_scale_customer_never_pays_more_than_a_starter_customer_would(quantity):
    """Sanity on the catalogue itself: at high volume the expensive-looking plan is cheaper.
    Not a law of the ladder -- a claim about how we priced it, worth knowing if it breaks."""
    assume(quantity >= 6_000_000)
    assert rate(quantity, SCALE_V1).total_paisa < rate(quantity, STARTER_V1).total_paisa
