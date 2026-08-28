# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""`tsvector` against `bm25`, on the same questions and the same installation.

`ZENITH_LEXICAL_ENGINE` selects between two implementations of the lexical half and defaults
to `tsvector`. Migration 0022 built the `bm25` path and left it switched off, with the
argument for flipping it stated as a scale trade rather than an upgrade: `ts_rank_cd` scores
every matching row before `LIMIT` can choose, so its cost grows with the number of matches,
while ParadeDB resolves the top N inside the index and its cost does not.

A scale trade is only a trade if the accuracy side is measured. This is that measurement, and
it is the whole comparison rather than the headline: recall into the page, recall at the top,
mean rank, the latency of the lexical stage specifically, and which questions each engine
loses.

## Both engines in one process, and why that is the point

Switching engine is `settings.lexical_engine`, read by `lexical.engine()` on every call, so
one process can score both against one corpus in one sitting. Two runs on two days would
compare two installations, and this corpus has grown since the figures in 0022 were taken.

The credit rule is `harness.score()`'s, unchanged and deliberately not reimplemented: a
passage counts when its `(document, page)` is one the question records. `harness.py` says why
— two reports with different credit rules are not comparable, which is the only thing either
of them exists to be.

## Three conditions, because the shipped question set cannot see the problem

`questions.toml` is thirty questions and **all thirty are in English**, over the public
English corpus it was built against — not one of them carries an accent. Accent stripping is
therefore a no-op on every one of them, and a report built from that set alone would score
the accent case as a perfect tie it had not earned and say nothing at all about the thing at
issue.

So there are three conditions:

- **`english`** — `questions.toml` as written. The comparability anchor: this is the number
  that sits beside `rerank-depth.json` and `live-recall.json`.
- **`spanish-accented`** — ten questions over the Spanish half of this installation's corpus,
  written the way somebody types them with a Spanish keyboard.
- **`spanish-unaccented`** — the same ten with their accents stripped, the way the runbook
  types them and the way half of real queries arrive.

The Spanish set lives here rather than in `questions.toml` on purpose. Adding it there would
change `live.py`, `production.py`, `ef_search.py` and `rerank_depth.py` at the same time —
every baseline in `eval/` would move, and none of those reports asked a question about
Spanish. This set answers one question, so it is scoped to the file that asks it.

**It is verified the same way `questions.toml` is**, and for the same reason: an unverified
question set measures whoever drafted it. Every anchor below is re-derived from the corpus in
the database before anything is scored, and a mismatch stops the run rather than quietly
scoring against pages the phrase is not on.

## The accent condition is the whole tokeniser argument

The two indexes do not fold accents the same way. The GIN side is `zenith_text`, `english`
with `unaccent` in front of the stemmer (migration 0018), so `maximo` finds `máximo`. The
BM25 side is a Tantivy tokeniser, and at pg_search 0.15.26 none of the sixteen it offers has
an ASCII-folding filter — that arrived with the `pdb.*` tokeniser API in 0.19. Whatever
tokeniser `ix_chunks_bm25` is built with, this file records it beside the numbers.

Ten questions is a small set: one rescued question moves its recall ten points, and a table
that turns on a single question is a table somebody should be suspicious of. So each run also
records `tokeniser_folding` — the same accent property counted over every accented word type
in the Spanish half of the corpus, thousands of them, which is what makes the recall row
evidence rather than a coincidence.

## The lexical stage is timed on its own

`harness.score()` times the whole request, which on this path is dominated by query embedding
and the cross-encoder — the two components this change does not touch. The engine difference
lives entirely in one call, so that call is timed by wrapping the name `service.py` imported.
Reaching in like this is laboratory behaviour and stays in `eval/`; nothing under `app/`
knows it happened.

## The report accumulates rather than overwrites

Each invocation appends one run, stamped with the tokeniser `ix_chunks_bm25` was built with
at the time. A tokeniser change is a reindex, and comparing before with after is exactly the
question this file gets asked — so the earlier run has to survive the later one, and each
entry says which index it measured rather than leaving the reader to guess.

**Read-only.** The queries this runs are searches; nothing writes to the corpus.

    docker compose exec -T api python -m eval lexical-engine
"""

import asyncio
import dataclasses
import json
import statistics
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import settings
from app.features.retrieval import service as retrieval_service
from app.features.retrieval.search import lexical as real_lexical
from eval.harness import Installation, installation, score
from eval.live import FILENAMES
from eval.questions import Question, Source

REPORT = Path(__file__).parent / "lexical-engine.json"

#: In the order the table should read: the engine that ships first, the candidate second.
ENGINES = ("tsvector", "bm25")

#: Ten questions over the Spanish half of this installation's corpus, each with the phrase
#: its answer comes from and every page of that document the phrase appears on — the shape
#: `questions.toml` uses, and re-derived from the corpus by `verify()` before every run.
#:
#: Chosen so the accented words are *content* words rather than interrogatives: `duración`,
#: `máxima`, `detención`, `prisión`, `Código`, `resolución`, `contratación`. A set whose only
#: accents were on `qué` and `cuál` would strip to something the stemmer treats identically
#: and would report a tie it had not earned.
#:
#: `document` is the filename itself. `questions.toml` uses a manifest id because its corpus
#: is fetched by `eval fetch`; these documents are the installation's own and there is no
#: manifest entry to name, so the indirection would be a lie with an extra step.
SPANISH: tuple[Question, ...] = (
    Question(
        id="es-constitucion-detencion",
        type="factual",
        question="¿Cuál es la duración máxima de la detención preventiva?",
        sources=(
            Source(
                document="constitucion-espanola.pdf",
                anchor="La detención preventiva no podrá durar más del tiempo estrictamente",
                pages=(6,),
            ),
        ),
    ),
    Question(
        id="es-constitucion-domicilio",
        type="factual",
        question="¿Qué establece la Constitución sobre la inviolabilidad del domicilio?",
        sources=(
            Source(
                document="constitucion-espanola.pdf",
                anchor="El domicilio es inviolable",
                pages=(6,),
            ),
        ),
    ),
    Question(
        id="es-civil-mayoria-edad",
        type="factual",
        question="¿Cuándo empieza la mayor edad según el Código Civil?",
        sources=(
            Source(
                document="codigo-civil.pdf",
                anchor="La mayor edad empieza a los dieciocho años cumplidos",
                pages=(68,),
            ),
        ),
    ),
    Question(
        id="es-estatuto-vacaciones",
        type="factual",
        question="¿Cuál es la duración mínima de las vacaciones anuales retribuidas?",
        sources=(
            Source(
                document="ley-estatuto-trabajadores.pdf",
                anchor="vacaciones anuales retribuidas",
                pages=(39,),
            ),
        ),
    ),
    Question(
        id="es-estatuto-jornada",
        type="factual",
        question="¿Cuál es la duración máxima de la jornada ordinaria de trabajo?",
        sources=(
            Source(
                document="ley-estatuto-trabajadores.pdf",
                anchor="duración máxima de la jornada ordinaria de trabajo",
                pages=(33, 34),
            ),
        ),
    ),
    Question(
        id="es-ley39-plazo-resolucion",
        type="factual",
        question=(
            "¿Cuál es el plazo máximo para notificar la resolución expresa "
            "de un procedimiento administrativo?"
        ),
        sources=(
            Source(
                document="ley-39-2015-procedimiento-administrativo.pdf",
                anchor=(
                    "El plazo máximo en el que debe notificarse la resolución "
                    "expresa será el fijado"
                ),
                pages=(24,),
            ),
        ),
    ),
    Question(
        id="es-penal-prision-permanente",
        type="factual",
        question="¿En qué supuestos se aplica la pena de prisión permanente revisable?",
        sources=(
            Source(
                document="codigo-penal.pdf",
                anchor="prisión permanente revisable",
                pages=(20, 22, 33, 34, 43, 60, 168, 195, 196),
            ),
        ),
    ),
    Question(
        id="es-penal-prescripcion",
        type="factual",
        question="¿En qué plazos prescriben los delitos según el Código Penal?",
        sources=(Source(document="codigo-penal.pdf", anchor="prescriben", pages=(57, 58)),),
    ),
    Question(
        id="es-lopdgdd-menores",
        type="factual",
        question=(
            "¿A partir de qué edad es válido el consentimiento de un menor "
            "para el tratamiento de sus datos personales?"
        ),
        sources=(
            Source(
                document="lopdgdd-proteccion-datos.pdf",
                anchor="mayor de catorce años",
                pages=(17,),
            ),
        ),
    ),
    Question(
        id="es-contratos-abierto-simplificado",
        type="factual",
        question="¿En qué consiste el procedimiento abierto simplificado de contratación pública?",
        sources=(
            Source(
                document="ley-9-2017-contratos-sector-publico.pdf",
                anchor="procedimiento abierto simplificado",
                pages=(15, 120, 122, 123, 201, 205, 249),
            ),
        ),
    ),
)

#: How long the lexical stage took, in milliseconds, for every search of the current
#: condition. Module-level because the wrapper below is installed once and `harness.score()`
#: has no channel to hand anything back through.
_TIMINGS: list[float] = []


def strip_accents(value: str) -> str:
    """What somebody typing without dead keys produces.

    Decompose, drop the combining marks, recompose. This is `unaccent`'s rule rather than a
    Spanish speaker's — `ñ` becomes `n`, which is wrong as Spanish and right as a model of
    the GIN side, whose corpus went through exactly this transformation before it was
    indexed. The condition exists to be hard on the BM25 side the way a real query is, not to
    be linguistically defensible.
    """
    decomposed = unicodedata.normalize("NFD", value)
    return unicodedata.normalize(
        "NFC", "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    )


async def _timed_lexical(
    session: AsyncSession,
    question: str,
    limit: int = 50,
    documents: list[UUID] | None = None,
) -> list[tuple[UUID, float]]:
    """`search.lexical`, plus a stopwatch. Same arguments, same result, same order."""
    started = time.perf_counter()
    try:
        return await real_lexical(session, question, limit, documents)
    finally:
        _TIMINGS.append((time.perf_counter() - started) * 1000)


def spanish(where: Installation, questions: tuple[Question, ...]) -> Installation:
    """The same installation, asked the Spanish set instead of the English one.

    Everything else is carried through: the same tenant, the same labels, the same embedding
    space and therefore the same `score()` and the same credit rule. Only the questions
    differ, which is what makes the three conditions rows of one table.
    """
    return dataclasses.replace(where, questions=list(questions))


def unaccented(questions: tuple[Question, ...]) -> tuple[Question, ...]:
    """The same questions with their accents stripped.

    Only the question text changes. `id`, `type` and `sources` are carried through untouched,
    so the stripped run is scored against exactly the passages the accented one was scored
    against — the only way the two rows mean anything side by side.
    """
    return tuple(
        dataclasses.replace(question, question=strip_accents(question.question))
        for question in questions
    )


async def verify(tenant: UUID, questions: tuple[Question, ...]) -> list[str]:
    """Re-derive every anchor from the corpus, and report what does not hold.

    `questions.py` states the rule this follows: an unverified question set measures the
    model that drafted it rather than the retrieval system, so the pages are re-derived
    rather than trusted. `test_questions.py` does it for `questions.toml` from the extracted
    text; this set has no manifest and no extracted text of its own, so it is derived from
    the chunks actually indexed — which is in fact the stricter check, because it is the text
    retrieval will search rather than the text ingestion started from.
    """
    engine = create_async_engine(settings.database_owner_url)
    problems: list[str] = []
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        for question in questions:
            for source in question.sources:
                found = tuple(
                    (
                        await conn.execute(
                            text(
                                "SELECT DISTINCT c.page_num FROM chunks c "
                                "JOIN documents d ON d.id = c.document_id "
                                "WHERE d.tenant_id = :tenant AND d.filename = :filename "
                                "AND c.text ILIKE :anchor ORDER BY 1"
                            ),
                            {
                                "tenant": tenant,
                                "filename": source.document,
                                "anchor": f"%{source.anchor}%",
                            },
                        )
                    ).scalars()
                )
                if found != source.pages:
                    problems.append(
                        f"{question.id}: {source.document!r} has {source.anchor!r} on "
                        f"{found or 'no page'}, the question claims {source.pages}"
                    )
        await conn.rollback()
    await engine.dispose()
    return problems


#: Every accented word type in the Spanish half of the corpus, and what each candidate
#: tokeniser does to it — does the word and its unaccented spelling reduce to the same token?
#: That is the whole accent question stated at the vocabulary level, one layer below recall,
#: where the answer does not depend on which questions somebody happened to write.
#:
#: "Spanish half" and "English half" are defined by the text rather than by filename: a chunk
#: holding an acute-accented vowel is Spanish enough for this purpose, and one holding none is
#: English enough. Ordered and limited so two runs measure the same words.
FOLDING = """
WITH spanish AS (
    SELECT c.text FROM chunks c WHERE c.tenant_id = :tenant AND c.text ~ '[áéíóú]'
    ORDER BY c.id LIMIT 3000
),
english AS (
    SELECT c.text FROM chunks c WHERE c.tenant_id = :tenant AND c.text !~ '[áéíóúñ]'
    ORDER BY c.id LIMIT 2000
),
accented AS (
    SELECT DISTINCT lower(word) AS word
    FROM spanish, LATERAL regexp_matches(spanish.text, '[[:alpha:]]+', 'g') AS m(parts),
         LATERAL unnest(parts) AS word
    WHERE word ~ '[áéíóúÁÉÍÓÚ]' AND length(word) > 2
),
plain AS (
    SELECT DISTINCT lower(word) AS word
    FROM english, LATERAL regexp_matches(english.text, '[[:alpha:]]+', 'g') AS m(parts),
         LATERAL unnest(parts) AS word
    WHERE length(word) > 3
),
folded AS (
    SELECT
        (SELECT string_agg(t.token, ' ' ORDER BY t.position)
           FROM paradedb.tokenize(CAST(:en AS jsonb), a.word) t) AS en_acc,
        (SELECT string_agg(t.token, ' ' ORDER BY t.position)
           FROM paradedb.tokenize(CAST(:en AS jsonb), u.word) t) AS en_plain,
        (SELECT string_agg(t.token, ' ' ORDER BY t.position)
           FROM paradedb.tokenize(CAST(:es AS jsonb), a.word) t) AS es_acc,
        (SELECT string_agg(t.token, ' ' ORDER BY t.position)
           FROM paradedb.tokenize(CAST(:es AS jsonb), u.word) t) AS es_plain
    FROM accented a, LATERAL (SELECT unaccent('unaccent'::regdictionary, a.word) AS word) u
),
divergence AS (
    SELECT
        (SELECT string_agg(t.token, ' ' ORDER BY t.position)
           FROM paradedb.tokenize(CAST(:en AS jsonb), p.word) t) AS en,
        (SELECT string_agg(t.token, ' ' ORDER BY t.position)
           FROM paradedb.tokenize(CAST(:es AS jsonb), p.word) t) AS es
    FROM plain p
)
SELECT
    (SELECT count(*) FROM folded) AS accented_types,
    (SELECT count(*) FILTER (WHERE en_acc = en_plain) FROM folded) AS en_stem_folds,
    (SELECT count(*) FILTER (WHERE es_acc = es_plain) FROM folded) AS spanish_stem_folds,
    (SELECT count(*) FROM divergence) AS english_types,
    (SELECT count(*) FILTER (WHERE en <> es) FROM divergence) AS english_types_diverging
"""


async def folding(tenant: UUID) -> dict[str, object]:
    """How much accent-insensitivity each candidate tokeniser actually has.

    Recall is the number that decides, and it is measured over ten Spanish questions — a set
    small enough that one rescued question moves it ten points. This is the same property
    counted over thousands of words instead, so the recall table can be read as evidence
    rather than as a coincidence.

    The GIN side needs no row here: `zenith_text` puts `unaccent` in front of the stemmer, so
    its figure is 100% by construction rather than by measurement.
    """
    engine = create_async_engine(settings.database_owner_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        row = (
            await conn.execute(
                text(FOLDING),
                {
                    "tenant": tenant,
                    "en": json.dumps({"type": "en_stem", "lowercase": True}),
                    "es": json.dumps({"type": "stem", "language": "Spanish", "lowercase": True}),
                },
            )
        ).one()
        await conn.rollback()
    await engine.dispose()
    return {
        "accented_types": row.accented_types,
        "en_stem_folds": row.en_stem_folds,
        "en_stem_fold_rate": round(row.en_stem_folds / row.accented_types, 4),
        "spanish_stem_folds": row.spanish_stem_folds,
        "spanish_stem_fold_rate": round(row.spanish_stem_folds / row.accented_types, 4),
        "english_types": row.english_types,
        "english_types_diverging": row.english_types_diverging,
        "english_divergence_rate": round(row.english_types_diverging / row.english_types, 4),
    }


async def _bm25_index() -> str:
    """The tokeniser `ix_chunks_bm25` currently holds, read from the index itself.

    Not from the migration files: the index a measurement ran against is a property of the
    database at that moment, and on an installation large enough to need a hand-built index
    (0022's rollout note) the two can legitimately differ.
    """
    engine = create_async_engine(settings.database_owner_url)
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        definition = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_bm25'")
            )
        ).scalar()
        await conn.rollback()
    await engine.dispose()
    return str(definition or "absent")


async def _measure(where: Installation) -> dict[str, object]:
    """One engine, one condition. The stage timings are collected around `score()`."""
    _TIMINGS.clear()
    measured = await score(where)
    measured["lexical_median_ms"] = round(statistics.median(_TIMINGS), 2) if _TIMINGS else None
    measured["lexical_p95_ms"] = (
        round(sorted(_TIMINGS)[int(len(_TIMINGS) * 0.95) - 1], 2) if _TIMINGS else None
    )
    measured["lexical_mean_ms"] = round(statistics.mean(_TIMINGS), 2) if _TIMINGS else None
    return measured


async def _run() -> int:
    where = await installation()
    if not where.questions:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    problems = await verify(where.tenant, SPANISH)
    if problems:
        # Refused rather than skipped. A Spanish condition scored against pages its anchor is
        # not on would produce a table that looks complete and decides the wrong way, which
        # is worse than no table.
        print("The Spanish question set does not match this corpus:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    # `harness.score()` resolves a question's document through this map. The Spanish
    # documents are the installation's own rather than entries in `corpus.toml`, so they name
    # themselves; `setdefault` so a real manifest id always wins.
    for question in SPANISH:
        for source in question.sources:
            FILENAMES.setdefault(source.document, source.document)

    conditions = {
        "english": where,
        "spanish-accented": spanish(where, SPANISH),
        "spanish-unaccented": spanish(where, unaccented(SPANISH)),
    }
    index = await _bm25_index()
    vocabulary = await folding(where.tenant)
    print(
        f"{where.space.n} embeddings, {len(where.questions)} English questions, "
        f"{len(SPANISH)} Spanish questions verified against the corpus\n{index}\n"
        f"  tokeniser folding  {json.dumps(vocabulary)}\n"
    )

    original_engine = settings.lexical_engine
    retrieval_service.lexical = _timed_lexical  # type: ignore[assignment]
    results: dict[str, dict[str, object]] = {}
    try:
        for engine in ENGINES:
            settings.lexical_engine = engine
            results[engine] = {}
            for condition, asked in conditions.items():
                measured = await _measure(asked)
                results[engine][condition] = measured
                print(f"  {engine:<9} {condition:<19} {json.dumps(measured)}", flush=True)
    finally:
        settings.lexical_engine = original_engine
        retrieval_service.lexical = real_lexical  # type: ignore[assignment]

    _append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "embeddings": where.space.n,
            "english_questions": len(where.questions),
            "spanish_questions": len(SPANISH),
            "bm25_index": index,
            "tokeniser_folding": vocabulary,
            "by_engine": results,
        }
    )
    print(f"\nWritten to {REPORT.name}")
    return 0


def _append(run: dict[str, object]) -> None:
    """Add this run to the file, keeping the ones already in it.

    A malformed or absent file starts a new list rather than aborting: the measurement has
    already been paid for by the time this is reached, and losing it to a parse error would
    be the expensive failure.
    """
    runs: list[Any] = []
    if REPORT.exists():
        try:
            existing = json.loads(REPORT.read_text())
            runs = existing.get("runs", []) if isinstance(existing, dict) else []
        except json.JSONDecodeError:
            runs = []
    runs.append(run)
    REPORT.write_text(json.dumps({"runs": runs}, indent=2) + "\n")


def run() -> int:
    return asyncio.run(_run())
