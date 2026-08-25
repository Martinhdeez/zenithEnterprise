# Aspects to improve for the best Zenith Enterprise MVP

**Date:** 25 August 2026  
**Scope:** Read-only review of the project structure, implementation, tests, execution reports, and capacity calculations.

## Executive summary

Zenith already has a strong enterprise foundation. It streams and deduplicates uploads, indexes documents asynchronously, combines lexical and semantic retrieval, opens citations on the source PDF, enforces tenant and label isolation in PostgreSQL, records an audit trail, and exposes degraded operation instead of hiding it.

The MVP should not try to prove unlimited scale. It should prove four specific claims:

1. A company can import a meaningful archive without losing track of files or failures.
2. Users can find exact identifiers and semantically related passages quickly.
3. Every answer links back to the original document and page.
4. Access controls, failures, and capacity limits remain visible and trustworthy.

The current product can demonstrate much of this, but several issues could undermine the presentation. The confirmed large-corpus bottleneck is lexical ranking under row-level security. The most immediate demo risks are incorrect batch-upload orchestration, false completion states, incomplete OCR behavior, and a temporary broad-access window during automatic classification.

## What is already strong

### Trust and security

- PostgreSQL row-level security enforces tenant and label isolation.
- Startup checks prevent the API from accidentally using owner credentials.
- Search narrows access in the database, including the vector retrieval path.
- Citations retain the document, page, passage, and ranking evidence.
- Generated answers can abstain when the corpus lacks evidence.
- Backup and restore procedures include the database, files, and database roles.

These controls distinguish Zenith from a generic “chat with PDFs” demo. They should form part of the commercial story.

### Archive ingestion

- Uploads stream in bounded chunks instead of loading complete files into memory.
- SHA-256 content addressing prevents path traversal and duplicate indexing.
- The queue separates HTTP upload time from document processing.
- Ingestion retries do not duplicate chunks.
- The parser handles columns, tables, spacing problems, and citation geometry with measured heuristics.
- The frontend already shows per-file upload and processing stages.

### Retrieval and user experience

- Search combines exact lexical matching with multilingual BGE-M3 embeddings.
- Reciprocal Rank Fusion avoids comparing incompatible lexical and vector scores.
- Identifier-specific retrieval protects product codes, legal citations, and form numbers.
- Search results report elapsed time and open the cited PDF page.
- Chat streams grounded answers and replaces unverified output with the final validated result.
- Analytics expose retrieval latency, model latency, abstentions, cited documents, and query history.

## Evidence and current limits

The reports establish several useful facts:

- A 26-document live corpus achieved 85% headline Recall@8, 100% document Recall@8, 768 ms median retrieval, and 898 ms p95 over 30 scored questions.
- On a 4-core, 7.6 GB VPS with about 19,500 synthetic passages, median retrieval was about 1.15 seconds for one user, 2.88 seconds for five concurrent users, and 5.16 seconds for ten.
- During continuous ingestion, median retrieval rose to about 3.87 seconds for one user and 4.49 seconds for five.
- CPU ingestion measured 0.88–1.39 seconds per chunk. At that rate, one million chunks require roughly 10–16 days on one worker.
- A 300,000-passage experiment measured the current three-term lexical query at 1.123 seconds without real RLS and 5.953 seconds with it.
- The same experiment estimated about 3.7 KB of HNSW index per vector: roughly 3.7 GB for one million vectors and 18.5 GB for five million.

These figures support a credible small-installation MVP. They do not prove end-to-end performance at millions of passages, many tenants, narrow label access, or high concurrency.

## P0: changes required before the company demo

### 1. Correct bulk-upload orchestration

The browser nominally uploads three files concurrently, but each upload worker continues polling ingestion for up to two minutes before accepting another file. Large batches therefore advance in groups of three and can appear frozen.

The upload pool should release its slot as soon as `POST /documents` returns. A separate watcher should track server-side processing. Status polling should be batched or bounded independently from byte-upload concurrency.

**MVP acceptance criteria**

- A batch starts uploading new files as soon as earlier byte transfers finish.
- Slow ingestion does not block the remaining byte uploads.
- A 20-file batch never leaves most files queued solely because three files are being processed.
- An integration test covers the complete upload-plus-poll orchestration.

### 2. Stop reporting unresolved ingestion as complete

When status polling times out or a transient status request fails, the frontend returns the last known document state. The caller marks every state other than `failed` as `done`, including `pending`, `parsing`, and `embedding`.

Polling should return an explicit outcome: settled, timed out, or polling failed. Unresolved files should remain “Processing—check Documents,” not “Done.”

**MVP acceptance criteria**

- Only `ready` appears as complete.
- `failed` shows the server's actionable reason.
- Polling timeout and polling failure preserve the last confirmed processing state.
- Folder counts and corpus status refresh after bytes are accepted, not after the whole batch settles.

### 3. Make OCR and layout behavior truthful

The `cpu` hardware profile advertises OCR, but the ingestion pipeline states that layout parsing is not wired. A fully scanned PDF fails visibly, but scanned pages inside a mixed PDF can produce no chunks while the document becomes `ready`.

For the MVP, either implement the OCR/layout route or mark unsupported pages as omitted and make the document incomplete. A green `ready` state must mean the searchable representation covers the material document.

**MVP acceptance criteria**

- A mixed readable/scanned document cannot silently drop scanned pages.
- The document status reports the number and reason for omitted pages.
- The hardware profile reports OCR as enabled only when an OCR implementation is available.
- Tests cover fully scanned and mixed-content PDFs.

### 4. Close the automatic-classification access window

An upload without explicit labels receives the broadly reachable default label. Automatic classification narrows it only after chunks and `ready` status have committed. During that interval, users who hold the default label may open the document. Classification failure can leave it there permanently.

The safest MVP option is to require an explicit access label. An alternative is a private `classification_pending` state visible only to the uploader and administrators.

**MVP acceptance criteria**

- An unclassified confidential document is never tenant-wide.
- Classification failure preserves restrictive access.
- The user can see and correct the final classification.

### 5. Fix fresh-install queue provisioning

The deployment guide applies Alembic migrations but omits the separate command that installs Procrastinate's queue tables. A new installation can start without a functional ingestion queue.

**MVP acceptance criteria**

- One documented installation path creates both application and queue schemas.
- Readiness fails if queue tables are missing.
- A stored document that remains pending beyond a threshold raises an operator-visible warning.

### 6. Prepare a controlled, truthful demo corpus

The live demo should use a pre-indexed corpus large enough to make manual browsing impractical. Live ingestion should cover a small batch only; the audience should not wait for a large CPU embedding job.

The demo should show:

1. An exact identifier query.
2. A semantic paraphrase with few shared keywords.
3. A query restricted to one folder or document.
4. A citation opened on the highlighted source page.
5. A grounded chat answer.
6. A question that correctly produces an abstention.
7. A duplicate upload reported as “Already existed; labels updated.”
8. An access-label example that hides the same document from another user.

Display the real corpus size, pages, passages, hardware profile, and measured retrieval time. Label projections as projections.

## P0: work required before claiming enterprise scale

### 7. Replace the linear lexical top-N path

The current lexical query computes `ts_rank_cd` for every matching passage before choosing 50 results. Common terms therefore approach linear cost. Under real RLS, the repository measured 5.953 seconds at 300,000 passages. The 10-second statement timeout will eventually turn slow searches into failed searches.

The planned ParadeDB top-N route measured about 45 ms, but its current label-array query omits reachable labelled passages. It fails closed, so it does not leak data, but it loses recall and cannot ship.

**Required work**

- Complete or reject the ParadeDB/BM25 design.
- Keep tenant, label, unlabelled, and document-scope predicates inside the indexed query.
- Run the full RLS isolation matrix against the new function.
- Compare both retrieval paths on the existing real-corpus evaluation.
- Preserve the current GIN path until rollback no longer requires a reindex.
- Measure index size, build time, and ingestion write amplification.

**Acceptance criteria for a scale claim**

- No cross-tenant or cross-label result in the isolation suite.
- Context Recall@8 does not regress from the established evaluation baseline.
- Identifier questions remain at 100%.
- Frequent-term p95 remains within the agreed interactive budget at the claimed corpus size.

### 8. Prove filtered vector recall

The HNSW index is global. Tenant, embedding-space, and label restrictions filter its results, but iterative scanning is enabled only for explicit document scopes. In a shared index, the nearest vectors may belong to other tenants or inaccessible labels and disappear after filtering.

**Required work**

- Generate many tenants and labels.
- Test accessible fractions from 100% to below 1%.
- Measure candidate count, Recall@8, p50, p95, p99, CPU, and index memory.
- Compare normal and iterative HNSW scans.
- Evaluate tenant partitioning only if measurements show that iterative scanning is insufficient.

Do not replace PostgreSQL with a dedicated vector database without evidence. The current design reduces operational complexity and remains appropriate for the MVP.

### 9. Build a reproducible capacity report

The repository contains valuable measurements, but some scripts, models, profiles, and sample sizes differ. The 300,000-passage lexical experiment also lacks a committed generating script.

Every performance report should include:

- Git revision.
- Date and hardware.
- Database, model, and container versions.
- Hardware profile and relevant configuration.
- Corpus documents, pages, passages, text distribution, tenants, and labels.
- Warm-up policy and number of samples.
- p50, p95, p99, throughput, errors, and degradations.
- CPU, peak memory, disk, index size, and database query plan.

Use synthetic data for controlled latency tests and real documents for relevance. Never infer quality from random vectors.

## P1: changes that strengthen the MVP after demo safety

### 10. Make ingestion bounded and resumable

The pipeline retains all parsed pages, chunks, and vectors before one persistence transaction. The configured 3,000-page limit is not enforced. A large document consumes memory proportional to its size, and a late transient failure repeats all work.

Improve the pipeline by:

- Enforcing page limits during parsing.
- Processing pages and chunks in bounded windows.
- Bulk-inserting pages, chunks, and embeddings.
- Recording checkpoints for parsing and embedding.
- Resuming safe completed work after transient failures.
- Measuring transaction duration, WAL volume, HNSW write amplification, and memory.

GPU inference is the practical answer to bulk embedding throughput. More CPU alone does not remove a sequential model bottleneck.

### 11. Reconcile files, rows, and jobs automatically

Upload crosses three durability boundaries: database row, stored file, and queued job. A failed move can leave a row without a file. A failed enqueue is intentionally logged and swallowed, leaving a pending document for manual repair. Re-uploading identical bytes follows the deduplicated path and may not enqueue the document.

Add a transactional outbox or periodic reconciler that:

- Detects rows with missing files.
- Detects pending documents without active jobs.
- Re-enqueues safe work automatically.
- Repairs a pending deduplicated document when the file is uploaded again.
- Reports stranded and repeatedly failing documents.

### 12. Separate archive metadata from access control

Current labels primarily represent authorization. Their slash-based hierarchy is a display convention, not a true taxonomy. Filename and description do not influence ingestion or retrieval.

Enterprise archives need descriptive metadata independent from ACLs:

- Document type.
- Source system.
- Author or owner.
- Effective, created, and expiration dates.
- Language.
- Retention class.
- External reference.
- Custom fields.

Use metadata for filters, facets, ranking, and lifecycle rules. Keep security labels focused on who may read a document.

### 13. Add multilingual lexical retrieval

BGE-M3 supports multilingual semantic retrieval, but PostgreSQL indexes and queries lexical text with the English configuration. Spanish and other enterprise corpora will receive incorrect stemming and stop-word behavior.

Adopt language-aware indexing or a multilingual-safe lexical strategy. Include Spanish and mixed-language questions in the evaluation set before promoting multilingual support.

### 14. Add production observability

Structured logs and `zenith diagnose` provide a solid base, but they do not provide time-series operations.

Track:

- API request p50, p95, and p99.
- Lexical, dense, exact, fusion, and reranking duration.
- Search degradation and timeout rates.
- Database pool wait and statement timeouts.
- Queue depth, oldest job age, retries, and failures.
- Ingestion pages and chunks per second.
- TEI request latency, CPU, and peak memory.
- PostgreSQL cache, HNSW and GIN sizes, WAL, and vacuum health.
- Storage usage and backup freshness.

Replace unconditional health with dependency-aware liveness and readiness. Keep expensive exact table counts out of frequent health probes.

### 15. Enforce quotas atomically

The 5,000-document quota counts only documents visible through the uploader's label context and uses a count-before-insert check. Hidden documents and concurrent uploads can exceed it. File and query throttles also live per API process, so adding Uvicorn workers multiplies effective limits.

Use authoritative tenant counters or database-enforced quotas. Move throttling to shared state before adding API replicas.

## P2: post-MVP scale boundary

### 16. Add object storage before multi-node deployment

Local content-addressed storage is correct for one host. It prevents independent API and worker placement across machines.

Keep local storage for the laptop MVP. Add an S3-compatible implementation behind the existing storage abstraction before horizontal deployment. Preserve content hashes, tenant separation, atomic publication, deletion, and backup semantics.

### 17. Define horizontal scaling explicitly

The current deployment runs one Uvicorn process and one ingestion worker. PostgreSQL stores application data, search indexes, and queue state. This is a reasonable single-node product, not a horizontally scalable cluster.

Before adding replicas:

- Share rate-limit and circuit-breaker state.
- Define API and worker concurrency per hardware profile.
- Separate interactive and bulk model capacity.
- Tune PostgreSQL for the measured HNSW and lexical workload.
- Add object storage.
- Test queue fairness, deployment restarts, and long-running ingestion.

### 18. Schedule off-site recovery

Backup and restore logic is unusually careful, but backups remain unscheduled and on-host. Schedule encrypted off-site backups, monitor freshness, measure restore time, and run periodic restore drills. Add coordinated snapshots or PostgreSQL point-in-time recovery if the target recovery objective requires them.

## Product presentation improvements

### Simplify the default search result

Raw lexical, dense, reranker, and RRF scores help technical debugging but can distract business viewers. Place them behind “Why this result?” Keep filename, page, labels, passage, elapsed time, and source opening prominent.

### Make degradation understandable

Translate internal exception names into user language. For example:

- “Semantic search is temporarily unavailable; showing keyword results.”
- “Advanced reranking is unavailable; showing fused search order.”

Keep the technical reason in diagnostics and logs.

### Improve recovery actions

Add retry actions for failed uploads and searches. Empty search results should suggest broader wording, removing a folder filter, or trying Chat.

### Expose useful migration feedback

Show upload rate and estimated time remaining. The calculation already exists in the frontend but the queue displays only percentage. Add folder selection only if it can be implemented and tested without risking the core demo.

## Recommended sequence

### Before the demo

1. Fix upload/poll separation and false completion.
2. Make OCR/layout status truthful.
3. Close the default-label access window.
4. Fix queue schema installation and readiness.
5. Prepare a realistic pre-indexed corpus and rehearsed queries.
6. Surface deduplication and simplify ranking diagnostics.
7. Verify backup, restore, and access-isolation talking points.

### Before making large-scale claims

1. Resolve lexical top-N scalability with full RLS and recall validation.
2. Benchmark multi-tenant, selective-label HNSW recall.
3. Commit reproducible benchmark tooling and provenance.
4. Define hardware-backed capacity tiers.
5. Publish measured limits and avoid linear hardware projections.

### After the MVP presentation

1. Make ingestion bounded, resumable, and self-repairing.
2. Add descriptive metadata and multilingual lexical retrieval.
3. Add metrics, readiness, alerts, and shared quotas.
4. Add object storage and worker replication.
5. Schedule off-site backups and measure recovery objectives.
6. Expand OCR and supported file formats.

## Definition of the best MVP

The best MVP is not the version with the most infrastructure. It is the version that makes a narrow promise and proves it:

- Import a realistic PDF archive safely.
- Show honest per-file progress and failures.
- Search exact terms and meaning within an interactive budget.
- Open every result on its original page.
- Produce grounded, cited answers and abstain when evidence is absent.
- Enforce access boundaries throughout upload, indexing, search, and viewing.
- State measured corpus and hardware limits clearly.
- Recover from ordinary failures without manual database repair.

Once Zenith proves these points in a controlled demonstration, its existing architecture and execution reports provide a credible path from MVP to a larger enterprise deployment.
