-- Does Postgres prune partitions when the key comes from a STABLE function?
--
-- This is the question that decides whether partitioning by tenant is available to this
-- product at all. Partitioning is the only remaining route to the million-document target
-- now that binary quantisation has been measured and rejected, and its whole value is that
-- a query touches one tenant's partition instead of the shared graph.
--
-- But the tenant does not arrive as a literal. It arrives from `zenith_current_tenant()`,
-- which reads a GUC and is therefore STABLE, not IMMUTABLE. Plan-time pruning needs a
-- constant. If Postgres cannot prune here, every query planned against a partitioned table
-- would scan every partition and the whole idea is dead on arrival.
--
-- Run against the live database; creates and drops its own schema.

\set ON_ERROR_STOP on
\timing on

DROP SCHEMA IF EXISTS prune_probe CASCADE;
CREATE SCHEMA prune_probe;

CREATE TABLE prune_probe.chunks (
  id         bigint,
  tenant_id  uuid NOT NULL,
  embedding  vector(1024)
) PARTITION BY LIST (tenant_id);

-- Eight tenants, so "one partition scanned" and "all partitions scanned" are far apart in
-- any plan output.
DO $do$
DECLARE t uuid; i int := 0;
BEGIN
  FOR t IN SELECT ('00000000-0000-0000-0000-' || lpad(g::text, 12, '0'))::uuid
           FROM generate_series(1, 8) g LOOP
    EXECUTE format(
      'CREATE TABLE prune_probe.p%s PARTITION OF prune_probe.chunks FOR VALUES IN (%L)',
      i, t);
    i := i + 1;
  END LOOP;
END $do$;

INSERT INTO prune_probe.chunks (id, tenant_id, embedding)
SELECT s.id,
       ('00000000-0000-0000-0000-' || lpad(((s.id % 8) + 1)::text, 12, '0'))::uuid,
       s.embedding
FROM (SELECT row_number() OVER () AS id, embedding FROM chunk_embeddings) s;

ANALYZE prune_probe.chunks;

SELECT count(*) AS rows, count(DISTINCT tenant_id) AS tenants FROM prune_probe.chunks;

\echo ''
\echo '=== 1. literal tenant: plan-time pruning, the easy case ==='
EXPLAIN (COSTS OFF)
SELECT id FROM prune_probe.chunks
WHERE tenant_id = '00000000-0000-0000-0000-000000000003'::uuid
LIMIT 5;

\echo ''
\echo '=== 2. STABLE function reading a GUC: the case that decides it ==='
SELECT set_config('zenith.tenant_id', '00000000-0000-0000-0000-000000000003', false);
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
SELECT id FROM prune_probe.chunks
WHERE tenant_id = zenith_current_tenant()
LIMIT 5;

\echo ''
\echo '=== 3. the same, inside a prepared statement (what the driver actually sends) ==='
PREPARE by_tenant AS
  SELECT id FROM prune_probe.chunks WHERE tenant_id = zenith_current_tenant() LIMIT 5;
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF) EXECUTE by_tenant;

\echo ''
\echo '=== 4. and under the real policy shape, which is what ships ==='
EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, SUMMARY OFF)
SELECT id FROM prune_probe.chunks
WHERE tenant_id = zenith_current_tenant()
ORDER BY embedding <=> (SELECT embedding FROM chunk_embeddings LIMIT 1)
LIMIT 5;

DROP SCHEMA prune_probe CASCADE;
