"""Price list value objects. Owned by billing-domain. See docs/adr/0005.

A versioned price list is THE pricing primitive. "Starter", "Growth" and "Scale" are not
special -- they are price lists that many customers share. A negotiated deal is a price
list with one customer on it. There is no override mechanism, so rating has exactly one
kind of input.

Pure: no I/O, no clock, no config. Money is integer paisa throughout (ADR-0004).
"""

from dataclasses import dataclass

from meter.money import Paisa


def _require_paisa(value: object, field: str) -> int:
    """Money is int paisa. bool is an int in Python, and would silently behave as 0 or 1."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be int paisa, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field} must not be negative, got {value}")
    return value


def _require_count(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field} must not be negative, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Band:
    """One marginal band of chargeable usage.

    `up_to` is the CUMULATIVE count of chargeable units covered through the end of this
    band, counting from the first unit beyond the included allowance. None means unbounded,
    and only the final band may be unbounded.

    Growth's "next 500,000 at Rs. 0.50, anything above at Rs. 0.35" is:
        Band(up_to=500_000, unit_price_paisa=50), Band(up_to=None, unit_price_paisa=35)
    """

    up_to: int | None
    unit_price_paisa: Paisa

    def __post_init__(self) -> None:
        _require_paisa(self.unit_price_paisa, "unit_price_paisa")
        if self.up_to is not None:
            _require_count(self.up_to, "up_to")
            if self.up_to == 0:
                raise ValueError("a bounded band must cover at least one unit")


@dataclass(frozen=True, slots=True)
class PriceList:
    """A versioned price list: a monthly fee, an included allowance, and marginal bands.

    Immutable once referenced by a charge (ADR-0005). A price change creates a new version;
    it never edits an existing one. That is what makes a past charge reproducible.
    """

    name: str
    version: int
    monthly_fee_paisa: Paisa
    included_quantity: int
    bands: tuple[Band, ...]

    def __post_init__(self) -> None:
        _require_paisa(self.monthly_fee_paisa, "monthly_fee_paisa")
        _require_count(self.included_quantity, "included_quantity")
        _require_count(self.version, "version")

        if not self.bands:
            raise ValueError("a price list needs at least one band")
        if self.bands[-1].up_to is not None:
            raise ValueError("the final band must be unbounded (up_to=None)")
        if any(band.up_to is None for band in self.bands[:-1]):
            raise ValueError("only the final band may be unbounded")

        bounds = [band.up_to for band in self.bands[:-1]]
        if any(later <= earlier for earlier, later in zip(bounds, bounds[1:], strict=False)):
            raise ValueError(f"band bounds must strictly increase, got {bounds}")

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"
