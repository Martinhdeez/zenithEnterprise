-- Can a `SECURITY DEFINER` function prune at *plan* time without being handed a tenant?
--
-- `unpruned-queries.json` measures the problem and one half of the answer. At 256 partitions
-- the lexical half of every search costs 71.5 ms of planning and 12.9 ms of execution against
-- 0.28/0.52 unpartitioned, and adding a redundant SQL qualifier on `tenant_id` takes the
-- *execution* to 0.47 ms and leaves planning at 55.3 ms. That is the whole shape of the
-- problem: `zenith_current_tenant()` is `STABLE`, so pruning can only happen at executor
-- startup, and by then the planner has already built paths for 256 partitions and five
-- indexes on each.
--
-- Plan-time pruning needs a constant, and the obvious way to get one is to add a tenant
-- parameter to `zenith_lexical_search`. **That would be a leak**, and it is worth being
-- explicit about why, because it is the tempting fix. The function is `SECURITY DEFINER`: it
-- runs as the owner, no policy applies to it, and its tenant clause is the only thing
-- standing between one customer and another's passages. A caller who may pass the tenant may
-- pass somebody else's.
--
-- This file tests the version that is not a leak. The tenant is still read from
-- `zenith_current_tenant()` and no caller can influence it; it is read **into a plpgsql
-- local variable first**, and a local variable becomes a parameter of the SQL statement
-- underneath, which the planner may treat as a constant when it builds a custom plan.
--
-- Three things have to hold together, and any one of them failing makes the change wrong:
--
--   1. It prunes at plan time — one partition in the plan, not 256 with 255 removed later.
--   2. **The ParadeDB custom scan still runs.** ADR 0002 records the F18 failure: with the
--      predicate outside the Tantivy query the custom scan does not execute, every
--      `paradedb.score(id)` is NULL, ranking by a NULL score ranks everything equally, and
--      search keeps answering — worse, and silently. The tenant clause is therefore *not*
--      moved out of the Tantivy query here. A second copy of it is added beside it, where the
--      planner looks.
--   3. **Scores are not NULL.** Checked directly rather than inferred from the plan, because
--      2 failing and 3 failing look identical in a timing.
--
-- And the fourth thing, which is why plpgsql rather than a literal: Postgres will switch a
-- prepared plan to a *generic* one after five executions, and a generic plan has no parameter
-- values to prune on. The last section runs the function well past that threshold and asks
-- again, because a fix that works five times is not a fix.
--
-- ## Measured, at MODULUS 32 over the real 13,549 passages
--
--   as shipped   32 partitions in the plan, planning 3.341 ms
--   candidate     1 partition  in the plan, planning 0.528 ms
--   candidate after nine executions, past the generic-plan threshold: still 1, 0.259 ms
--   candidate with `plan_cache_mode = force_generic_plan`: 1 executed, `Subplans Removed: 31`
--
--   both arms: 50 rows, 0 NULL scores, the same 50 passages, 0 rows at a different rank
--   scores differ by 0.000295043, and section 4c shows why: ParadeDB pushes the SQL
--   qualifier down into the Tantivy query as one more `must` clause, which adds the same
--   constant to every passage in the tenant. Fusion downstream is RRF and reads ranks.
--
-- The forced-generic case is the honest limit of the claim: `plan_cache_mode` is `auto`, and
-- `auto` is a heuristic. If it ever chooses a generic plan the fix degrades to runtime
-- pruning — correct, one partition executed, and the planning and locking costs back.
--
-- Run against the live database; creates and drops its own schema. Read-only with respect to
-- the corpus.

\set ON_ERROR_STOP on

DROP SCHEMA IF EXISTS plpgsql_prune CASCADE;
CREATE SCHEMA plpgsql_prune;

CREATE TABLE plpgsql_prune.chunks (
  id         uuid NOT NULL,
  tenant_id  uuid NOT NULL,
  label_ids  uuid[] NOT NULL DEFAULT '{}',
  unlabelled boolean GENERATED ALWAYS AS (label_ids = '{}'::uuid[]) STORED,
  text       text NOT NULL,
  PRIMARY KEY (id, tenant_id)
) PARTITION BY HASH (tenant_id);

DO $do$ DECLARE i int; BEGIN
  FOR i IN 0..31 LOOP
    EXECUTE format(
      'CREATE TABLE plpgsql_prune.c%s PARTITION OF plpgsql_prune.chunks '
      'FOR VALUES WITH (MODULUS 32, REMAINDER %s)', i, i);
  END LOOP;
END $do$;

-- Real passages rather than generated ones: BM25 scoring depends on term statistics, and a
-- column of synthetic text gets a different plan and different scores.
INSERT INTO plpgsql_prune.chunks (id, tenant_id, text)
SELECT c.id,
       ('00000000-0000-0000-0000-' || lpad(((row_number() OVER ()) % 8 + 1)::text, 12, '0'))::uuid,
       c.text
FROM chunks c;

CREATE INDEX ON plpgsql_prune.chunks
  USING bm25 (id, text, tenant_id, label_ids, unlabelled)
  WITH (key_field = 'id',
        text_fields = '{"text": {"tokenizer": {"type": "en_stem", "lowercase": true}}}');
ANALYZE plpgsql_prune.chunks;

-- The function as migration 0022 writes it, with the schema changed and nothing else.
CREATE FUNCTION plpgsql_prune.as_shipped(query_string text, want integer)
RETURNS TABLE(chunk_id uuid, score real)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = plpgsql_prune, public, paradedb
SET paradedb.enable_custom_scan = on
AS $$
BEGIN
    IF zenith_current_tenant() IS NULL THEN RETURN; END IF;
    RETURN QUERY
    SELECT c.id, paradedb.score(c.id)
    FROM plpgsql_prune.chunks c
    WHERE c.id @@@ paradedb.boolean(must => ARRAY[
            paradedb.match('text', query_string),
            paradedb.term('tenant_id', zenith_current_tenant()),
            paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
    ORDER BY paradedb.score(c.id) DESC
    LIMIT want;
END;
$$;

-- The candidate. Two differences and no others: the tenant is read into a local first, and
-- that local also appears as an ordinary SQL qualifier. The Tantivy term is untouched.
CREATE FUNCTION plpgsql_prune.candidate(query_string text, want integer)
RETURNS TABLE(chunk_id uuid, score real)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = plpgsql_prune, public, paradedb
SET paradedb.enable_custom_scan = on
AS $$
DECLARE
    v_tenant uuid := zenith_current_tenant();
BEGIN
    IF v_tenant IS NULL THEN RETURN; END IF;
    RETURN QUERY
    SELECT c.id, paradedb.score(c.id)
    FROM plpgsql_prune.chunks c
    WHERE c.tenant_id = v_tenant
      AND c.id @@@ paradedb.boolean(must => ARRAY[
            paradedb.match('text', query_string),
            paradedb.term('tenant_id', v_tenant),
            paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
    ORDER BY paradedb.score(c.id) DESC
    LIMIT want;
END;
$$;

SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);

-- Migration 0022 turns the custom scan off for the whole database and back on for
-- `zenith_lexical_search` alone, through the function's own `SET`. The `PREPARE` arms below
-- are not inside a function, so the session has to say it — and it is not optional: without
-- it ParadeDB answers through an `Index Only Scan` with the `@@@` as an index condition,
-- which is a different plan and, per ADR 0002, the one whose `paradedb.score()` is NULL.
-- The first version of this probe omitted this line and measured that plan instead.
SET paradedb.enable_custom_scan = on;

-- `EXPLAIN` on a call does not descend into a plpgsql body — it reports `Function Scan` and
-- one number — so the two plans are exhibited through `PREPARE`/`EXECUTE` instead. That is
-- not an approximation of what plpgsql does, it is the same machinery: a plpgsql statement is
-- an SPI prepared plan whose local variables are its parameters, and it obeys `plan_cache_mode`
-- exactly as these do. The function bodies above are still what sections 3 and 4 call, so the
-- correctness half is measured on the real thing.

PREPARE as_shipped_plan(text, int) AS
SELECT c.id, paradedb.score(c.id)
FROM plpgsql_prune.chunks c
WHERE c.id @@@ paradedb.boolean(must => ARRAY[
        paradedb.match('text', $1),
        paradedb.term('tenant_id', zenith_current_tenant()),
        paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
ORDER BY paradedb.score(c.id) DESC
LIMIT $2;

PREPARE candidate_plan(uuid, text, int) AS
SELECT c.id, paradedb.score(c.id)
FROM plpgsql_prune.chunks c
WHERE c.tenant_id = $1
  AND c.id @@@ paradedb.boolean(must => ARRAY[
        paradedb.match('text', $2),
        paradedb.term('tenant_id', $1),
        paradedb.boolean(should => ARRAY[paradedb.term('unlabelled', true)])])
ORDER BY paradedb.score(c.id) DESC
LIMIT $3;

\echo ''
\echo '=== 1. as shipped: how many partitions does the planner open ==='
\echo '(one `Custom Scan (ParadeDB Scan) on cNN` line per partition the planner opened)'
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE as_shipped_plan('de', 50);

\echo ''
\echo '=== 2. candidate: the tenant as a parameter, as a plpgsql local becomes ==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);

\echo ''
\echo '=== 3. do both still answer, and are the scores real ==='
\echo '(rows must match, and null_scores must be 0 on both: a NULL score is ADR 0002 F18)'
SELECT 'as_shipped' AS arm, count(*) AS rows,
       count(*) FILTER (WHERE score IS NULL) AS null_scores, round(max(score)::numeric, 4) AS top
FROM plpgsql_prune.as_shipped('de', 50)
UNION ALL
SELECT 'candidate', count(*),
       count(*) FILTER (WHERE score IS NULL), round(max(score)::numeric, 4)
FROM plpgsql_prune.candidate('de', 50);

\echo ''
\echo '=== 4. do they return the same passages, with the same scores, in the same order ==='
\echo '(`in_both` must be 50. `max_score_delta` must be 0: the top scores of the two arms'
\echo ' differed in an earlier run of this file, which would have meant the added qualifier'
\echo ' changed the ranking rather than only the plan — so it is compared per chunk here'
\echo ' rather than by taking a maximum over each side, which is what hid it.)'
WITH shipped AS (
  SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM plpgsql_prune.as_shipped('de', 50)
), cand AS (
  SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM plpgsql_prune.candidate('de', 50)
)
SELECT count(*) AS in_both,
       max(abs(s.score - c.score)) AS max_score_delta,
       count(*) FILTER (WHERE s.rank <> c.rank) AS rows_at_a_different_rank
FROM shipped s JOIN cand c ON c.chunk_id = s.chunk_id;

\echo ''
\echo '=== 4b. the same comparison with parallelism off ==='
\echo '(4 leaves a delta of about 3e-4 with the ranking unchanged. The candidate plan goes'
\echo ' parallel — `Gather Merge` over a `Parallel Custom Scan` — and BM25 scores are'
\echo ' computed against segment statistics, so two workers and one worker need not agree to'
\echo ' the last digit. If that is the cause the delta goes to zero here, and the added'
\echo ' qualifier is exonerated. If it does not, the qualifier changes scoring and the fix is'
\echo ' wrong. Asked rather than assumed, because "close enough" is how a scoring regression'
\echo ' gets shipped.)'
SET max_parallel_workers_per_gather = 0;
WITH shipped AS (
  SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM plpgsql_prune.as_shipped('de', 50)
), cand AS (
  SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM plpgsql_prune.candidate('de', 50)
)
SELECT count(*) AS in_both,
       max(abs(s.score - c.score)) AS max_score_delta,
       count(*) FILTER (WHERE s.rank <> c.rank) AS rows_at_a_different_rank
FROM shipped s JOIN cand c ON c.chunk_id = s.chunk_id;
RESET max_parallel_workers_per_gather;

\echo ''
\echo '=== 4c. where the delta comes from: read the two Tantivy queries ==='
\echo '(It is not parallelism — 4b holds the delta at the same value with parallelism off.'
\echo ' ParadeDB *pushes the SQL qualifier down into the Tantivy query* as one more `must`'
\echo ' clause, and a `must` clause contributes to the BM25 sum. So every passage in the'
\echo ' tenant gains the same constant, which is why 4 reports a delta and zero rows at a'
\echo ' different rank. Fusion downstream is RRF and reads ranks, not scores — ADR 0002 —'
\echo ' so a uniform shift changes nothing that is read. Printed rather than argued.)'
SET max_parallel_workers_per_gather = 0;
EXPLAIN (COSTS OFF)
SELECT c.id, paradedb.score(c.id) FROM plpgsql_prune.chunks c
WHERE c.id @@@ paradedb.boolean(must => ARRAY[
        paradedb.match('text', 'de'),
        paradedb.term('tenant_id', zenith_current_tenant())])
ORDER BY paradedb.score(c.id) DESC LIMIT 5;
EXPLAIN (COSTS OFF)
SELECT c.id, paradedb.score(c.id) FROM plpgsql_prune.chunks c
WHERE c.tenant_id = '00000000-0000-0000-0000-000000000003'::uuid
  AND c.id @@@ paradedb.boolean(must => ARRAY[
        paradedb.match('text', 'de'),
        paradedb.term('tenant_id', zenith_current_tenant())])
ORDER BY paradedb.score(c.id) DESC LIMIT 5;
RESET max_parallel_workers_per_gather;

\echo ''
\echo '=== 5. past the generic-plan threshold: nine executions, then ask again ==='
\echo '(Postgres plans custom-ly five times before it will consider a generic plan, and a'
\echo ' generic plan has no parameter values to prune on. A fix that works five times is not'
\echo ' a fix.)'
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);

\echo ''
\echo '=== 6. the state that would defeat it: a forced generic plan ==='
\echo '(`plan_cache_mode` is `auto` on this installation, and `auto` is a heuristic rather'
\echo ' than a promise. This is what the fix degrades to if that heuristic ever chooses'
\echo ' otherwise: runtime pruning, one partition executed, 256 planned.)'
SET plan_cache_mode = force_generic_plan;
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', 'de', 50);
RESET plan_cache_mode;

DEALLOCATE as_shipped_plan;
DEALLOCATE candidate_plan;
DROP SCHEMA plpgsql_prune CASCADE;
