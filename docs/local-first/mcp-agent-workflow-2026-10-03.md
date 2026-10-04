# Bounded local MCP workflows for agents

Extraction provenance: this report preserves the measured integrated-fork experiment.
The standalone upstream slice starts from `33b48812c95150348c52d2519159780252c92db2`;
its independent checks are reported in the PR. Historical benchmark and quality
figures have not been rerun on this extraction. Original implementation and
evaluation harness: [frozen fork source](https://github.com/Kripta-Studios/zenithEnterprise/tree/e49fd78e225c4176d4e439b502fa6d5d61f85127).

The existing MCP SDK **2.2.0**, stdio transport and authenticated Keycloak intranet
transport share nine read-only tools. No new agent framework, database, upload route,
credential minting or permission cache is introduced. Native capabilities resource:
`zenith://capabilities`; native prompt: `zenith_cited_answer`.

| Tool | Agent use | Bound |
|---|---|---|
| `zenith_capabilities` | Discover workflow, privacy and limits | Authenticated static metadata |
| `zenith_list_labels` | Discover permitted label names | 50 per page; only fresh grants |
| `zenith_list_documents` | Find documents by filename/state/label | 50 per opaque keyset page |
| `zenith_get_document` | Inspect one ingestion state | Existing REST metadata policy |
| `zenith_get_documents` | Inspect a batch | 32 IDs, ordered results |
| `zenith_wait_documents` | Wait for ready/failed states | 32 IDs, 20 seconds maximum |
| `zenith_search` | Retrieve authorized evidence | 8 hits, 2000 characters per excerpt |
| `zenith_read_source` | Read original citation coordinates | 8000 text characters |
| `zenith_read_sources` | Read/revalidate several sources together | 8 IDs, 16000 text characters total |

Batch source reads replace repeated authorization/SQL work with one fresh profile and
one RLS-protected source/document join. Results retain requested order. Unavailable IDs
mean missing, inaccessible, not ready or out of range; the response does not distinguish
hidden rows from nonexistent ones. Repeated IDs and oversized batches are refused.
Source fragments include a continuation offset, source/document identity, original
page/character ranges and bounded boxes. Metadata never includes parser diagnostics.

Use document discovery to select scope, wait for ingestion readiness when appropriate,
search, then read sources in one batch. Generate with the chosen local model and cite
original sources. Re-read cited sources after generation: a retained source ID is not a
permission grant. The reference client now batches both its initial evidence reads and
its final citation rechecks. Source text remains untrusted data.

This standalone extraction calls upstream main's existing local TEI search directly;
it includes no external-provider factory or Jev configuration. The integrated fork
explicitly pins TEI when its separate optional REST provider setting chooses Jev. The network transport uses
the same reads and fresh Keycloak/user binding rather than a global administrator key.
Every operation checks current user authority and session version. Waits reauthorize
each snapshot and again at the final disclosure. Tool work and queueing retain the
existing two-operation semaphore, 30-second budget, cancellation and sanitized errors.

The trusted host uploader remains the path for binary files and uses existing streaming
REST upload, deduplication and readiness handling. Agents receive status/discovery tools,
not filesystem or arbitrary-URL access. A cloud model connected to an otherwise local
MCP endpoint would still change the processing boundary; the selected reference workflow
uses a local model.

## Actual checks and measured batch effect

Real SDK/application-role PostgreSQL tests: **54 passed, 3 optional tests skipped**, pytest
281.29 seconds, wrapper 307.532 seconds, exit 0, code head `f79dc6e`.
Coverage includes permission/label/session revocation, missing/hidden equivalence,
source ranges, batch bounds, discovery/pagination, readiness/timeout, stdio startup,
authenticated network reads, native resource/prompt registration and explicit local
provider selection when the global REST setting chooses Jev.

Benchmark: **1 passed**, wrapper 245.782 seconds, exit 0, code head `1da2704`.
Eight sources, 15 alternating measured pairs after one discarded warmup pair:

| Same evidence and fresh authority | Eight single calls | One batch call |
|---|---:|---:|
| Median total wall seconds | 3.533691 | 0.512609 |
| SQL operations | 104 | 13 |

The batch is approximately **6.89x faster**, with **87.5% fewer SQL operations**.
Every measured pair verifies identical content and order. This is SDK in-process
transport plus a real database on the shared laptop, not a network percentile/SLA.
Database startup/provisioning is excluded from the request timings. The reduction comes
from batching, without cached permissions. Raw local measurements and exact checks are
retained under ignored `.local-evidence/mcp-benchmark/`.
Source-free trials and exact code/archive/log bindings are also published in
[performance aggregates](agent-performance-results-2026-10-03.json) and
[trial CSV](agent-performance-results-2026-10-03.csv).

Next operational validation: run the same bounded workflow with the approved intranet
client, realistic document counts and concurrent searches/uploads; measure authentication,
database and inference separately. The current source benchmark does not measure model
generation or demonstrate semantic citation support.

Implementation follows the installed SDK and
[official Python SDK documentation](https://github.com/modelcontextprotocol/python-sdk).
Authentication and data isolation retain the existing Zenith decision records and RLS.
