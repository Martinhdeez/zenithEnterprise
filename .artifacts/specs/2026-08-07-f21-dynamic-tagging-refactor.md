# F21 — Dynamic tagging refactor: scoping spec

Written before any implementation, per request. This is the document to review and edit
before a single line of code changes.

## 0. Where the request and the codebase disagree

The request describes three things as current problems. None of them match what's
actually in the repo today, and getting this straight matters more than any of the new
endpoints below — it changes what "done" means.

1. **"Labels behave like rigid, single-choice physical folders."** They don't. `documents.label_ids`
   is an array (`backend/app/features/documents/model.py`) — a document already carries
   zero, one, or many labels. `Folders.tsx` renders each label as a folder-shaped view
   *because that's a readable metaphor for a small tag set*, not because the data model
   enforces one-label-per-document.

2. **"Upload forces a label via a primitive dropdown."** It doesn't. `Upload.tsx`'s label
   picker is a multi-select chip grid — click any number of chips, including zero (the
   empty-state help text literally reads "no label — visible tenant-wide"). There has
   never been a `<select>` in this component. Zero-label uploads have worked since F3.

3. **"Thousands of tags create severe friction."** `access_labels` has no pagination, no
   fuzzy search, and the sidebar renders every label as a chip — that part is real. But it's
   worth being precise about the actual scale this product targets before designing for
   "thousands": `max_documents_per_tenant` is 5,000 (`core/config.py`), and mvp.md's
   design-partner scope is one tenant, English PDFs, a handful of departments. A tenant
   with thousands of *labels* — as opposed to thousands of *documents* — is a different
   product shape than what F0–F20 were built for. Worth confirming that is actually the
   target before building fuzzy search infrastructure for it.

None of this is a rejection of the underlying complaint — label search *does* get
unpleasant past a few dozen labels, rendered as an unpaginated chip wall. It's a
correction of the premise, so the plan below fixes the real gap instead of re-solving an
already-solved problem.

## 1. The non-negotiable constraint this whole feature sits on top of

**`access_labels` is not a folksonomy. It is the access-control primitive.**

```
role_labels     — which labels a role can see
document_labels — which labels a document carries
RLS policy      — visible iff label_ids = '{}' OR label_ids && caller's reachable labels
```

Every label merge, bulk delete, or "just clean this up" action is a change to who can see
what, enforced by Postgres RLS, not a filing-cabinet reorganisation. Three consequences
that must hold in whatever ships:

- **`LabelService.delete` already refuses to delete a label that's still on a document**,
  specifically because an unlabelled document is tenant-wide visible — deleting a label
  out from under a document would silently *widen* access (`labels/service.py:70-91`,
  comment already explains this). `DELETE /labels/{id}` already exists; the request's
  step 1.3 lists it as new work. It's not — the only new work is `POST /labels/merge`,
  and merge has the same widening risk delete does, on both sides of the operation:
  merging label A (reachable by Legal) into label B (reachable by everyone) instantly
  gives Legal's documents to everyone. **A merge endpoint needs a confirmation step that
  states this explicitly** — not just "merge these two names," but "role X currently
  cannot see label B; after this merge, N documents become visible to X."
- **A `#Uncategorized` fallback label is not free.** The system already has a concept for
  "no label was given": `label_ids = '{}'`, tenant-wide visible, handled explicitly
  throughout `folders.py`, `Documents.tsx`'s `unlabelled` filter, and the RLS policy
  itself. Introducing an actual `#Uncategorized` *label* (an `access_labels` row) changes
  that document from "visible to everyone, unconditionally" to "visible to whoever
  `role_labels` grants `#Uncategorized` to" — a **narrowing**, the opposite of the
  no-label case today, unless `#Uncategorized` is wired to be reachable by every role by
  construction. This needs an explicit decision, not an implicit default.
- **Fuzzy search over labels still runs under RLS.** `GET /labels` already branches on
  `may_manage` — a non-admin only sees labels they can reach (`router.py:18-27`). The new
  search endpoint has to preserve that, which rules out a naive `ILIKE`/trigram query
  against the whole table; it's the existing `visible()`/`reachable()` distinction with a
  search clause added, not a new code path next to it.

## 2. Where this request runs into mvp.md's own closed decisions

`.artifacts/specs/2026-07-27-mvp.md` §2.14 ("Explicit non-goals") lists, verbatim:

> Auto-categorisation · **Faceted navigation** · Per-document permissions · **Folder
> hierarchies** · ... SSO · External connectors ...

Step 2.2 of the request — rename Folders to "Smart Views," render tags as combinable
search filters, let users save `[#Legal] AND [#2026]` combinations as sidebar
shortcuts — **is faceted navigation.** That's not a loose resemblance; saved
multi-attribute filter combinations are the textbook definition of the thing §2.14 names.

This was also a closed decision *this session*: when Folders was being redesigned a few
turns ago, the option to build a fuller nested/faceted browsing model was explicitly on
the table (`AskUserQuestion`), and the answer was "solo mejorar el breadcrumb (rápido)" —
the lightweight option, specifically not this one.

Building it now would mean re-opening a decision mvp.md already records as made, silently,
inside a task framed as a UX cleanup. That's the main reason
this needed a spec before code: the request as written is significantly larger than "fix
the tag search UX," and the largest part of it (§2.2) is the part explicitly out of scope.

**Recommendation: do not build §2.2.** If real use has genuinely outgrown "a document has a
few labels, browse by clicking one," that's worth its own
conversation and its own spec — not a rider on a tagging cleanup.

## 3. What's actually worth building

Scoped to the real gap: label search past a few dozen labels is unpleasant, and there's
no bulk cleanup path for a tenant that's accumulated redundant labels.

### 3.1 Backend

- **`GET /labels/search?q=&sort=&cursor=&limit=`** — keyset-paginated (same pattern as
  `GET /documents`, not offset pages — this codebase already made that call for the same
  reason: RLS-filtered counts are expensive and stale). `q` matches on `name` (`ILIKE
  '%q%'` is enough at this scale; pg_trgm is already in the image via ParadeDB if fuzzier
  matching turns out to be needed, but that's a "measure first" call, not a day-one
  requirement). Reuses `visible()`'s admin/non-admin branch — no separate authorization
  path.
- **`POST /labels/merge`** (`{"from": [...], "into": UUID}`, `labels.manage`-gated):
  reassigns `document_labels` and `role_labels` from the source labels onto the
  destination, then deletes the sources. Response includes the visibility-widening
  count described in §1 so the client can show it before the caller confirms — the
  confirmation UI is part of this endpoint's contract, not a frontend nicety bolted on
  after.
- **`#Uncategorized` fallback**: recommend *against* a real label. The zero-label state
  already means "uncategorised, tenant-wide visible" everywhere in the codebase; the
  cheapest correct fix is a frontend label — render `label_ids = []` as an
  "Uncategorized" pill in the UI — with no schema change and no visibility semantics to
  get wrong. If a real assignable label is genuinely wanted (so it can be excluded from a
  role, for instance), that's a bigger conversation about what "uncategorised" is allowed
  to mean, and belongs in its own decision, not a default silently applied during
  ingestion.

### 3.2 Frontend

- **`Upload.tsx`**: swap the chip grid for a `Command`/`Popover` combobox once label count
  passes some threshold (worth picking one empirically, not guessing — 20–30 sounds
  right for what still reads as "a handful of chips" vs. "a wall"). Below the threshold,
  the current chip grid stays — it's more discoverable than a search box for the common
  case of 3–8 labels, and this session already iterated on it four rounds deep (contrast,
  padding, centering, persistent Upload button) at your direction. "Create label {query}"
  inline creation already exists as the "+ New label" affordance; the combobox needs the
  same action, not a redesigned one.
- **Admin → Tag Manager panel**: list + search (via 3.1's endpoint) + document counts +
  merge/delete. Delete already exists server-side; this is a UI in front of endpoints
  that are mostly already there.
- **Sidebar**: no rename, no faceted filters (§2). If label search genuinely needs surfacing
  outside Upload and Admin, the existing Folders view's breadcrumb search bar (if any) is
  the place to add a search box — not a new nav concept.

## 4. Explicitly not building

- "Smart Views" rename and saved multi-tag filter shortcuts — §2, contradicts mvp.md §2.14.
- Real `#Uncategorized` label row — §3.1, needs its own decision if wanted at all.
- Sidebar redesign beyond what F20-ish work already shipped this session.

## 5. Phased plan

Each phase ends with `make check` green and a checkpoint — not a silent run to the end.

1. **Backend**: `GET /labels/search`, `POST /labels/merge` (with the widening-count
   response), tests for both including the RLS-visibility edge cases (§1's merge risk
   specifically needs a test: merging a narrowly-reachable label into a widely-reachable
   one and asserting the response reports the newly-visible document count correctly).
2. **Frontend — Upload combobox**: threshold-gated `Command`/`Popover`, same underlying
   `selected` state and `send`/`confirmStaged` wiring already in place — this is a
   presentation change over the existing multi-select logic, not new upload logic.
3. **Frontend — Admin Tag Manager**: search, counts, merge (with the confirmation step
   §1 requires), delete (already has a server-side guard; the UI needs to surface its
   error — "N documents still carry this label" — clearly rather than as a generic
   failure toast).

Branch: `feat/f21-dynamic-tagging-refactor`, as requested. No merge to `main` without an
explicit go-ahead once phase 3 is verified — this project's own working conventions
(CLAUDE.md) call for that regardless of what a task description says.

## 6. Open questions for you before phase 1 starts

- Confirm dropping §2 (Smart Views / saved filters) — silence isn't consent for reopening
  a closed mvp.md decision.
- Confirm the `#Uncategorized` approach in §3.1 (frontend-only pill vs. a real label), or
  say what "uncategorised" needs to be able to do (excludable from a role? filterable on
  its own?) if the frontend-only version doesn't cover it.
- Confirm the combobox threshold is fine as "pick a number, no dedicated research" rather
  than something that needs measuring against real tenant data first.
