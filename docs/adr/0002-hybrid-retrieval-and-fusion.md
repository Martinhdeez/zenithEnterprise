# ADR 0002 — Hybrid retrieval, fused by RRF

**Status:** Accepted, amended three times by measurement — the third one reversing a
conclusion this ADR had drawn (see the last section)

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
a floor, not a weight. Recall@8 went 80% → 95% on one corpus of 13 public documents; not a benchmark.

**Amendment 2 (F15) — the same failure one layer down, inside the lexical half.**

`ts_rank_cd` has no IDF. A rare identifier scores no better per occurrence than a ubiquitous
word, so on the real corpus the chunk containing `10000W` ranked **52nd** — two places
outside the candidate set — beaten by chunks matching `catalog` and `number`. A third
signal was added: one query ANDing only identifier-shaped lexemes. Context ceiling 90.3% →
96.8%; identifier questions 66.7% → 100%.

## Evidence

Every number above is from `eval/`, against 19,533 chunks of real public documents.

## Superseded — BM25 via `pg_search`, and why the objection was too wide

**Status of this section: superseded by migration 0022 (2026-08-26).** It is kept rather than
deleted, because the reasoning below was right about the mechanism and wrong about its scope,
and the difference between those two is the useful part.

### What this section used to conclude

That BM25 was **incompatible with ADR 0001**. Attempted and reverted in F18: with the RLS
policies on `chunks` in force, the planner used the tenant and label b-tree indexes and
applied `@@@` as an ordinary filter —

```
Bitmap Heap Scan on chunks
  Filter: (id @@@ '{"with_index":...}'::paradedb.searchqueryinput)
    Bitmap Index Scan on ix_chunks_tenant_id
    Bitmap Index Scan on ix_chunks_label_ids
```

— so the rows came back correctly filtered, isolation was never at risk, and every
`paradedb.score(id)` was `NULL`. Ranking by a NULL score ranks everything equally, which is a
*silent* degradation: search keeps answering, and answers worse, with nothing to indicate it.
The conclusion drawn was that BM25 "cannot supply a score under row-level security", and that
revisiting it would need either a `pg_search` version whose custom scan composes with RLS
quals, or a bypass route ADR 0001 permits only for a security guarantee.

### What was actually true

The observation was correct and the generalisation was not. The score is not lost to *row-level
security*; it is lost to any predicate left **outside** the Tantivy query. A predicate inside
the query is part of the search. A predicate outside it is a filter applied to the search's
output, and applying one destroys the scoring and the plan together.

Migration 0022 moves the isolation predicates inside. `ix_chunks_bm25` indexes `tenant_id`,
`label_ids` and a materialised `unlabelled` alongside the text, and `zenith_lexical_search`
builds the tenant and label clauses as part of the query rather than around it — the label
alternatives as a `should` nested inside a `must`, because a `should` alongside a `must` is
optional in Tantivy and the flat construction would have ignored labels entirely. The custom
scan then executes, `TopNScanExecState` resolves the top N inside the index, and the score is
a real number.

### What it cost, and what it bought

It **did** need the thing this section said it would need. `zenith_lexical_search` is
`SECURITY DEFINER`, which adds one name to the bypass surface — now five entries, auditable by
the same grep as before. ADR 0001's rule is that the surface grows for a security guarantee and
never for ergonomics, and the argument that this qualifies is in the migration: the function
takes **no tenant and no label argument**, reads `zenith_current_tenant()` and
`zenith_current_labels()` exactly as the policies do, and therefore has no parameter through
which another tenant's corpus can be asked for. A session with no context matches nothing, the
same closed failure as every policy in the schema.

What it bought is the reason the trade was made at all, and it is not about ranking quality.
`ts_rank_cd` has no early termination: it scores every matching row before `LIMIT` can choose,
so a term appearing in a third of the corpus means ranking a third of the table. Measured on
**300,000 passages under the real policy: 5,953 ms** for the lexical half alone, against a
statement timeout of 10,000 ms. Resolving the top N inside the index is a different algorithm,
which is why the gap is ~130x rather than the ~4x a faster ranking function would buy, and why
no hardware closes it.

### Where it stands

**Implemented, migrated, and switched off.** `config.py`'s `lexical_engine` defaults to
`tsvector`; `bm25` selects the new path. The setting exists rather than the replacement being
straight, and migration 0022 keeps the GIN index for the same reason: every recall figure in
`eval/` was measured against `ts_rank_cd`, and reverting a retrieval change on a customer
installation has to be a restart rather than a redeploy or a reindex.

Flipping it is a real behaviour change and is not a documentation decision. Before anyone
does: the full RLS isolation matrix against `zenith_lexical_search`; headline Recall@8 not
below the established baseline and identifier questions still at 100%; and the accent case
migration 0022 flags, since ParadeDB's `en_stem` tokenizer lowercases and stems but does
**not** fold accents, so a Spanish corpus changes behaviour in a way no current eval question
covers.

F15's identifier query stands either way. It is narrower, it works inside the architecture,
and it is what the `tsvector` path still relies on.
