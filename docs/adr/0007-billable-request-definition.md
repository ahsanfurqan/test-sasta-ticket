# ADR-0007: A billable request is one we served — 2xx and client errors, nothing else

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** billing-domain, hot-path
- **Resolves:** open question #2

## Context

The brief never says which requests count. It is easy to miss, and it moves the invoice
more than most of the decisions that look bigger. Four cases have to be settled: a bad or
missing API key, an error caused by the customer's own request, an error caused by us, and a
request refused because the customer hit their spending limit.

Each has a way of being wrong. Billing unauthenticated requests means anyone who guesses at
a key can run up a stranger's bill. Billing our own failures means charging for our outage.
Billing a limit refusal means charging someone for being told "no" — by a limit they set.

## Decision

**A request is billable if we authenticated it and processed it.** Concretely:

| Outcome | Billed | Why |
|---|---|---|
| `2xx` success | **Yes** | We did the work |
| `4xx` from the customer's request | **Yes** | We authenticated, routed and processed it; the work was done |
| `401` / `403` — bad or missing key | No | Not a customer yet; billing this is an attack vector |
| `5xx` — our failure | No | We do not charge for our own faults |
| Refused for hitting the spending limit | No | Charging to be told "no" is indefensible |

The customer-facing sentence: *"You're billed for requests we served, including ones your
code got wrong. You're never billed for our failures, or for being told you're over your
limit."*

**Usage is therefore captured after the outcome is known** — after the handler runs, not
before it. That is a hot-path design constraint, not merely a billing rule.

## Alternatives considered

- **Successful requests only (2xx).** The easiest thing of all to defend to a customer, and
  a real contender. Rejected because a broken or malicious client can generate unlimited
  `4xx` traffic that costs us real capacity with no billing signal at all — and because a
  customer whose integration is misconfigured would consume our service indefinitely while
  believing it is free.
- **Every authenticated request, whatever the outcome.** Simplest to implement: capture
  before the handler, no outcome needed, and the cheapest possible hot path. Rejected
  because it charges customers for our 5xx responses and for limit refusals, both of which
  are impossible to justify on a phone call.
- **Bill limit-refused requests at a reduced rate** to recover the cost of refusing. Too
  clever. It produces an invoice line that needs a paragraph of explanation, which fails the
  test this whole design is built around.

## What it costs

- **Capture moves after the handler**, so usage recording sits on the response path. It
  cannot be a fire-and-forget at request entry, and the capture mechanism must survive a
  handler that raised.
- **The refusal path still costs us real resources** that nobody pays for. That is a
  deliberate subsidy; if limit-refused traffic ever becomes a significant load, the answer is
  rate limiting at the edge, not billing for it.
- **"Client error" is a judgement call at the margin.** A `429` we issue, a `413` for a body
  we chose to reject, a `400` from a validation rule we tightened — each is arguably ours
  rather than theirs. The rule needs a specific status-code list, maintained deliberately,
  not a category that drifts.
- **Unauthenticated traffic is invisible to billing entirely**, so abuse detection needs its
  own signal rather than falling out of usage data.

## Where it breaks

The clean line between "our fault" and "their fault" blurs under load. A `503` from our own
overload protection is our fault and is not billed — correct, but it means a bad day for us
is also a discount day, and the worse our availability the less we earn. That is the right
incentive, but it should be a conscious one.

It also breaks if a customer discovers that `4xx` and `2xx` cost the same and starts using
cheap-to-serve errors deliberately — unlikely, but the rule is "what we processed", not
"what succeeded", so the cost to us and the price to them can diverge.

Finally, capturing after the outcome means a request that kills the process mid-handler is
never captured. That window is `hot-path`'s and `pipeline`'s to bound and reconcile; this
ADR creates it.

## Consequences for other owners

`hot-path` captures usage after the response status is known, and the capture must run even
when the handler raised. `data-model` stores the outcome alongside the usage row, so a
disputed charge can be traced to what actually happened. `billing-domain` rates only
billable events and never re-derives billability. `test-engineer` proves each of the five
rows in the table above, including that a limit refusal leaves the counter untouched.
