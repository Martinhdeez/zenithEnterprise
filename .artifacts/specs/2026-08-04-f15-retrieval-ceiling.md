# F15 — The context ceiling, and why the identifiers were missing

F9 measured the ceiling on answer quality at **90.3%**: the share of questions whose anchor
reaches the model in the top 8. Two identifier questions accounted for most of the gap, and
neither was being found by *either* retrieval half.

The obvious explanation was tokenisation. It was wrong, and checking it first is the only
reason the real fix is three lines of SQL rather than a re-ingest.

---

## 1. It was not tokenisation

Postgres tokenises both sides identically, and it keeps the identifiers whole:

```
query "…citation 23 U.S.C. 101 refer to?"  →  '101' '23' 'citat' 'refer' 'u.s.c'
corpus "…(23 U.S.C. 101) applies"          →  '101' '23' 'u.s.c' 'code' 'section' …

query "What is Catalog Number 10000W?"     →  '10000w' 'catalog' 'number'
corpus "Cat. No. 10000W  Publication 15"   →  '10000w' 'cat' 'public' '15'
```

`10000w` matches `10000w`. `u.s.c` matches `u.s.c`. The index has the right terms and the
query asks for them. M0's regex bug — which destroyed identifiers on the query side — was
fixed in F6 and stayed fixed.

## 2. `ts_rank_cd` has no IDF, and that is the whole bug

Measured against the real 19,533-chunk corpus:

```
rank 1–51   chunks matching "catalog" or "number" — none containing the identifier
rank 52     the chunk that actually contains 10000W        ← CANDIDATES = 50
```

**The passage was found, ranked, and then buried by two common words in the same
question.** `ts_rank_cd` scores term frequency and proximity; it has no notion of how rare a
term is, so a decisive identifier is worth no more per occurrence than the word *number* in
a tax publication that says *number* on every page.

This is the same shape of failure F7 fixed one layer up: a decisive single signal outvoted
by agreement between weak ones. F7 fixed it in fusion. It was still happening underneath,
inside the lexical half, where fusion could not see it.

## 3. The fix: a third signal that ANDs the identifiers

One extra indexed query, run only when the question contains something identifier-shaped:

| Query | Rank of the correct chunk |
|---|---|
| `10000w \| catalog \| number` (what shipped) | **52** |
| `10000w` | **1** |
| `23 & 101` | 7 |
| `23 & 101 & u.s.c` | **4** |

**AND rather than OR, which is the opposite of `lexical()` and the point.** `lexical` ORs
because a question is not a filter. Here the identifiers *are* the filter: someone asking
about `23 U.S.C. 101` wants the passage with all three parts, not the thousand passages
containing `23`.

**What counts as an identifier**: a lexeme containing a digit, or containing a full stop.
Both markers came from the failures — the digit rule covers `10000w`, `1545-0074`, `23`,
`101`; the full-stop rule exists solely because `u.s.c` carries no digit and is exactly the
token that moved the citation from unfound to rank 4. A word has neither.

The rule is deliberately generous. A false positive costs one extra AND term on a query
that is discarded when it matches nothing; a false negative is a question that stays
permanently unanswerable.

The results join the candidate union as a third RRF ranking, and its leader gets the same
floor F7 gave the lexical and dense leaders — being the identifier query's first result is
strong evidence, and on RRF score alone a single short ranking can still lose to two halves
agreeing.

## 4. Measured result

| | F9 | **F15** |
|---|---|---|
| Extraction | 100% | 100% |
| **Context ceiling** | 90.3% | **96.8%** |
| factual | 100% | 100% |
| table | 100% | 100% |
| **identifier** | **66.7%** | **100%** |
| cross-document | 80% | 80% |

Both target questions fixed. `identifier-catalog-number` went from absent to **rank 1**.

One question remains outside the ceiling: `cross-platform-obligations`, a cross-document
question rather than an identifier one. It is a different problem and it is not this one.

## 5. Deliberately not BM25

ParadeDB ships `pg_search`, and switching the lexical half to BM25 would fix this class
properly rather than case by case — IDF is exactly what BM25 has and `ts_rank_cd` lacks.
The `score_bm25` column in `query_citations` is named for that plan.

It is not this change. Replacing the lexical half is a re-measurement of all 36 questions
with its own recall risk in both directions, and smuggling it in as a bug fix would mean
shipping a new retrieval engine under a commit message about identifiers. Recorded as the
principled successor, to be done as its own milestone with its own before-and-after.

## 6. Query history, and one uncomfortable filter

`GET /query/history` reads back what F8 has been writing since generation shipped.

**One filter here is application code doing security work, which this project otherwise
refuses.** RLS models tenant and label; neither models "my rows versus my colleagues'".
`queries` is tenant-scoped by policy, so a member holding only `query.history.own` would
see every question their colleagues asked if the service did not filter by `user_id`
itself.

That is worth stating plainly rather than burying: **the questions people ask are more
revealing than the documents they read.** *"What is my severance?"*, *"can I be dismissed
for this?"* — a history endpoint that leaked those would be a worse breach than the corpus,
and it would look like a working feature. So the filter lives in one place, the scope is
decided by the caller's permissions rather than by any request parameter, and a test
asserts a member cannot read another member's history.

**The better answer is a row-level policy on `user_id`**, which needs a third RLS context
variable — a schema change with its own migration and blast radius. Recorded here rather
than done quietly.

Pagination is keyset, for the reason F4 chose it for documents, and more sharply: the query
log is written by *every query*, so something is always inserted between two pages and an
offset would repeat or skip a row every time.

## 7. Still open

- **`cross-platform-obligations`** — the last question outside the ceiling.
- **BM25** — §5.
- **`queries` row-level policy** — §6.
- **M3 admin surfaces** — labels, roles and LLM configuration have endpoints for none of
  them; the permissions (`labels.manage`, `roles.manage`, `llm_config.manage`) exist and
  are enforced where they apply. Not started.
