# Technical Decisions — Zenith Enterprise

**Date:** 2026-07-27
**Status:** Approved candidates. Final confirmation subject to the M0 measurements.
**Related documents:** `2026-07-27-iteration-plan.md`, `2026-07-27-mvp.md`

> **What this document is.** The models chosen here are **candidates**, not closed decisions. The embedding model leaderboard shifts every few months. What does not expire is the method: validate against our own evaluation set before committing. And thanks to the reindexing pipeline (§4), getting it wrong costs GPU hours instead of a rewrite.

---

## Stack summary

| Component | Choice | Licence |
|---|---|---|
| Embeddings | BGE-M3 | MIT |
| Re-ranker | bge-reranker-v2-m3 | MIT |
| Storage, search and queues | Postgres + pgvector + pg_search | Open source |
| Hybrid fusion | Reciprocal Rank Fusion (k=60) | — |
| Async queue | procrastinate | Open source |
| PDF parsing | pdfplumber (fast) + Docling (heavy) | MIT / MIT |
| Transcription *(iteration 2)* | faster-whisper + WhisperX | MIT |
| Diarisation *(iteration 2)* | pyannote.audio | Commercial use permitted |
| Embedding serving | Text Embeddings Inference (TEI) | Apache 2.0 |
| Pluggable LLM | OpenAI-compatible interface | — |

---

## 1. Embeddings: BGE-M3

**`BAAI/bge-m3`.** MIT licence, commercial use without restrictions.

Three reasons, in order of weight:

**It produces dense and sparse representations from a single model.** One model covers both semantic search and exact lexical search. It also produces ColBERT-style multi-vector representations if they are ever needed.

**Multilingual out of the box (100+ languages).** The MVP starts in English, and opening up to Spanish will not require changing model. Reindexing (§4) makes that change survivable anyway, but avoiding it is still preferable.

**8192-token context**, which leaves room to choose the chunk size freely.

**Candidates to measure against in M0:**

| Model | Licence | Note |
|---|---|---|
| `intfloat/multilingual-e5-large` | MIT | Solid and lighter. Requires `query:` / `passage:` prefixes; asymmetric and easy to implement incorrectly |
| `Alibaba-NLP/gte-multilingual-base` | Apache 2.0 | Good quality-to-size ratio |
| `nomic-ai/nomic-embed-text-v2` | Apache 2.0 | Matryoshka embeddings: dimensions can be truncated to save storage |

**Ruled out:** Jina embeddings v3, despite its quality. CC-BY-NC licence, non-commercial. For a startup that is a blocker, not a detail.

---

## 2. Re-ranker: bge-reranker-v2-m3

**`BAAI/bge-reranker-v2-m3`.** MIT, multilingual, same family as the embedder.

It is a cross-encoder: it processes question and chunk together instead of comparing vectors. Far more accurate and far more expensive, which is why it only ever runs on the finalists.

**Configuration:** retrieve 50-100 candidates, re-rank, keep the top 8 for the LLM.

**Alternative if more power is needed:** `mxbai-rerank-v2` (Apache 2.0).

---

## 3. Storage, search and queues: Postgres

**Postgres with `pgvector` for vectors, `ParadeDB/pg_search` (or native FTS) for lexical search, and `procrastinate` for queues.**

This is deliberately the boring option. Reasons, in order of weight:

**The product is installed on-premise.** Every additional service is operational load handed to the customer. Deploying a Postgres inside a company's infrastructure is trivial. Asking their IT team to also maintain a Redis cluster and watch its persistence is friction that eventually gets paid in support tickets.

**RF-04 lives there naturally.** Tenants, roles and ACLs are relational data. Keeping them in the same transactional database as the vectors means isolation can be enforced with Row-Level Security (§5) rather than with programming discipline.

**At our scale it is more than enough.** 100,000 documents is on the order of 1-2 million chunks. pgvector with an HNSW index handles that comfortably.

**Idempotency for free (RNF-05).** If the transaction fails, the queue state rolls back together with the chunk inserts. Zero inconsistency, no extra code.

**Deduplication for free (RF-03.3).** A uniqueness constraint on the file hash, in the same database.

**Cascading delete for free.** Deleting a document deletes its pages, chunks and vectors in the same transaction — the minimum GDPR demands. Split across two systems it would be a permanent source of orphaned data.

**Connection pool separation.** Since Postgres concentrates every workload, user reads (search) use a different pool from the ingestion and reindexing workers. It stops a burst of writes from exhausting the connections available for querying. Detail and further mitigations in `mvp.md` §3.7.

*Common correction: `procrastinate` does not poll the queue in a loop — it uses `LISTEN`/`NOTIFY`, so workers wait for a notification. The I/O pressure from that side is smaller than usually assumed; the real contention is HNSW search CPU and bulk ingestion writes.*

**When to migrate to Qdrant — a measured criterion, not intuition.** Access-label filtering (RF-04.2) applies **inside** the vector query, and aggressive pre-filtering degrades the HNSW index: if a user reaches 5% of the corpus, the index has to walk much further to gather 50 candidates.

M0 measures Recall@50 and p95 with the user reaching 100%, 20% and 5% of the corpus. **If the drop exceeds 5%, migrating to Qdrant is justified by data** — in week four with an empty database, not with customers in production.

---

## 4. Hot reindexing and vector spaces

Satisfies RNF-08. One of the three decisions that are architecture rather than hygiene.

### 4.1 The problem

An embedding only means anything inside the model that produced it. Vectors from two different models **are not comparable**: it is measuring in centimetres and in inches with no conversion factor. So half a corpus cannot be embedded with one model and half with another — searches would return nonsense.

Without planning ahead, changing model forces you to: stop the system, delete every vector, regenerate them (tens of GPU hours on a real corpus) and trust the new model is actually better, because the old vectors no longer exist.

**With customers in production that is unacceptable.** And the underlying effect is worse: it paralyses, because it forces you to get it right first time.

### 4.2 The solution

**Allow more than one vector space to exist at once.** Embeddings do not live in a column of `chunks`, but in their own table:

```
chunk_embeddings
  chunk_id
  embedding_model      -- "bge-m3"
  embedding_version    -- "1.0"
  embedding            -- vector
  PRIMARY KEY (chunk_id, embedding_model, embedding_version)

embedding_spaces
  model, version, dimension, status   -- active | building | retired
```

Searches always query the space marked `active`. Changing model is changing one row.

*Detail: if the new model has a different dimensionality, pgvector requires a new column, so the migration creates one table per space rather than additional rows. The concept is unchanged.*

### 4.3 Reindexing is not reprocessing

The schema persists each stage separately: `pages.text` holds the parsing result and `chunks.text` the chunking result.

Re-parsing 60,000 PDFs with Docling takes days. Re-embedding already extracted text takes hours. **Separating and persisting the stages makes it possible to redo only the last one.** That is the underlying reason the data model has the shape it has.

### 4.4 Switchover procedure

1. The new model is registered as a space in `building` state.
2. A background job generates the new vectors **while the system serves normally** from the active space. It takes as long as it takes.
3. When it finishes, the evaluation harness runs against the new space and is compared against the real Recall of the active one — not against lab benchmark numbers.
4. If it improves, it is marked `active`. The switch is instantaneous.
5. The previous one stays `retired` for a week in case a rollback is needed.

**Zero downtime. Rollback in one second.**

### 4.5 Versioning the chunking strategy

The same reasoning applies to chunking. `chunks.chunking_version` allows a new strategy to be rolled out progressively and compared on which one retrieves better with real data.

---

## 5. Isolation and scope: Row-Level Security

**Both levels of filtering are enforced by Postgres RLS, not by `WHERE` clauses in application code.**

| Level | Requirement | Policy |
|---|---|---|
| Between tenants | RF-04.1 | The session's `app.tenant_id` |
| Between roles of the same tenant | RF-04.2 | Access labels the role reaches |

A data leak between customers is the worst possible outcome for this product. With application-level filtering, one query forgetting a `WHERE` is enough to cause it. With RLS, the database rejects the rows even when the code forgets. **It stops being a matter of discipline and becomes a structural guarantee.**

Implementation: the session sets `SET LOCAL app.tenant_id` and the reachable labels; every table carries its policy.

**Mandatory tests.** Two automated tests, the most important in the product:

1. **Cross-tenant leak** — two tenants with documents, verifying neither reaches the other's through vector search, BM25, listing or direct access by ID.
2. **Cross-label leak** — inside one tenant, a role without access to a label gets no answers based on those documents **and cannot infer that they exist**: not in citations, not in listings, not in messages of the "there is something you cannot see" kind.

They are the answer we show when a corporate customer asks about isolation.

### 5.1 The bypass surface

RLS is only a structural guarantee if the ways around it are few, named, and known. There are exactly **three**, and this list is the complete one. Anything added to it is an architectural change, not an implementation detail.

| Route | Why it exists | Who may use it |
|---|---|---|
| `owner_session()` | Creating the first tenant, and migrations. No context exists yet, so the operation cannot be scoped by one. | The install CLI and Alembic. **Never** a request handler. |
| `zenith_authenticate_lookup(email)` | Login must find a user before a tenant context exists — the context is what the login is establishing. | `AuthService._lookup`, and nothing else. |
| The three label-sync triggers | They maintain a derived value, not an access decision. | Postgres itself, on write. Not callable from application code. |
| The schema owner's credentials | Postgres never applies policies to a table's owner. | Nobody: the application connects as `zenith_app`. |

**The schema deliberately does not use `FORCE ROW LEVEL SECURITY`,** because the owner has to create the first tenant before any context exists. The risk that opens — pointing the application at the owner's credentials disables isolation with no symptom at all — is closed by `verify_rls_active()`, which refuses to serve if `tenants` returns anything with no context set.

**On `zenith_authenticate_lookup` (added in F2).** It is a `SECURITY DEFINER` function, the only one in the schema. The alternative was an `owner_session()` inside an unauthenticated HTTP handler, and the owner connection can read every row in the installation; this returns four fields — user id, tenant id, password hash, token version — for one address, and cannot be coaxed into reading anything else. `search_path` is pinned to `public, pg_temp`, because a `SECURITY DEFINER` function without that can be hijacked by a caller-controlled schema.

**On the label-sync triggers (added in F3).** `documents.label_ids` and `chunks.label_ids` are copies of `document_labels`, and they exist because the direct version does not run — a policy on `documents` reading `document_labels`, whose own policy reads `documents`, makes Postgres abort on mutual recursion. Three triggers keep the copies correct: one recomputes the document array from the join table, one propagates it to the chunks, and one fills a chunk's array from its document at insert time, because ingestion creates chunks minutes after the document was labelled and an empty array means visible to the whole tenant.

They are `SECURITY DEFINER` because the `WITH CHECK` on `documents` would otherwise block an administrator holding `labels.manage` from removing a label they do not personally reach — an error about a row they never mentioned. The trigger is not deciding access; the access decision is made where the user acts, by RLS on `document_labels` and by `requires("labels.manage")` on the endpoint. Deciding it twice, in the place with the least information, is not a second layer of defence.

Auditing the bypass surface is therefore two greps: `owner_session` and `SECURITY DEFINER`.

**F4 declined to add a fifth route, and the reason generalises.** Unique constraints see every row; RLS does not. So an upload of bytes already held under a label the caller cannot reach finds nothing with `SELECT` and still collides on `UNIQUE (tenant_id, sha256)`. Unioning the labels there — which is what deduplication otherwise does — would require reaching an invisible row, and the only mechanism for that is another `SECURITY DEFINER` function.

The upload is refused instead, with a message that does not confirm a document exists. In a compartmentalised installation, *the fact that Finance holds this particular file* is classified metadata, and an opaque error is better than a metadata leak. The rule this sets: **the bypass surface grows for a security guarantee, never for ergonomics.** The ergonomic answer is `.artifacts/backlog/2026-08-02-request-to-classify.md`, where an administrator who reaches both labels performs the union in their own context, with no bypass at all.

---

## 6. Hybrid search: RRF fusion

```
User question
   │
   ├──► BM25 / pg_search ────► top 50   (exact: codes, names, acronyms)
   │
   └──► Dense vector ────────► top 50   (semantic: intent, synonyms)
                │
                ▼
        Reciprocal Rank Fusion  ──────► combined top 50
                │
                ▼
        Cross-encoder re-ranker ──────► top 8
                │
                ▼
        Pluggable LLM + citations
```

**Formula:**

```
score(d) = Σ  1 / (k + rank_i(d))        with k = 60
```

**Why the lexical half is indispensable.** Pure semantic search fails precisely on exact identifiers: product codes, contract numbers, proper nouns, internal acronyms. If an employee searches for invoice `FAC-2026-99`, the vector looks for the *concept* of an invoice, not the exact string. In a corporate corpus that is a huge fraction of real queries.

**Why RRF instead of normalising scores.** BM25 scores and cosine similarity are not comparable: they live on different, corpus-dependent scales. Normalising them is fragile and demands recalibration on every data change. RRF uses **positions only**, is robust, and has virtually no parameters to tune.

---

## 7. PDF parsing: per-page routing

Extraction quality sets the ceiling for everything else. If the parser mangles a table, no embedder recovers it well. Tables matter especially here: contracts, budgets and financial documents are pure table, and that is where the figures people ask about live.

**Problem:** Docling understands layout through vision models and is computationally heavy. A 1,500-page technical manual would take hours.

**Solution: route per page, not per document.** That manual probably has 40 pages with tables and 1,460 of running text. Per document, you pay Docling on all 1,500. Per page, 1,460 are processed in seconds and Docling is reserved for the 40 that need it.

**Decision criterion, measurable:**

```
Does the page have an extractable text layer?
   No  → scanned            → Docling with OCR
   Yes → any tables detected?
           Yes → Docling
           No  → pdfplumber (fast)
```

`pdfplumber` (MIT) determines both cheaply before deciding.

### A document that yields no text must fail, never succeed quietly

**`status = 'ready'` with zero chunks is forbidden.** If extraction produces nothing — no
text layer and OCR also returns nothing, or OCR is unavailable — the document ends in
`failed` with a `status_detail` an operator can act on.

This is not defensive coding. Without it, an image-only PDF ingests *successfully*: no
exception, no failed status, nothing in any log. The document appears in the list, someone
asks a question about it, and the system answers from a different document entirely. There
is no symptom until a customer notices, and by then they have stopped trusting the answers.

It is the same failure shape as a silent data leak, and it gets the same treatment: the
system refuses rather than proceeds.

Every scan we could find in public archives had already been OCR'd by its publisher — NASA,
Wikimedia and the Library of Congress all run OCR before serving — so the case had to be
constructed to be tested at all. `eval/fixtures.py` builds a PDF with exactly zero
extractable characters by rasterising real pages, which is how a large share of enterprise
scans are produced in the first place.

### Coordinate extraction — mandatory

Both parsers must return, alongside the text, the **bounding boxes** of every chunk. They are what makes highlighting a citation in the PDF viewer reliable; the alternative, matching character offsets against pdf.js's text layer, is fragile and produces misaligned highlights (see `mvp.md` §2.9).

They are stored as an array of boxes — a chunk spans several lines and can cross pages — with **coordinates normalised to page size**, so they survive zoom and viewer scaling.

### Table structure is preserved, not flattened

Docling is routed to a page *because* it detected a table, and it returns that table's
structure. Flattening it back into running prose throws away the only reason we paid for
the expensive parser.

**Tables are written into the chunk text as Markdown.** A model reading

```
| Quarter | Revenue |
|---------|---------|
| Q1      | 1.2M    |
| Q2      | 1.5M    |
```

can answer "what was Q2 revenue?" from the structure. The same table flattened —
`Quarter Revenue Q1 1.2M Q2 1.5M` — leaves it guessing which number belongs to which
row, and the guess is confident and wrong. Contracts, budgets and financial reports are
where the figures people actually ask about live (see the opening of this section), so
this is not an edge case.

**Scope boundary.** This is a formatting decision at ingestion: no new service, no new
dependency, and it uses structure the parser already produced. Letting a model *execute
code* over those tables for exact arithmetic is a different feature with a sandboxing
problem, and it is out of the MVP — see
`.artifacts/ideas/2026-08-01-tables-and-audit.md`.

M0 measures table questions separately for exactly this reason: it records how badly
flattened tables perform, so the improvement here has a baseline to beat instead of an
anecdote.

This data is only available during parsing: omitting it forces a **re-parse** of the whole corpus, not just a re-embed.

### Licence warning

**`PyMuPDF` is AGPL.** It is the fastest option and the one most tutorials use, but in a proprietary product it forces you to release the source or buy a commercial licence from Artifex. **It is not used in this project**, and CI verifies that automatically (§12).

---

## 8. Audio *(iteration 2, out of MVP scope)*

- **`faster-whisper`** (CTranslate2 backend) with `large-v3`: roughly 4x faster than the original implementation and with less VRAM.
- **`WhisperX`** on top, which provides the thing that actually matters: **word-level timestamps**. Without them RF-03.2 cannot be met for audio, because segment-level timestamps drift by several seconds.
- **`pyannote.audio`** for diarisation — **approved as a requirement**, not optional. In corporate meeting minutes, "who said what" is exactly what gets asked. It requires accepting terms on HuggingFace, but it is commercially usable.

---

## 9. The connector: OpenAI-compatible interface

**The connector speaks OpenAI's chat completions protocol.** It is the de facto standard: Ollama, vLLM, llama.cpp, Azure OpenAI, Together and Groq all speak it natively. Bedrock and others need a thin adapter. One interface covers nearly the whole market without touching code.

**Serving local models at the customer:** vLLM if they have a suitable GPU, Ollama if ease of deployment matters more.

**Baseline model for development and CI: Llama 3.1 8B Instruct on Ollama.** The connector is pluggable, but development cannot be: citation prompts are tuned against a fixed model or no regression is reproducible. A small model is chosen on purpose — it is the floor of what a customer will plug in, and what holds there holds on any larger model. Tuning prompts against a large cloud model produces instructions the 8B breaks, and the failure shows up in the first on-premise deployment. Apache 2.0 alternative: Mistral 7B Instruct v0.3.

**Mandatory certification (RNF-06):** no model counts as supported until it passes a suite measuring source faithfulness, citation accuracy and abstention rate. Without this, the day a customer plugs in a small model and citations break, the problem becomes ours.

---

## 10. Chunking and contextual enrichment

- Chunks of ~500-800 tokens, ~15% overlap.
- Cuts respect structure (sections and headings from the markdown Docling produces), never blind fixed size.
- **Contextual enrichment:** before generating the embedding, prepend 1-2 sentences to each chunk placing which document and section it came from. Generated once per document with a cheap local LLM. It lifts retrieval noticeably, because an isolated chunk loses the context of where it lived.
- **Mandatory metadata from day 1:** `doc_id`, `page_num`, `bboxes`, `char_start`, `char_end`, `section`, `tenant_id`, `label_ids`, `chunking_version`. Adding them later forces reprocessing the whole corpus.

---

## 11. VRAM management and concurrency

Reference hardware: **one 24 GB GPU** (RTX 4090 or A10), or a modest cloud instance. The point of sizing to one commodity card is that there is no marginal cost per document once it is bought.

Resident consumption:

| Model | Approx. VRAM |
|---|---|
| BGE-M3 (fp16) | ~2.2 GB |
| bge-reranker-v2-m3 (fp16) | ~2.2 GB |
| faster-whisper large-v3 (int8) *(iteration 2)* | ~1.5 GB |
| **Total resident** | **~6 GB** |

**The OOM risk does not come from model size, but from unbounded concurrency.** Twenty simultaneous jobs, or embedding batches with no cap, will saturate 24 GB. The answer is not more GPU:

- **A semaphore allowing one heavy job at a time.**
- **TEI with a fixed batch size.**
- **Queue priority:** queries are real-time and take precedence; ingestion and reindexing yield the GPU.

With those bounds, consumption stays flat and predictable.

---

## 11b. Hardware profiles: one codebase, three deployments

**Decision:** a single environment variable, `ZENITH_HARDWARE`, selects a **profile** — a
named set of values — and nothing else in the system branches on hardware.

```
ZENITH_HARDWARE = gpu | cpu | low-spec
```

This is installed on-premise. A customer with an A100 and a customer with a 4-core VPS get the same
artifact, and maintaining a branch per customer server is how a product becomes
unsupportable. But the two cannot run the same batch sizes, and finding that out in
production is expensive.

### Why a profile rather than a flag

A boolean spreads. `if gpu:` appears in the compose file, then in the worker, then in the
reranker client, and each site drifts. A profile is a table read in one place:

| | `gpu` | `cpu` | `low-spec` |
|---|---|---|---|
| TEI image | `:cuda-*` | `:cpu-*` | `:cpu-*` |
| GPU devices mounted | yes | no | no |
| `--max-batch-tokens` | 16384 (default) | 8192 | **2048** |
| `--max-client-batch-size` | 32 | 16 | **4** |
| Ingestion embed batch | 256 | 32 | 8 |
| Reranker container | on | on | **off** |
| Concurrent ingestions/tenant | 4 | 1 | 1 |

### The numbers are measured, not guessed

The `low-spec` column exists because **M0 measured it the hard way**. On the development
VPS — 4 cores, 7.6 GB total, ~5 GB free — TEI serving BGE-M3 with **default batch settings
was killed by the kernel at 6.59 GB RSS, during warm-up, before processing a single
document**:

```
Out of memory: Killed process (text-embeddings) anon-rss:6587220kB
```

With `--max-batch-tokens 2048 --max-client-batch-size 4` the same model loads and serves at
**3.71 GB**. The difference between "does not run at all" and "runs" is two flags, and no
amount of design review would have produced them.

### Rules the flag must obey

**It may change performance. It must never change semantics.** Same corpus, same query,
same documents retrieved — RLS, label filtering, deduplication and idempotency are
identical on every profile. A flag that quietly changes what a user can see is a flag that
causes a leak.

**Degradations must be visible, never silent.** `low-spec` turns the reranker off, and M0
measured what that costs: Recall@8 of 75% against a Recall@50 ceiling of 95%, so the
reranker is worth up to 20 points. A customer running without it is getting a materially
worse product and has to be able to tell. `zenith diagnose` reports the active profile and
names every component the profile disabled.

**Unknown values fail at startup**, like every other setting (§ `mvp.md` cold start). An
operator who types `ZENITH_HARDWARE=CPU2` gets an error, not a silent fallback to defaults
that will OOM at 3am.

**`gpu` is not yet measured.** The table's GPU column is inherited from TEI's defaults and
carries no evidence behind it. It gets numbers when there is a GPU to measure on, and until
then it is marked as such rather than presented as a finding.

Consumed by: `docker/docker-compose.yml` (image and device selection), F5 ingestion (batch
sizes), F6 embeddings, and `zenith diagnose` (reporting).

---

## 12. Engineering standards

What separates this product from the hackathon prototype. It is not a matter of taste: these are requirements of selling B2B on-premise, because when something fails at the customer's site there is no way to connect and look.

### 12.1 End-to-end typing

- `pyright` in strict mode on the backend. Pydantic at every boundary.
- Strict TypeScript on the frontend.
- **API client generated automatically from FastAPI's OpenAPI schema.** Backend and frontend lose the ability to diverge silently. It is the highest return per unit of effort in the project.

### 12.2 Testing strategy

Three separate categories with distinct purposes:

| Category | What it covers | Where it runs |
|---|---|---|
| Unit | Pure logic: chunking, RRF fusion, parser routing | Every commit |
| Integration | Against **real Postgres via testcontainers** | Every commit |
| Evaluation harness | Recall@50, Recall@8, citations | On demand (needs a GPU) |

**No SQLite for testing.** The system depends on pgvector, RLS and `tsvector`, none of which SQLite has: testing against SQLite would be testing a different product.

**Tests mandatory because of their criticality:**
- Cross-tenant leak (§5).
- Retrying an interrupted ingestion without duplicates.
- Vector space switchover and rollback.

### 12.3 Continuous integration

From the first commit: `ruff` (lint and format), type checking, unit and integration tests, and validation that migrations apply against a populated database.

**Automated licence checking** that fails on AGPL, GPL or non-commercial clauses. It turns the worry about PyMuPDF and Jina into a mechanical guarantee instead of something to remember.

### 12.4 Parsing security

We process third-party PDFs, which is a known attack surface:

- Type verification by **magic bytes**, not by extension.
- Size limit enforced **before** loading into memory.
- Parsing under resource and time limits. A malicious PDF that exhausts the worker's memory is a trivial denial of service.
- Customer API keys encrypted at rest.

### 12.5 Observability

For a RAG product, logging the answer is not enough. Six months from now we will need to answer *"why did this query return garbage?"*, and that requires storing **the IDs of the retrieved chunks and their scores at every phase**: BM25, vector, post-RRF, post-re-ranking.

Structured logging with `structlog` from day one. It serves both to debug quality with real data and as the seed of the query audit trail (iteration 4).

### 12.6 Error taxonomy

No ad-hoc `try/except`. A closed catalogue of ingestion failures mapped to readable messages: encrypted PDF, no text layer, corrupt, too large, parsing timeout. It is what makes the "readable reason" promise in the UI possible.

Explicit separation between user error (400, human message) and system error (500, generic message plus a trace ID in the log).

### 12.7 Out of scope for now

Robustness for problems that do not exist yet: SSO, high availability, Kubernetes, microservices, feature flags, end-to-end UI tests.

---

## 13. Explicit prohibitions

| Item | Reason |
|---|---|
| `PyMuPDF` | AGPL — would force releasing the source or paying for a licence |
| Jina embeddings v3 | CC-BY-NC — non-commercial |
| LangChain / LlamaIndex in the core | They hide retrieval behind abstraction layers, and retrieval is the product |
| Redis or other extra services | Every service is a support call in an on-premise deployment |
| SQLite in tests | It has no pgvector, no RLS and no `tsvector` |
| Tenant filtering in the application only | One forgotten clause is a data leak between customers |
