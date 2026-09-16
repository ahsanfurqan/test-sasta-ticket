# ADR-0013: Corrections are credit notes — policy now, mechanism later

- **Status:** Accepted
- **Date:** 2026-09-16
- **Owner:** billing-domain
- **Resolves:** open question #5

## Context

Finance is unambiguous: once an invoice is sent, its amount must never change. Nobody asked
for a correction mechanism. But an invoice will eventually be wrong — a pricing bug, a
reconciliation gap, a mis-assigned plan change — and at that point either the system has an
answer or somebody improvises one under pressure.

The brief is also explicit that a documented gap costs far less than one discovered by the
assessors.

## Decision

Two parts, deliberately separated:

**Policy, effective now.** A correction is never an edit. An issued invoice stands exactly
as issued; an error is corrected by issuing a **credit note** (or debit note) that references
the original and offsets it. The customer's balance is the sum of the documents, never the
result of rewriting one.

**Mechanism, not built in v1.** Credit notes are not implemented. What *is* implemented is
the immutability that makes them the only possible correction: an issued invoice is
protected at the schema level, so no code path — including a well-intentioned bug-fix job —
can alter it.

Until the mechanism exists, a discovered error is handled manually by Finance, and this is
recorded as a known gap rather than presented as handled.

## Alternatives considered

- **Build credit notes in v1.** Complete and correct. Rejected on YAGNI while the five core
  capabilities are still unbuilt: correction machinery for invoices that do not yet exist is
  the wrong thing to spend the day on, and building it early risks designing it against
  imagined failures rather than real ones.
- **Void and reissue.** Visually cleaner than an invoice plus an offsetting note. Rejected
  because the customer has already seen the original number, so voiding does not undo it —
  it just removes our own record of what they saw. It is also weaker as an audit trail, which
  is the thing Finance actually needs.
- **No policy at all.** Defensible for a first version. Rejected because the policy costs
  nothing to state and determines a schema decision we are making *now*: immutability must
  be enforced in the database, and that is only obviously correct once the correction path
  is known to be "a second document".

## What it costs

- **A real gap between an error being found and being correctable.** Finance handles it by
  hand, outside the system, with no audit trail beyond whatever they write down. That is a
  genuine operational risk and it is being accepted knowingly.
- **Schema immutability now constrains the mechanism later.** Credit notes must be a
  separate document type rather than a negative adjustment on the original, which is the
  right model but forecloses simpler-looking options.
- **The customer's "balance" stops being a single number** once credit notes exist, becoming
  a sum over documents. Anything that displays an amount owed has to know that in advance.

## Where it breaks

The policy holds for ordinary corrections. It strains for a systematic error — a pricing bug
affecting every invoice for a month. Issuing thousands of credit notes is technically the
same operation repeated, but commercially it is a very different event, and the right
response might be a negotiated settlement rather than a document per customer.

It also breaks if an error is found *before* the customer sees the invoice but *after* it is
marked issued. Strictly, immutability applies; practically, a credit note for an invoice
nobody read is absurd. That gap between "issued" and "delivered" is not modelled, and
probably should be.

## Consequences for other owners

`data-model` enforces invoice immutability in the schema — constraint or trigger, not
application code — from the first invoice migration. `billing-domain` treats an issued
invoice as read-only in every code path. This ADR is referenced by the known-gaps section of
`DESIGN.md`, which must state plainly that corrections are manual today.
