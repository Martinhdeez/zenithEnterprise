# F5 — decisions taken without review

Written during an autonomous session, under the instruction to pick the safest MVP route at
a fork rather than stop and ask. Each entry says what was chosen, what it costs, and what
would change it. Nothing here is settled; all of it is reversible.

---

## 1. The worker's RLS context comes from the task payload

**Chosen:** the ingestion job carries `tenant_id` and the document's `label_ids`, captured
at upload when they had just been written.

**Why.** Writing `chunks` needs a context whose labels intersect the document's (F3's
`WITH CHECK`), but reading the document to *learn* its labels needs those labels already. A
worker with none cannot see the document it was told to ingest. The alternatives were a
worker holding every label in the tenant — a compartment-wide widening that something
user-facing would eventually reuse — or a fifth `SECURITY DEFINER` route, which F4's rule
forbids for convenience.

**Cost.** A document relabelled between enqueue and execution leaves a stale payload. The
worker cannot see it, reports `unknown`, and does nothing. **Nothing currently re-enqueues
it**, which means such a document sits in `pending` until someone notices.

**Closed, partly.** `zenith reingest` finds documents in `pending` or `failed` and
enqueues them with the labels read *now* rather than the ones the old payload carried. It
reports by default and only enqueues with `--apply`, because sending a thousand documents
to a machine sized for one at a time is an operator's decision rather than a side effect of
asking what is stuck.

**Still open:** the relabel operation does not re-enqueue automatically, so recovery is
manual. That would be a small change in `LabelService.set_document_labels`, and it touches
F3 code this session was not asked to modify.

## 2. Pages routed to `LAYOUT` are parsed by pdfplumber anyway, with a warning

**Chosen:** Docling is not wired. A page that routing says needs layout understanding is
still extracted by pdfplumber, and the page records
`pdfplumber (multi-column layout: ...; layout parsing arrives in F9)` in
`pages.extraction_method`.

**Why.** M0 moved Docling's justification to F9 — recall finds the page, so it cannot
measure whether a model can *read* the table. Dropping those pages instead would remove
real content from the index to avoid degrading it, which is the worse trade: a two-column
page read badly is still better than a two-column page absent.

**Cost.** Interleaved text is in the index and looks clean. The warning is the only signal.

## 3. The two-column detector is a heuristic, unvalidated against the corpus

**Chosen:** a page whose word positions show one wide gutter with substantial text on both
sides routes to `LAYOUT`.

**Why.** M0 found that §7's rule catches two of three extraction failures and misses column
interleaving entirely — the IRS case, where every word is correct and only the meaning is
wrong, invisible to character counts and to Recall@8.

**Cost.** Thresholds (`COLUMN_GAP = 0.08`, a quarter of words on each side) were chosen by
reasoning, not by measurement. Unit tests cover the shape of the signal, **not** the real
corpus.

**What would change it.** Run the detector across M0's thirteen documents and check the
IRS pages specifically. No aggregate metric will move, so this needs eyes on pages.

## 4. Chunks do not cross page boundaries

**Chosen:** a chunk belongs to exactly one page.

**Why.** A chunk spanning a page break needs boxes on two pages and a citation that
highlights two places. The retrieval cost of the occasional split paragraph is smaller than
the cost of getting that wrong, and M0's 75% baseline was measured with per-page chunking.

**Cost.** A paragraph split by a page break is weaker in both chunks than it would be whole.

## 5. Token estimation is characters ÷ 3

**Chosen:** a crude, pessimistic estimate rather than loading a real tokeniser.

**Why.** The number decides only where to split a list. Overestimating costs a slightly
smaller batch; underestimating costs a `413` and a document stuck in `embedding`. Loading
BGE-M3's tokeniser into the API process to compute it would be a large dependency for a
rounding decision.

**Cost.** Batches are smaller than they strictly need to be, so ingestion is slower than
optimal. Against M0's measured five minutes per hundred pages, this is not the bottleneck.

## 6. `reportlab` added as a test-only dependency

**Chosen:** tests generate PDFs rather than committing binary fixtures.

**Why.** The assertions depend on the exact text in the file, and a checked-in binary
drifts from what the test claims it contains. It is in the dev group, so the licence gate —
which since PR #8 scopes to `uv export --no-dev` — does not see it, and it never ships.

## 7. A failed enqueue does not fail the upload

**Chosen:** `enqueue_ingestion` logs and returns.

**Why.** The document is stored, its labels are correct, and it sits in `pending`. Losing a
customer's file because a queue insert failed is much the worse outcome.

**Cost.** A document can be `pending` with no job behind it. Closed by the same
`zenith reingest` as (1) — the two failures were different causes with one shape.

## 8. Procrastinate's schema is a separate install step

**Chosen:** `zenith install-queue`, run once after `alembic upgrade head`.

**Why.** Procrastinate owns and manages its own schema. Folding it into our migrations
would give our `downgrade` opinions about a library's tables.

**Cost.** One more step in the install runbook, and a worker started before it fails. That
failure is loud, which is the right direction.

---

## Known gaps, in the order they should be closed

1. **Relabelling does not re-enqueue.** `zenith reingest` recovers a stranded document, but
   only when an operator runs it. Automatic recovery belongs in `LabelService`.
2. ~~The two-column detector is unmeasured.~~ **Closed.** Measured against the corpus, which
   showed the shipped version detected nothing at all; the replacement flags 100% of
   two-column pages at a 1.0% false-positive rate. See `2026-08-02-f5-validation.md`.
3. **Docling is absent**, so `LAYOUT` is a label rather than a behaviour.
4. ~~No end-to-end test with a real TEI.~~ **Closed.** Both documents ingested on the VPS
   with zero 413s, peak memory 2.67 GiB and one request in flight; the negative control
   died with exit 139 after three sequential requests. See
   `2026-08-02-f5-validation.md`.
