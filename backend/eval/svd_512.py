# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under `app/`
# relaxes strictness.

"""Fit the `svd_512` basis, install it, reproject a corpus, and check the gate holds.

`dimensions.py` answered *whether* 512 dimensions are affordable and its answer is not
re-derived here: -2.0 points of index recall@10, the worst question unchanged at 0.50, and no
question worse by more than 0.1. This file answers the two questions that come after it —
*does the mechanism work on a real corpus*, and *is the plan still the one we think it is*.

    python -m eval svd512 --database zenith_svd            # fit, install, reproject, measure

**It writes.** It is the only file under `eval/` that does, and it must never be pointed at
the installation holding the user's corpus. `--database` has no default for that reason. The
intended target is a scratch copy in the same cluster; the `finally` drops the space it built,
and the `finally` does not run if the process is killed.

## The bars, written before the run

1. **The plan uses the vector index, at both widths.** Not "recall came out acceptable" —
   recall survives a sequential scan and the product does not. `plan_uses_index` is recorded
   for the 1024 space and for the 512 space, and the plan text is kept, not just the verdict.
2. **The gate errors.** All three ways of pairing a vector with the wrong basis must raise.
   A run in which they returned rows is a run that proved nothing.
3. **The projection is the one `dimensions.py` measured.** `svd_1024` is a rotation that
   discards nothing, so projecting at full width and searching must return what the identity
   space returns. If it does not, the basis is wrong and no figure below means anything —
   the plumbing check `dimensions.py` uses, applied to the shipped path instead of a numpy
   array.
4. **`bytes_per_vector` at 512 is about half the 1024 figure.** `dimensions.json` measured
   1,365.8 against 2,729.9 on freshly built arms; this rebuilds both on the real corpus.

A failed bar stays in the file.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

REPORT = Path(__file__).parent / "svd-512.json"

MODEL = "BAAI/bge-m3"
SOURCE_VERSION = "1"
#: The projected space. A new *version* of the same model, because that is what it is: the
#: same embeddings, read in a different basis. `pk_chunk_embeddings` already carries the
#: version, so one chunk holds a row in each space with no schema change at all.
TARGET_VERSION = "2"
TARGET_DIMENSION = 512
SOURCE_DIMENSION = 1024

M = 16
EF_CONSTRUCTION = 64
EF_SEARCH = 100


def _fit(matrix: Any) -> Any:
    """The eigenbasis of the **uncentred** second moment `X'X / n`.

    Character for character the transform `dimensions.py` fits for its `svd` family, and it
    has to be: the numbers this stage was approved on came from that code, and a second
    implementation that differed by one subtraction would be measuring something else while
    quoting `dimensions.json`.

    **No mean is subtracted, and that is the whole argument.** Centring is not a rotation. It
    moves every point and changes every norm, and cosine similarity is a function of the
    norms. `dimensions.json` measures `pca_1024` — a rotation onto a full-rank centred basis,
    discarding *nothing* — at 0.7800 index recall@10 against `identity_1024`'s 0.9667. That
    18.7-point gap is the cost of the subtraction, not of the reduction. Anything that
    re-fits this basis and finds itself writing `matrix - matrix.mean(axis=0)` has just
    thrown away nine times what the whole stage buys.

    `eigh` of a 1024x1024 symmetric matrix rather than an SVD of the data, so the cost does
    not care whether there are 13,549 rows or a million. The basis is **nested**: the first
    `k` columns are exactly the basis that would have been fitted for `k`.
    """
    import numpy as np

    moment = (matrix.T @ matrix) / len(matrix)
    values, vectors = np.linalg.eigh(moment)
    order = np.argsort(values)[::-1]
    values, basis = values[order], vectors[:, order]
    total = float(values.sum())
    kept = float(values[:TARGET_DIMENSION].sum() / total)
    return basis.astype(np.float32), kept


async def _corpus(conn: AsyncConnection) -> Any:
    """Every stored embedding of the source space, as fp32, in a stable order."""
    import numpy as np

    rows = (
        await conn.execute(
            text(
                "SELECT embedding::text FROM chunk_embeddings "
                "WHERE embedding_model = :model AND embedding_version = :version "
                "ORDER BY chunk_id"
            ),
            {"model": MODEL, "version": SOURCE_VERSION},
        )
    ).all()
    matrix = np.empty((len(rows), SOURCE_DIMENSION), dtype=np.float32)
    for position, row in enumerate(rows):
        matrix[position] = np.fromstring(row[0][1:-1], sep=",", dtype=np.float32)
    return matrix


def _digest(basis: Any) -> str:
    """sha256 over the axes as they will be stored.

    Over `tobytes()` of the fp32 array, so it identifies the *fit* rather than the text
    formatting of the insert. Two installations running the same model with differently
    fitted bases produce silently different neighbours, and this is the cheap way to ask.
    """
    return hashlib.sha256(basis[:, :TARGET_DIMENSION].copy(order="C").tobytes()).hexdigest()


async def _install(conn: AsyncConnection, basis: Any, digest: str) -> None:
    """Register the space, then its axes, in one transaction.

    Order matters: the foreign key runs from an axis to its space, so a basis cannot exist
    without a space to belong to. The space is `building` while it is filled — `active` is
    what `search.dense` reads, and a half-filled space that answered queries would be exactly
    the confident nonsense this stage exists to prevent.
    """
    await conn.execute(
        text(
            "INSERT INTO embedding_spaces "
            "(model, version, dimension, status, source_dimension, basis_digest) "
            "VALUES (:model, :version, :dim, 'building', :source, :digest)"
        ),
        {
            "model": MODEL,
            "version": TARGET_VERSION,
            "dim": TARGET_DIMENSION,
            "source": SOURCE_DIMENSION,
            "digest": digest,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO embedding_space_axes (model, version, component, axis) "
            "VALUES (:model, :version, :component, :axis)"
        ),
        [
            {
                "model": MODEL,
                "version": TARGET_VERSION,
                "component": component,
                "axis": "[" + ",".join(map(repr, basis[:, component].tolist())) + "]",
            }
            for component in range(TARGET_DIMENSION)
        ],
    )


#: The backfill, and **the tenant is a bound parameter on purpose**.
#:
#: `docs/partitioning-unpruned-surface.md`: a read prunes on `zenith_current_tenant()` and a
#: write does not. `UPDATE` and `DELETE` — and the `INSERT` half of this — choose their result
#: relations at *plan* time, and plan-time pruning needs a constant; `zenith_current_tenant()`
#: is `STABLE` and is not one. The same statement, differing only in where the tenant comes
#: from, was measured at 19 locks against 2,059 at modulus 256. The two forms look identical
#: in review.
#:
#: `zenith_project` rather than a matrix multiplied in this process, even though numpy is
#: right here and has the basis in memory. That is the point: the passages and the query
#: vectors go through the same rows of the same table, so there is no second implementation
#: to drift. A backfill that projected in Python would be the one copy of the basis nobody
#: would think to check.
#: Every placeholder carries an explicit cast, and that is not decoration. `:model` lands in a
#: `varchar` column and in a `text` function argument in the same statement, and Postgres
#: refuses to deduce one type for both: `AmbiguousParameter: inconsistent types deduced for
#: parameter $1`. It refused loudly here, which is the good case — the same class of mistake
#: silently produced a 37x regression in another harness this week, because a mis-declared
#: placeholder changed the plan instead of the result.
_BACKFILL = """
INSERT INTO chunk_embeddings (chunk_id, tenant_id, embedding_model, embedding_version, embedding)
SELECT e.chunk_id, e.tenant_id, CAST(:model AS varchar), CAST(:target AS varchar),
       zenith_project(e.embedding, CAST(:model AS text), CAST(:target AS text))
FROM chunk_embeddings e
WHERE e.tenant_id = :tenant
  AND e.embedding_model = CAST(:model AS varchar)
  AND e.embedding_version = CAST(:source AS varchar)
ON CONFLICT DO NOTHING
"""


async def _reproject(conn: AsyncConnection) -> dict[str, Any]:
    """Project the corpus, one tenant at a time.

    Per tenant because that is what makes it interruptible and what makes it prune. Idempotent
    on the primary key, so a run that stops halfway is resumed by running it again rather than
    by cleaning up after it.
    """
    tenants = [
        row[0]
        for row in (
            await conn.execute(
                text(
                    "SELECT DISTINCT tenant_id FROM chunk_embeddings "
                    "WHERE embedding_model = :model AND embedding_version = :version"
                ),
                {"model": MODEL, "version": SOURCE_VERSION},
            )
        ).all()
    ]
    started = time.perf_counter()
    for tenant in tenants:
        await conn.execute(
            text(_BACKFILL),
            {
                "model": MODEL,
                "source": SOURCE_VERSION,
                "target": TARGET_VERSION,
                "tenant": tenant,
            },
        )
    elapsed = (time.perf_counter() - started) * 1000
    written = await conn.scalar(
        text(
            "SELECT count(*) FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version"
        ),
        {"model": MODEL, "version": TARGET_VERSION},
    )
    return {
        "tenants": len(tenants),
        "vectors_projected": written,
        "ms": round(elapsed, 1),
        "ms_per_vector": round(elapsed / written, 4) if written else None,
    }


#: Rename every partition's copy after the parent index it belongs to.
#:
#: Migrations 0026 and 0027 both carry this and a reindex needs it for the same reason: left
#: alone, Postgres names a partition's index from the partition and the column —
#: `chunk_embeddings_p000_embedding_half_idx` — so a plan naming one says nothing about
#: *which* declared index was reached, and `pg_relation_size` cannot be summed by a pattern.
#: Both of those bit this file on its first run: the footprint came back 0 bytes per vector
#: and the plan verdict came back false because it was matching a name the index no longer
#: had. Neither was a product defect and both looked exactly like one.
_NAME_PARTITION_INDEXES = """
DO $do$
DECLARE entry record;
BEGIN
    FOR entry IN
        SELECT n.nspname AS schema,
               child.relname AS current_name,
               parent.relname || '_' || right(table_of.relname, 4) AS wanted
        FROM pg_inherits i
        JOIN pg_class child ON child.oid = i.inhrelid
        JOIN pg_class parent ON parent.oid = i.inhparent
        JOIN pg_index ix ON ix.indexrelid = child.oid
        JOIN pg_class table_of ON table_of.oid = ix.indrelid
        JOIN pg_namespace n ON n.oid = child.relnamespace
        WHERE table_of.relispartition
          AND pg_partition_root(table_of.oid) = 'public.chunk_embeddings'::regclass
          AND child.relname <> parent.relname || '_' || right(table_of.relname, 4)
    LOOP
        EXECUTE format('ALTER INDEX %I.%I RENAME TO %I',
                       entry.schema, entry.current_name, entry.wanted);
    END LOOP;
END $do$
"""


async def _index(conn: AsyncConnection) -> dict[str, Any]:
    """The projected space's own HNSW index, partial on its own space filter."""
    started = time.perf_counter()
    await conn.execute(text("SET LOCAL maintenance_work_mem = '512MB'"))
    await conn.execute(
        text(
            f"CREATE INDEX ix_chunk_embeddings_hnsw_half_{TARGET_VERSION} ON chunk_embeddings "
            f"USING hnsw ((embedding_half::halfvec({TARGET_DIMENSION})) halfvec_cosine_ops) "
            f"WITH (m = {M}, ef_construction = {EF_CONSTRUCTION}) "
            f"WHERE embedding_model = '{MODEL}' AND embedding_version = '{TARGET_VERSION}'"
        )
    )
    await conn.execute(text(_NAME_PARTITION_INDEXES))
    return {"build_ms": round((time.perf_counter() - started) * 1000, 1)}


async def _footprint(conn: AsyncConnection, version: str, dimension: int) -> dict[str, Any]:
    """Bytes per vector, summed over the partitions the space's index actually occupies."""
    # Summed over the *leaf* indexes (`relkind = 'i'`); the parent is a partitioned index
    # (`relkind = 'I'`) and always reports 0 bytes.
    #
    # `ix_chunk_embeddings_hnsw_half_p%` matches the 1024 space's children and
    # `ix_chunk_embeddings_hnsw_half_2_p%` the 512 space's, which is why the rename above is a
    # prerequisite for this figure existing at all rather than a tidiness.
    suffix = "" if version == SOURCE_VERSION else f"_{version}"
    total = await conn.scalar(
        text(
            "SELECT coalesce(sum(pg_relation_size(c.oid)), 0) FROM pg_class c "
            "WHERE c.relname LIKE :pattern AND c.relkind = 'i'"
        ),
        {"pattern": f"ix_chunk_embeddings_hnsw_half{suffix}\\_p%"},
    )
    count = await conn.scalar(
        text(
            "SELECT count(*) FROM chunk_embeddings "
            "WHERE embedding_model = :model AND embedding_version = :version"
        ),
        {"model": MODEL, "version": version},
    )
    # `int()` on both: `sum(pg_relation_size(...))` comes back as `numeric`, which psycopg
    # hands over as `Decimal`, which `json.dumps` refuses. It refused after the whole sweep
    # had run, which is the wrong end of the run to discover a formatting problem.
    total, count = int(total or 0), int(count or 0)
    return {
        "dimension": dimension,
        "vectors": count,
        "index_bytes": total,
        "bytes_per_vector": round(total / count, 1) if count else None,
    }


async def _plan(conn: AsyncConnection, version: str, dimension: int, vector: str) -> dict[str, Any]:
    """The plan the shipped query gets, recorded rather than inferred.

    **This is bar 1, and it is the one that matters most.** A query whose cast stops matching
    the index expression returns exactly the right rows from a sequential scan: recall does
    not move, nothing is marked `degraded`, and no test on a seeded corpus can see it. Recall
    survives a sequential scan; the product does not. So the verdict is a substring of the
    plan, and the plan text is kept beside it so a reader can check the verdict rather than
    believe it.
    """
    statement = (
        f"SELECT c.id, 1 - (e.embedding_half::halfvec({dimension}) "
        f"<=> CAST(:embedding AS halfvec({dimension}))) AS score "
        "FROM chunk_embeddings e "
        "JOIN chunks c ON c.id = e.chunk_id AND c.tenant_id = e.tenant_id "
        "WHERE e.embedding_model = :model AND e.embedding_version = :version "
        f"ORDER BY e.embedding_half::halfvec({dimension}) "
        f"<=> CAST(:embedding AS halfvec({dimension})) LIMIT :limit"
    )
    # Both discouraged, neither forbidden, and this is `test_vector_index.py`'s argument
    # rather than a new one. `enable_*` add a cost penalty; they do not remove the path. So a
    # scan-and-sort still appears when it is all the planner has — which is exactly the case
    # being detected — and the index path appears only if a matching operator class exists.
    #
    # Without them this file's first run reported `Limit -> Sort` for *both* spaces and looked
    # like a regression. It was not one: 13,549 rows spread over 128 partitions is a size at
    # which a sort is genuinely cheaper, and the planner was right. A check that cannot tell
    # "the index does not match" from "the index was not worth using" is not a check, and at
    # this corpus's size no latency threshold separates them either.
    await conn.execute(text("SET LOCAL enable_seqscan = off"))
    await conn.execute(text("SET LOCAL enable_sort = off"))
    rows = (
        await conn.execute(
            text("EXPLAIN (COSTS OFF) " + statement),
            {"model": MODEL, "version": version, "embedding": vector, "limit": 10},
        )
    ).all()
    plan = "\n".join(str(row[0]) for row in rows)
    # Named per space, not matched loosely. "Some index was used" would be satisfied by
    # `pk_chunk_embeddings`, which would mean the vector ordering had become a sort over a
    # full read — the very thing this is looking for.
    wanted = "ix_chunk_embeddings_hnsw_half" + ("" if version == SOURCE_VERSION else f"_{version}")
    return {
        "index_wanted": wanted,
        "plan_uses_index": f"Index Scan using {wanted}_p" in plan,
        "plan": " | ".join(line.strip() for line in plan.splitlines())[:400],
    }


async def _neighbours(
    conn: AsyncConnection, version: str, dimension: int, vector: str, limit: int = 10
) -> list[str]:
    rows = (
        await conn.execute(
            text(
                f"SELECT e.chunk_id::text FROM chunk_embeddings e "
                "WHERE e.embedding_model = :model AND e.embedding_version = :version "
                f"ORDER BY e.embedding_half::halfvec({dimension}) "
                f"<=> CAST(:embedding AS halfvec({dimension})) LIMIT :limit"
            ),
            {"model": MODEL, "version": version, "embedding": vector, "limit": limit},
        )
    ).all()
    return [str(row[0]) for row in rows]


#: Bar 2. Every way of pairing a vector with the wrong basis, and each must raise.
#:
#: A convention somebody has to remember is not good enough here, so the claim being checked
#: is that Postgres refuses — not that the code is careful. A run in which any of these
#: returned rows is a run that proved nothing, which is why the expected error text is
#: recorded beside the verdict.
_MIXES = (
    ("unprojected 1024-d query aimed at the 512 space", TARGET_VERSION, 512, SOURCE_DIMENSION),
    ("projected 512-d query aimed at the 1024 space", SOURCE_VERSION, 1024, TARGET_DIMENSION),
    ("correctly sized query, wrong space in the filter", SOURCE_VERSION, 512, TARGET_DIMENSION),
)


async def _gate(conn: AsyncConnection, at: dict[int, str]) -> list[dict[str, Any]]:
    """Ask for each mismatch and record what came back."""
    outcomes: list[dict[str, Any]] = []
    for label, version, cast_to, vector_width in _MIXES:
        try:
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "SELECT e.chunk_id FROM chunk_embeddings e "
                        "WHERE e.embedding_model = :model AND e.embedding_version = :version "
                        f"ORDER BY e.embedding_half::halfvec({cast_to}) "
                        f"<=> CAST(:embedding AS halfvec({cast_to})) LIMIT 10"
                    ),
                    {"model": MODEL, "version": version, "embedding": at[vector_width]},
                )
            outcomes.append({"mix": label, "raised": False, "error": None})
        except Exception as exc:  # noqa: BLE001 - the error text *is* the measurement
            message = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
            outcomes.append({"mix": label, "raised": True, "error": message})
    return outcomes


async def run(database: str, keep: bool = False) -> int:
    """Fit, install, reproject, measure, and put the corpus back.

    `keep` leaves the projected space in place, which is what a caller measuring the *live*
    path over HTTP needs — the space has to still be there when the api answers. Without it
    the `finally` removes everything this built, and the `finally` does not run if the
    process is killed.
    """
    import numpy as np

    url = f"postgresql+psycopg://zenith:zenith@localhost:5433/{database}"
    engine = create_async_engine(url)
    report: dict[str, Any] = {"database": database, "bars": {}}

    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL statement_timeout = 0"))
            matrix = await _corpus(conn)
            report["corpus"] = {
                "vectors": int(matrix.shape[0]),
                "source_dimension": int(matrix.shape[1]),
                "documents": await conn.scalar(text("SELECT count(*) FROM documents")),
            }

            started = time.perf_counter()
            basis, kept = _fit(matrix)
            report["fit"] = {
                "method": "uncentred SVD: eigenbasis of X'X / n",
                "centred": False,
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "explained_variance_ratio_at_512": round(kept, 4),
                "expected_from_dimensions_json": 0.991,
            }

            # Bar 3's ingredients. `svd_1024` is a rotation that discards nothing, so the
            # full-width projection of a vector must have the same norm as the vector. Checked
            # in numpy before anything is written, because a basis that fails this is not a
            # basis and everything downstream of it would be measuring a bug.
            probe = matrix[0]
            rotated = probe @ basis
            report["fit"]["orthonormal"] = {
                "norm_before": round(float(np.linalg.norm(probe)), 6),
                "norm_after_full_rotation": round(float(np.linalg.norm(rotated)), 6),
                "max_off_diagonal": round(
                    float(np.abs(basis.T @ basis - np.eye(basis.shape[0], dtype=np.float32)).max()),
                    6,
                ),
            }

            digest = _digest(basis)
            report["fit"]["basis_digest"] = digest
            await _install(conn, basis, digest)
            report["reproject"] = await _reproject(conn)
            report["index"] = await _index(conn)
            await conn.execute(text("ANALYZE chunk_embeddings"))

        async with engine.begin() as conn:
            footprint: dict[str, Any] = {
                "identity_1024": await _footprint(conn, SOURCE_VERSION, SOURCE_DIMENSION),
                "svd_512": await _footprint(conn, TARGET_VERSION, TARGET_DIMENSION),
            }
            report["footprint"] = footprint
            source: float = footprint["identity_1024"]["bytes_per_vector"] or 0.0
            target: float = footprint["svd_512"]["bytes_per_vector"] or 0.0
            ratio = round(source / target, 2) if target else None
            report["footprint_ratio"] = ratio

            # One real query vector, in both bases, produced the way the product produces it:
            # the 512 one comes out of `zenith_project`, not out of numpy.
            raw = "[" + ",".join(map(repr, matrix[7].tolist())) + "]"
            projected = await conn.scalar(
                text("SELECT zenith_project(CAST(:v AS vector), :model, :version)::text"),
                {"v": raw, "model": MODEL, "version": TARGET_VERSION},
            )
            at = {SOURCE_DIMENSION: raw, TARGET_DIMENSION: str(projected)}

            await conn.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
            report["plans"] = {
                "identity_1024": await _plan(conn, SOURCE_VERSION, SOURCE_DIMENSION, raw),
                "svd_512": await _plan(conn, TARGET_VERSION, TARGET_DIMENSION, str(projected)),
            }

            top_1024 = await _neighbours(conn, SOURCE_VERSION, SOURCE_DIMENSION, raw)
            top_512 = await _neighbours(conn, TARGET_VERSION, TARGET_DIMENSION, str(projected))
            overlap = len(set(top_1024) & set(top_512)) / len(top_1024) if top_1024 else 0.0
            report["one_query_overlap_at_10"] = round(overlap, 4)
            report["one_query_top1_preserved"] = bool(
                top_1024 and top_512 and top_1024[0] == top_512[0]
            )

            report["gate"] = await _gate(conn, at)

        report["bars"] = {
            "1_plan_uses_index_at_both_widths": bool(
                report["plans"]["identity_1024"]["plan_uses_index"]
                and report["plans"]["svd_512"]["plan_uses_index"]
            ),
            "2_every_mix_raises": all(outcome["raised"] for outcome in report["gate"]),
            "3_basis_is_a_rotation": report["fit"]["orthonormal"]["max_off_diagonal"] < 1e-3,
            "4_footprint_about_halves": bool(ratio and 1.7 <= ratio <= 2.3),
        }
        report["all_bars_met"] = all(report["bars"].values())

        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        for name, value in report["bars"].items():
            print(f"  {'PASS' if value else 'FAIL'}  {name}")
        print(f"\n  bytes/vector 1024: {report['footprint']['identity_1024']['bytes_per_vector']}")
        print(f"  bytes/vector  512: {report['footprint']['svd_512']['bytes_per_vector']}")
        print(f"  ratio: {report['footprint_ratio']}x")
        print(f"  written to {REPORT}")
        return 0 if report["all_bars_met"] else 1
    finally:
        if not keep:
            async with engine.begin() as conn:
                await conn.execute(
                    text(f"DROP INDEX IF EXISTS ix_chunk_embeddings_hnsw_half_{TARGET_VERSION}")
                )
                await conn.execute(
                    text(
                        "DELETE FROM chunk_embeddings "
                        "WHERE embedding_model = :model AND embedding_version = :version"
                    ),
                    {"model": MODEL, "version": TARGET_VERSION},
                )
                await conn.execute(
                    text(
                        "DELETE FROM embedding_spaces WHERE model = :model AND version = :version"
                    ),
                    {"model": MODEL, "version": TARGET_VERSION},
                )
        await engine.dispose()


#: Discovered by `eval/__main__.py` rather than announced to it. `CONTRIBUTING.md` calls a
#: registry every new feature must edit "a design smell"; this file adds a measurement by
#: existing.
COMMAND = "svd512"
USAGE = "svd512 --database <scratch db> [--keep]"


def cli(argv: list[str]) -> int:
    """`--database` has no default, and that is the safety.

    This is the only file under `eval/` that writes, and the thing it must never be pointed
    at is the installation holding the user's corpus. A default would be a default target.
    """
    import asyncio

    if "--database" not in argv:
        print("usage: python -m eval " + USAGE)
        print("\n  Refusing to guess a database: this sweep WRITES. Point it at a scratch copy.")
        return 2
    database = argv[argv.index("--database") + 1]
    return asyncio.run(run(database, keep="--keep" in argv))
