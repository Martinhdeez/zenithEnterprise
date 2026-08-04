# ADR 0002 — Hybrid retrieval, fused by RRF

**Status:** Accepted, amended twice by measurement

## Context

Dense retrieval finds meaning and misses exact strings. Lexical retrieval finds exact
strings and misses paraphrase. A knowledge base for regulations, contracts and tax
publications needs both: *"what must a controller do after a breach"* and *"1545-0074"* are
both real questions.

## Decision

Both halves run, and their rankings are fused by **Reciprocal Rank Fusion**:
`score = Σ 1/(60 + rank)`.

RRF uses **positions, never scores**. `ts_rank_cd` and cosine similarity live on different,
corpus-dependent scales; normalising them is fragile and needs recalibrating whenever the
data changes.

## Consequences and amendments

**Amendment 1 (F7) — RRF rewards agreement, and identifiers are single-half by nature.**

```
ranked 1st lexically, absent from dense →  1/61  = 0.0164
ranked 5th by both                      →  2/65  = 0.0308   ← wins
```

Identifier questions scored **0% at rank 8**. Two fixes: the candidate set became the
**union** of both halves rather than the fused top-N (fusion was choosing candidates *and*
ordering results, and is bad at the first), and each half's leader is guaranteed a place —
a floor, not a weight. Recall@8 went 80% → 95%.

**Amendment 2 (F15) — the same failure one layer down, inside the lexical half.**

`ts_rank_cd` has no IDF. A rare identifier scores no better per occurrence than a ubiquitous
word, so on the real corpus the chunk containing `10000W` ranked **52nd** — two places
outside the candidate set — beaten by chunks matching `catalog` and `number`. A third
signal was added: one query ANDing only identifier-shaped lexemes. Context ceiling 90.3% →
96.8%; identifier questions 66.7% → 100%.

## Evidence

Every number above is from `eval/`, against 19,533 chunks of real public documents.

## Not taken

**BM25 via `pg_search`.** ParadeDB ships it and it fixes the IDF class properly rather than
case by case — the `score_bm25` column is named for that plan. It is a re-measurement of all
36 questions with recall risk in both directions, and it deserves its own milestone rather
than arriving inside a bug fix.
