"""Pricing domain -- PURE. Owned by billing-domain.

This package imports nothing from meter.api, meter.storage or meter.pipeline, and
nothing that performs I/O: no database handle, no Redis, no clock, no config lookup.
Pure functions over integers and explicit inputs. Enforced by import-linter (`make lint`),
not by discipline.

Time is an input, never datetime.now(). A rating function that reads the clock cannot be
property-tested and cannot re-derive a historical charge.

Modules, owned by billing-domain:

    plans.py       plan / band value objects, versioned price lists (ADR-0005)
    catalogue.py   the launch price lists as seed data -- NOT the runtime source of truth
    rating.py      the marginal band ladder: quantity -> itemised charge
    proration.py   mid-month plan change, whole-day (ADR-0006)

Still to come:

    invoice.py     invoice value objects and immutability
"""
