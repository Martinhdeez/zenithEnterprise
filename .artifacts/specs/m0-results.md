# M0 — results

**Date:** 2026-08-02
**Corpus:** 13 public PDFs, 1,842 pages, 6,317 chunks
**Pipeline:** pdfplumber → 1,200-char chunks → BGE-M3 → pgvector HNSW + Postgres FTS → RRF
**Not present:** reranker, Docling routing, contextual prefixes, bounding boxes
**Hardware:** Apple M4 Pro, 24 GB. Embedding took 4m 11s. **Not customer hardware.**

Predictions were committed before the run: `m0-predictions.md`.

---

## 1. The headline

| Metric | Value |
|---|---|
| **Recall@8 (factual + cross-document)** | **75%** |
| Recall@50 | **95%** |
| **Document-level Recall@8** | **100%** |
| MRR | 0.49 |
| Median query latency | 20 ms *(M4 Pro — not a gate)* |

**Predicted 70–80%. Actual 75%.**

Under §4 of the plan this is the **60–85% band: works, needs the pieces we deliberately
removed.** F4–F7 proceed.

### The three numbers that matter more than the headline

**Document-level recall@8 is 100%.** The right *document* is in the top 8 for every single
answerable question. Retrieval is never lost — at worst it is imprecise about which passage.

**Recall@50 is 95% against Recall@8 of 75%.** The answer is almost always retrieved; it is
ranked between 9th and 50th. Four of the five misses sat at rank **9, 10, 16 and 23**.

That is not a retrieval problem. **That is precisely and only what a cross-encoder reranker
fixes** — it re-scores the top 50 and pulls the right passage into the top 8. The reranker
was removed from the tracer bullet on purpose, and the gap it has to close is now measured:
**20 points**, with a known ceiling of 95%.

---

## 2. Where the predictions were wrong

### Wrong, and instructively so: table questions

**Predicted 0–20%. Actual 80%** — four of five found at rank 1 or 2.

The reasoning was that flattened tables lose which figure belongs to which row. That is
true, and it is not what recall measures.

`pdfplumber` *does* extract the text inside table cells; what it destroys is the
**structure**. So retrieval finds the passage containing `1.45%` perfectly well. Whether a
model can then work out that 1.45% is the Medicare rate rather than the Social Security
rate is a **generation** question, not a retrieval one.

**Recall@k cannot measure table comprehension.** The prediction conflated two different
failures, and the measurement separated them. The consequence is concrete:

> The case for Docling in F5 **cannot be made on these numbers**, and the M0 plan's claim
> that this category gives F5 "a baseline to beat" was wrong. Justifying the expensive
> parser needs an answer-correctness measurement, which needs generation — F9. Until then,
> the argument for Docling rests on the structure being obviously destroyed, not on a
> retrieval number.

The one table question that did fail, `table-form-1040-status`, failed because Form 1040 is
two pages of layout with almost no prose — 11 chunks in total. That is the sparse-text case,
not the table case.

### Right: extraction damage is real and visible

Both arXiv questions that missed (`attention-optimizer` at rank 23, `attention-multihead` at
rank 16) were found by the **dense half and not the lexical half** — exactly as predicted
for documents that extract as `densevectorindexofWikipedia`. BM25 tokenises that into
nothing.

### Right: the corpus-reach measurement is not yet meaningful

Recall@50 at 100%, 20% and 5% corpus reach: **0.95, 0.95, 0.95.** Identical, as predicted.
Thirteen documents is far too small for HNSW filtering to degrade.

**This does not answer the partitioning question and must not be quoted as if it did.** It
needs a corpus two orders of magnitude larger. `project-state.md` §7 stays open.

---

## 3. The finding that changes an architectural assumption

| | Count |
|---|---|
| Found by **both** halves | 15 |
| Found by **dense only** | 4 |
| Found by **lexical only** | **0** |
| Found by neither | 1 |

**The lexical half never once found something the dense half missed.**

On this evidence hybrid search is not earning its complexity — and ParadeDB is a dependency
we could drop. **Two reasons that conclusion would be wrong, both of them limitations of
this measurement rather than results:**

1. **This is not BM25.** The tracer bullet uses Postgres `ts_rank_cd` over OR'd terms
   because it was already available. ParadeDB's `pg_search` implements real BM25 with
   different scoring. Testing a substitute and concluding about the original is a mistake.

2. **The question set contains no exact-identifier queries** — no invoice numbers, no
   product codes, no contract references. §6 of `technical-decisions.md` argues the lexical
   half exists *precisely* for `FAC-2026-99`, where a vector search returns the concept of
   an invoice rather than that string. **All 30 questions are natural-language and
   semantic.** The set was built to test retrieval quality and happens to exclude the one
   case BM25 is for.

**So this is a gap in the question set, not a verdict on hybrid search.** Action: add
identifier-lookup questions before M0 is used to justify anything about the lexical half.
Recorded as the first item in §5.

---

## 4. The bad question

`cross-platform-obligations` was the only complete miss — not retrieved at any rank, by
either half.

Reading it back: the question asks *"which EU regulations impose duties specifically on
online platforms?"*, and its recorded anchors are `status of trusted flagger` and `provider
of a high-risk AI system`. Those phrases are not what the question asks about. The anchors
were chosen for **distinctiveness** after the five-page cap tightened them, and in doing so
they drifted away from the question.

**That is a defect in the question, not in retrieval**, and it cost a point of headline
recall. It is left in for now, recorded rather than quietly fixed — deleting a question
after seeing it fail is how a benchmark becomes flattering.

---

## 5. What happens next

| Action | Where |
|---|---|
| **Add exact-identifier questions** — the case BM25 exists for and the set omits | Before the lexical half is judged |
| Rewrite `cross-platform-obligations` so its anchors match its question | Next M0 pass |
| **Reranker: close a measured 20-point gap**, ceiling 95% | F7 |
| Docling's value needs answer correctness, not recall | F9 |
| Re-measure corpus reach on a real corpus | Before the partitioning decision |
| Resource measurements per 100 pages | VPS, x86_64 — not yet run |

## 6. The verdict

**Hybrid retrieval works well enough to build on.** The architecture is not the problem: the
right document is found every time, the right passage is found 95% of the time within 50
candidates, and the gap to 75% at k=8 is a ranking problem with a known, measured size and a
known instrument for closing it.

**F4 proceeds.**

The three things this exercise actually bought, none of which would have appeared in a
design review:

- A measured target for the reranker instead of a hope.
- Proof that the lexical half is untested for its actual purpose.
- A demonstration that the table argument needs generation to make, which moves that
  evidence from F5 to F9.
