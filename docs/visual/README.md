# Visual companions

Optional offline exports of the three companion pages linked from
[`DESIGN.md`](../../DESIGN.md).

DESIGN.md links the live pages, which work today. This directory exists so those pages can
also be read with no network, no account and no share token — useful for a reviewer on a
locked-down machine, behind a proxy, or working from a copy on a USB stick.

**Nothing links here yet.** Add the exports and repoint DESIGN.md's companions table if you
want the offline copies to be the primary route.

| Suggested filename | Source page |
|---|---|
| `build-record.pdf` | Build record — capabilities, decisions and their costs, the worked example, the evidence |
| `schema-map.pdf` | Schema map — 12 tables, the invariants Postgres enforces itself, the path a rupee travels |
| `codebase-walkthrough.pdf` | Codebase walkthrough — a guided tour of the source |

## Exporting

Open the live page from DESIGN.md's companions table and use its export option. PDF is
preferred over PNG: text stays selectable and searchable, and tables and diagrams stay
legible when a reviewer zooms in.

These are a convenience, not the deliverable. DESIGN.md and the ADRs in `docs/adr/` answer
every question the brief asks on their own.
