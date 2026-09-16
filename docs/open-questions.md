# Open questions

The brief leaves these open, some because the teams genuinely have not decided and some
because what the teams asked for pulls against itself. **Nothing here is resolved.** Each
entry lists the options and what each one costs.

An open question is closed by writing an ADR in `docs/adr/` and updating the entry to point
at it. It is *not* closed by an implementation quietly assuming an answer. If code needs a
decision that lives here, that is a signal to write the ADR, not to pick the convenient
branch.

**Status key:** 🔴 blocking work now · 🟡 needed before the feature lands · 🟢 needed before
this goes live

---

## 1. 🔴 Mid-month plan change: what happens to the fee and the included allowance?

*The headline question.* Commercial explicitly handed this back: "your call, as long as we
can explain it to the customer." A customer on Growth upgrades to Scale on the 18th.

Two things need deciding, and they are separable: **what happens to the monthly fee**, and
**what happens to the included allowance and the band ladder**.

| Option | The customer-facing sentence | What it costs |
|---|---|---|
| **A. No proration.** Charge both full monthly fees; rate the whole month's usage on the new plan. | "You paid for Growth, then upgraded, so you're charged for both months." | Simple and exact, and Support can say it in one line — but it reads as double-charging and will generate complaints on every upgrade. Punishes the upgrade we want to encourage. |
| **B. Time-prorate both fee and allowance by days.** 17/30 of Growth + 13/30 of Scale; allowances scaled the same way. | "You had Growth for 17 days and Scale for 13, and you were charged for each in proportion." | The fairest-sounding and the most common in the industry. Introduces division, therefore rounding, therefore a remainder that must go somewhere deliberate (see #6). Also means the *included* allowance is a fraction, so the band boundaries move mid-month — the ladder now has to be computed per segment. |
| **C. Credit-and-replace (upgrade-favouring).** Charge the new plan's full fee, credit the unused portion of the old one; single ladder on the new plan for the whole month. | "We swapped you onto Scale and credited the part of Growth you hadn't used." | Generous, encourages upgrades, and the simplest ladder to compute and explain — one plan, one ladder. But it retroactively prices pre-change usage on the new plan, which conflicts with "the customer was on Growth at the time". Also needs a downgrade policy, where the same logic runs the wrong way. |
| **D. Segment the month.** Two independent billing periods, each with its own fee, its own allowance, and its own band ladder; the invoice shows both. | "Two periods on one invoice: Growth to the 18th, Scale after." | Most defensible per-charge, and each segment re-derives exactly. But a customer with 600k requests split across two segments may pay *more* than 600k on either plan alone, because each segment restarts the ladder — which is the hardest conversation of the four to have. |

**Coupled to:** #6 (rounding follows from whichever divides), and to whether usage is
counted against the plan in effect at request time (which B and D imply and A and C do not).

**What we would do differently:** if the answer were A or C, the usage table does not need
to know which plan was live at the time of each request, and rating stays a single call.
If it is B or D, the plan in effect must be resolvable for any instant in the month, which
is a schema requirement — so this question blocks `data-model` as much as `billing-domain`.

---

## 2. 🔴 What counts as a billable request?

Nobody has said, and it moves the invoice more than most of the rest of this list.

- **Requests rejected by authentication (401).** Billing them means an attacker with a bad
  key can run up a customer's bill; not billing them means we serve load for free, and a
  broken client retrying forever costs us real capacity.
- **Our own errors (5xx).** Billing for our failures is indefensible to a customer.
- **Requests refused because the customer hit their spending limit (402/429).** Billing
  these is perverse — the customer is charged for being told "no". But the refusal still
  costs us a request to serve.
- **Rate-limited requests**, if rate limiting is added later.

**Options:** (i) bill only 2xx; (ii) bill 2xx and 4xx that reached the customer's own
handler; (iii) bill everything authenticated, however it ended.

**Trade-off:** (i) is the easiest to defend to a customer and the easiest to abuse.
(iii) is the easiest to implement and the hardest to justify. The choice also determines
*where* in the request lifecycle usage is captured, which is a hot-path design constraint,
not just a billing rule — capture before the handler and you cannot know the status.

---

## 3. 🔴 Spending limit: exactly what is being limited, and how far can it overshoot?

Customers said being cut off "well after" passing the limit will generate complaints.
"Well after" is not a specification. Three sub-questions:

- **Does the limit count the monthly fee, or only usage charges?** A Growth customer with a
  Rs. 50,000 limit has either Rs. 35,000 or Rs. 50,000 of usage headroom. Both are
  defensible; only one matches what the customer meant, and we cannot ask them.
- **What is the acceptable overshoot?** This is the number the whole enforcement design
  hangs on. Enforcement without a synchronous billing lookup means checking a counter
  against a pre-computed threshold, so the overshoot is bounded by how stale that threshold
  and counter can be. **We should state a budget — e.g. "never more than N seconds or M
  requests past the limit" — and design to it**, rather than discovering it after the fact.
- **What happens when they raise the limit, or the month rolls over?** Does service resume
  immediately, or at the next refresh? Immediate resumption implies a cache invalidation
  path from a cold, infrequent operation into the hot path.

**Trade-off:** a tighter overshoot bound costs hot-path work per request — more frequent
threshold refreshes, more Redis traffic, or a shorter counter flush interval. A looser
bound is cheaper and risks serving traffic the customer explicitly asked us to stop. This
is the clearest fast-versus-exact tension in the brief, and it should be decided as a
number, not a mechanism.

---

## 4. 🟡 When is a month closed, and what happens to usage that arrives after?

Invoice immutability makes this unavoidable rather than a detail.

- **Timezone.** Asia/Karachi (UTC+5) is the obvious guess for a Pakistani company, but
  nobody said so, and customers may be anywhere. A month boundary in the wrong timezone
  misassigns five hours of usage every month, every time.
- **Cutoff and grace window.** Usage can be in flight when the month ends. Close instantly
  at midnight and some genuinely-in-period usage lands after close; wait for a grace window
  and Finance's invoice is late by exactly that window.
- **Stragglers.** Usage that arrives after close must go somewhere: (i) roll into next
  month's invoice, (ii) hold it as an adjustment line on a future invoice, (iii) drop it —
  which violates "no request goes unbilled", (iv) delay close until reconciliation proves
  zero outstanding.

**Trade-off:** (i) is simple and means a customer's invoice occasionally contains usage
from the previous month, which Support must explain. (iv) is the most correct and makes
invoice timing dependent on the health of the pipeline, which Finance will not enjoy on the
1st of the month.

---

## 5. 🟡 Invoices never change — so how do we correct a mistake?

Finance is unambiguous: a number that moves after it has been sent is unacceptable. But
bugs exist, and at some point an issued invoice will be wrong.

- **Credit note / debit note** — the standard accounting answer. The original stands; a
  second document offsets it. Nobody asked for this, so it is scope nobody has authorised.
- **Void and reissue**, with the void recorded. Cleaner to look at, but the customer has
  already seen the first number.
- **Nothing.** Wrong invoices stay wrong and are fixed by hand outside the system.

**Trade-off:** building credit notes now is unrequested scope; not building them means the
first billing bug becomes a manual finance process under time pressure. Worth naming before
go-live even if the answer is "not in v1".

---

## 6. 🟡 Rounding on prorated amounts — who gets the leftover paisa?

Every per-request price is exact in paisa (80, 50, 35, 25, 15), so **rating needs no
rounding at all**. Division enters only through proration, which means this question cannot
be answered before #1, and disappears entirely if #1 lands on option A.

Rs. 15,000 over 31 days is 48,387.09... paisa per day. Options: round each daily amount and
accept that the parts do not sum to the whole; compute the segment total and round once;
floor everything and give the remainder to the customer; or round half-up and give it to
us. The amounts are trivial. The principle is not: the rule must be stated once, applied at
one named boundary, and must never lose or invent a paisa. A reconciliation that is off by
one paisa is indistinguishable from a reconciliation that is off by a rupee — both mean the
model is wrong.

---

## 7. 🔴 Custom pricing: an override of a plan, or a price list of its own?

Commercial wants to give one negotiated customer a different fee, a different included
amount, or different prices, without a deployment. **This blocks `data-model` from
finalising the plan and price tables.**

- **A. Delta on a plan.** The customer stays on "Growth" with overrides for specific
  fields. Easy to reason about, easy to report on ("all Growth customers"), and awkward the
  moment a deal needs a differently-*shaped* ladder — three bands instead of two.
- **B. A full price list per customer.** Every customer points at a versioned price list;
  named plans are just price lists many customers share. Uniform, no special cases in the
  rating code, and it makes "custom pricing" free rather than a feature. Costs: more rows,
  and "what plan is this customer on?" becomes a lookup rather than a column.
- **C. Both** — a plan reference plus optional overrides. Superficially flexible; in
  practice two code paths through the most important calculation in the system.

**Trade-off:** B is the design that ages best and makes price versioning fall out naturally
(a price change is a new list; old charges cite the old one). A is faster to build and will
be regretted at the first non-standard deal. This should be settled early, because changing
it later means migrating the pricing tables while invoices reference them.

---

## 8. 🟡 If Redis is unavailable, do we fail open or fail closed?

Counters and limit thresholds live in Redis. If it is down or has restarted with an empty
keyspace, the hot path cannot check a spending limit.

- **Fail open** — keep serving, accept that limits are unenforced and usage capture is at
  risk. Protects the customer's traffic; risks serving a customer past the limit they
  explicitly set, and risks unbilled usage.
- **Fail closed** — refuse requests. Protects revenue and honours the limit; takes a paying
  customer's API down because of our infrastructure.

**Trade-off:** this is a business decision wearing a technical costume, and it should be
made by someone who can weigh "we served Rs. 200,000 past a limit" against "we were down
for nine minutes". It also has a middle option — fail open for serving but fail closed for
limit-bound customers specifically — which costs a branch on the hot path.

---

## 9. 🟡 What is the latency budget for usage capture?

"Must not add noticeable delay" is not a number. Until it is one, `hot-path` cannot tell
whether a change is acceptable, and "what did this add to p99?" has no threshold to fail
against. A budget stated as added p99 milliseconds at a given concurrency would make the
constraint testable; without one, every optimisation argument is an opinion.

---

## 10. 🟢 API key lifecycle

Not mentioned in the brief at all, and every one of these has a billing consequence.

Multiple active keys per customer (needed for rotation without downtime)? Revocation, and
how stale a revoked key may be on the hot path before it stops working — a cached key is
fast and a revoked-but-cached key serves an attacker. Keys stored hashed (they should be).
And: does usage from a revoked key still get billed?

---

## 11. 🟢 Usage data retention

Invoices must be explainable, which implies keeping enough detail to re-derive any charge.
For how long? Per-request rows for 7 years is a very large table; aggregated rollups are
small but cannot answer "which requests made up this line?" in full detail. The answer
determines the partitioning and retention strategy, and it is a compliance question as much
as a technical one.
