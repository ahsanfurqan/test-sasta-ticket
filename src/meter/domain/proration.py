"""Mid-month plan change: whole-day proration. Owned by billing-domain. See docs/adr/0006.

A billing period is split into SEGMENTS at each plan change. For each segment both the
monthly fee and the included allowance are prorated by whole days:

    segment_fee       = plan_fee      x segment_days / days_in_month   (rounded DOWN)
    segment_allowance = plan_included x segment_days / days_in_month   (rounded UP)
    segment_band_bound= band_bound    x segment_days / days_in_month   (rounded UP)

Usage in a segment is rated against that segment's own prorated allowance and its own
prorated band ladder. Band WIDTHS scale with time; band PRICES do not -- a price per
request has no time dimension (ADR-0017). The day of the change belongs to the NEW plan (ADR-0006). Days in the month are the
actual calendar days, 28-31, and they arrive as an integer input -- this module never sees a
clock or a timezone (ADR-0009 resolves "the 18th" before it gets here).

**Rounding, ADR-0006: the customer wins the fraction.** Fees round down; allowances and
band bounds round up. Applied once, here, at the proration boundary, and never again further
down. All of it is exact integer arithmetic; nothing in this module divides in floating
point.

The bound is per-boundary, not per-month (ADR-0017 corrects ADR-0006's wording on this):
across N segments the prorated fees fall short of the monthly fee by less than N paisa, and
the allowances exceed the monthly allowance by fewer than N requests.

Shape, and why
--------------
`prorate()` returns a PriceList rather than a bespoke "prorated plan" type, so that the
marginal ladder in `rating.rate()` stays the one and only implementation of a charge
(DRY about meaning: two implementations of the same charge disagree eventually, and the
disagreement is found by a customer). It keeps the ORIGINAL name and version, for two
reasons: a charge must cite the price list version it was computed from (ADR-0005), and it
makes `prorate(pl, 30, 30) == pl` exactly true, which is the identity check that proves the
rounding cannot drift on a full month.

That derived list is a RATING INPUT, not a new price list version. It must never be
persisted, referenced by an invoice as a version, or seeded. The stored fact is
(price list version, segment days, days in month); the prorated list is re-derivable from
those three integers at any time, which is what keeps a historical charge reproducible.

Pure: integers in, integers out. No clock, no I/O, no floats (ADR-0004).
"""

from dataclasses import dataclass, replace

from meter.domain.plans import PriceList
from meter.domain.rating import Charge, ChargeLine, rate
from meter.money import Paisa, format_paisa

# Actual calendar days (ADR-0006), never a notional 30. A month outside this range is a
# caller bug -- a 360-day-year convention, or a day count that never came from a calendar.
DAYS_IN_MONTH_MIN = 28
DAYS_IN_MONTH_MAX = 31


def _require_int(value: object, field: str) -> int:
    """bool is an int in Python, and would silently behave as 0 or 1."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")
    return value


def _require_days(days: object, days_in_month: object) -> tuple[int, int]:
    days = _require_int(days, "days")
    days_in_month = _require_int(days_in_month, "days_in_month")

    if not DAYS_IN_MONTH_MIN <= days_in_month <= DAYS_IN_MONTH_MAX:
        raise ValueError(
            f"days_in_month must be an actual calendar month "
            f"({DAYS_IN_MONTH_MIN}-{DAYS_IN_MONTH_MAX}), got {days_in_month}"
        )
    if days < 0:
        raise ValueError(f"days must not be negative, got {days}")
    if days > days_in_month:
        raise ValueError(f"a segment of {days} days does not fit in a {days_in_month}-day month")
    return days, days_in_month


def prorate_fee(fee_paisa: int, days: int, days_in_month: int) -> Paisa:
    """A monthly fee for part of a month, rounded DOWN. The customer keeps the fraction."""
    days, days_in_month = _require_days(days, days_in_month)
    fee_paisa = _require_int(fee_paisa, "fee_paisa")
    if fee_paisa < 0:
        raise ValueError(f"fee_paisa must not be negative, got {fee_paisa}")
    return Paisa(fee_paisa * days // days_in_month)


def prorate_allowance(included_quantity: int, days: int, days_in_month: int) -> int:
    """An included allowance for part of a month, rounded UP. The customer gets the fraction."""
    days, days_in_month = _require_days(days, days_in_month)
    included_quantity = _require_int(included_quantity, "included_quantity")
    if included_quantity < 0:
        raise ValueError(f"included_quantity must not be negative, got {included_quantity}")
    # Ceiling division on non-negative integers. No float, no math.ceil, no Decimal.
    return (included_quantity * days + days_in_month - 1) // days_in_month


def prorate_band_bound(up_to: int | None, days: int, days_in_month: int) -> int | None:
    """A band's cumulative upper bound for part of a month, rounded UP.

    Same rule as the allowance, because it is the same kind of quantity -- the included
    allowance is band zero priced at zero, and treating band zero one way and band one
    another way inside the same calculation is arbitrary (ADR-0017).

    An unbounded band stays unbounded: there is no fraction of infinity.
    """
    if up_to is None:
        return None
    return prorate_allowance(up_to, days, days_in_month)


def _prorated_bands(bands: tuple, days: int, days_in_month: int) -> tuple:
    """Scale every band bound, dropping any band left with no width in this segment.

    Two bands can collapse onto the same bound once scaled (and in a zero-day segment every
    bounded band collapses to zero), which would otherwise produce a ladder whose bounds no
    longer strictly increase. A band with no width does not exist for this segment, so it is
    dropped rather than kept as an empty rung.

    Dropping a rung means its price is skipped and that usage falls to the next band. For a
    catalogue whose prices fall with volume -- ours -- that reaches the cheaper price sooner,
    which is the same direction as every other rounding decision here.
    """
    kept = []
    previous = 0
    for band in bands:
        if band.up_to is None:
            kept.append(replace(band, up_to=None))
            continue
        bound = prorate_band_bound(band.up_to, days, days_in_month)
        if bound <= previous:
            continue
        kept.append(replace(band, up_to=bound))
        previous = bound
    return tuple(kept)


def prorate(price_list: PriceList, days: int, days_in_month: int) -> PriceList:
    """`price_list` as it applies to a `days`-long segment of a `days_in_month`-day month.

    Fee down; allowance and band bounds up (ADR-0006 as amended by ADR-0017). Band PRICES
    are untouched -- a price per request does not scale with time. Name and version are
    untouched too, so the charge still cites the version it came from and a full-month
    segment is the identity.
    """
    days, days_in_month = _require_days(days, days_in_month)
    return replace(
        price_list,
        monthly_fee_paisa=prorate_fee(price_list.monthly_fee_paisa, days, days_in_month),
        included_quantity=prorate_allowance(
            price_list.included_quantity, days, days_in_month
        ),
        bands=_prorated_bands(price_list.bands, days, days_in_month),
    )


@dataclass(frozen=True, slots=True)
class Segment:
    """One stretch of a billing period spent on one price list.

    `days` is whole days, with the change day belonging to the new plan (ADR-0006), and
    `quantity` is the usage attributable to this stretch. A zero-day segment is legal --
    two plan changes on the same day leave the middle plan with no days -- and costs
    nothing; usage should never be attributed to one.
    """

    price_list: PriceList
    days: int
    days_in_month: int
    quantity: int

    def __post_init__(self) -> None:
        _require_days(self.days, self.days_in_month)
        quantity = _require_int(self.quantity, "quantity")
        if quantity < 0:
            raise ValueError(f"quantity must not be negative, got {quantity}")

    @property
    def prorated_price_list(self) -> PriceList:
        """The rating input for this segment. Derived, never persisted as a version."""
        return prorate(self.price_list, self.days, self.days_in_month)


@dataclass(frozen=True, slots=True)
class SegmentCharge:
    """One segment, rated and decomposed. `charge.lines` sums to `total_paisa` exactly."""

    price_list_name: str
    price_list_version: int
    days: int
    days_in_month: int
    prorated_fee_paisa: Paisa
    prorated_allowance: int
    charge: Charge

    @property
    def quantity(self) -> int:
        return self.charge.quantity

    @property
    def lines(self) -> tuple[ChargeLine, ...]:
        return self.charge.lines

    @property
    def total_paisa(self) -> Paisa:
        return self.charge.total_paisa

    def explain(self) -> str:
        """The segment, as Support would read it to a customer."""
        header = (
            f"{self.price_list_name} v{self.price_list_version} for {self.days} of "
            f"{self.days_in_month} days - {self.days}/{self.days_in_month} of the monthly "
            f"fee ({format_paisa(self.prorated_fee_paisa)}) and of the included allowance "
            f"({self.prorated_allowance:,} requests)"
        )
        rows = [
            f"  {line.description}: {format_paisa(line.amount_paisa)}" for line in self.charge.lines
        ]
        return "\n".join([header, *rows, f"  Segment total: {format_paisa(self.total_paisa)}"])


@dataclass(frozen=True, slots=True)
class PeriodCharge:
    """A whole billing period, segment by segment.

    `total_paisa` is the exact sum of the segment totals, which are themselves the exact
    sums of their lines. Nothing is rounded here -- rounding happened once, at the
    proration boundary, before any of these numbers existed.
    """

    days_in_month: int
    segments: tuple[SegmentCharge, ...]
    total_paisa: Paisa

    @property
    def days(self) -> int:
        return sum(segment.days for segment in self.segments)

    @property
    def quantity(self) -> int:
        return sum(segment.quantity for segment in self.segments)

    @property
    def covers_the_whole_month(self) -> bool:
        return self.days == self.days_in_month

    @property
    def fee_paisa(self) -> Paisa:
        """The fee component of the bill: the sum of the PRORATED segment fees.

        ADR-0012's spending limit caps the total bill, and this is the part of it that a
        plan change moves. It is what the limit threshold must be computed net of.
        """
        return Paisa(sum(segment.prorated_fee_paisa for segment in self.segments))

    @property
    def lines(self) -> tuple[ChargeLine, ...]:
        """Every line of every segment, flattened. Sums to `total_paisa` exactly."""
        return tuple(line for segment in self.segments for line in segment.lines)

    def explain(self) -> str:
        """The period, as Support would read it to a customer."""
        blocks = [segment.explain() for segment in self.segments]
        return "\n".join([*blocks, f"Period total: {format_paisa(self.total_paisa)}"])


def rate_period(segments: tuple[Segment, ...] | list[Segment]) -> PeriodCharge:
    """Rate a billing period that is split into segments by plan changes.

    Each segment is rated against its OWN prorated allowance and its own band ladder
    (ADR-0006). The ladder therefore restarts at every segment boundary: usage split across
    a plan change can cost more than the same usage would have cost on either plan alone.
    That is the ADR's named, accepted consequence, not a bug -- see
    tests/unit/test_proration_examples.py, which pins the arithmetic of it.

    Segments must agree on how long the month is. They may cover less than the whole month
    (a customer who joined mid-period); `PeriodCharge.covers_the_whole_month` reports it
    rather than this function guessing, because partial-period signup is not a question
    ADR-0006 answered.
    """
    segments = tuple(segments)
    if not segments:
        raise ValueError("a billing period needs at least one segment")

    days_in_month = segments[0].days_in_month
    if any(segment.days_in_month != days_in_month for segment in segments):
        raise ValueError(
            "segments of one billing period must agree on days_in_month, got "
            f"{sorted({segment.days_in_month for segment in segments})}"
        )

    total_days = sum(segment.days for segment in segments)
    if total_days > days_in_month:
        raise ValueError(
            f"segments total {total_days} days, which does not fit in a "
            f"{days_in_month}-day month"
        )

    rated: list[SegmentCharge] = []
    for segment in segments:
        prorated = segment.prorated_price_list
        rated.append(
            SegmentCharge(
                price_list_name=segment.price_list.name,
                price_list_version=segment.price_list.version,
                days=segment.days,
                days_in_month=segment.days_in_month,
                prorated_fee_paisa=prorated.monthly_fee_paisa,
                prorated_allowance=prorated.included_quantity,
                charge=rate(segment.quantity, prorated),
            )
        )

    return PeriodCharge(
        days_in_month=days_in_month,
        segments=tuple(rated),
        total_paisa=Paisa(sum(segment.total_paisa for segment in rated)),
    )
