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

## Not taken — BM25 via `pg_search`, and why it cannot be

**Attempted and reverted in F18. It is incompatible with ADR 0001.**

BM25 does fix the IDF class properly: with a `bm25` index on `chunks`, the identifier that
`ts_rank_cd` ranked 52nd ranks **1st**, and the U.S.C. citation moves from unfound to
**40th** — inside the candidate set — with no custom identifier query at all. Measured on
the real 19,533-chunk corpus.

**It cannot supply a score under row-level security.** `paradedb.score(id)` produces a value
only when ParadeDB's custom scan executes. With the RLS policies on `chunks` in force, the
planner uses the tenant and label b-tree indexes and applies `@@@` as an ordinary
**filter**:

```
Bitmap Heap Scan on chunks
  Filter: (id @@@ '{"with_index":...}'::paradedb.searchqueryinput)
    Bitmap Index Scan on ix_chunks_tenant_id
    Bitmap Index Scan on ix_chunks_label_ids
```

The rows come back correctly filtered — isolation is never at risk — but every score is
`NULL`. Ranking by a NULL score ranks everything equally, which is a *silent* degradation:
search keeps answering, and answers worse, with nothing to indicate it.

Coaxing the planner (`enable_bitmapscan = off`) is not an answer. Correctness would then
depend on a plan choice, and the failure mode when the plan changes is the silent one
above — exactly what this project refuses everywhere else.

**Consequence for ADR 0001:** RLS-first is not free, and this is the first place its cost is
visible. The isolation guarantee is worth more than the ranker; if BM25 is ever revisited it
needs either a `pg_search` version whose custom scan composes with RLS quals, or a design
where the lexical index is queried in a context that has no policies to satisfy — and the
second would mean adding a bypass route, which ADR 0001 permits only for a security
guarantee and never for ergonomics.

F15's identifier query stands as the shipped answer. It is narrower, and it works inside the
architecture rather than against it.
