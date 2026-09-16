# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** repo

## Context

This system is being designed from nothing, under a brief that is under-specified on
purpose. Most of the work is choices: what a plan is as data, how usage gets recorded, how
the fast path and the exact path are reconciled, when a month closes. The assessment is
explicit that the reasoning is weighted as heavily as the code, and that the design
document is read carefully.

Two failure modes threaten that. First, a decision gets made, the reason is forgotten, and
six weeks later someone reverses it without knowing what it was protecting. Second — more
immediate — the reasoning lives only in a conversation, and the artefact that remains is
code that looks arbitrary.

There is a third pressure specific to this repo: work is split across several owners
(`.claude/agents/`), each defending different invariants. Those invariants conflict by
design. A shared, written record is how a conflict gets resolved once rather than
relitigated every session.

## Decision

Every non-obvious choice is recorded as an ADR in `docs/adr/`, numbered sequentially,
using `0000-template.md`. An ADR states the **decision**, the **alternatives considered**,
**what it costs**, and **where it breaks**.

ADRs are immutable once accepted. A decision that changes gets a new ADR that supersedes
the old one; the old one stays in the repo with its status updated. The history of what we
believed and when is part of the record.

An item in `docs/open-questions.md` is resolved *by* writing an ADR, and the open question
is then updated to point at it. That is the only way an open question gets closed — not by
an implementation quietly assuming an answer.

"Non-obvious" means: a reasonable engineer could have chosen differently. Choosing Postgres
over a bespoke storage engine is obvious. Choosing how to prorate a mid-month plan change
is not.

## Alternatives considered

- **A single DESIGN.md written at the end.** The brief asks for exactly this document, and
  it will exist — but written at the end, from memory, it reports conclusions and loses the
  alternatives. ADRs written at decision time feed DESIGN.md with material that cannot be
  reconstructed later. The two are complements, not substitutes.
- **Comments in the code.** Survive only as long as the code, and cannot record an
  alternative that was rejected — there is no line to attach it to.
- **Commit messages.** Correct location, wrong ergonomics: nobody reads git log to find out
  why the proration policy is what it is, and a commit cannot be superseded.
- **No formal record.** Viable for a one-day exercise with one author. It fails the moment
  someone asks "why?" in the walkthrough and the answer is a reconstruction rather than a
  citation.

## What it costs

Writing time on the critical path of a time-boxed exercise, and a real risk of
ceremony — ADRs for choices nobody would question. Mitigated by the "non-obvious" bar above.

There is also a discipline cost: an ADR written *after* the code is a rationalisation, not a
decision record, and it is tempting to write them that way when the code already works.

## Where it breaks

If ADRs are written after the fact to justify what was already built, this degrades into
paperwork that actively misleads — a reader trusts a recorded alternative was genuinely
weighed. The signal that this has happened is an ADR whose "alternatives considered"
section only contains straw men.

It also breaks down beyond roughly 30-40 ADRs without an index, at which point the
decisions become unfindable and get re-made.

## Consequences for other owners

Every agent writes ADRs for choices in its own area. Three are expected early and are
already flagged in `docs/open-questions.md`: the mid-month proration policy
(`billing-domain`), the shape of custom pricing (`data-model`, blocking), and the
fail-open/fail-closed behaviour when Redis is unavailable (`hot-path`).

`design-critic` reviews ADRs before they are accepted, and attacks the "where it breaks"
section hardest — it is the section most likely to be written optimistically.
