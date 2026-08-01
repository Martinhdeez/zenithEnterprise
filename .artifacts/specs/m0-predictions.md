# M0 — predictions, recorded before measuring

**Date:** 2026-08-02
**Written before the tracer bullet was run.** Committed first, deliberately: a prediction
made after seeing the numbers is a description, and it costs nothing to be wrong here.

The point is not to be right. It is that being *wrong* is information — a surprise means
the mental model of the system is faulty somewhere, and that is worth more than a
confirmation.

---

## What the pipeline is

`pdfplumber` → fixed-size chunks → BGE-M3 → pgvector HNSW, plus Postgres full-text search,
fused with Reciprocal Rank Fusion. **No reranker, no Docling routing, no contextual
prefixes, no bounding boxes.** Every one of those is a variable held constant.

## Headline prediction

**Recall@8 over factual + cross-document: 70–80%.**

Reasoning: 13 of the 20 headline questions target documents that extract cleanly — the
regulations, the IRS payroll guide, the BoE report. Those should be found. The rest are
degraded by extraction, and that is where the misses come from.

Under §4 of the plan that lands in the **60–85% band**: works, needs the pieces we removed
on purpose. If it comes in above 85% I will be suspicious of the question set before I am
pleased with the retrieval.

## Per-document predictions

| Document | Expectation | Why |
|---|---|---|
| GDPR, AI Act, DSA | **Good** | Clean extraction, distinct vocabulary per document. The risk is confusion *between* them, which is why three are in the corpus. |
| arXiv papers (×3) | **Poor on lexical, fair on dense** | No word boundaries at all: `densevectorindexofWikipedia`. BM25 tokenises that into nothing. Whether the embedder recovers is the interesting question — its tokeniser is sub-word, so it may partly cope. |
| IRS instructions | **Poor** | Two columns interleave mid-sentence, so chunks contain two unrelated half-thoughts. |
| IRS Pub 15, Form 1040 | **Mixed** | Rates appear on several pages, which makes the questions easy; the form itself is layout with almost no prose. |
| BoE report | **Fair** | Clean prose; the table questions will fail. |
| Infrastructure Act | **Untested by the question set** | 1,039 pages of distractors. Its job here is to make everything else harder. |
| NASA scans | **Poor** | 330 and 660 characters per page of degraded OCR. |

## Specific predictions

1. **Table questions: 0–20%.** Flattened tables lose which figure belongs to which row.
   This is the baseline that F5's Docling routing has to beat, and the reason the category
   is excluded from the headline.
2. **Unanswerable questions: the system will return confident nonsense.** There is no
   abstention logic in the tracer bullet — retrieval always returns its top *k*. The
   measurement here is how *similar* the top result looks, which tells us whether a score
   threshold could work as an abstention signal at all.
3. **BM25 will contribute almost nothing on the arXiv questions**, and RRF will be carried
   by the dense half. If fusion still scores well there, that is evidence the two halves
   are genuinely complementary rather than redundant.
4. **The 5%-corpus-reach measurement will show a drop under 5%**, because 13 documents is
   too small for HNSW filtering to hurt. That number will not be trustworthy at this scale
   and should be re-measured with a real corpus before it decides the partitioning
   question.
5. **Extraction, not retrieval, will be the dominant error source.** If that holds, F5 is
   where the effort belongs, and the ranking work in F7 is tuning rather than rescue.

## What would change the plan

- **Below 60% on clean documents** — the architecture is wrong, not the parser. Stop and
  diagnose before F4.
- **Above 90%** — the question set is too easy. The anchors would need to be harder before
  the number means anything.
- **BM25 contributing nothing anywhere** — hybrid search is not earning its complexity, and
  ParadeDB is a dependency we could drop.
