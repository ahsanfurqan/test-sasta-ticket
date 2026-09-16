"""Money representation. See docs/adr/0004-money-as-integer-paisa.md.

Money is an integer number of paisa, everywhere: domain, database (bigint), API,
tests, fixtures. Rupees exist only as display, produced at the response boundary.

This module deliberately contains NO arithmetic on charges. Rating, proration and
rounding belong to meter.domain and are owned by billing-domain.
"""

from typing import NewType

Paisa = NewType("Paisa", int)

PAISA_PER_RUPEE = 100


def rupees(amount: int) -> Paisa:
    """Whole rupees -> paisa. For readable constants: rupees(15_000)."""
    return Paisa(amount * PAISA_PER_RUPEE)


def format_paisa(amount: Paisa | int) -> str:
    """Render paisa for display: 1_500_000 -> 'Rs. 15,000.00'.

    Presentation only. Never feed the result back into a calculation.
    """
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError(f"money must be int paisa, got {type(amount).__name__}")
    sign = "-" if amount < 0 else ""
    whole, fraction = divmod(abs(amount), PAISA_PER_RUPEE)
    return f"{sign}Rs. {whole:,}.{fraction:02d}"
