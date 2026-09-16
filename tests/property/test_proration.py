"""Property tests over whole-day proration. ADR-0006. Owned by test-engineer.

The example tests prove proration produces the rupees a customer was promised. These prove
it cannot lose or invent a paisa or a request -- for any plan, any month length between 28
and 31 days, and any way of cutting that month into segments, including degenerate ones.

Every assertion is exact integer arithmetic. There is no float in this file, including in
the statements of the rounding properties: "rounds down" is written as
`prorated * days_in_month <= exact_numerator`, not as a comparison against a division.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from meter.domain.catalogue import GROWTH_V1, SCALE_V1, STARTER_V1
from meter.domain.plans import PriceList
from meter.domain.proration import (
    Segment,
    prorate,
    prorate_allowance,
    prorate_fee,
    rate_period,
)
from meter.domain.rating import USAGE, rate

# The same arbitrary-price-list generator the ladder is proved against. Proration must hold
# for price lists nobody would design, not just for the three in the catalogue.
from tests.property.test_rating import price_lists

MONTH_LENGTHS = st.integers(min_value=28, max_value=31)
quantities = st.integers(min_value=0, max_value=50_000_000)


@st.composite
def months(draw, max_segments: int = 4):
    """A calendar month, cut into segments that exactly cover it.

    Cut points may coincide, which produces zero-day segments -- two plan changes on the
    same day. Degenerate, legal, and exactly the shape that breaks a naive implementation.
    """
    days_in_month = draw(MONTH_LENGTHS)
    count = draw(st.integers(min_value=1, max_value=max_segments))
    cuts = sorted(
        draw(
            st.lists(
                st.integers(min_value=0, max_value=days_in_month),
                min_size=count - 1,
                max_size=count - 1,
            )
        )
    )
    bounds = [0, *cuts, days_in_month]
    return days_in_month, [b - a for a, b in zip(bounds[:-1], bounds[1:], strict=True)]


@st.composite
def segments(draw, max_segments: int = 3):
    days_in_month, days = draw(months(max_segments=max_segments))
    return tuple(
        Segment(
            price_list=draw(price_lists()),
            days=d,
            days_in_month=days_in_month,
            quantity=draw(quantities),
        )
        for d in days
    )


# --------------------------------------------------------------------------------------
# The identity. A full month must prorate to the plan itself, exactly.
# --------------------------------------------------------------------------------------


@given(price_list=price_lists(), days_in_month=MONTH_LENGTHS)
def test_a_full_month_segment_is_the_unprorated_plan(price_list, days_in_month):
    """prorate(pl, 30, 30) == pl. Not equivalent -- equal. Every customer who never changed
    plan goes through this path, and rounding must not touch them at all."""
    assert prorate(price_list, days_in_month, days_in_month) == price_list


@given(price_list=price_lists(), quantity=quantities, days_in_month=MONTH_LENGTHS)
def test_one_full_month_segment_rates_exactly_like_rate(price_list, quantity, days_in_month):
    period = rate_period(
        [Segment(price_list=price_list, days=days_in_month,
                 days_in_month=days_in_month, quantity=quantity)]
    )
    assert period.total_paisa == rate(quantity, price_list).total_paisa


@given(price_list=price_lists(), days_in_month=MONTH_LENGTHS)
def test_a_zero_day_segment_is_free_and_carries_no_allowance(price_list, days_in_month):
    empty = prorate(price_list, 0, days_in_month)
    assert empty.monthly_fee_paisa == 0
    assert empty.included_quantity == 0


# --------------------------------------------------------------------------------------
# The rounding direction. ADR-0006: "down on what you pay, up on what you get."
# --------------------------------------------------------------------------------------


@given(price_list=price_lists(), month=months(max_segments=1))
def test_a_prorated_fee_is_the_exact_value_rounded_down(price_list, month):
    """Stated without division: the prorated fee is the largest integer f with
    f x days_in_month <= fee x days. That is floor, exactly, with no float to trust."""
    days_in_month, (days,) = month
    fee = prorate_fee(price_list.monthly_fee_paisa, days, days_in_month)
    exact_numerator = price_list.monthly_fee_paisa * days

    assert fee * days_in_month <= exact_numerator          # never more than the true value
    assert (fee + 1) * days_in_month > exact_numerator     # and never less by a whole paisa


@given(price_list=price_lists(), month=months(max_segments=1))
def test_a_prorated_allowance_is_the_exact_value_rounded_up(price_list, month):
    """The mirror image: the smallest integer a with a x days_in_month >= included x days."""
    days_in_month, (days,) = month
    allowance = prorate_allowance(price_list.included_quantity, days, days_in_month)
    exact_numerator = price_list.included_quantity * days

    assert allowance * days_in_month >= exact_numerator        # never fewer than the true value
    assert (allowance - 1) * days_in_month < exact_numerator   # and never a whole request more


@given(price_list=price_lists(), month=months(max_segments=1))
def test_the_customer_never_loses_on_the_fraction(price_list, month):
    """The two directions together: a prorated segment never costs more, and never includes
    less, than the exact unrounded share. This is the whole of ADR-0006's rounding rule."""
    days_in_month, (days,) = month
    prorated = prorate(price_list, days, days_in_month)

    assert prorated.monthly_fee_paisa * days_in_month <= price_list.monthly_fee_paisa * days
    assert prorated.included_quantity * days_in_month >= price_list.included_quantity * days


@given(price_list=price_lists(), days_in_month=MONTH_LENGTHS, data=st.data())
def test_proration_is_monotonic_in_days(price_list, days_in_month, data):
    """A longer segment never costs less and never includes less. If this can invert, a
    customer could pay less by staying on a plan longer."""
    a = data.draw(st.integers(min_value=0, max_value=days_in_month))
    b = data.draw(st.integers(min_value=0, max_value=days_in_month))
    short, long = sorted((a, b))

    assert prorate_fee(price_list.monthly_fee_paisa, short, days_in_month) <= prorate_fee(
        price_list.monthly_fee_paisa, long, days_in_month
    )
    assert prorate_allowance(
        price_list.included_quantity, short, days_in_month
    ) <= prorate_allowance(price_list.included_quantity, long, days_in_month)


# --------------------------------------------------------------------------------------
# Conservation across a whole month. No paisa lost, no paisa invented.
# --------------------------------------------------------------------------------------


@given(price_list=price_lists(), month=months())
def test_prorated_fees_never_exceed_the_full_monthly_fee(price_list, month):
    """However the month is cut, the customer never pays more fee than one month of it.
    Segmenting a month must not be a way to charge two fees."""
    days_in_month, days = month
    total = sum(prorate_fee(price_list.monthly_fee_paisa, d, days_in_month) for d in days)
    assert total <= price_list.monthly_fee_paisa


@given(price_list=price_lists(), month=months())
def test_the_fee_shortfall_is_bounded_by_one_paisa_per_segment(price_list, month):
    """Where the remainder goes, stated exactly: the customer keeps it, and there is at most
    one paisa of it per segment. Nothing is lost to the void and nothing is invented."""
    days_in_month, days = month
    total = sum(prorate_fee(price_list.monthly_fee_paisa, d, days_in_month) for d in days)
    shortfall = price_list.monthly_fee_paisa - total

    assert 0 <= shortfall < len(days)


@given(price_list=price_lists(), month=months())
def test_prorated_allowances_never_fall_short_of_the_full_monthly_allowance(price_list, month):
    """The customer gets at least a whole month of included requests, and at most one extra
    request per segment. The same remainder rule, pointing the other way."""
    days_in_month, days = month
    total = sum(prorate_allowance(price_list.included_quantity, d, days_in_month) for d in days)
    excess = total - price_list.included_quantity

    assert 0 <= excess < len(days)


@given(month=months(), price_list=st.sampled_from([STARTER_V1, GROWTH_V1, SCALE_V1]))
def test_a_published_plan_never_bills_two_monthly_fees(month, price_list):
    """The same conservation, pinned to the plans customers are actually on."""
    days_in_month, days = month
    period = rate_period(
        [Segment(price_list=price_list, days=d, days_in_month=days_in_month, quantity=0)
         for d in days]
    )
    assert period.fee_paisa <= price_list.monthly_fee_paisa
    assert period.total_paisa == period.fee_paisa  # no usage, so the bill is fee only


# --------------------------------------------------------------------------------------
# The period charge itself: exact, decomposable, integral.
# --------------------------------------------------------------------------------------


@settings(max_examples=100)
@given(period_segments=segments())
def test_segment_charges_sum_to_the_period_total_exactly(period_segments):
    period = rate_period(period_segments)
    assert sum(segment.total_paisa for segment in period.segments) == period.total_paisa


@settings(max_examples=100)
@given(period_segments=segments())
def test_every_line_of_every_segment_sums_to_the_period_total(period_segments):
    """The decomposition IS the total, at period level as well as segment level. If these
    can drift, no invoice line is explainable."""
    period = rate_period(period_segments)
    assert sum(line.amount_paisa for line in period.lines) == period.total_paisa


@settings(max_examples=100)
@given(period_segments=segments())
def test_every_amount_is_a_non_negative_integer(period_segments):
    period = rate_period(period_segments)
    for line in period.lines:
        assert type(line.amount_paisa) is int  # not bool, not float, not Decimal
        assert line.amount_paisa >= 0
    for segment in period.segments:
        assert type(segment.prorated_fee_paisa) is int
        assert type(segment.prorated_allowance) is int
        assert segment.prorated_fee_paisa >= 0
        assert segment.prorated_allowance >= 0
    assert type(period.total_paisa) is int
    assert type(period.fee_paisa) is int
    assert period.total_paisa >= 0


@settings(max_examples=100)
@given(period_segments=segments())
def test_no_request_is_lost_or_invented_across_the_period(period_segments):
    """Every request handed in is rated in exactly one segment, and every chargeable request
    is charged exactly once -- chargeable meaning beyond THAT segment's prorated allowance."""
    period = rate_period(period_segments)

    assert period.quantity == sum(segment.quantity for segment in period_segments)
    assert period.days == sum(segment.days for segment in period_segments)

    for segment in period.segments:
        charged = sum(line.quantity for line in segment.lines if line.kind == USAGE)
        assert charged == max(0, segment.quantity - segment.prorated_allowance)


@settings(max_examples=100)
@given(period_segments=segments())
def test_the_fee_component_is_the_sum_of_the_prorated_segment_fees(period_segments):
    """ADR-0012 needs this number: the spending limit caps the total bill, and under a plan
    change the fee part of it is the prorated sum, not a full monthly fee."""
    period = rate_period(period_segments)
    assert period.fee_paisa == sum(
        prorate_fee(s.price_list.monthly_fee_paisa, s.days, s.days_in_month)
        for s in period_segments
    )


@settings(max_examples=50)
@given(period_segments=segments())
def test_the_period_can_be_read_aloud(period_segments):
    """Explainability is a property, not a nice-to-have: every segment must name its plan,
    its version, its days, and its own total."""
    period = rate_period(period_segments)
    explanation = period.explain()

    for segment in period.segments:
        assert f"{segment.price_list_name} v{segment.price_list_version}" in explanation
        assert f"for {segment.days} of {segment.days_in_month} days" in explanation
    assert "Period total:" in explanation


@given(price_list=price_lists(), month=months(max_segments=1))
def test_a_prorated_list_keeps_its_name_and_version_and_bands(price_list, month):
    """A prorated list is a rating INPUT derived from a stored version, never a new version.
    Keeping the identity is what lets a charge cite the version it came from (ADR-0005), and
    keeping the bands is what makes the ladder restart -- ADR-0006's accepted consequence."""
    days_in_month, (days,) = month
    prorated = prorate(price_list, days, days_in_month)

    assert isinstance(prorated, PriceList)
    assert prorated.name == price_list.name
    assert prorated.version == price_list.version
    assert prorated.bands == price_list.bands
