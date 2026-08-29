"""The BM25 function is subject to the same isolation matrix as the policies.

`zenith_lexical_search` is `SECURITY DEFINER`: the row-level policies do not constrain it,
and the argument for it existing is that it expresses the *same rule* the policies express,
in a form the index can resolve. That argument is only worth anything if it is tested, so
this file runs the label and tenant matrix against the function rather than against the
table — same fixture shape as `test_rls_label_isolation.py`, one more subject.

The failure this exists to catch is not a crash. It is the function returning one row too
many, quietly, on an installation nobody is looking at.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

pytestmark = pytest.mark.asyncio

#: A term every seeded chunk contains, so the search itself never decides the outcome. What
#: is being measured is which rows are *reachable*, not which rank well.
COMMON = "notice"


@dataclass
class Scenario:
    tenant: UUID
    other_tenant: UUID
    finance_label: UUID
    hr_label: UUID
    #: chunk id -> filename, read as the owner. **The joins that would resolve this inside
    #: the caller's session are themselves RLS-protected**, so resolving it there hides the
    #: very leak these tests exist to find: a function returning another tenant's chunk id
    #: looks identical to one returning nothing, because the join drops the row. The first
    #: version of this file did exactly that, and a mutant with the tenant clause deleted
    #: passed all seven tests.
    names: dict[UUID, str]


@pytest.fixture
async def corpus(owner_engine: AsyncEngine) -> Scenario:
    """Two tenants; in the first, one document per label and one unlabelled."""
    data = Scenario(
        tenant=uuid4(),
        other_tenant=uuid4(),
        finance_label=uuid4(),
        hr_label=uuid4(),
        names={},
    )
    async with owner_engine.begin() as conn:
        for tenant, name in ((data.tenant, "Ours"), (data.other_tenant, "Theirs")):
            await conn.execute(
                text("INSERT INTO tenants (id, name) VALUES (:id, :n)"),
                {"id": tenant, "n": f"{name} {tenant}"},
            )
        for label, name in ((data.finance_label, "Finance"), (data.hr_label, "HR")):
            await conn.execute(
                text("INSERT INTO access_labels (id, tenant_id, name) VALUES (:id, :t, :n)"),
                {"id": label, "t": data.tenant, "n": name},
            )

        rows: tuple[tuple[UUID, str, list[UUID]], ...] = (
            (data.tenant, "payroll.pdf", [data.finance_label]),
            (data.tenant, "personnel-files.pdf", [data.hr_label]),
            (data.tenant, "handbook.pdf", []),
            (data.other_tenant, "their-handbook.pdf", []),
        )
        for tenant, filename, labels in rows:
            document = uuid4()
            chunk = uuid4()
            data.names[chunk] = filename
            await conn.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, filename, sha256, size_bytes, "
                    "label_ids) VALUES (:id, :t, :f, :sha, 10, :labels)"
                ),
                {"id": document, "t": tenant, "f": filename, "sha": str(uuid4()), "labels": labels},
            )
            await conn.execute(
                text(
                    "INSERT INTO chunks (id, document_id, tenant_id, label_ids, page_num, "
                    "char_start, char_end, text) "
                    "VALUES (:id, :d, :t, :labels, 1, 0, 40, :body)"
                ),
                {
                    "id": chunk,
                    "d": document,
                    "t": tenant,
                    "labels": labels,
                    "body": f"the party shall provide {COMMON} in {filename}",
                },
            )
    return data


async def reached(
    engine: AsyncEngine,
    corpus: Scenario,
    tenant: UUID | None,
    labels: list[UUID],
    term: str = COMMON,
) -> set[str]:
    """What `zenith_lexical_search` hands back for that session, as the app role.

    **The ids it returns are resolved against the fixture, not against the database.** A
    join to `chunks` or `documents` from inside this session would be filtered by the very
    policies the function bypasses, so a leaked id would silently vanish on the way to a
    filename and the test would report success. Anything the function returns that the
    fixture cannot name is a row from outside the seeded corpus and is reported as such.
    """
    session = AsyncSession(engine)
    try:
        await session.execute(
            text("SELECT set_config('zenith.tenant_id', :t, true)"),
            {"t": str(tenant) if tenant else ""},
        )
        await session.execute(
            text("SELECT set_config('zenith.label_ids', :l, true)"),
            {"l": ",".join(str(label) for label in labels)},
        )
        ids = await session.scalars(
            text("SELECT chunk_id FROM zenith_lexical_search(:q, 100)"), {"q": term}
        )
        return {corpus.names.get(chunk, f"UNKNOWN {chunk}") for chunk in ids}
    finally:
        await session.close()


async def test_a_session_reaches_its_labels_and_the_unlabelled(
    app_engine: AsyncEngine, corpus: Scenario
) -> None:
    """Exactly what the policy says: `label_ids = '{}' OR label_ids && current_labels()`."""
    assert await reached(app_engine, corpus, corpus.tenant, [corpus.finance_label]) == {
        "payroll.pdf",
        "handbook.pdf",
    }


async def test_a_session_with_no_labels_reaches_only_the_unlabelled(
    app_engine: AsyncEngine, corpus: Scenario
) -> None:
    """The case the nested boolean exists for.

    A `should` clause placed beside the `must` clauses instead of inside one is *optional*
    in Tantivy: it boosts scoring and does not filter, so the whole tenant comes back and
    every compartment is open. This assertion is what fails when that happens.
    """
    assert await reached(app_engine, corpus, corpus.tenant, []) == {"handbook.pdf"}


async def test_two_labels_reach_both_compartments(
    app_engine: AsyncEngine, corpus: Scenario
) -> None:
    both = [corpus.finance_label, corpus.hr_label]

    assert await reached(app_engine, corpus, corpus.tenant, both) == {
        "payroll.pdf",
        "personnel-files.pdf",
        "handbook.pdf",
    }


async def test_no_label_reaches_another_tenant(app_engine: AsyncEngine, corpus: Scenario) -> None:
    """Holding every label of one tenant must not reach one row of another.

    The function is `SECURITY DEFINER`, so this is not enforced by a policy — it is enforced
    by the tenant term inside the Tantivy query, and this is the test that says so.
    """
    reachable = await reached(
        app_engine, corpus, corpus.tenant, [corpus.finance_label, corpus.hr_label]
    )

    assert reachable == {"payroll.pdf", "personnel-files.pdf", "handbook.pdf"}


async def test_the_other_tenant_sees_only_its_own(
    app_engine: AsyncEngine, corpus: Scenario
) -> None:
    """And the isolation is symmetric, which one-directional tests do not show."""
    assert await reached(app_engine, corpus, corpus.other_tenant, []) == {"their-handbook.pdf"}


async def test_a_session_with_no_context_reaches_nothing(
    app_engine: AsyncEngine, corpus: Scenario
) -> None:
    """The closed failure, and deliberately the *same* closed failure the policies have.

    Without the guard, a NULL tenant makes `paradedb.term` raise `no value provided to term
    query` — also closed, but a different behaviour from a policy, which would undermine the
    only argument for this function existing.
    """
    assert await reached(app_engine, corpus, None, []) == set()


async def test_a_crafted_query_string_still_cannot_leave_the_tenant(
    app_engine: AsyncEngine, corpus: Scenario
) -> None:
    """Belt and braces rather than the mechanism.

    The tenant term is its own `must` clause, so even a query string parsed as syntax is
    ANDed with it and cannot reach another tenant. Worth pinning anyway: it is the property
    somebody will want evidence of, and it holds for a reason independent of tokenisation.
    """
    hostile = f"{COMMON}) OR tenant_id:({corpus.other_tenant}"

    assert await reached(app_engine, corpus, corpus.tenant, [], term=hostile) == {"handbook.pdf"}


async def test_an_identifier_survives_tokenisation(
    app_engine: AsyncEngine, corpus: Scenario, owner_engine: AsyncEngine
) -> None:
    """The reason this path uses `match` and not `parse`.

    `lexical.py` records what the alternative cost: M0 tokenised queries with a regex, which
    split `1545-0074` into `1545` and `0074` while the corpus side had stored it whole, and
    lexical search found one identifier in six. `parse` repeats that shape — it takes a
    query DSL, so the string has to be escaped, and an escaper is a second tokeniser wearing
    a different hat. `match` hands the string to the field's own analyser.

    An identifier query is also the one thing the lexical half is *for*: the dense half
    cannot find these at all.
    """
    chunk = uuid4()
    async with owner_engine.begin() as conn:
        document = uuid4()
        await conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, filename, sha256, size_bytes, "
                "label_ids) VALUES (:id, :t, 'form.pdf', :sha, 10, '{}')"
            ),
            {"id": document, "t": corpus.tenant, "sha": str(uuid4())},
        )
        await conn.execute(
            text(
                "INSERT INTO chunks (id, document_id, tenant_id, label_ids, page_num, "
                "char_start, char_end, text) VALUES (:id, :d, :t, '{}', 1, 0, 60, :body)"
            ),
            {
                "id": chunk,
                "d": document,
                "t": corpus.tenant,
                "body": "Form 1545-0074 applies to withholding under section 119/33",
            },
        )
    corpus.names[chunk] = "form.pdf"

    assert await reached(app_engine, corpus, corpus.tenant, [], term="1545-0074") == {"form.pdf"}


async def test_a_malformed_query_string_answers_rather_than_erroring(
    app_engine: AsyncEngine, corpus: Scenario
) -> None:
    """Punctuation a person types must not become a 500.

    `parse` would read `what is the "party` as a query DSL with an unterminated phrase and
    raise. Nothing about that is the reader's fault, and a search box that errors on a
    quotation mark is a search box people stop trusting.
    """
    assert await reached(
        app_engine, corpus, corpus.tenant, [], term='what is the "party notice'
    ) == {"handbook.pdf"}
