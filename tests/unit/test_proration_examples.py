"""Proration against numbers derived by hand. ADR-0006.

Property tests prove proration is internally consistent for any month and any split.
These prove it produces the specific rupees a customer would be told on the phone, and
they pin the arithmetic of the sharp edge ADR-0006 accepted.

Every number in this file was computed by hand first and is written as an integer paisa
literal. A test that re-derives the expected value with the same formula as the code
proves nothing.
"""

import pytest

from meter.domain.catalogue import GROWTH_V1, SCALE_V1, STARTER_V1
from meter.domain.proration import (
    PeriodCharge,
    Segment,
    prorate,
    prorate_allowance,
    prorate_fee,
    rate_period,
)
from meter.domain.rating import USAGE, rate
from meter.money import rupees


class TestTheGrowthToScaleUpgrade:
    """The scenario ADR-0006 was written for: Growth for 17 days, then Scale for 13, in a
    30-day month. The change day belongs to the new plan, which is why it is 17 + 13 and
    not 18 + 13 or 17 + 14.

    Derivation, by hand:

      Growth, 17/30 days
        fee       Rs. 15,000 x 17/30 = 1_500_000 x 17 // 30 =   850_000 paisa  (Rs.  8,500)
        allowance    500,000 x 17/30 = ceil(8_500_000 / 30)  =   283,334 requests
        usage     400,000 requests, so 400,000 - 283,334     =   116,666 chargeable
                  all inside the Rs. 0.50 band (width 500,000, not prorated)
                  116,666 x 50                               = 5_833_300 paisa
        segment                           850_000 + 5_833_300 = 6_683_300 paisa

      Scale, 13/30 days
        fee       Rs. 90,000 x 13/30 = 9_000_000 x 13 // 30  = 3_900_000 paisa  (Rs. 39,000)
        allowance  5,000,000 x 13/30 = ceil(65_000_000 / 30) = 2,166,667 requests
        usage     3,000,000 requests, so 3,000,000 - 2,166,667 = 833,333 chargeable
                  all inside the Rs. 0.25 band
                  833,333 x 25                              = 20_833_325 paisa
        segment                       3_900_000 + 20_833_325 = 24_733_325 paisa

      Period                           6_683_300 + 24_733_325 = 31_416_625 paisa
                                                              = Rs. 314,166.25
    """

    @pytest.fixture
    def period(self) -> PeriodCharge:
        return rate_period(
            [
                Segment(price_list=GROWTH_V1, days=17, days_in_month=30, quantity=400_000),
                Segment(price_list=SCALE_V1, days=13, days_in_month=30, quantity=3_000_000),
            ]
        )

    def test_the_prorated_fees_and_allowances(self, period):
        growth, scale = period.segments

        assert growth.prorated_fee_paisa == 850_000       # Rs. 8,500.00
        assert growth.prorated_allowance == 283_334       # 283,333.33 rounded UP
        assert scale.prorated_fee_paisa == 3_900_000      # Rs. 39,000.00
        assert scale.prorated_allowance == 2_166_667      # 2,166,666.67 rounded UP

    def test_the_usage_is_rated_against_the_segments_own_allowance_and_ladder(self, period):
        growth, scale = period.segments

        growth_usage = [line for line in growth.lines if line.kind == USAGE]
        assert [(line.quantity, line.unit_price_paisa, line.amount_paisa) for line in
                growth_usage] == [(116_666, 50, 5_833_300)]

        scale_usage = [line for line in scale.lines if line.kind == USAGE]
        assert [(line.quantity, line.unit_price_paisa, line.amount_paisa) for line in
                scale_usage] == [(833_333, 25, 20_833_325)]

    def test_the_segment_totals_and_the_period_total(self, period):
        growth, scale = period.segments

        assert growth.total_paisa == 6_683_300            # Rs. 66,833.00
        assert scale.total_paisa == 24_733_325            # Rs. 247,333.25
        assert period.total_paisa == 31_416_625           # Rs. 314,166.25
        assert period.total_paisa == growth.total_paisa + scale.total_paisa

    def test_the_fee_component_is_the_sum_of_the_prorated_fees(self, period):
        """ADR-0012: the spending limit caps the total bill, and this is the part of it a
        plan change moves. Rs. 8,500 + Rs. 39,000 = Rs. 47,500, not Rs. 105,000."""
        assert period.fee_paisa == rupees(47_500)
        assert period.fee_paisa < GROWTH_V1.monthly_fee_paisa + SCALE_V1.monthly_fee_paisa

    def test_the_period_covers_every_day_of_the_month(self, period):
        assert period.days == 30
        assert period.covers_the_whole_month
        assert period.quantity == 3_400_000

    def test_support_can_read_the_whole_thing_aloud(self, period):
        explanation = period.explain()

        assert "Growth v1 for 17 of 30 days" in explanation
        assert "17/30 of the monthly fee (Rs. 8,500.00)" in explanation
        assert "of the included allowance (283,334 requests)" in explanation
        assert "Scale v1 for 13 of 30 days" in explanation
        assert "13/30 of the monthly fee (Rs. 39,000.00)" in explanation
        assert "Segment total: Rs. 66,833.00" in explanation
        assert "Segment total: Rs. 247,333.25" in explanation
        assert "Period total: Rs. 314,166.25" in explanation


class TestTheRoundingDirection:
    """ADR-0006: 'we round in your favour -- down on what you pay, up on what you get.'

    A 30-day month divides Rs. 15,000 and 500,000 evenly at 17/30 and hides the rule. A
    31-day month does not, so this is where the direction is actually visible.
    """

    def test_a_fee_that_does_not_divide_evenly_rounds_down(self):
        # 1_500_000 x 17 / 31 = 822_580.645... paisa
        assert prorate_fee(GROWTH_V1.monthly_fee_paisa, 17, 31) == 822_580
        assert prorate_fee(GROWTH_V1.monthly_fee_paisa, 14, 31) == 677_419

    def test_an_allowance_that_does_not_divide_evenly_rounds_up(self):
        # 500_000 x 17 / 31 = 274_193.548... requests
        assert prorate_allowance(GROWTH_V1.included_quantity, 17, 31) == 274_194
        assert prorate_allowance(GROWTH_V1.included_quantity, 14, 31) == 225_807

    def test_the_customer_keeps_the_paisa_and_gains_the_request(self):
        """Across a 17 + 14 split of a 31-day month the whole rule is visible at once:
        the customer pays one paisa LESS than a full monthly fee and gets one request MORE
        than a full monthly allowance. Neither is lost; both went to the customer."""
        fees = prorate_fee(GROWTH_V1.monthly_fee_paisa, 17, 31) + prorate_fee(
            GROWTH_V1.monthly_fee_paisa, 14, 31
        )
        allowances = prorate_allowance(GROWTH_V1.included_quantity, 17, 31) + prorate_allowance(
            GROWTH_V1.included_quantity, 14, 31
        )

        assert fees == 1_499_999
        assert GROWTH_V1.monthly_fee_paisa - fees == 1        # one paisa, in their favour
        assert allowances == 500_001
        assert allowances - GROWTH_V1.included_quantity == 1   # one request, in their favour

    def test_a_free_plan_prorates_to_free(self):
        assert prorate_fee(STARTER_V1.monthly_fee_paisa, 17, 31) == 0
        assert prorate_allowance(STARTER_V1.included_quantity, 17, 31) == 5_484  # 10,000 x 17/31


class TestAFullMonthIsTheIdentity:
    def test_a_full_month_segment_prorates_to_the_plan_itself(self):
        """Exactly equal, not merely equivalent. If this ever drifts, every customer who
        never changed plan is being charged a rounded number for no reason."""
        for days_in_month in (28, 29, 30, 31):
            for price_list in (STARTER_V1, GROWTH_V1, SCALE_V1):
                assert prorate(price_list, days_in_month, days_in_month) == price_list

    def test_a_single_full_month_segment_rates_exactly_like_the_unprorated_plan(self):
        period = rate_period(
            [Segment(price_list=GROWTH_V1, days=30, days_in_month=30, quantity=1_200_000)]
        )
        assert period.total_paisa == rate(1_200_000, GROWTH_V1).total_paisa
        assert period.total_paisa == rupees(335_000)  # the brief's worked example, unchanged


class TestTheLadderRestartsAtEverySegmentBoundary:
    """ADR-0006's named sharp edge, recorded as behaviour rather than reported as a bug.

    A customer who does 2,000,000 requests on Growth across a mid-month change pays MORE
    than the same 2,000,000 requests would have cost with no change at all, because each
    segment enters the expensive Rs. 0.50 band from the start. Prorating the allowances
    softens it; it does not remove it.

    Derivation, 17 + 13 days of Growth in a 30-day month, 1,000,000 requests in each:

      Segment A (17 days)  fee 850_000, allowance 283,334
        chargeable 1,000,000 - 283,334 = 716,666
        500,000 x 50 = 25_000_000  then  216,666 x 35 =  7_583_310
        segment total                                  = 33_433_310

      Segment B (13 days)  fee 650_000, allowance 216,667
        chargeable 1,000,000 - 216,667 = 783,333
        500,000 x 50 = 25_000_000  then  283,333 x 35 =  9_916_655
        segment total                                  = 35_566_655

      Period                                           = 68_999_965  (Rs. 689,999.65)

    Unsplit, Growth for the whole month, 2,000,000 requests:
        1_500_000 + 500,000 x 50 + 1,000,000 x 35      = 61_500_000  (Rs. 615,000.00)

    The split costs Rs. 74,999.65 more. Support needs to know this exists.
    """

    def test_the_split_costs_more_than_the_same_usage_unsplit(self):
        split = rate_period(
            [
                Segment(price_list=GROWTH_V1, days=17, days_in_month=30, quantity=1_000_000),
                Segment(price_list=GROWTH_V1, days=13, days_in_month=30, quantity=1_000_000),
            ]
        )
        unsplit = rate(2_000_000, GROWTH_V1)

        assert [segment.total_paisa for segment in split.segments] == [33_433_310, 35_566_655]
        assert split.total_paisa == 68_999_965
        assert unsplit.total_paisa == 61_500_000

        assert split.total_paisa > unsplit.total_paisa
        assert split.total_paisa - unsplit.total_paisa == 7_499_965  # Rs. 74,999.65

    def test_the_fees_alone_did_not_cause_it(self):
        """The extra is entirely the ladder restarting. The prorated fees still add up to
        exactly one monthly fee, so nothing was double-charged."""
        split = rate_period(
            [
                Segment(price_list=GROWTH_V1, days=17, days_in_month=30, quantity=1_000_000),
                Segment(price_list=GROWTH_V1, days=13, days_in_month=30, quantity=1_000_000),
            ]
        )
        assert split.fee_paisa == GROWTH_V1.monthly_fee_paisa

    def test_below_the_allowance_the_split_is_harmless(self):
        """The edge only bites usage that crosses a band boundary twice. A customer inside
        the prorated allowances pays exactly the monthly fee, split or not."""
        split = rate_period(
            [
                Segment(price_list=GROWTH_V1, days=17, days_in_month=30, quantity=200_000),
                Segment(price_list=GROWTH_V1, days=13, days_in_month=30, quantity=200_000),
            ]
        )
        assert split.total_paisa == GROWTH_V1.monthly_fee_paisa
        assert split.total_paisa == rate(400_000, GROWTH_V1).total_paisa


class TestSegmentValidation:
    def test_a_notional_thirty_day_month_is_refused_for_february(self):
        """ADR-0006: actual calendar days, 28-31. A day count that never came from a
        calendar is a caller bug, and a silent one if we accept it."""
        with pytest.raises(ValueError, match="actual calendar month"):
            prorate(GROWTH_V1, 17, 360)
        with pytest.raises(ValueError, match="actual calendar month"):
            Segment(price_list=GROWTH_V1, days=1, days_in_month=0, quantity=0)

    def test_a_segment_longer_than_its_month_is_refused(self):
        with pytest.raises(ValueError, match="does not fit"):
            Segment(price_list=GROWTH_V1, days=31, days_in_month=30, quantity=0)

    def test_segments_must_agree_on_how_long_the_month_is(self):
        with pytest.raises(ValueError, match="agree on days_in_month"):
            rate_period(
                [
                    Segment(price_list=GROWTH_V1, days=17, days_in_month=30, quantity=0),
                    Segment(price_list=SCALE_V1, days=13, days_in_month=31, quantity=0),
                ]
            )

    def test_segments_totalling_more_than_the_month_are_refused(self):
        """Two segments that between them claim 34 days of a 30-day month would charge
        more fee than the month contains. That is a caller bug, not a bill."""
        with pytest.raises(ValueError, match="does not fit in a 30-day month"):
            rate_period(
                [
                    Segment(price_list=GROWTH_V1, days=17, days_in_month=30, quantity=0),
                    Segment(price_list=SCALE_V1, days=17, days_in_month=30, quantity=0),
                ]
            )

    def test_a_period_needs_at_least_one_segment(self):
        with pytest.raises(ValueError, match="at least one segment"):
            rate_period([])

    def test_days_must_be_an_integer_not_a_fraction_of_a_month(self):
        with pytest.raises(TypeError):
            prorate(GROWTH_V1, 17.0, 30)
        with pytest.raises(TypeError):
            prorate(GROWTH_V1, 17, 30.0)

    def test_money_in_must_be_integer_paisa(self):
        with pytest.raises(TypeError):
            prorate_fee(15_000.0, 17, 30)

    def test_a_zero_day_segment_costs_nothing(self):
        """Two plan changes on the same day leave the middle plan with no days. It is
        legal, it is free, and it carries no allowance -- usage must never land in one."""
        empty = prorate(SCALE_V1, 0, 30)
        assert empty.monthly_fee_paisa == 0
        assert empty.included_quantity == 0

        period = rate_period(
            [
                Segment(price_list=GROWTH_V1, days=0, days_in_month=30, quantity=0),
                Segment(price_list=SCALE_V1, days=30, days_in_month=30, quantity=0),
            ]
        )
        assert period.total_paisa == SCALE_V1.monthly_fee_paisa
