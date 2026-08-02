# F5 — Parsing and ingestion

F4 stops at `status='pending'`. F5 turns those bytes into `pages` and `chunks`, and stops
before the vectors: embedding is F6. What F5 owns is **text quality**, which M0 established
is the ceiling on everything downstream — a mangled table is not recoverable by any
embedder, and no reranker fixes a page that was never read.

M0 also made this the first feature where the hardware profile is load-bearing rather than
documented.

---

## 1. The profile becomes code

`ZENITH_HARDWARE` is currently a decision in `technical-decisions.md` §11b and nothing
else. F5 implements it, in one module, as a table:

```python
# app/core/hardware.py
@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    max_batch_tokens: int        # must match TEI's own flag
    max_client_batch_size: int
    embed_batch: int
    ingestion_concurrency: int
    reranker: bool
    ocr: bool
```

Three rules from §11b, restated because F5 is where they are first testable:

- **It may change performance. It must never change semantics.** Same corpus, same query,
  same documents retrieved. A test asserts that the parser routing decision for a given
  page is identical on every profile — only *how long it takes* may differ.
- **Degradations are visible.** `zenith diagnose` reports the active profile and names
  every component it disabled.
- **An unknown value fails at startup**, like every other setting.

The one place `low-spec` changes an outcome rather than a duration is OCR (§4), and that
change is a *refusal*, never a silent success.

## 2. Strict sequential processing on `low-spec`

M0 earned three failure modes on the 8 GB VPS, and each one becomes a rule here.

**Exit 139.** `--max-concurrent-requests 4` panics TEI `cpu-1.8` outright:
*"Queue background task dropped the receiver... This is a bug."* The flag is never passed.
Concurrency is controlled on **our** side, by the number of workers, because a queue we own
degrades and a queue inside TEI crashes.

**OOM at 6.59 GB during warm-up.** TEI holds the model plus the activations for a whole
batch. On `low-spec` the ingestion worker runs with `concurrency=1`: one document, one page
at a time, no overlap between the parser's memory and the embedder's.

**413 Payload Too Large.** Covered in §3 — it is a batching bug, not a concurrency one.

Procrastinate is already a dependency. The worker's concurrency comes from
`Profile.ingestion_concurrency`, so `low-spec` is genuinely serial rather than
"usually serial".

## 3. Dynamic batch sizing, by token budget

The 413 in M0 happened because two numbers were set independently: TEI ran with
`--max-batch-tokens 2048`, and the client sent 8 chunks of roughly 350 tokens — 2,800, and
every request failed. Fixing it by hard-coding 4 fixes the symptom for one chunk size.

**Chunks are accumulated into a request until the estimated token count reaches the
profile's `max_batch_tokens`, then sent.** A batch is a token budget, not a count, so a
page of dense tables and a page of sparse prose both produce a legal request without anyone
tuning anything.

The estimate is deliberately crude and deliberately pessimistic — characters ÷ 3 rather
than a real tokeniser — because the cost of overestimating is a smaller batch and the cost
of underestimating is a failed request. F5 sets the chunk size bound that makes the budget
satisfiable: **no single chunk may exceed `max_batch_tokens`**, or no batch containing it
can ever be legal.

Consumed by F6, defined here, because the chunk size and the batch budget are one decision
in two places — exactly the pair a profile exists to hold together.

## 4. Per-page routing, and the rule M0 proved incomplete

§7's criterion:

```
extractable text layer?
   no  → Docling with OCR
   yes → tables detected? → yes: Docling / no: pdfplumber
```

M0 found three distinct extraction failures, and **only two of them this rule catches**:

| Document | Failure | Caught by §7? |
|---|---|---|
| arXiv papers | missing spaces — `densevectorindexofWikipedia` | no, and BM25 is dead on those pages |
| NASA report | missing OCR, 925 chars/page against ~4,000 expected | yes — no text layer |
| IRS forms | **scrambled column order** | **no** |

The IRS case is the dangerous one: every word is present and correct, so character counts,
extraction ratios and Recall@8 all look healthy. Only the meaning is wrong.

**F5 adds a third routing signal: two-column layout detection.** pdfplumber already returns
word positions; a page whose word x-coordinates form two well-separated clusters is routed
to Docling regardless of whether tables were detected. It is a heuristic, and it is
proposed as one — the validation is re-running M0's corpus and checking the IRS pages
specifically, since no aggregate metric will move.

The missing-spaces case gets a **detector rather than a fix**: a page whose text contains
long runs with no whitespace is recorded in `pages.extraction_method` as suspect. We cannot
repair it here, but a page that silently poisons BM25 should not be indistinguishable from
a clean one.

## 5. A document that yields no text must fail

`status='ready'` with zero chunks is forbidden — the constraint M0 made non-negotiable, and
F4 deliberately left to F5 because F5 owns the transition.

The witness already exists: `eval/fixtures.py` builds a PDF with exactly **zero**
extractable characters, and `tests/test_corpus.py` asserts that property still holds.
F5 adds the other half — that document reaching ingestion ends in `failed` with a
`status_detail` an operator can act on.

**On `low-spec`, OCR is off**, so a scanned document fails with
`"no text layer, and OCR is disabled on the low-spec profile"`. That is a degradation, and
per §11b it must be visible: it appears in the document's status, and `zenith diagnose`
names OCR among the disabled components. It is not silently treated as an empty document.

Sabotage check: remove the zero-chunk guard and confirm the image-only fixture reaches
`ready`.

## 6. Bounding boxes are mandatory

Both parsers return, alongside the text, the bounding boxes of every chunk — **normalised
to page size**, so they survive zoom and viewer scaling. A chunk spans several lines and can
cross pages, hence an array.

This data exists only during parsing. Omitting it forces a **re-parse** of the whole corpus
rather than a re-embed, which M0 priced at roughly 5 minutes per 100 pages — eight hours
for a 10,000-page corpus. It is cheap now and expensive later, which is the definition of
something to get right the first time.

Tables are written into chunk text as **Markdown**, not flattened. M0 could not measure the
benefit — recall finds the page, so it cannot tell whether the model can *read* the table —
and the evidence for it therefore arrives in F9. The formatting decision still belongs
here, because it uses structure the parser already produced and throwing it away would mean
paying for Docling and discarding what we paid for.

## 7. Idempotency

A task that runs twice must not produce two sets of chunks. Re-ingestion deletes the
document's existing `pages` and `chunks` inside the same transaction that writes the new
ones. Procrastinate retries on failure, and a partially-parsed document that gains a second
copy of every chunk on retry would double every later search result.

`chunking_version` on `chunks` exists for the other direction: a chunking change re-chunks
without re-parsing, because `pages` persists the extracted text. That is what makes a
reindex hours instead of days.

---

## The one open question, and my recommendation

**The worker needs an RLS context, and the obvious ones are all wrong.**

Writing `chunks` requires a context whose labels intersect the document's — that is the
`WITH CHECK` from F3. But reading the document to *learn* its labels requires already
holding them. A worker with no labels cannot see the document it was asked to ingest.

Three ways out:

1. **The task payload carries the document's labels, captured at enqueue time** — the
   uploader has just written them, so no read is needed. Zero bypass. The cost is that a
   document relabelled between enqueue and execution has a stale context, and the task
   fails; the relabel operation re-enqueues it.
2. **The worker holds every label in the tenant.** No cross-tenant exposure, but a
   compartment-wide widening inside a tenant, and it is exactly the shape of thing that
   later gets reused by something that does return data to a user.
3. **A fourth `SECURITY DEFINER` route.** Rejected on the rule F4 set: the bypass surface
   grows for a security guarantee, never for convenience.

**I recommend (1).** It keeps the bypass surface at four routes and both audit greps valid,
and the failure mode it introduces is loud, rare, and self-healing. It also makes the
ingestion task a pure function of its payload, which is the property that makes retries
safe.

---

## Order of work

1. `app/core/hardware.py` — the profile table, startup validation, `zenith diagnose`
   reporting. Small, and everything else reads it.
2. Parsers behind one protocol: `PdfPlumberParser` first, with bounding boxes.
3. Routing, including the two-column detector, with the M0 corpus as its test set.
4. Chunking with bboxes, `section`, `context_prefix`, and the size bound from §3.
5. The pipeline and status machine, including the zero-chunk refusal.
6. The Procrastinate task and the worker, with `ingestion_concurrency` from the profile.
7. `DoclingParser` — last, because it is the heaviest dependency and everything above must
   be correct without it.

Probably two PRs: 1–5 (the pipeline, testable with no new services) and 6–7 (the worker and
Docling).
