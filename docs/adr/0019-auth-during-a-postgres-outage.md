# ADR-0019: Serve stale auth entries while Postgres is unavailable

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** hot-path
- **Corrects:** [ADR-0018](0018-capture-before-ack-and-the-failure-matrix.md)
- **Amends:** [ADR-0015](0015-api-key-model.md)

## Context

ADR-0018 states that a Postgres outage does not stop serving, because Redis can still count
usage and enforce spending limits. That is true of counting and enforcing, and false of the
system as a whole.

Authentication resolves an API key from a Redis cache with a 30-second TTL (ADR-0015). On a
miss it makes one Postgres read. With Postgres down, every cache entry expires within 30
seconds and no further key can be resolved — so the API returns `503` and stops serving,
roughly half a minute after the database goes away.

**ADR-0018's failure matrix is therefore wrong on its own terms.** It was written from the
usage path and did not consider the auth path. This was found by building it, not by review.

The current behaviour is a `503` with `key_directory_unavailable`, which is at least honest —
but it converts a Postgres outage into a total outage with a delay, which is the worst of
both worlds: long enough to look fine on a dashboard, short enough to take everything down.

## Decision

**Auth entries are served stale while Postgres is unavailable.**

A cached auth entry carries two ages: its normal TTL (30 seconds, unchanged) and a longer
**stale ceiling**. Between them, the entry is refreshed on access. Past the TTL, if the
refresh fails *because Postgres is unreachable*, the stale entry is served and the request
proceeds. Past the stale ceiling, it is not: the key is refused.

Three qualifications, all deliberate:

- **Only a Postgres failure licenses staleness.** A key that Postgres positively reports as
  revoked is revoked immediately. Staleness is a response to not knowing, never to a known
  answer.
- **Negative entries are never served stale.** An unknown key stays unknown; a Postgres
  outage must not turn "no such key" into "maybe".
- **The stale ceiling is bounded and alarmed.** Serving stale auth is a degraded mode, and
  the system must say so loudly rather than quietly continuing.

ADR-0015's revocation guarantee is therefore amended: **revocation takes effect within 30
seconds, except while Postgres is unavailable, when it takes effect within the stale ceiling
from the outage's start.**

## Alternatives considered

- **Accept it and correct ADR-0018 to say "Postgres down means the API stops ~30s later".**
  The honest minimum, and it was tempting given the brief's emphasis on writing gaps down
  rather than fixing them. Rejected because the outage is avoidable at low cost, and because
  "we go down 30 seconds after the database does, and we knew" is a poor answer at a
  walkthrough.
- **Cache auth for much longer** (hours), so an outage is unlikely to exhaust it. Simpler:
  no second age, no stale path. Rejected because it degrades the revocation window in
  *normal* operation to buy resilience in a rare one — precisely the wrong trade. The
  stale-while-error shape confines the cost to the outage.
- **Fail closed on Postgres entirely**, symmetrically with Redis (ADR-0011). Consistent and
  easy to explain. Rejected because Postgres is genuinely not needed to serve a request:
  counting and enforcement both run off Redis, so refusing traffic would be an outage we
  chose rather than one we had to take.
- **Keep an in-process key mirror** refreshed in the background. Removes the dependency
  entirely, and is what a much larger deployment would do. Rejected as premature: it
  introduces a second source of key truth, and every instance's copy can diverge.

## What it costs

- **A revocation window that stretches exactly when you least want it to.** If a key is
  leaked during a Postgres outage, revoking it does nothing until the database returns. That
  is a real security cost and it is being accepted knowingly, because the alternative is
  being down.
- **A second age on every cache entry**, and a code path that runs only during an outage —
  which ADR-0018 itself warns is the least-tested code in the system. It needs a test that
  actually stops Postgres, not a mock.
- **Degraded mode is easy not to notice.** Without a loud alarm the system happily serves
  stale auth for the whole ceiling and nobody learns the database is gone, since usage and
  limits keep working. The alarm is the feature here, not the fallback.

## Where it breaks

A Postgres outage longer than the stale ceiling still takes the API down; this buys a
window, not immunity. The ceiling is a guess until there is real outage data, and the
pressure will be to keep raising it — at which point the revocation guarantee erodes by
increments, which is how security properties usually disappear.

It also does nothing for a **new** customer or a **newly issued** key during an outage:
those have no cache entry to go stale, so they cannot authenticate at all. Existing traffic
continues, onboarding stops. That is the right priority, but it means "we kept serving" is
true only for customers who were already there.

## Consequences for other owners

`hot-path` implements the two ages, restricts staleness to Postgres-unreachable, never
serves negative entries stale, and emits the degraded-mode signal. `test-engineer` proves
it by actually stopping Postgres and showing that existing keys keep working, a revoked key
is still refused if Postgres reported it revoked before the outage, and an unknown key is
still refused. `data-model` is unaffected.
