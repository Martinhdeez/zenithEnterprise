-- Do partitions inherit the parent's row-level security? They do not.
--
-- Partitioning by tenant is the structural route to the corpus sizes this product is aimed
-- at, and it is the only route that attacks the HNSW graph's own recall degradation rather
-- than only the index's size. Before any of that is worth planning, one thing has to be
-- true: RLS must still be the only access control.
--
-- It is, through the parent, and it is not, through a partition. Measured, not assumed:
--
--     relname | rls_enabled | own_policies
--     chunks  | t           | 1
--     p2      | f           | 0
--
-- With `zenith.tenant_id` set to tenant 1, a query against the parent returns tenant 1's
-- 10,000 rows, correctly. A query against partition `p2` returns tenant 3's 10,000 rows.
-- The parent's policy does not reach it. A partition is an ordinary table with its own
-- grants and its own (absent) policies.
--
-- The exposure needs a grant on the partition, and nothing grants one today. But
-- `GRANT ... ON ALL TABLES IN SCHEMA public TO zenith_app` would, and so would a migration
-- written by somebody who assumed inheritance — which is the assumption this file exists to
-- kill. It is invariant 1 broken by a schema change, in a product whose whole claim is that
-- isolation is enforced by the database.
--
-- So a partitioned schema needs, as a hard prerequisite:
--   1. ENABLE ROW LEVEL SECURITY and the policy on EVERY partition, not only the parent, so
--      the failure is closed even when a grant leaks;
--   2. a test that enumerates partitions and asserts both, in the shape of the existing
--      SECURITY DEFINER allowlist test — a list somebody has to justify rather than a
--      convention somebody has to remember.
--
-- Run against the live database; creates and drops its own schema.

\set ON_ERROR_STOP on

DROP SCHEMA IF EXISTS rls_part CASCADE;
CREATE SCHEMA rls_part;

CREATE TABLE rls_part.chunks (
  id bigint, tenant_id uuid NOT NULL, label_ids uuid[] NOT NULL DEFAULT '{}', body text
) PARTITION BY LIST (tenant_id);

DO $do$ DECLARE t uuid; i int := 0; BEGIN
  FOR t IN SELECT ('00000000-0000-0000-0000-'||lpad(g::text,12,'0'))::uuid
           FROM generate_series(1,6) g LOOP
    EXECUTE format('CREATE TABLE rls_part.p%s PARTITION OF rls_part.chunks FOR VALUES IN (%L)',
                   i, t);
    i := i + 1;
  END LOOP;
END $do$;

INSERT INTO rls_part.chunks
SELECT g, ('00000000-0000-0000-0000-'||lpad(((g % 6)+1)::text,12,'0'))::uuid, '{}', 'row '||g
FROM generate_series(1, 60000) g;

-- The project's own policy template, on the PARENT only — the mistake being demonstrated.
ALTER TABLE rls_part.chunks ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON rls_part.chunks FOR SELECT USING (
  tenant_id = zenith_current_tenant()
  AND (label_ids = '{}' OR label_ids && zenith_current_labels()));

GRANT USAGE ON SCHEMA rls_part TO zenith_app;
GRANT SELECT ON rls_part.chunks TO zenith_app;
GRANT SELECT ON rls_part.p2 TO zenith_app;   -- the grant a careless migration would make
ANALYZE rls_part.chunks;

\echo '=== what each relation actually carries ==='
SELECT relname, relrowsecurity AS rls_enabled,
       (SELECT count(*) FROM pg_policy WHERE polrelid = c.oid) AS own_policies
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'rls_part' AND relname IN ('chunks', 'p2');

SET ROLE zenith_app;
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000001', false);
SELECT set_config('zenith.label_ids', '', false);

\echo '=== context is tenant 1; p2 holds tenant 3 ==='
SELECT count(*) AS via_parent FROM rls_part.chunks;
SELECT count(*) AS via_partition_direct FROM rls_part.p2;
SELECT DISTINCT tenant_id::text AS whose_rows_came_back FROM rls_part.p2;

RESET ROLE;
DROP SCHEMA rls_part CASCADE;
