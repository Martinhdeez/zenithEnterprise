# Vector quantisation — what the index costs per passage, and what shrinking it costs

**Status:** measured. No production change proposed here, and none made.
**Evidence:** `backend/eval/quantisation.json`, written by `python -m eval quantisation`.
Every figure below is from that file. Nothing here is derived by hand.

---

## The question

The HNSW index is the structural ceiling on how much corpus one installation can hold, and
HNSW wants to be resident. The live `ix_chunk_embeddings_hnsw` is **8,939.3 bytes per
vector** for `vector(1024)` at `m=16, ef_construction=64` over 13,549 passages. A freshly
built index of the same parameters is **8,188.4 bytes per vector** — the difference is
ingestion churn in the live index, not representation, and every ratio below is
fresh-against-fresh so the comparison is like for like.

pgvector 0.8.0 offers two smaller representations. This measures both against the fp32 index
they would replace, on the live corpus, with the real question set.

## Method, and the three things it fixes

An earlier throwaway probe reported a favourable binary result. It was wrong in three ways
and each one is corrected here.

1. **The queries are questions.** The probe sampled 50 stored chunk embeddings and used them
   as queries, which measures passage→passage similarity. Retrieval is question→passage,
   which is asymmetric. All 30 scorable questions from `questions.toml` are embedded through
   the same `TeiClient` the product uses. The correction is not cosmetic: at 13,549 vectors
   the probe reported recall@10 of 0.8820 for binary at rescore 20; with real questions it is
   **0.8133**.
2. **Both index recall and the product are measured.** Index recall is against exact cosine
   with `enable_indexscan = off` and `enable_bitmapscan = off` — never against another
   approximation. End to end runs the product's own `lexical`, `exact`, `candidates`, `fuse`,
   `hydrate` and cross-encoder with only the dense half swapped, scored by `eval/live.py`'s
   credit rule. The fp32 baseline reproduces `live-recall.json` exactly (headline 0.9000,
   recall@1 0.6667, 20 headline questions), which is what says the substitution is faithful.
3. **The trend across N is measured, not assumed.** 2,000 / 5,000 / 10,000 / 13,549 vectors,
   nested subsets of one deterministic ordering, so the sweep adds vectors rather than
   resampling. 300M cannot be built here; the slope can be.

Scratch objects live in one schema, `zenith_quantisation`, dropped in a `finally` and
verified gone at the end of every run. `chunk_embeddings` is read and never written. No
`SECURITY DEFINER` function is created; none is needed.

---

## The measured table

At the full corpus, 13,549 vectors, `ef_search = 100`, `m = 16`, `ef_construction = 64`.
Index recall is against exact cosine. Latency is fastest-of-5 per query, median across
questions.

| variant | bytes/vector | ratio | recall@10 | worst q@10 | recall@50 | index ms | rescore ms | build |
|---|---|---|---|---|---|---|---|---|
| `vector_cosine_ops` (fp32) | 8,188.4 | 1.00x | **1.0000** | 1.00 | **1.0000** | 22.64 | — | 5.36 s |
| `halfvec_cosine_ops` (fp16) | 2,729.9 | **3.00x** | **1.0000** | 1.00 | **1.0000** | 20.09 | — | 3.01 s |
| `bit_hamming_ops` + rescore 20 | 434.7 | 18.84x | 0.8133 | 0.20 | 0.3453 † | 1.24 | 1.03 | 1.29 s |
| `bit_hamming_ops` + rescore 50 | 434.7 | 18.84x | 0.9400 | 0.40 | 0.6280 | 1.27 | 1.16 | 1.29 s |
| `bit_hamming_ops` + rescore 100 | 434.7 | 18.84x | 0.9533 | 0.50 | 0.8153 | 1.32 | 1.32 | 1.29 s |
| `bit_hamming_ops` + rescore 200 | 434.7 | 18.84x | 0.9833 | 0.80 | 0.9287 | 1.42 | 1.64 | 1.29 s |
| `bit_hamming_ops` + rescore 400 | 434.7 | 18.84x | 0.9967 | 0.90 | 0.9800 | 1.62 | 2.25 | 1.29 s |

† A 20-wide rescore can return at most 20 of the exact top 50, so 0.40 is its arithmetic
ceiling at that depth. Reported rather than hidden.

The five rescore widths share one `bit` index, so one build time serves all five: the width
is a query-time choice.

**Three things this table says that the headline ratio does not.**

- **fp16 is free.** Not "cheap" — free. Recall 1.0000 at both depths, worst query 1.00, and
  it is *faster* than fp32 (20.09 ms against 22.64 ms) because a third of the index is a
  third of the pages to walk.
- **The mean hides a per-query collapse in binary.** Binary at rescore 100 averages 0.9533 at
  depth 10, and its **worst question recovers 5 of the 10 correct neighbours**. Averages are
  what a benchmark reports; the worst question is what a customer asks.
- **The rescore is a real cost that grows with the width.** It reads the fp32 vectors from
  the heap, so binary shrinks the index and not the table. At width 400 the rescore (2.25 ms)
  costs more than the Hamming walk (1.62 ms). pgvector also raises the effective `ef_search`
  to the query's `LIMIT`, so a width above `hnsw_ef_search` widens the graph walk too.

---

## The trend across N, and the finding that decides this

Gap to exact is `1 - recall`. The slope is least squares against `log10(N)` over the four
subset sizes, so it reads as *gap added per decade of corpus growth*.

| variant | gap@10 at 2k | 5k | 10k | 13.5k | per decade | per decade @50 |
|---|---|---|---|---|---|---|
| fp32 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | **+0.0000** | +0.0000 |
| fp16 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | **+0.0000** | +0.0002 |
| binary r20 | 0.2100 | 0.1633 | 0.1800 | 0.1867 | −0.0263 ‡ | −0.0017 ‡ |
| binary r50 | 0.0500 | 0.0533 | 0.0767 | 0.0600 | **+0.0221** | +0.0366 |
| binary r100 | 0.0167 | 0.0233 | 0.0433 | 0.0467 | **+0.0384** | +0.0500 |
| binary r200 | 0.0033 | 0.0133 | 0.0133 | 0.0167 | **+0.0147** | +0.0246 |
| binary r400 | 0.0000 | 0.0033 | 0.0033 | 0.0033 | +0.0039 | +0.0100 |

‡ Rescore 20 is non-monotone across the four sizes and its fit is noise around an already
unusable 0.81. It is not evidence that narrow binary improves with N.

**Binary's gap to exact widens with N, and fp16's does not.** At the rescore width that looks
best in the headline table — 100, where the throwaway probe reported 0.9920 — the gap
**triples across less than one decade**: 0.0167 at 2,000 vectors to 0.0467 at 13,549, a slope
of **+0.0384 per decade**. The worst-query figure widens harder still, 0.90 → 0.50 over the
same range. Rescore 200 widens at +0.0147 per decade; only rescore 400 is near flat at
+0.0039, and rescore 400 is the width whose rescore step already dominates its own latency.

Extrapolating the fitted line — an extrapolation, and labelled one in the report — rescore
100 reaches a **gap of 0.2137 at 300M passages**, i.e. index recall@10 around 0.79. fp16
reaches 0.0000.

The mechanism is the expected one and is why the trend should be believed rather than
dismissed as sampling noise: a 1024-bit Hamming space has a fixed number of distinguishable
neighbourhoods, so adding vectors adds near-collisions. Nothing about that stops at 13,549.

---

## End to end, through the real pipeline

At the full corpus, all 30 scorable questions, 20 of them counting towards the headline.

| variant | headline Recall@8 | Recall@8 (all) | Recall@1 (all) | search median ms |
|---|---|---|---|---|
| fp32 | 0.9000 | 0.9000 | 0.6667 | 833 |
| fp16 | 0.9000 | 0.9000 | 0.6667 | 864 |
| binary r20 | 0.9000 | 0.9000 | **0.6333** | 853 |
| binary r50 | 0.9000 | 0.9000 | 0.6667 | 833 |
| binary r100 | 0.9000 | 0.9000 | 0.6667 | 847 |
| binary r200 | 0.9000 | 0.9000 | 0.6667 | 872 |
| binary r400 | 0.9000 | 0.9000 | 0.6667 | 872 |

**At this corpus size the pipeline absorbs everything binary loses**, down to and including
rescore 20 with its index recall@10 of 0.8133. The lexical half, the identifier signal and
the cross-encoder together repair a dense half that has lost a fifth of its neighbours. The
only movement anywhere is Recall@1 at rescore 20 — one question of thirty.

**This is the number most likely to be misread, so read it carefully.** The headline is
scored over 20 questions, so it cannot resolve anything finer than 5 percentage points, and
"unchanged" here means "unchanged by less than one question". It is a ceiling effect at
13,549 vectors, measured at the one size where binary's index recall is still high. It is
evidence that *quantisation at this N* is survivable end to end. It is not evidence about
300M, and the section above says why: what feeds the pipeline degrades with N, and this
measurement was taken before that degradation has anywhere to go.

---

## Index build time

Single index build, `maintenance_work_mem = '512MB'` (the 64 MB default makes HNSW
construction crawl and would have reported a figure for the memory setting).

| vectors | fp32 | fp16 | binary | fp32 vec/s |
|---|---|---|---|---|
| 2,000 | 0.56 s | 0.41 s | 0.17 s | 3,603.8 |
| 5,000 | 1.60 s | 0.96 s | 0.47 s | 3,117.9 |
| 10,000 | 3.78 s | 2.31 s | 1.05 s | 2,645.1 |
| 13,549 | 5.36 s | 3.01 s | 1.29 s | 2,527.6 |

Build is roughly 1.8x faster in fp16 and 4.2x faster in binary — the same page-count argument
as the query side. **The build rate itself falls with N**: fp32 drops from 3,604 to 2,528
vectors per second across this range, so build cost is superlinear and a linear projection of
it would be dishonest. This is a scaling wall in its own right and nobody has measured it at
a size where it matters. It is listed under *what this does not establish*.

---

## Projection

Linear in the measured bytes per vector. HNSW's per-vector cost is set by `m` and the
dimension, neither of which changes with N, so the projection is sound for **size** — it says
nothing about recall or build time, both of which do change with N and are measured above.

**The passages-per-document assumption is stated, not buried.** This corpus measures
**322.6 passages per document** over 42 documents and 13,549 chunks, derived at run time and
recorded in the report. It is high because these are long legal and standards PDFs. A
realistic corporate mix of memos, contracts and slide decks is much shorter; **80** is quoted
alongside as a second assumption, and it is a guess, labelled as one, exactly as ADR 0005
labels `rerank_candidates`.

Index size in GiB, at 322.6 passages per document:

| documents | fp32 | fp16 | binary |
|---|---|---|---|
| 100,000 | 246.0 | 82.0 | 13.1 |
| 1,000,000 | **2,460.2** | **820.2** | **130.6** |
| 10,000,000 | 24,601.6 | 8,201.8 | 1,306.0 |

At 80 passages per document:

| documents | fp32 | fp16 | binary |
|---|---|---|---|
| 100,000 | 61.0 | 20.3 | 3.2 |
| 1,000,000 | 610.1 | 203.4 | 32.4 |
| 10,000,000 | 6,100.8 | 2,033.9 | 323.9 |

By passages, which is the unit that is actually indexed:

| passages | fp32 | fp16 | binary |
|---|---|---|---|
| 1M | 7.6 | 2.5 | 0.4 |
| 10M | 76.3 | 25.4 | 4.0 |
| 50M | 381.3 | 127.1 | 20.2 |
| 100M | 762.6 | 254.2 | 40.5 |
| 300M | 2,287.8 | 762.7 | 121.5 |

The headline: **a million documents of this corpus's shape is 2.4 TiB of fp32 index, 820 GiB
in fp16, and 131 GiB in binary.** The fp32 figure is not resident on any machine this product
is sold onto. The fp16 figure is not either, at that document count — but it moves the wall
out by a factor of three for nothing, and at 100,000 documents it is the difference between
246 GiB and 82 GiB, which is the difference between a server that exists and one that does
not.

---

## Recommendation

**Adopt `halfvec` now. Block binary pending a test at a scale this run could not reach.**

`halfvec` costs nothing that this measurement can detect. Index recall 1.0000 at both depths
and every subset size, worst query 1.0000, a flat trend (+0.0000 per decade at depth 10,
+0.0002 at depth 50), identical end-to-end recall, faster queries and faster builds, for a
3.00x reduction. There is no trade to weigh. The only reason it is not already the storage
type is that nobody had measured it.

**Binary is not recommended, and the small-N number is the reason to be careful rather than
the reason to adopt.** At 13,549 vectors binary at rescore 100 looks nearly free and the
pipeline absorbs it completely. Both of those facts are properties of 13,549 vectors. The gap
triples across less than one decade of N, the worst question falls from 0.90 to 0.50 over the
same range, and the extrapolated gap at 300M passages is 0.21. Recommending binary on the
13.5k figure would be F7's mistake — tuning on the evidence available rather than the
evidence needed — and ADR 0005 already records what that costs in the other direction.

If binary is wanted anyway, rescore 400 is the only width with a near-flat trend
(+0.0039 per decade at depth 10), and it is also the width whose rescore step costs more than
its index scan and which raises the effective `ef_search` to 400. It buys the 18.84x index
saving back with heap I/O that quantisation did not shrink. That trade might still be worth
making at 300M. It cannot be decided from here.

### The one measurement that would reverse this

**Build the binary index at 1M–10M passages of comparable text and measure index recall@10
and end-to-end Recall@8 at rescore 100 and 200.** If the gap at depth 10 at 10M passages is
at or below the value the flat hypothesis predicts — that is, if it has not grown past
roughly 0.05 — then the widening measured here is a small-N artefact, binary becomes the
recommendation, and the projection above says it fits 10M documents of this corpus's shape in
1.3 TiB. If instead the gap is near the fitted +0.0384 per decade, arriving around 0.157 at
10M, binary is finished as an option at any scale this product cares about and the question
becomes whether fp16 plus a bigger machine, or a different index family, carries the corpus.

That measurement needs a corpus this installation does not have. It does not need new
production code — this harness runs it unchanged by passing larger `--subsets`.

### If binary is ever adopted

It would need a migration and a storage-type change on `chunk_embeddings`, which is
production work and deliberately not done here. Two things would have to be settled first and
neither is a documentation decision: the dense half's rescore would have to become part of
the query in `search.py` rather than two statements, and every recall figure in `eval/`
was measured against `vector_cosine_ops`, so the same argument migration 0022 makes for
keeping the GIN index applies — a retrieval change on a customer installation has to be
revertible by a restart.

---

## What this does not establish

- **Nothing here tests a corpus in a different language mix.** The question set and the
  corpus are the ones in this repository. Whether fp16 and binary preserve recall on a corpus
  that is predominantly Spanish, or mixed at a different ratio, is untested. This matters more
  than it looks: migration 0022 already flags that the BM25 tokenizer does not fold accents,
  so the lexical half — the half that repaired binary's losses in the end-to-end table above
  — is itself language-sensitive.
- **Nothing here tests index build time at the scale that matters.** The build rate falls
  measurably across 2k→13.5k (3,604 → 2,528 vectors per second for fp32) and is therefore
  superlinear. Extrapolating it would be inventing a number. Build time at 300M passages is a
  scaling wall of its own and it is unmeasured.
- **The latencies are not comparable to `ef-search.json` or `latency.json`.** They were taken
  against scratch copies that carry no RLS policy and no join to `chunks`, while the scratch
  schema competed with the production corpus for 128 MB of `shared_buffers`. They are
  comparable to each other, which is what the sweep is for.
- **The end-to-end result is at one corpus size only.** A subset that excluded the passage a
  question is credited for would score a missing document as a quantisation failure, which is
  a different measurement, so end to end runs at the full corpus and nowhere else.
- **The extrapolated gaps assume the trend stays log-linear.** That assumption is exactly what
  a test at scale exists to check, and it is why the recommendation blocks binary rather than
  rejecting it.
