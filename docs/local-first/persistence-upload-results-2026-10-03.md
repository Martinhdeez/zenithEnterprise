# Atomic persistence and fresh upload-to-ready measurements

Extraction provenance: this report preserves the measured integrated-fork experiment.
The standalone upstream slice starts from `33b48812c95150348c52d2519159780252c92db2`;
its independent checks are reported in the PR. Historical benchmark and quality
figures have not been rerun on this extraction. Original implementation and
evaluation harness: [frozen fork source](https://github.com/Kripta-Studios/zenithEnterprise/tree/e49fd78e225c4176d4e439b502fa6d5d61f85127).

Subsequent work isolates and optimizes the non-persistence embedding bottleneck;
see [the separate paired GPU ingestion report](https://github.com/Kripta-Studios/zenithEnterprise/blob/e49fd78e225c4176d4e439b502fa6d5d61f85127/docs/local-first/ingestion-phase-results-2026-10-03.md).
The figures below remain the original persistence experiment, not its new comparator.

For the same 938 public chunks and full 1024-dimensional vectors, persistence falls
from **111.082928 seconds to 26.826970 seconds** at the median: **4.140718x faster,
75.849601% less wall time**. These are three alternating measured replacement pairs,
after discarding one warmup pair, against frozen fork main
`d12620a8cd973e5f2d1012900a63f1726b225e05` on the same database and hardware.
The candidate code head is `989478a340ba173673ed181ae6755842eca71357`.

## Mechanism and integrity

The original ordered ORM/default-return path emits 938 separate chunk INSERTs.
Assigning UUIDs alone did not remove this cost. A subsequent DBAPI executemany attempt
reduced client calls without reducing the 938 server INSERT statements; it did not
produce a useful speed improvement. The final change uses explicit UUIDs and actual
multi-row `INSERT ... VALUES` statements, bounded to 128 chunks: **eight statements**.
This distinction follows the [SQLAlchemy bulk DML contracts](https://docs.sqlalchemy.org/en/20/orm/queryguide/dml.html).

| Measured SQL phase median | Original | Multi-row candidate |
|---|---:|---:|
| Chunk INSERT seconds | 94.112796 | 6.484627 |
| Full-vector INSERT seconds | 11.068482 | 10.393478 |
| Complete atomic persistence seconds | 111.082928 | 26.826970 |

SQL phase medians are not additive: commit, deletion, Python work and different
median trials account for the remaining time. Vector writes retain the existing
64-item batches, full dimensions and projected index. Existing server defaults,
label/tsvector triggers, row-level security, metadata and classification transitions
remain active. Pages autoflush before chunk inserts. All writes and deletions remain
inside one transaction, including a retry that replaces an existing index.

Application-role checks verify all 938 texts, original coordinates, labels and exact
float32 vectors. Final regression checks: **22 passed in 366.40 seconds**, wrapper
394.25 seconds, exit 0, at the candidate head. Late invalid-vector failures roll back
the old complete index; failed first ingestion leaves no partial index. Tenant/label
isolation and retry alignment are tested. No schema or dependency changes are required.

The paired benchmark itself passed: **1 passed**, wrapper 758.562 seconds, exit 0.
ParadeDB 0.15.26/PostgreSQL 17 is capped at two CPUs and 768 MiB, with
`max_locks_per_transaction=2560`. It measures replacement after initial insertion,
not the first-ever upload. The laptop is shared; three pairs establish a measured
local gain, not a production latency guarantee or a reliable p95.

Fixture SHA-256: `e758fa43ba86e5279bef214a27fc1fd107d5b0bdc55f50bcc444ef066c02f743`.
Frozen original pipeline SHA-256:
`982746cf88738367c717419edee93beb3608363a2fb8496571f2ee33c02a6d43`.
Full code tree/archive/image and log hashes are in the aggregate JSON.

## New complete upload baseline with real GPU embeddings

One fresh public text file, **844643 bytes / 938 chunks**, traversed actual streaming
upload, queue, one worker, parsing/chunking, real TEI GPU embeddings, persistence,
classification and ready status in an isolated database with the same resource cap.
Code head: `33011ec1505002a5b9b04df8b11ace3d1282dde0`.

| Measurement | Seconds |
|---|---:|
| HTTP 201 acknowledgement from experiment start | 5.272500 |
| Ingestion work | 129.090405 |
| Persistence within ingestion | 35.740958 |
| Remaining ingestion work | 93.349447 |
| Ready settled from experiment start | 134.798445 |
| Ready observed by polling | 136.012936 |

The acknowledgement timer includes setup/login/queue initialization; it is not a pure
HTTP upload latency. TEI cold model readiness took 51.297 seconds before the upload
timer and is excluded. The test passed: **1 passed in 259.02 seconds**, wrapper
286.766 seconds, exit 0. Corpus SHA-256:
`b1b7d0a5fc83e081b5e7bf141af74d1e41e47c635807f68adcf5f4ca9715263d`.

TEI 1.9.4, build `e80ef225ed0e6cb1717ce632a6a84b6cf211bb67`, serves cached BAAI/bge-m3
revision `5617a9f61b028005a4858fdac845db406aefb181`, float16/CLS on RTX 5070 Ti 12 GiB.
Observed service bounds: 1024 input tokens, 1024 batch tokens, two client items,
eight concurrent requests, three tokenization workers and auto-truncation enabled.
Existing memory-safe client splitting remains in use. This establishes ingestion
completion; it does not measure answer quality or prove full-token semantic coverage.
Other GPU activity was present; no unrelated training was paused in this task.

Historical ready times used a different multi-file layout. Comparing those numbers
against this monolith would conflate upload scheduling with persistence. Only the
paired persistence experiment above supports the reported speedup.

## Next measured bottleneck

The new complete run spends **93.349447 seconds, 72.31% of ingestion**, outside
persistence. Parsing, embeddings, status changes and classification are included;
there is no separate embedding timer, so attributing all of it to GPU inference
would be unsupported. Instrument those phases first and capture tokens/items per
request, queue wait, retries and service time before increasing TEI concurrency.
Within persistence the remaining vector INSERT median, **10.393478 seconds**, exceeds
the chunk INSERT median, **6.484627 seconds**. Profile vector serialization, database
execution and index work before changing the existing memory-safe batch limit.

Preserve the transaction and RLS rollback gates in every further experiment. Measure
multiple fresh uploads at fixed file counts and document sizes to establish ready
latency distribution; do not reuse the three replacement pairs as a fresh-upload SLA.
Docker and WSL were shut down after the measurements; retained volumes and cached
models allow future authorized experiments without another download.

[Source-free measurements and artifact bindings](agent-performance-results-2026-10-03.json),
[paired trial CSV](agent-performance-results-2026-10-03.csv),
[MCP workflow](https://github.com/Kripta-Studios/zenithEnterprise/blob/e49fd78e225c4176d4e439b502fa6d5d61f85127/docs/local-first/mcp-agent-workflow-2026-10-03.md).
