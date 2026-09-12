# F25 — The lexical wall, and making the corpus size claim true

Everything in this product scales except one query. This plan is about that query, the
measurements that found it, and the architectural decision it forces.

The trigger is commercial: the corpus in the demo is 14 documents, and the conversation
with an enterprise buyer is about hundreds of thousands. The honest answer today is that
retrieval falls over long before that, and it falls over in the half nobody suspects.

---

## 1. What was measured

300,000 synthetic passages of ~1,200 characters in the project's own Postgres
(`paradedb/paradedb:0.15.26-pg17`), one tenant, vocabulary shaped like a legal corpus —
a few very frequent terms and a long tail of rare ones. Measured 2026-08-21 on the
development machine.

### The lexical half is the wall

| Query | Time | Plan chosen |
|---|---|---|
| Rare term (`term12345`, identifier-shaped) | **0.07 ms** | GIN index, ideal |
| Frequent term (`contrato`) | **188 ms** | planner **abandons GIN** → parallel seq scan |
| Three terms OR'd, as `lexical()` builds them | **1,123 ms** | parallel seq scan, 300k rows |
| The same, under the **real RLS policy** | **5,953 ms** | seq scan, 260k rows discarded |

The cost is **linear in rows that match**, not logarithmic. `ts_rank_cd` has to be computed
for every matching row before `LIMIT 50` can choose. A term appearing in 30% of a corpus
means ranking a third of the table.

Extrapolating from the 1,123 ms figure: **1M passages ≈ 3.7 s, 5M ≈ 19 s** — for the
lexical half alone, before the dense half and before reranking.

### The dense half is not a problem

| | |
|---|---|
| HNSW, 60,000 vectors, no filter | **1.4 ms** |
| Index size | **3.7 KB per vector** (the vector is stored in the index) |

Logarithmic, as advertised. Its constraint is memory, not time: **1M vectors ≈ 3.7 GB,
5M ≈ 18.5 GB**, and it has to be resident.

### ParadeDB does something different in kind

| Query | Time | Plan |
|---|---|---|
| BM25, no competing predicate | **2.5 ms** | `Custom Scan (ParadeDB Scan)`, `Scores: true`, `TopNScanExecState` |
| BM25 with `tenant_id` inside the Tantivy query | **4.98 ms** | `Parallel Custom Scan`, scores present |
| BM25 with `tenant_id` as an ordinary SQL filter | 159 ms | plain `Index Scan`, **scores NULL** |
| BM25 under the real RLS policy | 1,476 ms | **Seq Scan**, `@@@` as filter, scores NULL |

`TopNScanExecState` is the whole story. ParadeDB does not score every match and then sort;
it resolves the top N **inside the index**. That is why the gap is ~130× rather than the
~4× a faster ranking function would buy. It is a different algorithm, not the same
algorithm on better hardware — which is also why **no amount of hardware closes this gap**.

---

## 2. A correction to ADR 0002

ADR 0002 records BM25 as rejected because `paradedb.score()` "cannot supply a score under
row-level security". On `pg_search` 0.15.26 that statement is too broad, and the precision
matters because it is what makes this plan possible.

With a **simple** tenant policy, the custom scan is chosen and pg_search **absorbs the RLS
predicate into the Tantivy query**. Observed directly in the plan, as the app role, with
RLS enforced:

```
Parallel Custom Scan (ParadeDB Scan) on bm_test
  Scores: true
  Tantivy Query: {"boolean":{"must":[
      {"with_index":{"query":{...parse "text:(contrato OR articulo)"...}}},
      {"term":{"field":"tenant_id","value":1}}]}}      ← the RLS qual, pushed down
Execution Time: 4.790 ms
```

What defeats the pushdown is specifically the **label clause**:

```sql
tenant_id = zenith_current_tenant()
AND (label_ids = '{}'::uuid[] OR label_ids && zenith_current_labels())
```

The `OR` over an array is not expressible as a pushdown, so the planner falls back to a
sequential scan and `@@@` degrades to a filter — scores NULL, exactly as F18 reported.

**The conclusion changes from "BM25 is incompatible with RLS" to "BM25 is incompatible with
the label clause as currently written".** That is a solvable problem.

---

## 3. The design

### 3.1 The index

```sql
ALTER TABLE chunks ADD COLUMN unlabelled boolean
  GENERATED ALWAYS AS (label_ids = '{}'::uuid[]) STORED;

CREATE INDEX ix_chunks_bm25 ON chunks
  USING bm25 (id, text, tenant_id, label_ids, unlabelled)
  WITH (key_field = 'id');
```

`unlabelled` is materialised because Tantivy expresses "this field has no values" poorly
and the product's rule — *a document with no labels is visible tenant-wide* — has to be a
first-class clause rather than an absence.

The isolation columns are **in the index on purpose**. That is the mechanism: a predicate
inside the Tantivy query is part of the search, while a predicate outside it is a filter
applied to the search's output, and the second one destroys both the scoring and the plan.

### 3.2 The function

```sql
CREATE FUNCTION zenith_lexical_search(query_string text, want int)
RETURNS TABLE(id uuid, score real)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, paradedb AS $$
  SELECT c.id, paradedb.score(c.id)
  FROM chunks c
  WHERE c.id @@@ paradedb.boolean(must => ARRAY[
      paradedb.parse('text:(' || query_string || ')'),
      paradedb.term('tenant_id', zenith_current_tenant()),
      paradedb.boolean(should => <unlabelled OR each reachable label>)])
  ORDER BY paradedb.score(c.id) DESC
  LIMIT want;
$$;
```

**The function takes no tenant and no labels.** It reads them from `zenith.tenant_id` and
`zenith.label_ids` through the same `zenith_current_tenant()` / `zenith_current_labels()`
the policies use. There is no argument through which a caller can ask about somebody else's
corpus, which is the property that makes this defensible rather than a hole.

Verified on 2026-08-21, as a non-superuser role with RLS enforced:

- returns rows from exactly one tenant — the session's
- changing `zenith.tenant_id` mid-session changes the results and never leaks across
- **~45 ms** against the 5,953 ms the current query costs under the same policy

### 3.3 What changes in the application

`lexical()` in `app/features/retrieval/search.py` calls the function instead of building
the `ts_rank_cd` statement. The document scope (`scoped()`) becomes another Tantivy `must`
clause rather than a SQL `AND`, for the same reason as the isolation columns.

Everything downstream is untouched: the function returns `(id, score)`, which is the shape
`lexical()` already returns. RRF sees positions, as it always has.

---

## 4. The architectural decision, stated rather than smuggled

ADR 0001: *"The bypass surface … grows for a security guarantee, never for ergonomics."*
ADR 0002 anticipated this exact design and priced it as ergonomics.

**This is neither ergonomics nor a security guarantee, and pretending otherwise is the
failure mode.** It is the difference between a product that holds an enterprise corpus and
one that does not. That deserves its own weighing, in writing, and the honest form of it is:

> The isolation guarantee does not change. What changes is where the predicate is
> expressed: from a policy the planner applies, to a single audited function that reads the
> same session variables and expresses the same rule in a form the index can use.

Three things keep that from being a rationalisation, and all three are obligations of this
plan rather than claims about it:

1. **No tenant or label parameter.** Enforced by the signature.
2. **The full RLS isolation matrix runs against the function**, not just against the
   policies — the same tests, the same fixtures, one more subject.
3. **The audit stays one grep.** `owner_session`, `platform_session`, `SECURITY DEFINER`.
   This adds one name to a list that already has four.

If any of the three cannot be delivered, the plan fails and Plan B (§8) applies.

ADR 0001 and ADR 0002 are both amended by this work. Neither is quietly edited: the
amendment says what was believed, what was measured, and what changed.

---

## 5. The open risk, measured and unresolved

The `should` clause over `label_ids` returned **only the unlabelled passages** — the
labelled ones the session does reach were missing.

This fails **safe**: it is too restrictive, never too permissive, so it cannot leak. But it
silently loses recall on every labelled document, which is precisely the class of silent
degradation this project refuses.

This is a `uuid[]` field indexed into Tantivy and queried with `paradedb.term`, and the
mismatch is somewhere in that chain. Options, in the order they should be tried:

1. `label_ids` indexed as `text[]` rather than `uuid[]`
2. a `paradedb.parse` clause against a denormalised space-joined label key column
3. `paradedb.term_set`, if it handles multi-valued fields differently

**This is a blocker for Phase 1 and must be resolved before anything ships.** A version that
only returns unlabelled passages is worse than what exists today.

### RESOLVED — 2026-08-26

**None of the three options was needed. `uuid[]` was never the problem; the boolean
structure was.**

`should` alongside `must` in a Tantivy boolean is *optional* — it boosts scoring and does
not filter. So the original construction was not "too restrictive": asked for again on a
clean corpus it returns **every** row the `must` clauses match, labels ignored entirely.
That is the opposite failure and the dangerous direction, which is worth stating plainly:
the note above recorded this as failing safe, and it does not.

The label alternatives have to be a `should` **nested inside a `must`**, which is how
"at least one of these, required" is expressed:

```sql
paradedb.boolean(must => ARRAY[
    paradedb.parse('text:(' || query_string || ')'),
    paradedb.term('tenant_id', zenith_current_tenant()),
    paradedb.boolean(should => ARRAY[            -- <- nested, therefore required
        paradedb.term('unlabelled', true),
        paradedb.term('label_ids', <label 1>),
        paradedb.term('label_ids', <label 2>)])])
```

Measured on `paradedb/paradedb:0.15.26-pg17`, 80,000 rows across two tenants, labels
distributed as they are in the product (a quarter unlabelled, the rest across three
compartments), against SQL ground truth computed with the real policy predicate:

| session reaches | Tantivy | the policy's own SQL | |
|---|---|---|---|
| no labels | 15,000 | 15,000 | exact |
| `legal` | 45,000 | 45,000 | exact |
| `legal` + `hr` | 60,000 | 60,000 | exact |
| the other tenant's rows | **0** | — | no leak |

And the plan is the one this whole document is about:

```
Parallel Custom Scan (ParadeDB Scan) on chunks
  Exec Method: TopNScanExecState
  Scores: true
  Top N Limit: 50
Execution Time: 5.180 ms
```

The same query the product builds today, on the same 80,000 rows: `Seq Scan`, 45,000 rows
scored to return 50, **31.7 ms**. The shape is the wall — cost linear in rows matched — and
at 300k it is the 5,953 ms measured in §1.

§8's Plan B is therefore not needed. It stays in this document as the record of a fallback
that was prepared and did not have to be used.

---

## 6. Rollout

The index is built on a table that ingestion writes to, on machines where a long exclusive
lock is not acceptable.

- `CREATE INDEX CONCURRENTLY`, and the migration must tolerate being interrupted.
- **Both halves run against `eval/` before the switch.** 72 questions and
  `production-recall.json` already exist; this change does not get to be the first one that
  ships on an opinion.
- The switch itself is one function call in `lexical()`. Reverting is the same edit, and
  the old GIN index stays until the new path has been exercised on real data — dropping it
  in the same migration would make the rollback a reindex.

Two costs to measure before committing, neither of them known today:

- **Index build time and size** on a real corpus. The 300k synthetic build was minutes, not
  seconds.
- **Write amplification during ingestion.** Every chunk insert now maintains a BM25 index as
  well as a GIN one, and ingestion is already the slowest thing in the product.

---

## 7. What comes with it

**A possible deletion.** BM25 has IDF, which is exactly what `ts_rank_cd` lacks and exactly
what `identifiers.py` was written to compensate for. F18 measured the identifier that
ranked 52nd moving to rank 1 under BM25 **with no identifier query at all**. If `eval/`
confirms it, that module and its third SQL statement go. Not assumed — measured, and only
after Phase 1 is green.

**Postgres tuning, which is free.** `shared_buffers` is **128 MB**, the stock default.
There is not one line of Postgres configuration in `docker-compose.yml` or in
`docker-compose.prod.yml`. For a database expected to hold a multi-gigabyte HNSW index that
is leaving performance on the floor. `shared_buffers`, `effective_cache_size`,
`max_parallel_workers_per_gather`, `work_mem` — one commit, measured before and after.

---

## 8. Plan B, if §5 cannot be resolved

Make the lexical half a **recall** device and stop asking it to rank.

Today it does `ORDER BY ts_rank_cd DESC LIMIT 50`, which forces every match to be scored.
But RRF and the cross-encoder reorder everything downstream anyway — `ts_rank_cd`'s
precision matters far less than its position in the pipeline suggests. Taking 50 candidates
without ordering them removes the sort and most of the cost.

It is a smaller change with a lower ceiling and a real risk: the 50 arbitrary candidates may
not contain the right passage, where the 50 best-ranked ones did. `eval/` decides it, and it
is a fallback rather than the plan because it trades quality for speed, which is the trade
this product usually refuses.

---

## 9. What this does not fix

Two of the three scaling walls are hardware, and it is worth writing down that they are not
in scope here so nobody reads this plan as solving them.

- **Ingestion throughput.** 0.88–1.39 s per chunk measured on CPU. 1M chunks is **10–16
  days** on one worker. A GPU is the answer and it is a purchase, not a rewrite.
- **Vector memory.** 3.7 GB of HNSW index per million chunks, resident. The 4-core, 7.6 GB
  VPS tops out somewhere around 200–300k passages, which is thousands of documents rather
  than millions.

Neither is a design flaw. Both should be stated as hardware requirements in the commercial
conversation rather than discovered by a customer.

---

## 9b. Phase 1, built — 2026-08-26

Migration 0022, `ZENITH_LEXICAL_ENGINE`, the isolation matrix, and the measurement the
rollout section demanded. Two findings the plan did not anticipate, both worth more than
the feature.

**The custom scan hijacks unrelated queries.** Putting the isolation columns in the BM25
index — the mechanism this whole plan rests on — tells the planner that pg_search's custom
scan can serve any query filtering on them. On 0.15.26 it then cannot:
`SELECT max(length(text)) FROM chunks WHERE tenant_id = ...` fails with `rt_fetch used
out-of-bounds`. No `@@@` anywhere. It surfaced as an unrelated label-sync test failing on a
`xmin` read, which is a symptom nobody traces back to retrieval. Resolved by confining the
custom scan to the one function that needs it, through a per-function `SET`. Every other
query in the product is now planned as though pg_search were not installed.

**BM25 is a scale trade, not an upgrade**, and §7's hoped-for deletion of `identifiers.py`
does not follow. Measured against the 30-question set on 13,549 passages:

| engine | headline Recall@8 | Recall@1 | mean rank |
|---|---|---|---|
| `tsvector` | **90.0%** | **66.7%** | **1.41** |
| `bm25`, default tokeniser | 80.0% | 56.7% | 1.40 |
| `bm25`, `en_stem` | 85.0% | 46.7% | 1.65 |

At this size `ts_rank_cd` is both fast enough and more accurate — its proximity component
does real work that BM25's term statistics do not replace. So the default stays `tsvector`
and this builds a path nothing takes yet, which is the honest outcome: the switch belongs
to the installation whose corpus has outgrown the accurate engine.

**What remains before that switch is defensible**, and none of it may be promised:

1. The same measurement at **300k** rather than 13.5k, where the trade reverses.
2. Index build time and size on a real corpus.
3. Write amplification during ingestion.
4. **An accent-folding tokeniser.** The GIN side is `zenith_text` — `english` with
   `unaccent` in front of the stemmer (migration 0018) — and the BM25 side is `en_stem`,
   which does not fold accents. `maximo` finds `máximo` today and would stop doing so, with
   no error and no degraded flag. That is a blocker for any Spanish corpus and it is not a
   blocker for an English one, which is exactly the kind of distinction that gets lost.

The trigger for the switch is concrete rather than aspirational: **an installation whose
lexical half is already hitting the 10-second statement timeout on `tsvector`.** Until then
the accurate engine wins and the fast one is a path nobody takes.

## 10. Sequencing

This plan is not the most valuable work available, and it would be dishonest to file it as
though it were. Three things come first:

1. **A realistic corpus.** Fourteen documents is the weakest part of any evaluation, ahead
   of anything in this document: retrieval on a handful of files is easy because there is
   nothing to confuse it with. A few thousand public documents is the highest-value hour
   available.
2. **The 20-second query has to go.** That number makes every other property irrelevant.
3. **Backups.** An installation holding other people's documents without a tested restore
   is not finished, whatever its recall is.

**This plan is what makes the scalability claim true afterwards.** Its value in the meantime
is that it converts "we think it will scale" into "the bottleneck is identified, measured,
and has a proven path with a 130× factor" — which is a claim that can be checked.

---

## Test plan

**Isolation — the part that earns the bypass**

- The full RLS matrix, re-pointed at `zenith_lexical_search`: cross-tenant, unreachable
  label, unlabelled-is-tenant-wide, label narrowing.
- A caller cannot reach another tenant's passages by any argument — asserted on the
  signature as well as on the behaviour.
- Changing `zenith.tenant_id` mid-session changes the result set and never mixes the two.
- The document scope composes with the label narrowing rather than replacing it — the
  property `test_scoping_cannot_widen_a_label_narrowing` already asserts for the old path.

**Recall — the part that decides whether it ships**

- `eval/` on both paths, same corpus, same 72 questions. Context ceiling must not regress
  from 96.8%.
- Identifier questions specifically, since they are the class BM25 should *improve*: 100%
  today via `identifiers.py`, and the question is whether BM25 holds it without that query.
- Labelled documents appear in results — the regression §5 describes, asserted so it cannot
  ship silently.

**Performance — the reason for the work**

- The 300k measurement repeated against the real schema, before and after, recorded here.
- Ingestion throughput before and after the second index, on the same document.
