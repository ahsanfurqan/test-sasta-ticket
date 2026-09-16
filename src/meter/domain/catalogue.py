"""The launch price lists, as published in the brief.

SEED DATA, NOT THE RUNTIME SOURCE OF TRUTH. Pricing lives in the database (ADR-0005) so a
negotiated deal needs no deployment. This module exists for two things: seeding those rows,
and testing our arithmetic against the numbers the brief published.

If these ever disagree with the database, the database is right.
"""

from meter.domain.plans import Band, PriceList
from meter.money import Paisa, rupees

STARTER_V1 = PriceList(
    name="Starter",
    version=1,
    monthly_fee_paisa=rupees(0),
    included_quantity=10_000,
    bands=(Band(up_to=None, unit_price_paisa=Paisa(80)),),  # Rs. 0.80
)

GROWTH_V1 = PriceList(
    name="Growth",
    version=1,
    monthly_fee_paisa=rupees(15_000),
    included_quantity=500_000,
    bands=(
        Band(up_to=500_000, unit_price_paisa=Paisa(50)),  # Rs. 0.50
        Band(up_to=None, unit_price_paisa=Paisa(35)),     # Rs. 0.35
    ),
)

SCALE_V1 = PriceList(
    name="Scale",
    version=1,
    monthly_fee_paisa=rupees(90_000),
    included_quantity=5_000_000,
    bands=(
        Band(up_to=5_000_000, unit_price_paisa=Paisa(25)),  # Rs. 0.25
        Band(up_to=None, unit_price_paisa=Paisa(15)),       # Rs. 0.15
    ),
)

LAUNCH_PRICE_LISTS = (STARTER_V1, GROWTH_V1, SCALE_V1)
