"""`chunks` and `chunk_embeddings` are partitioned by tenant.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-29

Every other lever in ADR 0009 shrinks the bytes a vector costs. This one is the only lever
that changes *N*: with one HNSW graph over the whole corpus, either the graph is resident or
every query reads a fraction of it, and a fraction of a very large graph is still millions of
vectors. A tenant holding 1.6M passages should search a 1.6M graph, and after this migration
it does — the graph is per partition, and `chunk_embeddings` is partitioned by `tenant_id`.

## `HASH` modulus 256, not `LIST` per tenant

`eval/partition-shape.json` measured both shapes at 10, 100, 1,000, 2,000, 5,000 and 10,000
partitions and they cost the same per partition: same `Subplans Removed`, same lock count,
planning time within noise of each other. What separates them is not a cost, it is a knob.
LIST forces partitions = tenants, so the partition count is set by how many customers have
been sold to; HASH decouples it, and the distinguishing measurement is that the widening
costs almost nothing precisely *because* the tenant distribution is skewed.

The thing that makes the partition count matter is the **lock table, and it fails during
planning**. A query takes `relations_per_partition x partitions` locks before it runs, and
`max_locks_per_transaction x max_connections` sizes one table shared by the whole cluster.
Over it, `psycopg.errors.OutOfMemory: out of shared memory` is raised while the plan is being
built: a 500 on an ordinary search, not a slow answer. `eval/partition-shape.json` has the
ladder, and `eval/partition-swap.json` has what this installation's schema actually costs
per partition afterwards, measured rather than derived — including the concurrency the
default `max_locks_per_transaction = 64` supports at this modulus, which is the number an
operator has to look at before raising it.

## `query_citations`, and why it gains a column instead of losing a foreign key

Partitioning by `tenant_id` forces the partition key into every unique constraint, so
`pk_chunks` becomes `(id, tenant_id)` and every foreign key pointing at `chunks.id` has to
become composite. `chunk_embeddings` already carries `tenant_id` and pays nothing.
`query_citations` does not carry it, and its row-level security is *derived* — `EXISTS
(SELECT 1 FROM queries q WHERE q.id = query_citations.query_id)` — so there was a real choice
between adding the column and dropping `fk_query_citations_chunk_id`.

**The column.** Three reasons, in the order they bind:

1. **The foreign key is load-bearing behaviour, not decoration.** `ON DELETE CASCADE` is what
   makes a citation disappear when its passage does — re-ingesting a document runs
   `_clear_previous`, and `DocumentService.delete` says in as many words that
   `query_citations` falls with the row. Without the constraint, deleting a document leaves
   citations pointing at chunk ids that no longer exist, and `analytics.py` and
   `DocumentService` both count through that join. The failure is a number that is quietly
   too big and a citation that opens nothing.

2. **`chunks.id` stops being unique the moment the primary key becomes composite.** Uniqueness
   is now `(id, tenant_id)`; nothing in the schema forbids two tenants sharing an id.
   `gen_random_uuid()` makes that collision negligible in practice, but "negligible in
   practice" is exactly the kind of guarantee this repository declines to move from the
   schema into a probability. With the composite foreign key it is not a probability at all:
   a citation can only reference a chunk of the *same tenant*, enforced by Postgres. That is
   strictly stronger than what `query_citations` had before this migration.

3. **Dropping a foreign key to make a migration easier is the quiet weakening this repository
   refuses.** It would be invisible in every test — the tests insert citations for chunks
   that exist — and would surface as an inflated count months later.

**The policy stays derived, and that is the more important half of the decision.** The
obvious follow-on — "there is a `tenant_id` column now, make it level 1 like
`chunk_embeddings`" — is wrong, and migration 0005 is why. `queries` is not tenant-scoped:
its policy is

    tenant_id = zenith_current_tenant()
    AND (zenith_reads_all_history() OR user_id = zenith_current_user_id())

because the questions people ask are more revealing than the documents they read. A citation
row names the passages one question retrieved, so `query_citations` inherits that per-user
narrowing through the `EXISTS`, and 0005 says so in a comment where the column would have
been added. Replacing it with `tenant_id = zenith_current_tenant()` would let any member of
the organisation read which passages a colleague's question pulled back — the substance of a
question they are not allowed to read. The new column is a foreign-key carrier and nothing
else, and `tests/integration/test_partition_rls_guard.py` is not what checks that;
`app/features/query/tests/test_history.py` is.

## The path: new table, backfill, swap — and it is interruptible

`ALTER TABLE ... PARTITION BY` does not exist. The only route is a second table, a copy and a
swap, and on an installation with a real corpus the copy and the index builds are the whole
cost. Every step below is written to be re-runnable, in the pattern 0022 and 0025 established
for `CREATE INDEX`: the expensive half can be done by hand, outside this migration and
outside its transaction, and the migration then finds the work done and only swaps.

    -- out of band, before the deployment window, as the owner:
    --   run _create_partitioned_chunks() and _create_partitioned_embeddings()'s DDL,
    --   then copy in batches rather than in one statement.
    -- the migration afterwards is the swap, and it is seconds.

Alembic runs a migration inside one transaction and DDL is transactional in Postgres, so an
interruption *during* the migration is a rollback to the schema that was there before. There
is no half-partitioned state to recover from; there is only "not started" and "done".

Two ordering decisions inside it:

- **Indexes are built after the backfill, not before.** Four indexes per `chunks` partition
  and one per `chunk_embeddings` partition maintained row by row through the copy is the
  slower way round, and for the BM25 and HNSW indexes it is very much the slower way round.
- **The label trigger is attached after the backfill.** `zenith_fill_chunk_labels` fills
  `label_ids` from the document when the incoming row has none. Copying rows verbatim is what
  a repartition means; a trigger that recomputes gives the new table a chance to disagree with
  the old one, and the disagreement would be invisible.

## Extension indexes on a partitioned parent, checked rather than assumed

`CREATE INDEX` on a partitioned parent creates one index per partition. That is btree
behaviour, and neither pg_search nor pgvector is btree. Both were probed on this stack —
ParadeDB 0.15.26, Postgres 17.5, pgvector 0.8 — before this migration was written, and both
recurse correctly: 256 `bm25` indexes and 256 `hnsw` indexes appear, one per partition, and
`@@@` plans as a `Merge Append` over per-partition `Custom Scan (ParadeDB Scan)` nodes that
merges correctly on `paradedb.score()`. The plan is in `eval/partition-swap.json`.

**One consequence that is not a defect but is a change in meaning.** BM25 scores are
corpus-relative: IDF is computed over the index. With one index per partition, a term's IDF
is computed over the tenant's own passages rather than over every customer's corpus at once.
For a multi-tenant product that is arguably the more correct denominator — a term common in
one customer's corpus should not be discounted in another's — but it is a different number
from the one the same query returned yesterday, and `score_bm25` in `query_citations` is
therefore not comparable across this migration.

## Grants

The parents are granted to `zenith_app` and `zenith_platform`; the partitions are granted to
nobody. Access through the parent needs privileges on the parent only, so this costs nothing
and closes the direct-partition path entirely — which is the path
`tests/integration/test_partition_rls_guard.py` exists because of. Every partition still gets
its own `ENABLE ROW LEVEL SECURITY` and its own policy, because a grant that arrives later
(`GRANT ... ON ALL TABLES IN SCHEMA public` is one statement) must not be enough to open it.
"""

from alembic import op
from app.core.partitions import LABEL_POLICY, TENANT_POLICY, create_hash_partition

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

#: Buckets per table. 512 relations of user data in total, and the number that decides how
#: many locks a search takes; `eval/partition-swap.json` records what that works out to on
#: this schema and what concurrency it leaves at the default `max_locks_per_transaction`.
MODULUS = 256

#: 0025's parameters, unchanged. A differently-tuned graph would make
#: `eval/quantisation.json` inapplicable to the thing this produces.
M = 16
EF_CONSTRUCTION = 64
DIMENSION = 1024

#: 0024's analyser, and 0022's column list. Both are copied here rather than imported
#: because a migration describes the schema at its own revision: if 0027 changes the
#: tokeniser again, this file must keep producing what 0026 produced.
BM25_COLUMNS = "(id, text, tenant_id, label_ids, unlabelled)"
BM25_TEXT_FIELDS = (
    '{"text": {"tokenizer": {"type": "stem", "language": "Spanish", "lowercase": true}}}'
)


def upgrade() -> None:
    # The migration rewrites two tables and builds 1,792 indexes. Neither belongs under the
    # installation's ordinary statement timeout, and the HNSW and BM25 builds spill at the
    # 64 MB default. `SET LOCAL` keeps both inside this transaction.
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")

    _create_partitioned_chunks()
    _create_partitioned_embeddings()
    _carry_tenant_into_query_citations()
    _backfill()
    _swap()
    _create_indexes()
    _name_the_partition_indexes()
    _wire_foreign_keys()
    _trigger_and_grants()


def _create_partitioned_chunks() -> None:
    """The parent, its 256 buckets, and the policy on every one of them.

    `LIKE ... INCLUDING GENERATED` rather than a retyped column list. `chunks.tsv` is
    `GENERATED ALWAYS AS (to_tsvector('zenith_text', text)) STORED` — `zenith_text`, the
    accent-insensitive configuration migration 0018 installed, and *not* the `'english'` the
    model still declares — and `unlabelled` is generated too. Retyping either from memory is
    how a repartition silently changes what the lexical half indexes; copying the definition
    from the live table cannot.
    """
    op.execute(
        """
        CREATE TABLE chunks_partitioned (
            LIKE chunks
              INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING COMMENTS INCLUDING STORAGE
        ) PARTITION BY HASH (tenant_id)
        """
    )
    # `(id, tenant_id)`, because the partition key must appear in every unique constraint.
    # Named for the swap and renamed back afterwards: a constraint name may repeat across
    # tables, but the index behind a primary key is a relation and its name may not.
    op.execute(
        "ALTER TABLE chunks_partitioned "
        "ADD CONSTRAINT pk_chunks_partitioned PRIMARY KEY (id, tenant_id)"
    )
    op.execute("ALTER TABLE chunks_partitioned ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY chunks_isolation ON chunks_partitioned "
        f"USING ({LABEL_POLICY}) WITH CHECK ({LABEL_POLICY})"
    )
    for remainder in range(MODULUS):
        create_hash_partition(
            "chunks_partitioned", f"chunks_p{remainder:03d}", MODULUS, remainder, LABEL_POLICY
        )


def _create_partitioned_embeddings() -> None:
    """The same, one level down, with the tenant-scoped policy 0001 gave this table.

    `TENANT_POLICY` and not `LABEL_POLICY`: `chunk_embeddings` has no `label_ids`, and label
    isolation for the dense half arrives through the join to `chunks` in `search.dense`.
    Giving the partitions a policy the parent does not have would be a difference nobody
    would find until a query returned less than it should.
    """
    op.execute(
        """
        CREATE TABLE chunk_embeddings_partitioned (
            LIKE chunk_embeddings
              INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING COMMENTS INCLUDING STORAGE
        ) PARTITION BY HASH (tenant_id)
        """
    )
    op.execute(
        "ALTER TABLE chunk_embeddings_partitioned ADD CONSTRAINT pk_chunk_embeddings_partitioned "
        "PRIMARY KEY (chunk_id, embedding_model, embedding_version, tenant_id)"
    )
    op.execute("ALTER TABLE chunk_embeddings_partitioned ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY chunk_embeddings_isolation ON chunk_embeddings_partitioned "
        f"USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
    )
    for remainder in range(MODULUS):
        create_hash_partition(
            "chunk_embeddings_partitioned",
            f"chunk_embeddings_p{remainder:03d}",
            MODULUS,
            remainder,
            TENANT_POLICY,
        )


def _carry_tenant_into_query_citations() -> None:
    """The column the composite foreign key needs, and nothing more than that.

    Filled from `queries` rather than from `chunks`, and that is the direction that makes it
    self-consistent: a citation's tenant is by definition its query's tenant, so there is no
    argument through which a caller can supply a wrong one. If the cited chunk belonged to a
    different tenant the foreign key added later would reject the row — which is the
    behaviour this column buys, not a problem with it.

    Added `NOT NULL` in two steps because a single `ADD COLUMN ... NOT NULL` with no default
    cannot succeed on a table that already has rows.
    """
    op.execute("ALTER TABLE query_citations ADD COLUMN tenant_id uuid")
    op.execute(
        "UPDATE query_citations c SET tenant_id = q.tenant_id "
        "FROM queries q WHERE q.id = c.query_id"
    )
    op.execute("ALTER TABLE query_citations ALTER COLUMN tenant_id SET NOT NULL")
    op.execute(
        "ALTER TABLE query_citations ADD CONSTRAINT fk_query_citations_tenant_id "
        "FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE"
    )


def _backfill() -> None:
    """The copy, with the generated columns left out and duplicates tolerated.

    Generated columns are not insertable, so `tsv` and `unlabelled` are absent from the
    `chunks` list and `embedding_half` from the embeddings list; Postgres recomputes all
    three from the same expressions, which is the point of them being generated.

    `ON CONFLICT DO NOTHING` so a copy already done out of band — see the module docstring —
    is completed rather than refused. It is a no-op on an installation that has not done one.
    """
    op.execute(
        """
        INSERT INTO chunks_partitioned (
            id, document_id, tenant_id, label_ids, page_num, char_start, char_end,
            bboxes, section, text, context_prefix, chunking_version)
        SELECT id, document_id, tenant_id, label_ids, page_num, char_start, char_end,
               bboxes, section, text, context_prefix, chunking_version
        FROM chunks
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO chunk_embeddings_partitioned (
            chunk_id, tenant_id, embedding_model, embedding_version, embedding)
        SELECT chunk_id, tenant_id, embedding_model, embedding_version, embedding
        FROM chunk_embeddings
        ON CONFLICT DO NOTHING
        """
    )


def _swap() -> None:
    """Drop the originals and take their names.

    The two foreign keys pointing at `chunks.id` are dropped by name rather than by
    `DROP TABLE ... CASCADE`. A cascade would remove exactly the same two constraints and
    would also remove anything else that had come to depend on the table since — silently,
    and in a migration whose whole subject is a foreign key somebody might have been tempted
    to drop.
    """
    op.execute("ALTER TABLE query_citations DROP CONSTRAINT fk_query_citations_chunk_id")
    op.execute("ALTER TABLE chunk_embeddings DROP CONSTRAINT fk_chunk_embeddings_chunk_id")
    op.execute("DROP TABLE chunk_embeddings")
    op.execute("DROP TABLE chunks")

    op.execute("ALTER TABLE chunks_partitioned RENAME TO chunks")
    op.execute("ALTER TABLE chunks RENAME CONSTRAINT pk_chunks_partitioned TO pk_chunks")
    op.execute("ALTER TABLE chunk_embeddings_partitioned RENAME TO chunk_embeddings")
    op.execute(
        "ALTER TABLE chunk_embeddings "
        "RENAME CONSTRAINT pk_chunk_embeddings_partitioned TO pk_chunk_embeddings"
    )


def _create_indexes() -> None:
    """One statement each, 1,792 indexes.

    Created on the parents after the swap so they take their production names directly, and
    after the backfill so the copy does not maintain them row by row.
    """
    op.execute("CREATE INDEX ix_chunks_label_ids ON chunks USING gin (label_ids)")
    op.execute("CREATE INDEX ix_chunks_tenant_id ON chunks (tenant_id)")
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    op.execute(
        f"""
        CREATE INDEX ix_chunks_bm25 ON chunks
          USING bm25 {BM25_COLUMNS}
          WITH (key_field = 'id', text_fields = '{BM25_TEXT_FIELDS}')
        """
    )
    op.execute(
        f"""
        CREATE INDEX ix_chunk_embeddings_hnsw_half ON chunk_embeddings
          USING hnsw (embedding_half halfvec_cosine_ops)
          WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})
        """
    )


#: Rename every partition's indexes after the parent index they belong to.
#:
#: Postgres names them from the partition and the column — `chunk_embeddings_p017_embedding_
#: half_idx`, `chunks_p017_id_text_tenant_id_label_ids_unlabelled_idx` — and a plan naming
#: those tells a reader nothing about *which* declared index was reached. That question is
#: the whole subject of `app/features/retrieval/tests/test_vector_index.py`, which exists
#: because migration 0025 left three queries ordering by a column the fp16 operator class
#: does not cover: the rows and the order were unchanged and only the plan said so. After
#: this, a scan of the vector index reads
#: `Index Scan using ix_chunk_embeddings_hnsw_half_p017`, and the check that was written for
#: one index keeps working against 256 without being loosened to a substring of a column
#: list.
#:
#: Driven off `pg_inherits` rather than off a name this migration predicts, because the
#: generated names are Postgres's to choose and predicting them is a way to rename the wrong
#: relation quietly.
_NAME_PARTITION_INDEXES = """
DO $do$
DECLARE entry record;
BEGIN
    FOR entry IN
        SELECT child.relname AS current_name,
               parent.relname || '_' || right(table_of.relname, 4) AS wanted
        FROM pg_inherits i
        JOIN pg_class child ON child.oid = i.inhrelid
        JOIN pg_class parent ON parent.oid = i.inhparent
        JOIN pg_index ix ON ix.indexrelid = child.oid
        JOIN pg_class table_of ON table_of.oid = ix.indrelid
        WHERE table_of.relname LIKE 'chunks\\_p%%'
           OR table_of.relname LIKE 'chunk\\_embeddings\\_p%%'
    LOOP
        EXECUTE format('ALTER INDEX %I RENAME TO %I', entry.current_name, entry.wanted);
    END LOOP;
END $do$
"""


def _name_the_partition_indexes() -> None:
    op.execute(_NAME_PARTITION_INDEXES)


def _wire_foreign_keys() -> None:
    """Every foreign key, including the three `LIKE` does not bring.

    **`CREATE TABLE ... (LIKE ...)` copies no foreign keys at all** — not with `INCLUDING
    CONSTRAINTS`, which copies `CHECK` constraints and nothing else. It copies types, `NOT
    NULL`, defaults and generation expressions, which is why the generated `tsv` survives,
    and it silently leaves `fk_chunks_document_id`, `fk_chunks_tenant_id` and
    `fk_chunk_embeddings_tenant_id` behind. This paragraph replaces one that asserted the
    opposite; `tests/integration/test_migrations.py::test_schema_matches_models` is what
    caught it, which is the whole reason that test compares the models against the database
    rather than against the migration that wrote it.

    The two keys into `chunks` become composite. Postgres expands a key that *references* a
    partitioned table into one constraint per referenced partition plus one on the parent,
    and clones it onto every partition of the referencing side, so these two statements are
    about 770 catalogue rows. That is a size, not a cost on the read path: the referential
    triggers fire on writes, and their check is `chunk_id = $1 AND tenant_id = $2`, which
    prunes to one partition like everything else here.
    """
    op.execute(
        "ALTER TABLE chunks ADD CONSTRAINT fk_chunks_document_id "
        "FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE chunks ADD CONSTRAINT fk_chunks_tenant_id "
        "FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE chunk_embeddings ADD CONSTRAINT fk_chunk_embeddings_tenant_id "
        "FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE chunk_embeddings ADD CONSTRAINT fk_chunk_embeddings_chunk_id "
        "FOREIGN KEY (chunk_id, tenant_id) REFERENCES chunks (id, tenant_id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE query_citations ADD CONSTRAINT fk_query_citations_chunk_id "
        "FOREIGN KEY (chunk_id, tenant_id) REFERENCES chunks (id, tenant_id) ON DELETE CASCADE"
    )


def _trigger_and_grants() -> None:
    """The label trigger, and privileges on the parents only.

    A row-level `BEFORE INSERT` trigger on a partitioned table recurses to every partition
    (Postgres 13 and later), so this is one statement rather than 256.

    Nothing is granted on the partitions. Reaching a row through the parent needs privileges
    on the parent alone, so the direct-partition path — the one a bare partition would leak
    through — is closed by there being no grant at all, on top of every partition carrying
    its own policy.
    """
    op.execute(
        "CREATE TRIGGER chunks_inherit_labels BEFORE INSERT ON chunks "
        "FOR EACH ROW EXECUTE FUNCTION zenith_fill_chunk_labels()"
    )
    for role in ("zenith_app", "zenith_platform"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO {role}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunk_embeddings TO {role}")


def downgrade() -> None:
    """Back to two ordinary tables, with the rows intact.

    Exact in the way that matters: no column is lost and no value is rounded. What does not
    come back is `query_citations.tenant_id`, which this migration added and which nothing
    before it read — and the physical layout of two graphs, which no query can observe.

    The one thing a downgrade cannot restore is a citation whose chunk was deleted while the
    schema was partitioned. There cannot be one: the composite foreign key cascaded it away
    at the time, which is the same thing the simple key would have done.
    """
    op.execute("SET LOCAL statement_timeout = 0")
    op.execute("SET LOCAL maintenance_work_mem = '512MB'")

    op.execute(
        """
        CREATE TABLE chunks_flat (
            LIKE chunks
              INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING COMMENTS INCLUDING STORAGE
        )
        """
    )
    op.execute("ALTER TABLE chunks_flat ADD CONSTRAINT pk_chunks_flat PRIMARY KEY (id)")
    op.execute("ALTER TABLE chunks_flat ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY chunks_isolation ON chunks_flat "
        f"USING ({LABEL_POLICY}) WITH CHECK ({LABEL_POLICY})"
    )
    op.execute(
        """
        CREATE TABLE chunk_embeddings_flat (
            LIKE chunk_embeddings
              INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING COMMENTS INCLUDING STORAGE
        )
        """
    )
    op.execute(
        "ALTER TABLE chunk_embeddings_flat ADD CONSTRAINT pk_chunk_embeddings_flat "
        "PRIMARY KEY (chunk_id, embedding_model, embedding_version)"
    )
    op.execute("ALTER TABLE chunk_embeddings_flat ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY chunk_embeddings_isolation ON chunk_embeddings_flat "
        f"USING ({TENANT_POLICY}) WITH CHECK ({TENANT_POLICY})"
    )

    op.execute(
        """
        INSERT INTO chunks_flat (
            id, document_id, tenant_id, label_ids, page_num, char_start, char_end,
            bboxes, section, text, context_prefix, chunking_version)
        SELECT id, document_id, tenant_id, label_ids, page_num, char_start, char_end,
               bboxes, section, text, context_prefix, chunking_version
        FROM chunks
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO chunk_embeddings_flat (
            chunk_id, tenant_id, embedding_model, embedding_version, embedding)
        SELECT chunk_id, tenant_id, embedding_model, embedding_version, embedding
        FROM chunk_embeddings
        ON CONFLICT DO NOTHING
        """
    )

    op.execute("ALTER TABLE query_citations DROP CONSTRAINT fk_query_citations_chunk_id")
    op.execute("ALTER TABLE chunk_embeddings DROP CONSTRAINT fk_chunk_embeddings_chunk_id")
    op.execute("DROP TABLE chunk_embeddings")
    op.execute("DROP TABLE chunks")

    op.execute("ALTER TABLE chunks_flat RENAME TO chunks")
    op.execute("ALTER TABLE chunks RENAME CONSTRAINT pk_chunks_flat TO pk_chunks")
    op.execute("ALTER TABLE chunk_embeddings_flat RENAME TO chunk_embeddings")
    op.execute(
        "ALTER TABLE chunk_embeddings "
        "RENAME CONSTRAINT pk_chunk_embeddings_flat TO pk_chunk_embeddings"
    )

    op.execute("CREATE INDEX ix_chunks_label_ids ON chunks USING gin (label_ids)")
    op.execute("CREATE INDEX ix_chunks_tenant_id ON chunks (tenant_id)")
    op.execute("CREATE INDEX ix_chunks_tsv ON chunks USING gin (tsv)")
    op.execute(
        f"""
        CREATE INDEX ix_chunks_bm25 ON chunks
          USING bm25 {BM25_COLUMNS}
          WITH (key_field = 'id', text_fields = '{BM25_TEXT_FIELDS}')
        """
    )
    op.execute(
        f"""
        CREATE INDEX ix_chunk_embeddings_hnsw_half ON chunk_embeddings
          USING hnsw (embedding_half halfvec_cosine_ops)
          WITH (m = {M}, ef_construction = {EF_CONSTRUCTION})
        """
    )

    # The same three `LIKE` does not bring, restored on the way back for the same reason.
    op.execute(
        "ALTER TABLE chunks ADD CONSTRAINT fk_chunks_document_id "
        "FOREIGN KEY (document_id) REFERENCES documents (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE chunks ADD CONSTRAINT fk_chunks_tenant_id "
        "FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE chunk_embeddings ADD CONSTRAINT fk_chunk_embeddings_tenant_id "
        "FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE chunk_embeddings ADD CONSTRAINT fk_chunk_embeddings_chunk_id "
        "FOREIGN KEY (chunk_id) REFERENCES chunks (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE query_citations ADD CONSTRAINT fk_query_citations_chunk_id "
        "FOREIGN KEY (chunk_id) REFERENCES chunks (id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE query_citations DROP CONSTRAINT fk_query_citations_tenant_id")
    op.execute("ALTER TABLE query_citations DROP COLUMN tenant_id")

    op.execute(
        "CREATE TRIGGER chunks_inherit_labels BEFORE INSERT ON chunks "
        "FOR EACH ROW EXECUTE FUNCTION zenith_fill_chunk_labels()"
    )
    for role in ("zenith_app", "zenith_platform"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO {role}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunk_embeddings TO {role}")
