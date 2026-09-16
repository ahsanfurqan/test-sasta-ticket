# ADR-0015: Multiple hashed keys per customer; revocation effective within 30s

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path, data-model
- **Resolves:** open question #10

## Context

The brief says customers get an API key and never mentions the lifecycle. Every part of that
lifecycle has a billing consequence, so it cannot be left implicit: whether a customer can
hold more than one key, whether keys are recoverable from the database, and how long a
revoked key keeps working.

That last one is forced by the hot path. Authentication cannot make a synchronous Postgres
lookup per request, so keys are cached — and a cached revoked key keeps working until the
cache lets go of it.

## Decision

**Multiple active keys per customer.** Rotation is: issue a new key, migrate traffic,
revoke the old one. No cutover, no downtime, which is what makes rotation something customers
will actually do.

**Keys are stored hashed**, never in plaintext or reversibly encrypted. The full key is shown
exactly once, at creation. A database leak is then not a key leak. A key carries a
non-secret prefix for identification in lists and logs; the secret material is never logged.

**Revocation takes effect within 30 seconds**, bounded by the auth cache TTL. This is a
stated, tested window, not an accident of configuration.

Usage from a key that was valid when the request was served **is billed**, even if the key is
revoked moments later. The charge reflects what we served.

## Alternatives considered

- **Immediate revocation via cache invalidation.** Pub/sub to every API instance on
  revocation, with the TTL as a backstop. The strongest security story, and where this should
  end up. Rejected for v1 on YAGNI: it adds a distributed invalidation path that must itself
  be correct under partition, to shrink a 30-second window that no requirement has yet called
  too long. The TTL design is a prerequisite for it anyway, so nothing is wasted.
- **Single key per customer.** Least to build. Rejected because rotation becomes a hard
  cutover with downtime for the customer's integration, and the predictable result is that
  keys are never rotated — which is a worse security outcome than a 30-second revocation
  window.
- **Storing keys encrypted rather than hashed**, so they can be displayed again later. A real
  convenience, and a real liability: it makes the key database a decryptable secret store.
  Rejected. Customers who lose a key create a new one.

## What it costs

- **A 30-second window in which a revoked key still works.** If a key is revoked because it
  leaked, an attacker has up to 30 seconds of continued access — and the usage they generate
  is billed to the customer, because it was served. That is the security cost of keeping auth
  off Postgres, and it is a real one.
- **Keys cannot be recovered or displayed after creation**, which generates support contacts
  from customers who did not store theirs.
- **Multiple keys means usage may need attributing per key**, not just per customer, before
  anyone can answer "which of my keys is generating all this traffic?". Not required by the
  brief, but the schema should not make it impossible.
- **The auth cache is now a correctness surface**, not just an optimisation: its TTL is a
  security parameter, and changing it changes the revocation guarantee.

## Where it breaks

30 seconds is fine for routine rotation and unacceptable for an active compromise. The first
time a customer reports a leaked key in anger, this window will be the thing they focus on,
and the answer will have to be immediate invalidation.

It also breaks if the auth cache is per-instance and instances are numerous: "within 30
seconds" then means "within 30 seconds of the last instance's TTL expiring", which is not the
same thing and is easy to get wrong when measuring. The window must be tested against the
worst instance, not the average.

Finally, billing usage from a just-revoked key is correct but will feel wrong to a customer
whose key was stolen. That conversation needs a policy, and it does not have one.

## Consequences for other owners

`data-model` stores key hashes with a non-secret prefix, supports multiple active keys per
customer, and records revocation as a timestamp rather than a delete — so a disputed charge
can be traced to a key that no longer exists. `hot-path` resolves keys from cache with a
30-second TTL, compares with `compare_digest`, never logs key material, and owns proving the
window. `test-engineer` proves that a revoked key stops working within the stated window, and
that usage served before revocation is still billed.
