-- Does pruning survive the real policy, as the application role, on guarded partitions?
--
-- `partition-pruning.sql` answers the narrower question and is honest about its scope: it
-- runs as the owner, with a bare `tenant_id = zenith_current_tenant()` predicate and no
-- policy at all. That was enough to retire the fear that killed the idea on paper — the key
-- comes from a `STABLE` function, so plan-time pruning cannot happen and it had to be
-- runtime pruning or nothing.
--
-- It is not enough for the plan that was written on top of it. Three things differ between
-- that probe and what ships, and each of them could plausibly have defeated pruning:
--
--   1. The real policy is a **conjunction**, not an equality: the tenant clause is ANDed with
--      `label_ids = '{}' OR label_ids && zenith_current_labels()`. A planner that cannot see
--      through the second half to the first would prune nothing.
--   2. It arrives as a **policy**, not as a WHERE clause the query author wrote. RLS
--      predicates are injected during planning, and being injected is not the same as being
--      written down.
--   3. It runs as **`zenith_app`**, which is the role the policy applies to. As the owner
--      the policy is not even consulted, so the owner-side probe never exercised the path.
--
-- And a fourth thing that only exists because of stage 0: every partition here carries
-- `ENABLE ROW LEVEL SECURITY` and its own copy of the policy, because a partition inherits
-- neither — see `partition-rls.sql`. So each partition is being pruned while carrying its
-- own predicate, which is the arrangement that will actually be deployed.
--
-- Measured: `Subplans Removed: 7` of 8, twice — once for the InitPlan and once for the scan
-- the ORDER BY drives — with the filter showing the full conjunction. Seven partitions
-- discarded at execution time.
--
-- This file exists because that result was first obtained by hand and quoted in a plan
-- without being written to disk, which is the rule this repository states last and means
-- first. A claim nobody can re-run is a claim somebody has to believe.
--
-- Run against the live database; creates and drops its own schema.

\set ON_ERROR_STOP on

DROP SCHEMA IF EXISTS prune_policy CASCADE;
CREATE SCHEMA prune_policy;

CREATE TABLE prune_policy.chunks (
  id bigint,
  tenant_id uuid NOT NULL,
  label_ids uuid[] NOT NULL DEFAULT '{}',
  unlabelled boolean GENERATED ALWAYS AS (label_ids = '{}'::uuid[]) STORED,
  embedding vector(1024)
) PARTITION BY LIST (tenant_id);

DO $do$ DECLARE t uuid; i int := 0; BEGIN
  FOR t IN SELECT ('00000000-0000-0000-0000-' || lpad(g::text, 12, '0'))::uuid
           FROM generate_series(1, 8) g LOOP
    EXECUTE format(
      'CREATE TABLE prune_policy.p%s PARTITION OF prune_policy.chunks FOR VALUES IN (%L)',
      i, t);
    i := i + 1;
  END LOOP;
END $do$;

-- Real vectors rather than random ones: the ORDER BY has to be a plan the planner would
-- actually produce, and a column of noise gets a different one.
INSERT INTO prune_policy.chunks (id, tenant_id, label_ids, embedding)
SELECT s.id,
       ('00000000-0000-0000-0000-' || lpad(((s.id % 8) + 1)::text, 12, '0'))::uuid,
       CASE WHEN s.id % 3 = 0
            THEN '{}'::uuid[]
            ELSE ARRAY['11111111-1111-1111-1111-111111111111'::uuid] END,
       s.embedding
FROM (SELECT row_number() OVER () AS id, embedding FROM chunk_embeddings) s;

-- The parent, and then every partition, exactly as `app.core.partitions.create_partition`
-- does it. Parent-only is the mistake `partition-rls.sql` demonstrates.
ALTER TABLE prune_policy.chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY iso ON prune_policy.chunks FOR SELECT USING (
  tenant_id = zenith_current_tenant()
  AND (label_ids = '{}' OR label_ids && zenith_current_labels()));

DO $do$ DECLARE r record; BEGIN
  FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'prune_policy' AND c.relispartition LOOP
    EXECUTE format('ALTER TABLE prune_policy.%I ENABLE ROW LEVEL SECURITY', r.relname);
    EXECUTE format(
      'CREATE POLICY iso ON prune_policy.%I FOR SELECT USING (tenant_id = '
      'zenith_current_tenant() AND (label_ids = ''{}'' OR label_ids && '
      'zenith_current_labels()))', r.relname);
  END LOOP;
END $do$;

GRANT USAGE ON SCHEMA prune_policy TO zenith_app;
GRANT SELECT ON prune_policy.chunks TO zenith_app;
ANALYZE prune_policy.chunks;

SET ROLE zenith_app;
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);
SELECT set_config('zenith.label_ids', '11111111-1111-1111-1111-111111111111', false);

\echo '=== the shape that ships: injected policy, app role, vector ORDER BY ==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
SELECT id FROM prune_policy.chunks
ORDER BY embedding <=> (SELECT embedding FROM prune_policy.chunks LIMIT 1)
LIMIT 5;

RESET ROLE;
DROP SCHEMA prune_policy CASCADE;
