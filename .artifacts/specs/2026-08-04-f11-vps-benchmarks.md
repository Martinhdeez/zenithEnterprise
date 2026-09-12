# F11 results — the first numbers from hardware a customer would buy

Every latency this project has quoted came from a MacBook, one request at a time. This is
the first measurement on the target: **a 4-core, 7.6 GB VPS**, 19,500 chunks, the
shipped code, real TEI.

Three questions were open. All three are now answered, and one answer is a bug that was
shipping.

---

## 1. The bug: the reranker was configured never to run

`docker-compose.yml` started `tei-rerank` with no batch flags at all:

```yaml
command: ["--model-id", "BAAI/bge-reranker-v2-m3", "--auto-truncate"]
```

so TEI kept its own small defaults while `TeiReranker.plan_batches` built batches sized by
the **hardware profile** — 16 on `cpu`. Every rerank request came back:

```
ERROR rerank: batch size 14 > maximum allowed batch size 4
```

and `SearchService` did exactly what F7 designed it to do: caught the failure, fell back to
the fused order, returned a good answer, and marked it `degraded`. **Nothing crashed. No
test failed.** The only symptom would have been recall fifteen points below what F7
measured, on a customer's server, for as long as nobody looked.

This is the failure mode the whole project keeps warning about, and it reached `main`.

**Two fixes.** The compose file now pins both flags on both TEI services, reading the same
two variables — one setting must never be configurable in two places that can disagree. And
`tests/integration/test_compose_matches_profiles.py` asserts it: every TEI service declares
its batch limits, both read the same variables, and the defaults match `low-spec`, the
smallest supported machine.

**It nearly escaped a second time.** The first benchmark run reported `failures 0` for every
rerank configuration while 100% of rerank calls were failing, because the harness counted
exceptions and a degraded search raises none. It also reported reranking 100 candidates as
*faster* than reranking none — which is what prompted the check. The harness now prints
`DEGRADED n/n` loudly, and that is why the rest of these numbers can be trusted.

## 2. Reranking is not viable on this hardware. At any candidate count.

F7 left `rerank_candidates = 50` for the `cpu` profile as an explicit assumption, flagged
twice as the number most likely to be wrong. It is worse than wrong.

| Candidates | Median | Outcome |
|---|---|---|
| 0 | **1,152 ms** | fine |
| 10 | 6,386 ms | **timed out, 3/3 degraded** |
| 25 | 6,284 ms | **timed out, 3/3 degraded** |
| 50 | 6,237 ms | **timed out, 3/3 degraded** |
| 100 | 6,042 ms | **timed out, 3/3 degraded** |

The cross-encoder cannot rerank **ten** passages inside the 5-second interactive timeout on
four CPU cores. The candidate count is not the variable — the hardware is.

And with the `cpu` profile's own TEI settings (`--max-batch-tokens 8192`) the container does
not even start here:

```
Out of memory: Killed process (text-embeddings) anon-rss:4,308,228kB
```

OOM-killed during warm-up, before serving one request, on a box with 2.1 GB free.

### What this means for the profile table

**`low-spec` is confirmed correct by measurement.** It disables the reranker, and this is
the first evidence that the decision was right rather than merely cautious.

**`cpu` remains uncalibrated, and now has a documented floor.** This VPS cannot run it, so
nothing here tells us what `rerank_candidates` should be on the 8-core/16 GB machine `cpu`
targets. Changing the number on the strength of a machine that cannot run the profile at
all would repeat exactly the mistake F7 made. What has been added to `hardware.py` is the
measured minimum: **`cpu` requires more than 4 cores and more than 7.6 GB, or the reranker
container is OOM-killed at start-up.**

### A follow-up worth naming

A reranker that times out costs **5 seconds on every query** and returns nothing — the
degraded path is slower than having no reranker configured (6.2 s versus 1.2 s). The
timeout protects the answer but not the latency. A circuit breaker that stops calling a
reranker after repeated timeouts would turn a 6-second degraded query back into a
1-second one. Recorded, not built: it is a behaviour change and it belongs with its own
tests.

## 3. Concurrency: linear, no failures, no pool exhaustion

`low-spec` profile, reranker off, which is the shipped configuration for this machine.

| Concurrent users | Median query |
|---|---|
| 1 | **1,152 ms** |
| 2 | 1,383 ms |
| 5 | 2,877 ms |
| 10 | 5,159 ms |

Zero errors, zero degradations, no `statement_timeout`, no pool exhaustion at
`api_pool_size = 10`. Latency scales roughly linearly with load, which is what four cores
saturating looks like — the box runs out of CPU long before the code runs out of
connections.

**Ten simultaneous askers is the practical ceiling** at five seconds a query. For a design
partner of ten to thirty employees, where simultaneous asking is rare, that is comfortable.

## 4. Query during ingestion: F6's design holds

F5 measured ingestion holding TEI for 13.4 minutes per 100 dense pages. F6 responded with a
5-second timeout on `embed_query` and a documented fallback to the lexical half. **Neither
had ever been run together.**

| | Idle | While ingesting | |
|---|---|---|---|
| 1 user | 906 ms | **3,870 ms** | 4.3× slower |
| 5 users | 1,676 ms | **4,488 ms** | 2.7× slower |

**Zero degradations in either case.** The 5-second timeout never fired: queries got slower,
stayed correct, and kept both halves of the search. The design works, and the worst case a
user experiences during an upload is a four-and-a-half-second answer rather than a broken
one.

## 5. The latency budget F12 has to design against

| Situation | Retrieval |
|---|---|
| Typical, idle | **~1.2 s** |
| Busy (5 users) | ~2.9 s |
| During ingestion | **~4.5 s** |
| Worst measured (10 users) | ~5.2 s |

Generation is **not** included and was not measured here: an 8B model needs roughly 5 GB
and this box has 2. On the laptop, generation added ~9 s to a ~2 s retrieval, and it
dominates. A customer running generation on-premise needs a machine that can hold both, or
a remote endpoint.

**The design consequence for the frontend is direct: nothing may block on a complete
answer.** A one-second p50 that becomes four seconds mid-upload is exactly the range where
a spinner reads as a hang. Streaming is not a nicety here — it is the only reason the
product feels alive on the hardware it is sold for.

## 6. Method, and what these numbers are not

The corpus is **synthetic**: 19,500 generated chunks with random unit vectors, sized to
match the real corpus. HNSW cost depends on index size and dimensionality, and `ts_rank_cd`
on text length and term distribution — not on whether the text means anything.

**Nothing here reports recall, deliberately.** Quality is measured against real documents by
`eval.production` and `eval.grounding`. This measures speed, and mixing the two would let a
fast configuration look accurate.

Ingestion load is simulated by its effect on TEI — continuous embedding batches — rather
than by parsing PDFs. What ingestion does to an interactive query is occupy the embedder;
where the text came from does not change the contention.

## Reproducing

```bash
# on the VPS, with docker/docker-compose.bench.yml up
uv run python -m eval.vps_bench --rounds 3        # sweep + concurrency
uv run python -m eval.vps_contention --rounds 4   # query during ingestion
```
