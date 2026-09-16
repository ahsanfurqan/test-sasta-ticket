"""Pricing domain -- PURE. Owned by billing-domain.

This package imports nothing from meter.api, meter.storage or meter.pipeline, and
nothing that performs I/O: no database handle, no Redis, no clock, no config lookup.
Pure functions over integers and explicit inputs. Enforced by import-linter (`make lint`),
not by discipline.

Time is an input, never datetime.now(). A rating function that reads the clock cannot be
property-tested and cannot re-derive a historical charge.

Nothing here yet, deliberately -- no function in this repo calculates money in session 1.
Next session, owned by billing-domain:

    plans.py       plan / band value objects, versioned price lists
    rating.py      the marginal band ladder: quantity -> itemised charge
    proration.py   mid-month plan change (blocked on open question #1)
    invoice.py     invoice value objects and immutability
"""
