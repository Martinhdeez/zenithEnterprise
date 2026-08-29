# Vector quantisation — what the index costs per passage, and what shrinking it costs

**Status:** measured. No production change proposed here, and none made.
**Evidence:** `backend/eval/quantisation.json`, written by `python -m eval quantisation`.
Every figure below is from that file. Nothing here is derived by hand.

> **Corrected 2026-08-29.** The first version of this document reported index recall of
> **1.0000** for fp32 and fp16 at every subset size. Those numbers were void. `_truth()`
> turned the planner's index scans off with `SET LOCAL`, which lasts to the end of the
> *transaction*, and the arms ran in the same transaction — so every arm was answered by a
> sequential scan and the fp32 and fp16 rows compared exact retrieval against itself. The
> harness now restores the settings, refuses a sequential scan for the arms, and records the
> query plan of every measurement; `plan_uses_index` and `truth_is_sequential` are in the
> report so this is checkable rather than asserted. **The end-to-end table did not move at
> all**, because it always ran on its own connection. The binary rows moved by one to three
> points at depth 10 and did not change any verdict. What changed materially is that HNSW's
> own cost at `ef_search = 100` is now visible, and it is not zero.

---

## The question

The HNSW index is the structural ceiling on how much corpus one installation can hold, and
HNSW wants to be resident. A freshly built `vector(1024)` index at `m=16, ef_construction=64`
over these 13,549 passages is **8,188.4 bytes per vector**, and that is the fp32 baseline
every ratio below is taken against. The live index is no longer that: migration 0025 moved it
to `halfvec`, and `ix_chunk_embeddings_hnsw_half` now measures **2,729.9 bytes per vector**,
which is the `fp16` row of the table below and not the fp32 one. Every ratio below is
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
   **0.8000**.
2. **Both index recall and the product are measured.** Index recall is against exact cosine
   with `enable_indexscan = off` and `enable_bitmapscan = off` — never against another
   approximation. Those two settings are then *restored*, and `enable_seqscan` turned off, so
   that each arm is answered by its own index and not by a second exact scan; the plan of
   every measurement is recorded in the report and each arm carries `plan_uses_index`. This
   is the correction described at the top of this document, and the second half of it is not
   redundant: at 13,549 rows the planner sometimes prefers a sequential scan even when
   offered the index, and an arm that opts out of its index measures nothing. End to end runs
   the product's own `lexical`, `exact`, `candidates`, `fuse`,
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

At the full corpus, 13,549 vectors, `m = 16`, `ef_construction = 64`. Index recall is against
exact cosine, measured through each arm's own HNSW index. Latency is fastest-of-5 per query,
median across questions.

| variant | bytes/vector | ratio | ef_search | recall@10 | worst q@10 | recall@50 | index ms | rescore ms | build |
|---|---|---|---|---|---|---|---|---|---|
| `vector_cosine_ops` (fp32) | 8,188.4 | 1.00x | 100 | 0.9600 | 0.50 | 0.9680 | 0.86 | — | 4.87 s |
| `halfvec_cosine_ops` (fp16) | 2,729.9 | **3.00x** | 100 | 0.9600 | 0.50 | 0.9660 | 0.77 | — | 3.00 s |
| `bit_hamming_ops` + rescore 20 | 434.7 | 18.84x | 100 | 0.8000 | 0.10 | 0.3360 † | 0.61 | 0.87 | 1.32 s |
| `bit_hamming_ops` + rescore 50 | 434.7 | 18.84x | 100 | 0.9167 | 0.30 | 0.6267 | 0.63 | 0.98 | 1.32 s |
| `bit_hamming_ops` + rescore 100 | 434.7 | 18.84x | 100 | 0.9333 | 0.40 | 0.8233 | 0.68 | 1.05 | 1.32 s |
| `bit_hamming_ops` + rescore 200 | 434.7 | 18.84x | 200 | 0.9500 | 0.50 | 0.9193 | 0.92 | 1.56 | 1.32 s |
| `bit_hamming_ops` + rescore 400 | 434.7 | 18.84x | 400 | 0.9800 | 0.60 | 0.9753 | 1.44 | 2.54 | 1.32 s |

† A 20-wide rescore can return at most 20 of the exact top 50, so 0.40 is its arithmetic
ceiling at that depth. Reported rather than hidden.

The five rescore widths share one `bit` index, so one build time serves all five: the width
is a query-time choice.

**Four things this table says that the headline ratio does not.**

- **Four points of the gap to exact are HNSW's, not the representation's.** fp32 — the
  uncompressed vector, read through its own index — recalls 0.9600 at depth 10, not 1.0000,
  and its worst question recovers 5 of 10. That is the graph at `ef_search = 100`, and it is
  the floor every other row in this table sits on. It was invisible in the first version of
  this document, which measured every arm by sequential scan and therefore reported the graph
  as costless.
- **fp16 costs nothing that this measurement can separate from fp32.** Identical recall@10
  (0.9600 against 0.9600), 0.9660 against 0.9680 at depth 50, the same worst question (0.50),
  and it is faster (0.77 ms against 0.86 ms) because a third of the index is a third of the
  pages to walk. The claim is not "fp16 loses nothing"; it is "fp16 and fp32 are the same
  index at three times the density", which is the claim that matters for adopting it.
- **The mean hides a per-query collapse in binary.** Binary at rescore 100 averages 0.9333 at
  depth 10, and its **worst question recovers 4 of the 10 correct neighbours** against fp32's
  5. Averages are what a benchmark reports; the worst question is what a customer asks.
- **The rescore is a real cost that grows with the width, and so is the walk it needs.** The
  rescore reads the fp32 vectors from the heap, so binary shrinks the index and not the
  table; at width 400 it costs more (2.54 ms) than the Hamming walk (1.44 ms). And pgvector
  does **not** raise the effective `ef_search` to the query's `LIMIT` — an HNSW scan returns
  at most `ef_search` rows, measured: at `ef_search = 100`, `LIMIT 200` and `LIMIT 400` both
  return 100. A rescore wider than the profile's `ef_search` is therefore unreachable unless
  the walk is widened to match, which is why the two widest arms run at `ef_search` 200 and
  400 and why their rows are not directly comparable to the four at 100.

---

## The trend across N, and the finding that decides this

Gap to exact is `1 - recall`. The slope is least squares against `log10(N)` over the four
subset sizes, so it reads as *gap added per decade of corpus growth*.

| variant | gap@10 at 2k | 5k | 10k | 13.5k | per decade | per decade @50 |
|---|---|---|---|---|---|---|
| fp32 | 0.0000 | 0.0100 | 0.0233 | 0.0400 | **+0.0445** | +0.0417 |
| fp16 | 0.0033 | 0.0200 | 0.0233 | 0.0400 | **+0.0386** | +0.0325 |
| binary r20 | 0.2000 | 0.1700 | 0.1967 | 0.2000 | +0.0044 ‡ | +0.0101 ‡ |
| binary r50 | 0.0500 | 0.0600 | 0.0900 | 0.0833 | **+0.0477** | +0.0360 |
| binary r100 | 0.0167 | 0.0333 | 0.0567 | 0.0667 | **+0.0606** | +0.0415 |
| binary r200 | 0.0033 | 0.0167 | 0.0267 | 0.0500 | **+0.0496** | +0.0362 |
| binary r400 | 0.0000 | 0.0033 | 0.0067 | 0.0200 | +0.0200 | +0.0152 |

‡ Rescore 20 is non-monotone across the four sizes and its fit is noise around an already
unusable 0.80. It is not evidence about how narrow binary scales.

**Everything widens with N, and the honest reading is that most of the widening belongs to
the graph.** fp32 — no compression at all — widens at +0.0445 per decade, and fp16 at
+0.0386, which is the same number at this resolution. That is HNSW at a fixed `ef_search` of
100 losing ground as the graph grows, and it is the correct baseline to judge binary against.
The first version of this document reported fp32 and fp16 as flat at 0.0000, which was an
artefact of measuring them by sequential scan.

**What binary adds on top of the graph is still the finding.** At equal `ef_search` — the
four arms at 100 — rescore 100 sits at a gap of 0.0667 against fp16's 0.0400 at 13,549, and
it got there faster: 0.0167 to 0.0667, **quadrupling across less than one decade**, a slope
of **+0.0606 per decade** against fp16's +0.0386. The worst question falls 0.90 → 0.40 over
the same range. Rescore 200 and 400 look better than fp16 in places, but they are running at
`ef_search` 200 and 400 and are buying that with a two- to four-times wider graph walk, not
with quantisation; the comparison to make is within a column of equal `ef_search`.

Extrapolating the fitted line — an extrapolation, and labelled one in the report — rescore
100 reaches a **gap of 0.3300 at 300M passages**, i.e. index recall@10 around 0.67. fp16
reaches 0.2076, and fp32 0.2335. Two things follow, and the second is new: binary is
unshippable at that scale, and **so is `ef_search = 100`**, for any representation. The
extrapolation says the graph parameters have their own scaling wall and it arrives before
the storage one does.

The mechanism for binary's share is the expected one and is why that trend should be believed
rather than dismissed as sampling noise: a 1024-bit Hamming space has a fixed number of
distinguishable neighbourhoods, so adding vectors adds near-collisions. Nothing about that
stops at 13,549.

---

## End to end, through the real pipeline

At the full corpus, all 30 scorable questions, 20 of them counting towards the headline.

| variant | headline Recall@8 | Recall@8 (all) | Recall@1 (all) | search median ms |
|---|---|---|---|---|
| fp32 | 0.9000 | 0.9000 | 0.6667 | 951 |
| fp16 | 0.9000 | 0.9000 | 0.6667 | 951 |
| binary r20 | 0.9000 | 0.9000 | **0.6333** | 987 |
| binary r50 | 0.9000 | 0.9000 | 0.6667 | 943 |
| binary r100 | 0.9000 | 0.9000 | 0.6667 | 926 |
| binary r200 | 0.9000 | 0.9000 | 0.6667 | 879 |
| binary r400 | 0.9000 | 0.9000 | 0.6667 | 815 |

Every recall figure in this table is unchanged from the first version of this document. It is
the one section the planner defect never touched: end to end runs on its own connection, so
its dense stage always used an index. The search medians moved by about a tenth of a second
because three other sweeps were competing for the machine during the re-run, which is what
the latency caveat below is for.

**At this corpus size the pipeline absorbs everything binary loses**, down to and including
rescore 20 with its index recall@10 of 0.8000. The lexical half, the identifier signal and
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
| 2,000 | 0.47 s | 0.30 s | 0.16 s | 4,219.7 |
| 5,000 | 1.42 s | 0.84 s | 0.43 s | 3,518.8 |
| 10,000 | 3.25 s | 2.02 s | 0.92 s | 3,077.8 |
| 13,549 | 4.87 s | 3.00 s | 1.32 s | 2,780.3 |

Build is roughly 1.6x faster in fp16 and 3.7x faster in binary — the same page-count argument
as the query side. **The build rate itself falls with N**: fp32 drops from 4,220 to 2,780
vectors per second across this range, so build cost is superlinear and a linear projection of
it would be dishonest. This is a scaling wall in its own right and nobody has measured it at
a size where it matters. It is listed under *what this does not establish*. The absolute
figures here are a few tenths of a second faster than the first run of this sweep and the
ratios a little tighter; both runs were taken on a contended machine and the shape, not the
second decimal, is what this table is for.

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

`halfvec` costs nothing that this measurement can separate from fp32. Read through their own
indexes at the same `ef_search`, the two are the same index: recall@10 0.9600 against 0.9600,
recall@50 0.9660 against 0.9680, the same worst question at 0.50, trends of +0.0386 and
+0.0445 per decade, identical end-to-end recall, faster queries and faster builds, for a
3.00x reduction. There is no trade to weigh. The only reason it is not already the storage
type is that nobody had measured it.

**Note what that sentence no longer says.** The first version of this document justified
`halfvec` with "index recall 1.0000 at both depths, worst query 1.0000, a flat trend" — a
comparison of exact retrieval against itself, which was evidence for nothing. The claim that
survives is the weaker and sufficient one: *fp16 is indistinguishable from fp32 through the
same index*. The 4-point gap to exact that both of them carry is HNSW's, it was there before
migration 0025, and it is unchanged by it.

**Binary is not recommended, and the small-N number is the reason to be careful rather than
the reason to adopt.** At 13,549 vectors binary at rescore 100 looks nearly free and the
pipeline absorbs it completely. Both of those facts are properties of 13,549 vectors. The gap
quadruples across less than one decade of N, the worst question falls from 0.90 to 0.40 over
the same range, and the extrapolated gap at 300M passages is 0.33 against fp16's 0.21.
Recommending binary on the 13.5k figure would be F7's mistake — tuning on the evidence
available rather than the evidence needed — and ADR 0005 already records what that costs in
the other direction.

If binary is wanted anyway, rescore 400 remains the flattest width (+0.0200 per decade at
depth 10 against rescore 100's +0.0606), but the correction has weakened that case rather
than strengthened it. It is no longer *near flat*, it costs more in its rescore step
(2.54 ms) than in its index scan (1.44 ms), and it is only reachable at `ef_search = 400` —
which is not a free knob but a four-times wider graph walk that fp16 would also benefit from
and has not been measured with. It buys the 18.84x index saving back with heap I/O that
quantisation did not shrink, and with a walk width that is a cost of its own. That trade
might still be worth making at 300M. It cannot be decided from here.

### The one measurement that would reverse this

**Build the binary index at 1M–10M passages of comparable text and measure index recall@10
and end-to-end Recall@8 at rescore 100 and 200.** If the gap at depth 10 at 10M passages is
at or below the value the flat hypothesis predicts — that is, if it has not grown past
roughly 0.07 — then the widening measured here is a small-N artefact, binary becomes the
recommendation, and the projection above says it fits 10M documents of this corpus's shape in
1.3 TiB. If instead the gap is near the fitted +0.0606 per decade, arriving around 0.23 at
10M, binary is finished as an option at any scale this product cares about and the question
becomes whether fp16 plus a bigger machine, or a different index family, carries the corpus.
**Measure fp16 at the same sizes in the same run.** It is no longer a control that can be
assumed perfect: it widens at +0.0386 per decade here, and the question "does binary degrade"
has to be asked as "does binary degrade *faster than the graph does*".

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
- **Nothing here separates fp16 from fp32 below the graph's own error.** Both arms lose four
  points to HNSW at `ef_search = 100`, and a difference between them smaller than that is
  not resolvable by this measurement. "Indistinguishable from fp32" is the claim; "lossless"
  is not, and the first version of this document should not have implied it.
- **The rows at `ef_search` 200 and 400 are not comparable to the rows at 100.** A rescore
  width above the profile's `ef_search` is unreachable without widening the walk, so those
  two arms were measured with a wider one. Compare within a column of equal `ef_search`.
