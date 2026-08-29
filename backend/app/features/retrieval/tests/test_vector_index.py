"""Every vector-ordered query can still reach the vector index.

This exists because migration 0025 moved the HNSW index onto `chunk_embeddings.embedding_half`
and left three copies of the old query behind. An operator class covers **one** type, so a
query that orders by `embedding`, or by `embedding_half` with a `vector` cast, stops matching
the index and plans a sequential scan. It returns the same rows, in the same order, and gets
slower in proportion to the corpus — no error, nothing marked `degraded`, and every recall
figure still looks right. Two measurement harnesses were in exactly that state before this
test was written, reporting an exact scan's recall as the index's.

**A latency assertion cannot catch this.** The seeded corpus here is a handful of rows, where
a sequential scan is genuinely faster and the planner is right to choose one; no threshold
separates the two cases at this size. So the assertion is on the *plan*, taken with
`enable_seqscan = off`. That is not a trick to force a pass: `enable_seqscan` discourages
sequential scans, it does not forbid them, so the index path appears only if one exists — and
one exists only if the ordering expression matches the operator class. The check is therefore
about the query's shape rather than about the size of the table, which is what makes it work
on three rows and on three hundred million.

`test_a_query_on_the_wrong_column_is_caught` is the control. Without it this file would pass
just as happily if `EXPLAIN` had stopped saying anything at all.
"""

from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import owner_session
from app.features.embeddings.client import DIMENSION, MODEL, VERSION
from app.features.embeddings.space import SHIPPED
from app.features.retrieval.search import CANDIDATES, dense
from eval.ef_search import NEIGHBOURS
from eval.tenant_scale import DENSE

#: The index migration 0025 builds. Named here rather than matched loosely, because "some
#: index was used" is not the claim — `pk_chunk_embeddings` would satisfy that and would mean
#: the vector ordering had become a sort over a full read.
INDEX = "ix_chunk_embeddings_hnsw_half"

#: The same query with the cast the codebase used before 0025. It has to fail the assertion,
#: or the assertion is not testing anything.
WRONG_COLUMN = (
    "SELECT c.id FROM chunk_embeddings e JOIN chunks c ON c.id = e.chunk_id "
    "WHERE e.embedding_model = :model AND e.embedding_version = :version "
    "ORDER BY e.embedding <=> CAST(:embedding AS vector) LIMIT :limit"
)


class _Recorder:
    """Stands in for a session, so `dense()` can be inspected instead of run.

    The product's SQL is built inside `dense()` and is not a constant anywhere. Retyping it
    here would test a copy — which is the failure this file is about — so the real function is
    called against a session that records what it was asked to execute and returns nothing.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, clause: object, params: object = None) -> list[Any]:
        self.statements.append(str(clause))
        return []


async def _dense_sql() -> str:
    """The statement `dense()` actually issues, minus the `SET LOCAL`s that precede it."""
    recorder = _Recorder()
    await dense(
        cast(AsyncSession, recorder),
        embedding=[0.0] * DIMENSION,
        space=SHIPPED,
        ef_search=100,
    )
    return recorder.statements[-1]


async def _plan(sql: str) -> str:
    vector = [0.0] * DIMENSION
    vector[0] = 1.0
    async with owner_session() as session:
        # Both discouraged, neither forbidden — `enable_*` add a cost penalty, they do not
        # remove the path. So the planner still answers with a scan-and-sort when that is all
        # it has, which is exactly the case being detected.
        #
        # `enable_sort` matters as much as `enable_seqscan` here, and only the second was
        # obvious. On an empty table the first attempt at this test planned
        # `Index Scan using pk_chunk_embeddings` followed by a `Sort` on the distance — an
        # ordering satisfied by reading everything and sorting it, which is what the vector
        # index exists to avoid and is available at any size. With both off, the only way to
        # produce the required order without a penalty is an index that already provides it,
        # and only a matching operator class does.
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        await session.execute(text("SET LOCAL enable_sort = off"))
        rows = await session.execute(
            text("EXPLAIN (COSTS OFF) " + sql),
            {
                "model": MODEL,
                "version": VERSION,
                "embedding": str(vector),
                "limit": CANDIDATES,
            },
        )
        return "\n".join(str(row[0]) for row in rows)


async def test_the_product_dense_query_reaches_the_vector_index(
    configured_engines: None,
) -> None:
    """`dense()` in `search.py`, as it is written rather than as it is remembered."""
    plan = await _plan(await _dense_sql())
    assert f"Index Scan using {INDEX}" in plan, (
        f"the dense half no longer matches the vector index and is planning a scan:\n{plan}"
    )


@pytest.mark.parametrize(
    ("harness", "sql"),
    [("eval/ef_search.py", NEIGHBOURS), ("eval/tenant_scale.py", DENSE)],
    ids=["ef_search", "tenant_scale"],
)
async def test_each_harness_measures_the_index_and_not_a_scan(
    harness: str, sql: str, configured_engines: None
) -> None:
    """The harnesses copy the dense query on purpose; this is the cost of that decision.

    Both hold their own copy so they can watch the plan the shipped SQL gets — see the
    comments on the constants. A copy drifts, and a drifted copy here does not fail loudly:
    it publishes a JSON report in which `ef_search` appears to do nothing and a shared HNSW
    graph appears to lose no rows to the policy, both of which are what a sequential scan
    looks like.
    """
    plan = await _plan(sql)
    assert f"Index Scan using {INDEX}" in plan, (
        f"{harness} orders by a column the vector index does not cover; every figure it "
        f"reports would be an exact scan's:\n{plan}"
    )


async def test_a_query_on_the_wrong_column_is_caught(configured_engines: None) -> None:
    """The control. `embedding` is real, indexed by nothing, and must not satisfy the check.

    It is the exact query all three sites held before migration 0025, so this also records
    what the regression looked like.
    """
    plan = await _plan(WRONG_COLUMN)
    assert f"Index Scan using {INDEX}" not in plan, (
        "ordering by the fp32 column reached the fp16 index, which is impossible — the "
        f"assertion above is not testing what it claims:\n{plan}"
    )
