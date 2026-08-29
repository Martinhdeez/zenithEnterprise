-- Can the *dense* half prune at plan time, and does a redundant tenant qualifier weaken RLS?
--
-- `unpruned-plpgsql-pruning.sql` answered the same question for the lexical half and answered
-- it for a `SECURITY DEFINER` function, where the tenant clause inside the body is the only
-- thing between one customer and another's passages. That is why it refused a tenant
-- parameter: a caller who may pass the tenant may pass somebody else's.
--
-- The dense half is a different function in exactly the way that matters. It has no reason to
-- be `SECURITY DEFINER` — there is no ParadeDB custom scan to keep alive, no owner-only
-- object to reach — so it can be `SECURITY INVOKER`, and a `SECURITY INVOKER` body runs under
-- the caller's policies. Stage 02 read the dense arm's 2,342 locks and 27.3 ms of planning at
-- modulus 256 as irreducible, on the reasoning that the dense predicate *is* the RLS policy
-- and handing the planner a constant there would break invariant 1. This file tries to prove
-- that reasoning wrong, and — more importantly — tries to prove the replacement wrong too.
--
-- ## The bars, written before the run
--
-- Bar 1 — it has to prune at *plan* time. One partition named per relation, no `Append`, no
--   `Subplans Removed`. A plan that names 32 partitions and removes 31 is the cost being
--   measured, not the fix.
--
-- Bar 2 — the rows have to be identical. The same 50 chunk ids at the same 50 ranks, and the
--   scores equal to the last bit. The lexical fix was allowed a uniform score shift because
--   ParadeDB folds the qualifier into the BM25 sum; cosine distance has no such excuse, so
--   here the bar is zero.
--
-- Bar 3 — the qualifier must never be the thing that decides. For every pair of (session
--   context, requested tenant) the arm must return the session tenant's rows on the diagonal
--   and **nothing at all** off it. A single row returned off the diagonal fails the whole
--   idea and no timing rescues it.
--
-- Bar 4 — Bar 3 has to survive the plan cache. Nine calls under tenant A, then tenant B on
--   the same backend, then a forced generic plan. A fix that isolates correctly until the
--   sixth execution is not a fix; it is a leak with a delay.
--
-- Bar 5 — labels. `chunk_embeddings` is tenant-scoped only and the join to `chunks` is what
--   carries label isolation (`search.py`). A tenant qualifier that let the join be dropped,
--   or that changed which labelled rows come back, would return passages from documents the
--   caller cannot open.
--
-- ## What section 6 is for
--
-- The honest way this could still be wrong is not a leak between tenants; it is that the
-- redundant qualifier becomes load-bearing on a path where the policy is absent. There is
-- exactly one such path in this system — `owner_session()` and `platform_session()` bypass
-- RLS — so section 6 runs both arms as a `BYPASSRLS` role and prints what each returns. The
-- qualifier does decide there. The question the section answers is which way it decides.
--
-- ## Measured, at MODULUS 32 over the real 13,549 passages, 8 synthetic tenants
--
--   as shipped   `Subplans Removed: 31` on both `Append`s — executor-startup pruning —
--                planning 3.929 ms, execution 2.395 ms, 231 locks
--   candidate     no `Append` at all: one partition per relation chosen while planning,
--                planning 0.222 ms, execution 0.862 ms, 14 locks
--
-- **The lock figures here are lower than `dense-plan-time.json`'s** — 231 against 297 at the
-- same modulus — and the difference is this file's index set, not its query. It builds the
-- three indexes the dense join can use and skips the bm25 and `tsv` indexes the installation
-- also carries on `chunks`. A lock count counts relations opened rather than relations useful,
-- so those two are locked in production and are not locked here. The JSON is the file to quote
-- a lock count from; this one is the file to read a plan in.
--   control      the same qualifier as the `STABLE` call rather than a local:
--                32 partitions, `Subplans Removed: 31`, planning 1.376 ms. The local is the
--                mechanism; the extra qualifier on its own buys nothing.
--
--   Bar 2  50 rows both arms, in_both 50, max_score_delta 0, 0 rows at a different rank
--   Bar 3  64 cells of the 8x8 grid: 400 rows on the diagonal, **0 off it**
--   Bar 4  nine calls as tenant 3 then tenant 5 on the same backend: tenant 5's 50 rows,
--          0 foreign; the same with `plan_cache_mode = force_generic_plan`: identical
--   Bar 5  under one label, both arms 50 rows, 0 at a different rank, 0 rows the caller
--          may not open
--
-- The forced-generic case is the honest limit of the *performance* claim and nothing more:
-- section 5b shows the plan falling back to `Subplans Removed: 31` and 1.039 ms of planning.
-- It is not a limit of the *isolation* claim — section 5 shows the generic plan isolating
-- exactly as the custom one does, because what isolates is the policy and the policy is in
-- both plans.
--
-- Built at MODULUS 32, which is enough to exhibit a plan; the modulus ladder and its lock
-- counts are `eval/dense-plan-time.json`. Read-only with respect to the corpus: everything is
-- inside `zenith_denseplan_iso`, dropped at the end.
--
--   docker exec -i zenith-db-1 psql -U zenith -d zenith -f dense-plan-time-pruning.sql

\set ON_ERROR_STOP on
\pset pager off

DROP SCHEMA IF EXISTS zenith_denseplan_iso CASCADE;
CREATE SCHEMA zenith_denseplan_iso;
GRANT USAGE ON SCHEMA zenith_denseplan_iso TO zenith_app;

CREATE TABLE zenith_denseplan_iso.emb (
  chunk_id          uuid NOT NULL,
  tenant_id         uuid NOT NULL,
  embedding_model   varchar NOT NULL,
  embedding_version varchar NOT NULL,
  embedding_half    halfvec(1024) NOT NULL
) PARTITION BY HASH (tenant_id);

CREATE TABLE zenith_denseplan_iso.chk (
  id           uuid NOT NULL,
  document_id  uuid NOT NULL,
  tenant_id    uuid NOT NULL,
  label_ids    uuid[] NOT NULL DEFAULT '{}',
  text         varchar NOT NULL
) PARTITION BY HASH (tenant_id);

ALTER TABLE zenith_denseplan_iso.emb
  ADD PRIMARY KEY (chunk_id, embedding_model, embedding_version, tenant_id);
CREATE INDEX ON zenith_denseplan_iso.emb
  USING hnsw (embedding_half halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
ALTER TABLE zenith_denseplan_iso.chk ADD PRIMARY KEY (id, tenant_id);
CREATE INDEX ON zenith_denseplan_iso.chk (tenant_id);
CREATE INDEX ON zenith_denseplan_iso.chk USING gin (label_ids);

ALTER TABLE zenith_denseplan_iso.emb ENABLE ROW LEVEL SECURITY;
ALTER TABLE zenith_denseplan_iso.chk ENABLE ROW LEVEL SECURITY;
CREATE POLICY emb_isolation ON zenith_denseplan_iso.emb
  USING (tenant_id = zenith_current_tenant());
CREATE POLICY chk_isolation ON zenith_denseplan_iso.chk
  USING (tenant_id = zenith_current_tenant()
         AND (label_ids = '{}' OR label_ids && zenith_current_labels()));
GRANT SELECT ON zenith_denseplan_iso.emb, zenith_denseplan_iso.chk TO zenith_app;

-- A partition inherits neither the enable nor the policy — CLAUDE.md's first invariant and
-- `eval/partition-rls.sql`. Both are given to every partition here, and not as decoration: a
-- policy on a partition is an expression the planner fetches and applies for every unpruned
-- child, so a ladder built without them understates planning at exactly the point being
-- watched.
DO $do$ DECLARE i int; BEGIN
  FOR i IN 0..31 LOOP
    EXECUTE format('CREATE TABLE zenith_denseplan_iso.e%s PARTITION OF zenith_denseplan_iso.emb'
                   ' FOR VALUES WITH (MODULUS 32, REMAINDER %s)', i, i);
    EXECUTE format('ALTER TABLE zenith_denseplan_iso.e%s ENABLE ROW LEVEL SECURITY', i);
    EXECUTE format('CREATE POLICY e%s_isolation ON zenith_denseplan_iso.e%s'
                   ' USING (tenant_id = zenith_current_tenant())', i, i);
    EXECUTE format('CREATE TABLE zenith_denseplan_iso.c%s PARTITION OF zenith_denseplan_iso.chk'
                   ' FOR VALUES WITH (MODULUS 32, REMAINDER %s)', i, i);
    EXECUTE format('ALTER TABLE zenith_denseplan_iso.c%s ENABLE ROW LEVEL SECURITY', i);
    EXECUTE format($p$CREATE POLICY c%s_isolation ON zenith_denseplan_iso.c%s
                      USING (tenant_id = zenith_current_tenant()
                             AND (label_ids = '{}' OR label_ids && zenith_current_labels()))$p$,
                   i, i);
  END LOOP;
END $do$;

-- Ground truth, outside RLS. Which tenant a chunk really belongs to is read from here and
-- never from the arm under test, so "did it leak" is answered by a table the arm cannot
-- filter. Eight synthetic tenants, and one label on every third chunk.
CREATE TABLE zenith_denseplan_iso.assign AS
SELECT chunk_id,
       ('00000000-0000-0000-0000-'
        || lpad(((('x' || substr(md5(chunk_id::text), 1, 8))::bit(32)::bigint
                  & 2147483647) % 8 + 1)::text, 12, '0'))::uuid AS tenant_id,
       CASE (('x' || substr(md5(chunk_id::text), 9, 8))::bit(32)::bigint & 2147483647) % 3
         WHEN 0 THEN ARRAY['aaaaaaaa-0000-0000-0000-000000000001'::uuid]
         WHEN 1 THEN ARRAY['bbbbbbbb-0000-0000-0000-000000000002'::uuid]
         ELSE '{}'::uuid[]
       END AS label_ids
FROM chunk_embeddings;
ALTER TABLE zenith_denseplan_iso.assign ADD PRIMARY KEY (chunk_id);
GRANT SELECT ON zenith_denseplan_iso.assign TO zenith_app;

INSERT INTO zenith_denseplan_iso.emb
  (chunk_id, tenant_id, embedding_model, embedding_version, embedding_half)
SELECT a.chunk_id, a.tenant_id, e.embedding_model, e.embedding_version, e.embedding_half
FROM zenith_denseplan_iso.assign a JOIN chunk_embeddings e ON e.chunk_id = a.chunk_id;

INSERT INTO zenith_denseplan_iso.chk (id, document_id, tenant_id, label_ids, text)
SELECT a.chunk_id, c.document_id, a.tenant_id, a.label_ids, c.text
FROM zenith_denseplan_iso.assign a JOIN chunks c ON c.id = a.chunk_id;

DO $do$ DECLARE r record; BEGIN
  FOR r IN SELECT c.oid::regclass AS rel FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'zenith_denseplan_iso' AND c.relkind = 'r'
             AND c.relname <> 'assign' AND pg_relation_size(c.oid) > 0
  LOOP EXECUTE 'ANALYZE ' || r.rel; END LOOP; END $do$;

-- ## The three arms
--
-- `as_shipped` is `search.dense()` with the schema changed and nothing else. `candidate` adds
-- two things and no others: the tenant is read from `zenith_current_tenant()` into a plpgsql
-- local, and that local appears as an ordinary SQL qualifier beside the join. The join itself
-- is untouched — dropping it would drop label isolation.
--
-- `forced` is not a candidate for anything. It takes the tenant as an argument, which is the
-- shape `unpruned-plpgsql-pruning.sql` refused, and it exists so sections 4 and 5 can make
-- the qualifier and the policy disagree on purpose. If the qualifier is ever the thing that
-- decides, this is the function that shows it.
CREATE FUNCTION zenith_denseplan_iso.as_shipped(q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
BEGIN
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM zenith_denseplan_iso.emb e
  JOIN zenith_denseplan_iso.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;

CREATE FUNCTION zenith_denseplan_iso.candidate(q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
DECLARE v_tenant uuid := zenith_current_tenant();
BEGIN
  IF v_tenant IS NULL THEN RETURN; END IF;
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM zenith_denseplan_iso.emb e
  JOIN zenith_denseplan_iso.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.tenant_id = v_tenant
    AND e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;

CREATE FUNCTION zenith_denseplan_iso.forced(
  v_tenant uuid, q halfvec(1024), mdl text, ver text, want int)
RETURNS TABLE(chunk_id uuid, score double precision)
LANGUAGE plpgsql STABLE SECURITY INVOKER
AS $body$
BEGIN
  RETURN QUERY
  SELECT c.id, 1 - (e.embedding_half <=> q)
  FROM zenith_denseplan_iso.emb e
  JOIN zenith_denseplan_iso.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
  WHERE e.tenant_id = v_tenant
    AND e.embedding_model = mdl AND e.embedding_version = ver
  ORDER BY e.embedding_half <=> q
  LIMIT want;
END;
$body$;

GRANT EXECUTE ON FUNCTION
  zenith_denseplan_iso.as_shipped(halfvec(1024), text, text, int),
  zenith_denseplan_iso.candidate(halfvec(1024), text, text, int),
  zenith_denseplan_iso.forced(uuid, halfvec(1024), text, text, int)
TO zenith_app;

-- The query vector, kept in a table as well as in a psql variable. psql does not interpolate
-- `:'vec'` inside a dollar-quoted body, so section 4's loop reads it from here; the `EXECUTE`
-- lines, which are ordinary statements, take the variable. One row, so the two can never be
-- different vectors.
CREATE TABLE zenith_denseplan_iso.probe AS
SELECT embedding_half AS q, embedding_model AS mdl, embedding_version AS ver
FROM zenith_denseplan_iso.emb
WHERE tenant_id = '00000000-0000-0000-0000-000000000003' LIMIT 1;
GRANT SELECT ON zenith_denseplan_iso.probe TO zenith_app;

SELECT q::text AS vec, mdl, ver FROM zenith_denseplan_iso.probe \gset

-- Everything below runs as the application role, with the policies in force. Querying as the
-- owner proves nothing here: the owner bypasses them, which is the whole subject.
SET ROLE zenith_app;
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);
SELECT set_config('zenith.label_ids', '', false);
SET hnsw.iterative_scan = relaxed_order;
-- Serial: a parallel plan over a few thousand partitions asks for a shared segment larger
-- than this container's /dev/shm and dies naming the wrong resource. `eval/partition_shape.py`
-- and `eval/modulus_cost.py` do the same, for the same reason.
SET max_parallel_workers_per_gather = 0;

-- `EXPLAIN` on a call does not descend into a plpgsql body — it reports `Function Scan` and
-- one number — so the plans are exhibited through `PREPARE`/`EXECUTE`. That is the same
-- machinery rather than an approximation: a plpgsql statement *is* an SPI prepared plan whose
-- locals are its parameters, and it obeys `plan_cache_mode` exactly as these do. The function
-- bodies above are still what sections 3 to 6 call, so correctness is measured on the real
-- thing.
PREPARE shipped_plan(halfvec(1024), text, text, int) AS
SELECT c.id, 1 - (e.embedding_half <=> $1) AS score
FROM zenith_denseplan_iso.emb e
JOIN zenith_denseplan_iso.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.embedding_model = $2 AND e.embedding_version = $3
ORDER BY e.embedding_half <=> $1 LIMIT $4;

PREPARE candidate_plan(uuid, halfvec(1024), text, text, int) AS
SELECT c.id, 1 - (e.embedding_half <=> $2) AS score
FROM zenith_denseplan_iso.emb e
JOIN zenith_denseplan_iso.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.tenant_id = $1
  AND e.embedding_model = $3 AND e.embedding_version = $4
ORDER BY e.embedding_half <=> $2 LIMIT $5;

-- The naive fix, and the control that says which half of the candidate does the work: the
-- same redundant qualifier, written as the `STABLE` function call instead of a parameter.
PREPARE guc_plan(halfvec(1024), text, text, int) AS
SELECT c.id, 1 - (e.embedding_half <=> $1) AS score
FROM zenith_denseplan_iso.emb e
JOIN zenith_denseplan_iso.chk c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id
WHERE e.tenant_id = zenith_current_tenant()
  AND e.embedding_model = $2 AND e.embedding_version = $3
ORDER BY e.embedding_half <=> $1 LIMIT $4;

\echo ''
\echo '=== 1. as shipped: what the planner opens (one Index Scan line per partition) ==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE shipped_plan(:'vec', :'mdl', :'ver', 50);

\echo ''
\echo '=== 2. candidate: the tenant as a parameter, as a plpgsql local becomes ==='
\echo '(Bar 1: one partition per relation, no Append, no Subplans Removed. And read the'
\echo ' One-Time Filter — that is the RLS policy, still there, reduced by the planner to a'
\echo ' single comparison between the session GUC and the parameter. It is section 4 in one'
\echo ' line: if they ever disagree the whole plan returns nothing.)'
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000003', :'vec', :'mdl', :'ver', 50);

\echo ''
\echo '=== 2b. control: the same qualifier written as the STABLE call rather than a local ==='
\echo '(If this prunes too, the local is doing nothing and the fix is simpler than claimed.'
\echo ' If it does not, the local is the whole mechanism.)'
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE guc_plan(:'vec', :'mdl', :'ver', 50);

\echo ''
\echo '=== 3. Bar 2: the same passages, the same ranks, the same scores ==='
\echo '(in_both must be 50, rows_at_a_different_rank 0, max_score_delta exactly 0.)'
WITH shipped AS (
  SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM zenith_denseplan_iso.as_shipped(:'vec', :'mdl', :'ver', 50)
), cand AS (
  SELECT chunk_id, score, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50)
)
SELECT (SELECT count(*) FROM shipped) AS shipped_rows,
       (SELECT count(*) FROM cand) AS candidate_rows,
       count(*) AS in_both,
       max(abs(s.score - c.score)) AS max_score_delta,
       count(*) FILTER (WHERE s.rank <> c.rank) AS rows_at_a_different_rank
FROM shipped s JOIN cand c ON c.chunk_id = s.chunk_id;

\echo ''
\echo '=== 4. Bar 3: the cross-tenant grid, with the qualifier and the policy disagreeing ==='
\echo '(`forced` takes the tenant as an argument, so every cell asks a session that is tenant'
\echo ' `context` for the rows of tenant `requested`. The diagonal must return rows and every'
\echo ' other cell must return none. `foreign_rows` is counted against `assign`, which no'
\echo ' policy filters, so a leak cannot hide behind the arm under test.)'
DO $probe$
DECLARE
  v_q    halfvec(1024);
  v_mdl  text;
  v_ver  text;
  ctx    uuid;
  req    uuid;
  n      int;
  wrong  int;
  worst  int := 0;
  cells  int := 0;
  diag   int := 0;
BEGIN
  SELECT q, mdl, ver INTO v_q, v_mdl, v_ver FROM zenith_denseplan_iso.probe;
  FOR ctx IN SELECT DISTINCT tenant_id FROM zenith_denseplan_iso.assign ORDER BY 1 LOOP
    PERFORM set_config('zenith.tenant_id', ctx::text, false);
    FOR req IN SELECT DISTINCT tenant_id FROM zenith_denseplan_iso.assign ORDER BY 1 LOOP
      SELECT count(*), count(*) FILTER (WHERE a.tenant_id <> ctx)
        INTO n, wrong
      FROM zenith_denseplan_iso.forced(req, v_q, v_mdl, v_ver, 50) f
      JOIN zenith_denseplan_iso.assign a ON a.chunk_id = f.chunk_id;
      cells := cells + 1;
      IF ctx = req THEN
        diag := diag + n;
      ELSIF n > 0 THEN
        worst := worst + n;
        RAISE WARNING 'LEAK: context % asked for % and got % rows (% foreign)', ctx, req, n, wrong;
      END IF;
    END LOOP;
  END LOOP;
  RAISE NOTICE 'cells=%  rows_on_the_diagonal=%  rows_off_the_diagonal=%', cells, diag, worst;
END
$probe$;

\echo ''
\echo '=== 4b. and the plan that makes it so ==='
\echo '(context is tenant 3, the parameter is tenant 5. The One-Time Filter is false, the'
\echo ' scan below it never runs, and the answer is empty rather than tenant 5.)'
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000005', :'vec', :'mdl', :'ver', 50);

\echo ''
\echo '=== 5. Bar 4: does isolation survive the plan cache ==='
\echo '(Nine calls as tenant 3 on this backend, then tenant 5 on the same backend, then the'
\echo ' same again with a generic plan forced. `foreign_rows` must be 0 in every row and the'
\echo ' tenant column must follow the context.)'
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);

SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000005', false);
SELECT 'after nine calls as tenant 3, now tenant 5' AS state,
       count(*) AS rows,
       count(DISTINCT a.tenant_id) AS distinct_tenants,
       min(a.tenant_id::text) AS tenant,
       count(*) FILTER (WHERE a.tenant_id <> '00000000-0000-0000-0000-000000000005') AS foreign_rows
FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50) f
JOIN zenith_denseplan_iso.assign a ON a.chunk_id = f.chunk_id;

SET plan_cache_mode = force_generic_plan;
SELECT 'the same, with a generic plan forced' AS state,
       count(*) AS rows,
       count(DISTINCT a.tenant_id) AS distinct_tenants,
       min(a.tenant_id::text) AS tenant,
       count(*) FILTER (WHERE a.tenant_id <> '00000000-0000-0000-0000-000000000005') AS foreign_rows
FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50) f
JOIN zenith_denseplan_iso.assign a ON a.chunk_id = f.chunk_id;

\echo ''
\echo '=== 5b. what a forced generic plan costs: the fix degrades to runtime pruning ==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY ON)
EXECUTE candidate_plan('00000000-0000-0000-0000-000000000005', :'vec', :'mdl', :'ver', 50);
RESET plan_cache_mode;

\echo ''
\echo '=== 5c. no tenant in the session at all ==='
\echo '(Both arms must return 0. The failure mode is inverted on purpose: a query with no'
\echo ' tenant returns nothing rather than everything.)'
SELECT set_config('zenith.tenant_id', '', false);
SELECT 'as_shipped' AS arm, count(*) AS rows
FROM zenith_denseplan_iso.as_shipped(:'vec', :'mdl', :'ver', 50)
UNION ALL
SELECT 'candidate', count(*)
FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);

\echo ''
\echo '=== 5d. Bar 5: labels, which arrive through the join and not through the qualifier ==='
\echo '(A third of the corpus carries label A, a third label B, a third none. With label A in'
\echo ' the session both arms must return the same rows and neither may return a label-B one.'
\echo ' `chunk_embeddings` is tenant-scoped only, so if the qualifier had made the join look'
\echo ' redundant to someone this is the row that would appear.)'
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);
SELECT set_config('zenith.label_ids', 'aaaaaaaa-0000-0000-0000-000000000001', false);
WITH shipped AS (
  SELECT chunk_id, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM zenith_denseplan_iso.as_shipped(:'vec', :'mdl', :'ver', 50)
), cand AS (
  SELECT chunk_id, row_number() OVER (ORDER BY score DESC, chunk_id) AS rank
  FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50)
)
SELECT (SELECT count(*) FROM shipped) AS shipped_rows,
       (SELECT count(*) FROM cand) AS candidate_rows,
       count(*) AS in_both,
       count(*) FILTER (WHERE s.rank <> c.rank) AS rows_at_a_different_rank,
       (SELECT count(*) FROM cand JOIN zenith_denseplan_iso.assign a USING (chunk_id)
         WHERE a.label_ids = ARRAY['bbbbbbbb-0000-0000-0000-000000000002'::uuid])
         AS rows_the_caller_may_not_open
FROM shipped s JOIN cand c ON c.chunk_id = s.chunk_id;
SELECT set_config('zenith.label_ids', '', false);

\echo ''
\echo '=== 5e. the locks each arm charges the caller ==='
\echo '(Counted inside the transaction that takes them, because they are released at commit.'
\echo ' The lock table is cluster-wide and shared with other work on this machine, so this is'
\echo ' a difference between two readings taken seconds apart rather than an absolute.)'
BEGIN;
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', true);
SELECT 'as_shipped, before' AS state, count(*) AS locks
FROM pg_locks WHERE pid = pg_backend_pid();
SELECT count(*) FROM zenith_denseplan_iso.as_shipped(:'vec', :'mdl', :'ver', 50);
SELECT 'as_shipped, after' AS state, count(*) AS locks
FROM pg_locks WHERE pid = pg_backend_pid();
COMMIT;

BEGIN;
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', true);
SELECT 'candidate, before' AS state, count(*) AS locks
FROM pg_locks WHERE pid = pg_backend_pid();
SELECT count(*) FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);
SELECT 'candidate, after' AS state, count(*) AS locks
FROM pg_locks WHERE pid = pg_backend_pid();
COMMIT;

RESET ROLE;

\echo ''
\echo '=== 6. the one path where the qualifier does decide: a BYPASSRLS role ==='
\echo '(`owner_session()` and `platform_session()` bypass RLS, so the policy is not there to'
\echo ' agree or disagree with. Run as the owner, `as_shipped` sees every tenant and the'
\echo ' candidate sees one. That is the qualifier deciding — and it decides *narrowly*: the'
\echo ' candidate is a subset of what the caller was already entitled to, never a superset.'
\echo ' The direction is the whole argument, so it is printed rather than asserted.)'
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);
SELECT 'as_shipped, as the owner' AS arm,
       count(*) AS rows, count(DISTINCT a.tenant_id) AS distinct_tenants
FROM zenith_denseplan_iso.as_shipped(:'vec', :'mdl', :'ver', 50) f
JOIN zenith_denseplan_iso.assign a ON a.chunk_id = f.chunk_id
UNION ALL
SELECT 'candidate, as the owner',
       count(*), count(DISTINCT a.tenant_id)
FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50) f
JOIN zenith_denseplan_iso.assign a ON a.chunk_id = f.chunk_id;

\echo ''
\echo '=== 6b. and the same as the owner with no tenant set at all ==='
\echo '(`as_shipped` answers from the whole corpus, which is what a CLI diagnostic wants.'
\echo ' The candidate returns nothing, because its guard clause refuses a NULL tenant. A'
\echo ' function shaped like this is therefore not a drop-in for an owner-side caller, and'
\echo ' that is a fact about where it may be used rather than a leak.)'
SELECT set_config('zenith.tenant_id', '', false);
SELECT 'as_shipped, owner, no tenant' AS arm, count(*) AS rows
FROM zenith_denseplan_iso.as_shipped(:'vec', :'mdl', :'ver', 50)
UNION ALL
SELECT 'candidate, owner, no tenant', count(*)
FROM zenith_denseplan_iso.candidate(:'vec', :'mdl', :'ver', 50);

DEALLOCATE ALL;
DROP SCHEMA zenith_denseplan_iso CASCADE;
