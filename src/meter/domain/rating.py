"""The marginal band ladder. Owned by billing-domain. See docs/adr/0005, 0007, 0008.

Bands are MARGINAL, never retroactive: crossing into a cheaper band prices only the units
in that band. It is not a discount applied to everything once the customer crosses the line.

Every charge decomposes. `rate()` returns lines that each name a quantity, a unit price and
the price list version used, and those lines sum to exactly the total. That decomposition
is the answer to "why does this line say what it says?" -- Support reads it aloud, rather
than someone re-deriving it by hand.

Pure: integers in, integers out. No clock, no I/O, no floats (ADR-0004).
"""

from dataclasses import dataclass

from meter.domain.plans import PriceList
from meter.money import Paisa, format_paisa

FEE = "monthly_fee"
INCLUDED = "included"
USAGE = "usage"


@dataclass(frozen=True, slots=True)
class ChargeLine:
    """One explainable component of a charge."""

    kind: str
    description: str
    quantity: int
    unit_price_paisa: Paisa
    amount_paisa: Paisa


@dataclass(frozen=True, slots=True)
class Charge:
    """A rated quantity, decomposed. `lines` always sums to `total_paisa` exactly."""

    price_list_name: str
    price_list_version: int
    quantity: int
    lines: tuple[ChargeLine, ...]
    total_paisa: Paisa

    def explain(self) -> str:
        """The charge, as Support would read it to a customer."""
        rows = [
            f"  {line.description}: {format_paisa(line.amount_paisa)}" for line in self.lines
        ]
        header = (
            f"{self.price_list_name} v{self.price_list_version} "
            f"- {self.quantity:,} requests"
        )
        return "\n".join([header, *rows, f"  Total: {format_paisa(self.total_paisa)}"])


def _require_quantity(quantity: object) -> int:
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise TypeError(f"quantity must be an int, got {type(quantity).__name__}")
    if quantity < 0:
        raise ValueError(f"quantity must not be negative, got {quantity}")
    return quantity


def rate(quantity: int, price_list: PriceList) -> Charge:
    """Rate `quantity` requests against `price_list`, returning an itemised charge."""
    quantity = _require_quantity(quantity)

    lines: list[ChargeLine] = [
        ChargeLine(
            kind=FEE,
            description=f"{price_list.name} monthly fee",
            quantity=1,
            unit_price_paisa=price_list.monthly_fee_paisa,
            amount_paisa=price_list.monthly_fee_paisa,
        )
    ]

    included_used = min(quantity, price_list.included_quantity)
    if price_list.included_quantity > 0:
        lines.append(
            ChargeLine(
                kind=INCLUDED,
                description=(
                    f"{included_used:,} of {price_list.included_quantity:,} included "
                    f"requests (covered by the monthly fee)"
                ),
                quantity=included_used,
                unit_price_paisa=Paisa(0),
                amount_paisa=Paisa(0),
            )
        )

    remaining = quantity - included_used
    consumed = 0

    for band in price_list.bands:
        if remaining <= 0:
            break
        capacity = remaining if band.up_to is None else band.up_to - consumed
        take = min(remaining, capacity)
        if take <= 0:
            continue

        first = price_list.included_quantity + consumed + 1
        last = price_list.included_quantity + consumed + take
        lines.append(
            ChargeLine(
                kind=USAGE,
                description=(
                    f"requests {first:,}-{last:,} at {format_paisa(band.unit_price_paisa)} "
                    f"each ({take:,} x {format_paisa(band.unit_price_paisa)})"
                ),
                quantity=take,
                unit_price_paisa=band.unit_price_paisa,
                amount_paisa=Paisa(take * band.unit_price_paisa),
            )
        )
        consumed += take
        remaining -= take

    return Charge(
        price_list_name=price_list.name,
        price_list_version=price_list.version,
        quantity=quantity,
        lines=tuple(lines),
        total_paisa=Paisa(sum(line.amount_paisa for line in lines)),
    )


def max_quantity_within(budget_paisa: int, price_list: PriceList) -> int:
    """The largest quantity whose total charge does not exceed `budget_paisa`.

    This is the inversion ADR-0008 depends on: converting a customer's rupee spending limit
    into a request-count threshold, so the hot path compares two integers instead of running
    a ladder calculation per request.

    It is exact, not a search: the ladder is monotonic in quantity, so the answer is found by
    walking the bands once. Returns 0 when the monthly fee alone exceeds the budget -- a
    limit that can never be satisfied, which ADR-0012 requires be rejected when it is set.
    """
    if not isinstance(budget_paisa, int) or isinstance(budget_paisa, bool):
        raise TypeError(f"budget must be int paisa, got {type(budget_paisa).__name__}")

    remaining_budget = budget_paisa - price_list.monthly_fee_paisa
    if remaining_budget < 0:
        return 0

    quantity = price_list.included_quantity
    consumed = 0

    for band in price_list.bands:
        capacity = None if band.up_to is None else band.up_to - consumed

        if band.unit_price_paisa == 0:
            if capacity is None:
                raise ValueError(
                    f"{price_list.label} ends in an unbounded free band, so no spending "
                    "limit can ever be reached"
                )
            quantity += capacity
            consumed += capacity
            continue

        affordable = remaining_budget // band.unit_price_paisa
        if capacity is None or affordable < capacity:
            return quantity + affordable

        quantity += capacity
        consumed += capacity
        remaining_budget -= capacity * band.unit_price_paisa

    return quantity  # unreachable: the final band is always unbounded
