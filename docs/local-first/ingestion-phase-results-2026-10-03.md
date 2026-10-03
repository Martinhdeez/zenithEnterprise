# Local GPU ingestion: measured embedding bottleneck and bounded batch profile

Extraction provenance: this report preserves the measured integrated-fork experiment.
The standalone upstream slice starts from `33b48812c95150348c52d2519159780252c92db2`;
its independent checks are reported in the PR. Historical benchmark and quality
figures have not been rerun on this extraction. Original implementation and
evaluation harness: [frozen fork source](https://github.com/Kripta-Studios/zenithEnterprise/tree/e49fd78e225c4176d4e439b502fa6d5d61f85127).

The non-persistence bottleneck is embedding inference and request overhead. A bounded
4096-token/eight-item profile reduces embedding time **53.3764%** and observed
upload-to-ready time **36.5550%** against the previous 1024-token/two-item recipe in
three alternating fresh-upload pairs. It uses the existing sequential TEI client,
cached BGE-M3 weights and atomic persistence implementation. No provider calls,
model downloads, concurrency increase or document-source changes were required.

## Diagnose before tuning

The earlier fresh-upload result was 129.090405 seconds of ingestion, including
35.740958 seconds of persistence and **93.349447 seconds outside persistence**.
That observation motivated this task; it is not the controlled comparator for the
new result. Background GPU occupancy and database state vary on this shared laptop.

An instrumented fresh upload on code `2cdb82c` measured 96.044023 seconds of ingestion:

| Measured phase | Seconds |
|---|---:|
| Parsing | 0.006065 |
| Routing/chunking | 0.023629 |
| Four status writes | 0.428444 |
| Embedding | 69.329471 |
| Persistence | 26.010567 |
| Classification hook | 0.072219 |
| Total outside persistence | 70.033457 |

Embedding accounts for approximately 99% of that non-persistence time. Its 469
sequential successful HTTP calls consumed 67.995754 seconds of client HTTP wall
time, with 59.607 seconds summed server total time. TEI tokenization/queue/inference
headers report averages over batch inputs, so their sums are not disjoint wall-time
phases. See the pinned [header construction](https://raw.githubusercontent.com/huggingface/text-embeddings-inference/e80ef225ed0e6cb1717ce632a6a84b6cf211bb67/router/src/http/server.rs).
Stage timers around the actual operations are the primary evidence.

## Paired result

The completed eight-run test used one warmup pair, then three alternating measured
pairs: candidate/baseline, baseline/candidate, candidate/baseline. Each upload used
a fresh tenant and document, returned HTTP 201, and exercised streaming upload,
the real Procrastinate queue, parsing, chunking, GPU embedding, atomic persistence
and the ready transition. The same bounded database was retained across trials;
it was not reset to empty. Both embedding services were resident during the paired
measurement. No unrelated training was paused.

| Median of three measured uploads per arm | 1024 tokens / 2 items | 4096 tokens / 8 items | Reduction |
|---|---:|---:|---:|
| Embedding seconds | 62.579710 | 29.176888 | 53.3764% |
| Outside persistence seconds | 63.383079 | 29.862768 | 52.8853% |
| Complete ingestion seconds | 89.790303 | 55.798642 | 37.8567% |
| Persistence seconds | 25.101025 | 26.985834 | -7.5089% |
| Observed ready seconds | 93.420162 | 59.270400 | 36.5550% |
| Embedding HTTP calls per upload | 469 | 118 | 74.8401% |

Embedding is **2.144838x faster**. Persistence was slightly slower in this run;
this task makes no persistence improvement claim. Growing shared indexes and host
load remain sources of variability. Three pairs establish a useful local result,
not a production p95, confidence interval or concurrent-load SLA. Phase medians are
not additive because they can come from different trials.

The ready clock includes experiment setup, authentication, queue work and polling.
Cold model startup is excluded. The first newly created candidate container took
149.997 seconds to become ready; the existing baseline took 57.591 seconds in the
diagnostic and 46.475 seconds after its paired-run restart. No cold-start improvement
is claimed. The persistence wrapper includes writing local vector/source proof
files before calling the unchanged persistence method, in both arms.

## Data, hardware and integrity

The public Spanish SQAC text monolith is **844643 bytes**, **938 chunks** and
**228816 processed embedding tokens in every trial**. Corpus SHA-256:
`b1b7d0a5fc83e081b5e7bf141af74d1e41e47c635807f68adcf5f4ca9715263d`.
This benchmark supplies an explicit Finance label, so the classification hook does
not measure automatic LLM filing. It does not qualify PDF/OCR, generated answers,
private corpora, dedup-hit latency or concurrent search/reranking under ingestion.

All eight uploads passed checks through the application role under RLS:

- Exact original source hashes, page/character coordinates and order for all chunks.
- Expected label on every chunk; all 938 full 1024-dimensional vectors persisted.
- Finite vectors, unit norm within `1e-4`, and exact float32 equality between returned
  and stored vectors for each upload.
- Cosine similarity to the baseline warmup of at least **0.9999095798** across all
  returned vectors. Batch shape changes float16 numerical output; cross-arm bitwise
  equality is not claimed. Individual differences are retained in the JSON report.
- Identical token counts, 938 returned items and successful responses for every call.

The host was an RTX 5070 Ti Laptop GPU (12 GiB) with 32 GB RAM. Both model containers
were capped at four CPUs and 4 GiB RAM, using float16, CLS pooling and admission
capacity eight. The database used ParadeDB 0.15.26 / PostgreSQL 17 with two CPUs,
768 MiB and `max_locks_per_transaction=2560`. The resource monitor recorded 124
samples, a minimum 3226.38 MiB free host memory, and no memory-guard abort. Monitoring
includes trailing shutdown activity, so its GPU minimum is not a resident-pair metric.

Model: `BAAI/bge-m3`, revision `5617a9f61b028005a4858fdac845db406aefb181`.
TEI 1.9.4 source: `e80ef225ed0e6cb1717ce632a6a84b6cf211bb67`.
Image: `ghcr.io/huggingface/text-embeddings-inference:120-1.9@sha256:bd8e5b1954146f7fe8590b64b959bc194433c6c38c036592a84d736841ca9400`.
The model and precision did not change. Equal token counts establish no truncation
loss for this corpus, not for arbitrary future oversized inputs. TEI's
[batch limits](https://huggingface.co/docs/text-embeddings-inference/cli_arguments)
bound admitted tokens and items separately.

## Activation and rollback

The new optional `gpu-local` hardware profile uses 4096 batch tokens, eight items,
one ingestion worker, and the existing CPU retrieval bounds (100 HNSW search effort,
eight reranking candidates). Existing `cpu`, `low-spec` and `gpu` values remain
unchanged. The profile remains marked unmeasured for whole-deployment qualification:
this report qualifies local embedding ingestion only.

From the project root, on an existing configured RTX 50-series installation:

```bash
docker compose --env-file .env --env-file docker/ingestion-gpu-local.env \
  -f docker/docker-compose.yml -f docker/docker-compose.gpu.yml \
  -f docker/docker-compose.blackwell.yml up -d tei-embed tei-rerank api worker
```

Keep the private installation `.env` first and the public preset second. The preset
reuses the existing GPU device and Blackwell image overrides. Both API and worker
receive the profile explicitly: an additional CLI interpolation file alone does
not override a service's `env_file`. An actual Compose-render regression test covers
that distinction and verifies matching limits on both TEI services, float16 and
admission capacity eight without starting Docker. CPU TEI 1.8 is unaffected.
Concurrent residency and performance of a production reranker are not qualified by
this two-embedder experiment; measure that deployment before treating it as an SLA.

Roll back by removing the public preset and restoring the installation's previous
matching client/server batch settings, then recreating these services. No schema
migration, re-index or source change is required. This task does not start the full
production deployment; Docker Desktop and WSL were shut down after the experiment.

## Evidence and checks

The paired run is bound to code head
`99c24598cc7816d376f42f7815149de952002390`, tree
`676401afa4b8c663195cb42e0b8628fdf0a169a7`, source archive SHA-256
`9914c7c3314b806a0caf27ce590829fb8fefd7a7c70599e73fb9d136a37dc2fd`.
The completed real GPU test reports **8 passed in 785.89 seconds**, wrapper
**807.934 seconds**, exit zero. Log SHA-256:
`7bc197fef872c8da2c384e9ac459bcf566a00c5120956fd0422b0f2d68eb6c13`.
Subsequent edits add strict NumPy annotations without changing arithmetic, pin
deployment settings and test their Compose wiring; no later real GPU run is claimed.

Focused host checks: **24 passed in 10.25 seconds**, strict Pyright **zero errors**.
The host Compose test initially exposed Windows decoding of Unicode paths; explicit
UTF-8 fixed it. Ruff lint/format and diff whitespace checks are required before
publication. Full backend/frontend CI is the merge gate; a full local `make check`
is not claimed because Docker was shut down as requested after GPU operations.
No dependency lock changes, external processing or paid calls are involved.

The first full Linux run, [37152257042](https://github.com/Kripta-Studios/zenithEnterprise/actions/runs/37152257042),
reported 1085 passing, 29 skipped and two failing backend tests; frontend passed.
The Compose guard now copies the unchanged overlays into a temporary installation
with its own placeholder `.env`, resolves service environment files normally, and
includes stderr on failure. This avoids depending on loader handling of absent
private environment files. Its updated six tests pass locally with Docker off.
An existing candidate-relevance test also assumed full ANN recall for tied synthetic
vectors in a shared growing database. That test now calls the real dense SQL using
an exact scan under the same application-role RLS, retaining every assertion and
the real lexical/reranking path. Vector index planning and isolation acceptance
tests remain unchanged. These test corrections do not change production retrieval
or any measured ingestion operation; subsequent full CI must pass before integration.

Read the generated [source-free JSON](ingestion-phase-results-2026-10-03.json) and
[per-trial CSV](ingestion-phase-results-2026-10-03.csv). Raw public uploads, vectors,
telemetry, resource samples and diagnostic/tuning runs are retained locally under
the worktree's ignored `.local-evidence/`. The exporter refuses incomplete or failed
checks and records artifact hashes. These measurements do not alter the earlier
Spanish Jev/BGE quality conclusions or any upstream PR.

## Next measured bottleneck

Embedding now takes **29.176888 seconds**, while persistence takes **26.985834
seconds**. Profile vector serialization, bounded INSERT execution, index/trigger
work and commit separately in this new fresh-upload workload. The old 10.393-second
vector INSERT result belongs to a different persistence experiment and must not
be substituted for a current measurement. Keep one atomic replacement transaction,
full vectors, rollback/idempotency and application-role isolation. Test further
changes with alternating uploads and fresh database-size/resource controls before
changing batch sizes or adopting parallel workers. Broader PDF, automatic filing,
reranker contention and upload-size acceptance remain separate measurements.
