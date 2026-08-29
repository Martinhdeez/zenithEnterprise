-- Why a query vector cannot reach the wrong basis, demonstrated rather than asserted.
--
-- Stage 04 of ceiling 3 projects the embedding space from 1024 to 512 dimensions. The
-- projection itself is arithmetic and `dimensions.json` already priced it: -2.0 points of
-- index recall@10, worst question unchanged at 0.50, no question worse by more than 0.1.
--
-- The part that needed proving is the other one. A stored vector projected with one basis and
-- a query vector projected with another is *confident nonsense* — the failure ADR 0002 already
-- names for mixing embedding models, and the one this repository refuses to leave to a
-- convention somebody has to remember.
--
-- This file is the evidence that it is not a convention. Run it against any Postgres carrying
-- pgvector and read the last three blocks: every way of pairing a vector with the wrong basis
-- is a hard error at the first row touched, raised by pgvector's own type check.
--
--   docker cp backend/eval/svd-basis.sql zenith-db-1:/tmp/
--   docker exec zenith-db-1 psql -U zenith -d postgres -c 'CREATE DATABASE svd_probe'
--   docker exec zenith-db-1 psql -U zenith -d svd_probe -f /tmp/svd-basis.sql
--
-- Recorded output is in `svd-512.json` under `gate`. Nothing here touches a real corpus: it
-- builds its own rows, and a scratch database is dropped afterwards.

\set ON_ERROR_STOP off
\timing on

-- ---------------------------------------------------------------------------------------
-- One column, two dimensions.
--
-- `embedding` loses its typmod so that a row's width is whatever its space says it is. That
-- is what lets the 1024 space and the 512 space be resident at the same time, which is what
-- `embedding_spaces` exists for and what makes a reindex something other than an outage.
-- ---------------------------------------------------------------------------------------
DROP TABLE IF EXISTS probe;
CREATE TABLE probe (id int, version text, e halfvec);

INSERT INTO probe
SELECT g, '1', (SELECT array_agg(random()::real) FROM generate_series(1, 1024))::vector::halfvec
FROM generate_series(1, 2000) g;
INSERT INTO probe
SELECT g, '2', (SELECT array_agg(random()::real) FROM generate_series(1, 512))::vector::halfvec
FROM generate_series(1, 2000) g;

\echo ''
\echo '=== two spaces, one column ==='
SELECT version, count(*), min(vector_dims(e::vector)) AS dim FROM probe GROUP BY 1 ORDER BY 1;

-- One partial expression index per space. The predicate is the space filter `dense()` already
-- emits, so naming a space is what selects its index.
CREATE INDEX probe_v1 ON probe USING hnsw ((e::halfvec(1024)) halfvec_cosine_ops) WHERE version = '1';
CREATE INDEX probe_v2 ON probe USING hnsw ((e::halfvec(512))  halfvec_cosine_ops) WHERE version = '2';

SELECT '[' || array_to_string(array_fill(0.0123::real, ARRAY[512]),  ',') || ']' AS q512  \gset
SELECT '[' || array_to_string(array_fill(0.0123::real, ARRAY[1024]), ',') || ']' AS q1024 \gset

-- ---------------------------------------------------------------------------------------
-- Both spaces are searchable, and each uses its own index.
--
-- Read the plan, not the timing. A projection that silently fell back to a sequential scan
-- would return exactly the right rows and would be invisible to every test in this
-- repository — the failure mode `search.dense`'s docstring already documents for the
-- halfvec cast of migration 0025.
-- ---------------------------------------------------------------------------------------
\echo ''
\echo '=== A. 512-d query, 512 space -> Index Scan using probe_v2 ==='
EXPLAIN (COSTS OFF)
SELECT id FROM probe WHERE version = '2'
ORDER BY e::halfvec(512) <=> :'q512'::halfvec(512) LIMIT 10;

\echo ''
\echo '=== B. 1024-d query, 1024 space -> Index Scan using probe_v1 ==='
EXPLAIN (COSTS OFF)
SELECT id FROM probe WHERE version = '1'
ORDER BY e::halfvec(1024) <=> :'q1024'::halfvec(1024) LIMIT 10;

-- ---------------------------------------------------------------------------------------
-- The gate. Three ways to mix a vector with the wrong basis, three hard errors.
--
-- `ON_ERROR_STOP` is off so all three run: each one is the evidence, and a file that stopped
-- at the first would only prove one of them.
-- ---------------------------------------------------------------------------------------
\echo ''
\echo '=== C. unprojected 1024-d query aimed at the 512 space -> ERROR ==='
SELECT id FROM probe WHERE version = '2'
ORDER BY e::halfvec(512) <=> :'q1024'::halfvec(1024) LIMIT 10;

\echo ''
\echo '=== D. projected 512-d query aimed at the 1024 space -> ERROR ==='
SELECT id FROM probe WHERE version = '1'
ORDER BY e::halfvec(1024) <=> :'q512'::halfvec(512) LIMIT 10;

\echo ''
\echo '=== E. correctly sized query, wrong space in the filter -> ERROR ==='
SELECT id FROM probe WHERE version = '1'
ORDER BY e::halfvec(512) <=> :'q512'::halfvec(512) LIMIT 10;

-- ---------------------------------------------------------------------------------------
-- The projection itself, and what it costs.
--
-- 512 inner products against the axes of one space. In SQL rather than numpy, for two
-- reasons that both matter: `pyproject.toml` keeps numpy in the `eval` group so the shipped
-- image does not carry it, and — the larger one — a basis that lives only in the database is
-- a basis with no second copy to drift. Query vectors and stored vectors go through the same
-- rows of the same table because there is nowhere else for either of them to go.
--
-- `<#>` is *negative* inner product in pgvector, hence the negation.
-- ---------------------------------------------------------------------------------------
DROP TABLE IF EXISTS axes;
CREATE TABLE axes (component int PRIMARY KEY, axis vector(1024));
INSERT INTO axes
SELECT c, (SELECT array_agg(((c * 7 + i) % 13 - 6)::real / 7.0 ORDER BY i)
           FROM generate_series(1, 1024) i)::vector
FROM generate_series(0, 511) c;

CREATE OR REPLACE FUNCTION probe_project(v vector) RETURNS halfvec AS $$
  SELECT (array_agg((a.axis <#> v) * -1 ORDER BY a.component))::real[]::vector::halfvec
  FROM axes a
$$ LANGUAGE sql STABLE PARALLEL SAFE;

\echo ''
\echo '=== F. the projection agrees with a dot product computed by hand ==='
WITH v AS (SELECT (SELECT array_agg((i % 5)::real) FROM generate_series(1, 1024) i)::vector AS q)
SELECT vector_dims(probe_project(q)::vector) AS dims,
       round(((probe_project(q)::vector::real[])[1])::numeric, 4) AS component_0_in_sql,
       round((SELECT sum(a * b)
              FROM unnest((SELECT axis FROM axes WHERE component = 0)::real[]) WITH ORDINALITY t1(a, i)
              JOIN unnest(q::real[]) WITH ORDINALITY t2(b, j) ON i = j)::numeric, 4) AS by_hand
FROM v;

\echo ''
\echo '=== G. hot path: one query vector projected. This is what /search pays. ==='
SELECT probe_project((SELECT array_agg(random()::real) FROM generate_series(1, 1024))::vector) IS NOT NULL;
SELECT probe_project((SELECT array_agg(random()::real) FROM generate_series(1, 1024))::vector) IS NOT NULL;
SELECT probe_project((SELECT array_agg(random()::real) FROM generate_series(1, 1024))::vector) IS NOT NULL;

\echo ''
\echo '=== H. backfill: 13,549 vectors, the size of this corpus. What a reindex pays. ==='
DROP TABLE IF EXISTS src;
CREATE TABLE src (id int, e vector(1024));
INSERT INTO src SELECT g, (SELECT array_agg(random()::real) FROM generate_series(1, 1024))::vector
FROM generate_series(1, 13549) g;
CREATE TABLE dst AS SELECT id, probe_project(e) AS e512 FROM src;
SELECT count(*), min(vector_dims(e512::vector)) AS dim FROM dst;
