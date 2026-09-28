"""Packet selection, final citation mapping, and application-role expansion."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.common.llm import GenerationResponse
from app.core.config import settings
from app.core.database import owner_session, tenant_session
from app.features.generation import service as generation_service
from app.features.generation.adapters.mock import MockProvider
from app.features.generation.answering import citations, packet, prompt
from app.features.generation.service import AnswerService
from app.features.retrieval.direct import verify_current
from app.features.retrieval.search import Hit
from app.features.retrieval.service import SearchService
from app.features.retrieval.tests.test_search import profile_for, seed
from app.features.tenancy.context import TenantContext
from conftest import Account, WorkingEmbedder


def hit(body: str, *, document: UUID | None = None, start: int = 0) -> Hit:
    return Hit(
        chunk_id=uuid4(),
        document_id=document or uuid4(),
        filename="public.txt",
        media_type="text/plain",
        page_num=None,
        char_start=start,
        char_end=start + len(body),
        text=body,
        bboxes=[],
        lexical_rank=None,
        dense_rank=None,
        score=1.0,
        source_sha256="source-v1",
    )


async def test_packet_preserves_exception_and_cites_its_own_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = uuid4()
    rule = hit("Residents pay 5%.", document=document)
    exception = hit("Except nonresidents pay 0%.", document=document, start=100)

    async def neighbor(context: TenantContext, primary: Hit, *, before: bool) -> Hit | None:
        return exception if primary.chunk_id == rule.chunk_id and not before else None

    monkeypatch.setattr(packet, "_neighbor", neighbor)
    built = await packet.build_packet(
        TenantContext.for_tenant(uuid4()), "What rate applies?", [rule]
    )
    assert built.complete
    assert [item.chunk_id for item in built.hits] == [rule.chunk_id, exception.chunk_id]
    assert built.reasons == ("direct_fact", "exception")
    assert "[2]" in prompt.build("What rate applies?", list(built.hits), roles=built.reasons)
    bound = citations.bind("Nonresidents pay 0% [2].", list(built.hits))
    assert [item.chunk_id for item in bound.citations] == [exception.chunk_id]


async def test_packet_omits_a_fact_when_required_context_cannot_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = hit("Residents pay 5%.")
    exception = hit("Except nonresidents pay 0%.", document=rule.document_id, start=100)

    async def neighbor(context: TenantContext, primary: Hit, *, before: bool) -> Hit | None:
        return exception if not before else None

    monkeypatch.setattr(packet, "_neighbor", neighbor)
    full = await packet.build_packet(TenantContext.for_tenant(uuid4()), "Rate?", [rule])
    built = await packet.build_packet(
        TenantContext.for_tenant(uuid4()),
        "Rate?",
        [rule],
        token_budget=full.rendered_cost - 1,
    )
    assert not built.complete
    assert built.hits == ()
    assert built.unresolved == ("required_context_unavailable",)


async def test_packet_keeps_two_sources_and_flags_missing_explicit_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = hit("The notice is due tomorrow.")
    second = hit("The filing is electronic.")

    async def no_neighbor(context: TenantContext, primary: Hit, *, before: bool) -> Hit | None:
        return None

    async def no_reference(context: TenantContext, primary: Hit, reference: str) -> Hit | None:
        return None

    monkeypatch.setattr(packet, "_neighbor", no_neighbor)
    monkeypatch.setattr(packet, "_referenced", no_reference)
    built = await packet.build_packet(
        TenantContext.for_tenant(uuid4()), "When and how?", [first, second]
    )
    assert built.complete and len(built.hits) == 2
    missing = hit("This is subject to section 7.")
    built = await packet.build_packet(TenantContext.for_tenant(uuid4()), "What rule?", [missing])
    assert built.hits == () and not built.complete


async def test_skipped_lower_priority_fact_is_not_a_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = hit("One relevant fact.")
    second = hit("A separate lower priority fact.")

    async def no_neighbor(context: TenantContext, primary: Hit, *, before: bool) -> Hit | None:
        return None

    monkeypatch.setattr(packet, "_neighbor", no_neighbor)
    one = await packet.build_packet(TenantContext.for_tenant(uuid4()), "What?", [first])
    built = await packet.build_packet(
        TenantContext.for_tenant(uuid4()),
        "What?",
        [first, second],
        token_budget=one.rendered_cost,
    )
    assert [item.chunk_id for item in built.hits] == [first.chunk_id]
    assert built.complete and built.unresolved == ()


async def test_optional_counterevidence_is_bounded_and_never_labeled_a_contradiction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = hit("Residents pay 5%.")
    exception = hit("However, another rule may apply.", document=rule.document_id, start=500)
    probes = 0

    async def no_neighbor(context: TenantContext, primary: Hit, *, before: bool) -> Hit | None:
        return None

    async def counter(
        context: TenantContext, primary: Hit, excluded: set[UUID], cap: int
    ) -> tuple[Hit, ...]:
        nonlocal probes
        probes += 1
        assert cap == 1 and rule.chunk_id in excluded
        return (exception,)

    monkeypatch.setattr(packet, "_neighbor", no_neighbor)
    monkeypatch.setattr(packet, "_counter_candidates", counter)
    ordinary = await packet.build_packet(TenantContext.for_tenant(uuid4()), "Rate?", [rule])
    assert probes == 0 and ordinary.counterevidence_status == "disabled"
    expanded = await packet.build_packet(
        TenantContext.for_tenant(uuid4()),
        "Rate?",
        [rule],
        counterevidence=True,
        max_counterevidence=1,
    )
    assert probes == 1 and expanded.counterevidence_ids == (exception.chunk_id,)
    assert expanded.reasons[-1] == "possible_exception_applicability_unassessed"
    assert expanded.counterevidence_status == "bounded_scan_complete"


async def test_counterevidence_scans_only_documents_in_the_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = hit("A rate applies.")
    skipped = hit("A second source has another rate.")
    scanned: list[UUID] = []

    async def no_neighbor(context: TenantContext, primary: Hit, *, before: bool) -> Hit | None:
        return None

    async def counter(
        context: TenantContext, primary: Hit, excluded: set[UUID], cap: int
    ) -> tuple[Hit, ...]:
        scanned.append(primary.document_id)
        return ()

    monkeypatch.setattr(packet, "_neighbor", no_neighbor)
    monkeypatch.setattr(packet, "_counter_candidates", counter)
    budget = (
        await packet.build_packet(TenantContext.for_tenant(uuid4()), "Rate?", [kept])
    ).rendered_cost
    built = await packet.build_packet(
        TenantContext.for_tenant(uuid4()),
        "Rate?",
        [kept, skipped],
        token_budget=budget,
        counterevidence=True,
    )
    assert [item.chunk_id for item in built.hits] == [kept.chunk_id]
    assert scanned == [kept.document_id]


async def test_packet_neighbor_expansion_uses_real_application_role(account: Account) -> None:
    profile = await profile_for(account)
    rule = "Residents pay 5%."
    exception = "Except nonresidents pay 0%."
    later = "However, a separate clause may apply."
    sha = str(uuid4())
    async with owner_session() as session:
        document = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:tenant, 'packet.txt', :sha, 100, 'ready') RETURNING id"
            ),
            {"tenant": account.tenant_id, "sha": sha},
        )
        assert document is not None
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document, "l": account.default_label},
        )
        ids: list[UUID] = []
        for start, body in ((0, rule), (100, exception), (500, later)):
            identity = await session.scalar(
                text(
                    "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                    "char_end, text, bboxes) VALUES (:d, :t, NULL, :start, :end, :body, '[]') "
                    "RETURNING id"
                ),
                {
                    "d": document,
                    "t": account.tenant_id,
                    "start": start,
                    "end": start + len(body),
                    "body": body,
                },
            )
            assert identity is not None
            ids.append(UUID(str(identity)))
    async with tenant_session(profile.context) as session:
        visible = (
            await session.execute(text("SELECT id FROM chunks WHERE id = ANY(:ids)"), {"ids": ids})
        ).all()
    assert {row.id for row in visible} == set(ids)
    primary = hit(rule, document=UUID(str(document)))
    primary = Hit(
        chunk_id=ids[0],
        document_id=primary.document_id,
        filename="packet.txt",
        media_type="text/plain",
        page_num=None,
        char_start=0,
        char_end=len(rule),
        text=rule,
        bboxes=[],
        lexical_rank=None,
        dense_rank=None,
        score=1.0,
        source_sha256=sha,
    )
    built = await packet.build_packet(profile.context, "What rate?", [primary])
    assert [item.chunk_id for item in built.hits] == ids[:2]
    assert await verify_current(profile, profile.context, built.hits)
    expanded = await packet.build_packet(
        profile.context, "What rate?", [primary], counterevidence=True, max_counterevidence=1
    )
    assert expanded.counterevidence_ids == (ids[2],)
    assert expanded.reasons[-1] == "possible_exception_applicability_unassessed"
    forbidden = TenantContext.for_tenant(account.tenant_id, ())
    exception_primary = Hit(
        chunk_id=ids[1],
        document_id=primary.document_id,
        filename="packet.txt",
        media_type="text/plain",
        page_num=None,
        char_start=100,
        char_end=100 + len(exception),
        text=exception,
        bboxes=[],
        lexical_rank=None,
        dense_rank=None,
        score=1.0,
        source_sha256=sha,
    )
    denied_packet = await packet.build_packet(forbidden, "What rate?", [exception_primary])
    assert denied_packet.hits == () and not denied_packet.complete
    assert not await verify_current(profile, forbidden, built.hits)


async def test_explicit_section_reference_uses_the_heading_under_rls(account: Account) -> None:
    profile = await profile_for(account)
    sha = str(uuid4())
    bodies = (
        "The rule applies subject to Section 3.",
        "This paragraph mentions Section 3 but is not its definition.",
        "3. Required Disclosure. An exception applies only on court order.",
    )
    async with owner_session() as session:
        document = await session.scalar(
            text(
                "INSERT INTO documents (tenant_id, filename, sha256, size_bytes, status) "
                "VALUES (:tenant, 'sections.txt', :sha, 100, 'ready') RETURNING id"
            ),
            {"tenant": account.tenant_id, "sha": sha},
        )
        assert document is not None
        await session.execute(
            text("INSERT INTO document_labels (document_id, label_id) VALUES (:d, :l)"),
            {"d": document, "l": account.default_label},
        )
        ids: list[UUID] = []
        for start, body in zip((0, 100, 200), bodies, strict=True):
            identity = await session.scalar(
                text(
                    "INSERT INTO chunks (document_id, tenant_id, page_num, char_start, "
                    "char_end, text, bboxes) VALUES (:d, :t, NULL, :start, :end, :body, '[]') "
                    "RETURNING id"
                ),
                {
                    "d": document,
                    "t": account.tenant_id,
                    "start": start,
                    "end": start + len(body),
                    "body": body,
                },
            )
            assert identity is not None
            ids.append(UUID(str(identity)))
    primary = Hit(
        chunk_id=ids[0],
        document_id=UUID(str(document)),
        filename="sections.txt",
        media_type="text/plain",
        page_num=None,
        char_start=0,
        char_end=len(bodies[0]),
        text=bodies[0],
        bboxes=[],
        lexical_rank=None,
        dense_rank=None,
        score=1.0,
        source_sha256=sha,
    )
    built = await packet.build_packet(profile.context, "When?", [primary])
    assert [item.chunk_id for item in built.hits] == [ids[0], ids[2]]
    assert built.reasons == ("direct_fact", "definition")
    assert built.complete


async def test_opt_in_generation_uses_packet_and_buffers_stream(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "evidence_packets_enabled", True)
    profile = await profile_for(account)
    provider = MockProvider(["The controller implements measures [1]."])
    service = AnswerService(
        profile,
        provider=provider,
        search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
    )
    result = await service.answer("What must the controller implement?")
    assert result.citations and not result.abstained
    assert "Context tags are selection hints" in provider.calls[0][1]
    events = [item async for item in service.stream("What must the controller implement?")]
    assert len(events) == 2
    assert events[0].token == events[1].result.answer  # type: ignore[union-attr]
    assert events[1].result is not None and events[1].result.citations


async def test_changed_packet_source_is_not_disclosed_after_generation(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    document_id = await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "evidence_packets_enabled", True)
    profile = await profile_for(account)

    class ChangingProvider(MockProvider):
        async def complete(self, system: str, user: str) -> GenerationResponse:
            response = await super().complete(system, user)
            async with owner_session() as session:
                await session.execute(
                    text(
                        "UPDATE chunks SET text = 'Changed after generation.' "
                        "WHERE document_id = :d"
                    ),
                    {"d": document_id},
                )
            return response

    service = AnswerService(
        profile,
        provider=ChangingProvider(["Technical measures [1]."]),
        search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
    )
    result = await service.answer("What must the controller implement?")
    assert result.abstained and result.citations == [] and result.consulted == []
    assert result.reason == "packet_source_changed"


async def test_failed_post_generation_source_check_abstains(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "evidence_packets_enabled", True)
    original = generation_service.verify_current
    checks = 0

    async def unavailable(*args: object, **kwargs: object) -> bool:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise RuntimeError("source state unavailable")
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(generation_service, "verify_current", unavailable)
    profile = await profile_for(account)
    service = AnswerService(
        profile,
        provider=MockProvider(["Technical measures [1]."]),
        search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
    )
    result = await service.answer("What must the controller implement?")
    assert checks == 2
    assert result.abstained and result.citations == [] and result.consulted == []
    assert result.reason == "packet_source_changed"


async def test_packet_deadline_withholds_generation(
    account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed(account.tenant_id, account.default_label)
    monkeypatch.setattr(settings, "evidence_packets_enabled", True)
    monkeypatch.setattr(settings, "evidence_packet_deadline_seconds", 0.01)

    async def stalled(*args: object, **kwargs: object) -> packet.EvidencePacket:
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    monkeypatch.setattr(generation_service, "build_packet", stalled)
    profile = await profile_for(account)
    provider = MockProvider(["Unsupported claim [1]."])
    service = AnswerService(
        profile,
        provider=provider,
        search=SearchService(profile, embedder=WorkingEmbedder()),  # type: ignore[arg-type]
    )
    answer = await service.answer("What must the controller implement?")
    assert answer.abstained and answer.citations == []
    assert answer.reason == "packet_unavailable" and answer.degraded
    assert provider.calls == []
