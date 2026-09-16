"""Property tests over the pricing math. Owned by test-engineer.

Empty until there is pricing math to test. Next session, the properties that matter:
the sum of per-band charges equals the total exactly; the total is monotonic in quantity;
crossing a band boundary never reduces the bill; rating a quantity in one call equals
rating it split across the boundary; no input yields a negative or non-integer charge.
"""
