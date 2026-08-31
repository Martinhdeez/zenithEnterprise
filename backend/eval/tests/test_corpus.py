"""The corpus is data, and data can rot quietly.

These tests do not need the documents downloaded — that would make the suite depend on
thirteen third-party hosts. They check the things that break without anyone noticing: a
manifest that no longer parses, an id that stopped being unique, a fixture that quietly
grew a text layer.
"""

from pathlib import Path

import pytest

from eval.corpus import DOCUMENTS, load_manifest


def test_the_manifest_parses_and_every_field_is_present() -> None:
    documents = load_manifest()

    assert len(documents) >= 13
    for document in documents:
        assert document.url.startswith("https://"), document.id
        assert document.why, f"{document.id} has no stated reason for being in the corpus"


def test_ids_are_unique() -> None:
    """Ids name files on disk, so a duplicate silently overwrites another document and the
    corpus quietly shrinks."""
    ids = [document.id for document in load_manifest()]

    assert len(ids) == len(set(ids))


def test_every_document_has_a_recorded_checksum() -> None:
    """The checksum is what makes the baseline reproducible.

    Public URLs are not immutable: a regulator reissues a PDF, an agency re-scans an
    archive. Without a recorded digest the corpus can change under a measurement and the
    number would move for reasons nobody could reconstruct.
    """
    for document in load_manifest():
        assert document.sha256, f"{document.id}: run `python -m eval fetch --record`"
        assert document.pages, document.id


def test_the_difficult_categories_are_all_represented() -> None:
    """The corpus exists to be hard. If a category quietly disappears, the measurement
    silently gets easier and the number silently gets better."""
    difficulties = {tag for document in load_manifest() for tag in document.difficulty}

    assert {"scanned", "tables", "two-column", "very-long", "cross-referential"} <= difficulties


def test_there_are_confusable_documents() -> None:
    """Retrieval across unrelated documents is easy because nothing competes.

    Three EU regulations that share structure and vocabulary, and three papers on adjacent
    topics, are what make Recall@8 measure ranking rather than lookup.
    """
    by_category: dict[str, int] = {}
    for document in load_manifest():
        by_category[document.category] = by_category.get(document.category, 0) + 1

    assert by_category["regulation"] >= 3
    assert by_category["technical-paper"] >= 3


@pytest.mark.skipif(
    not (DOCUMENTS / "gdpr.pdf").exists(),
    reason="corpus not downloaded; run `python -m eval fetch`",
)
def test_the_image_only_fixture_has_no_extractable_text(tmp_path: Path) -> None:
    """The property the whole fixture exists for.

    A PDF with no text layer ingests successfully, produces zero chunks and retrieves
    nothing — no exception, no failed status, nothing in a log. The document appears in the
    list, the user asks about it, and the system answers about something else.

    Every scan we could find had been OCR'd by its publisher, so this case had to be
    constructed. If it ever gains a text layer, this fixture stops testing anything and the
    guarantee in `technical-decisions.md` §7 loses its only witness.

    Built into `tmp_path`. It used to call `build(force=True)`, which rasterised six pages
    straight onto `backend/eval/documents/image-only.pdf` — a fixed path `build` itself
    reads back, and `Image.save` truncates before it writes. Two runs in one checkout and
    one reads the other's half-written PDF. Invisible on a machine without the corpus,
    because this test skips there; the first developer to fetch it would have owned the
    flake. The corpus source is still the repository's own, because that is what the
    fixture is made of — read, never written.
    """
    from eval.fixtures import build, extractable_characters

    assert extractable_characters(build(tmp_path / "image-only.pdf")) == 0


def test_recording_checksums_is_idempotent(tmp_path: Path) -> None:
    """Running `fetch --record` twice must not corrupt the manifest.

    It did. The second run appended a second set of `sha256`/`pages`/`bytes` keys to every
    entry, and duplicate keys are not untidy — TOML parsing fails outright with "Cannot
    overwrite a value" and the corpus becomes unloadable. Found by fetching onto a second
    machine, which is the ordinary case rather than an exotic one.

    The first attempt at the fix was also wrong, in a way only repetition exposed: it
    cleared the current-document marker immediately after inserting, which switched the
    de-duplication off for exactly the lines it was meant to remove. Two runs looked clean;
    three were corrupt. Hence four here.

    Recorded against a copy in `tmp_path`. It used to record against the repository's own
    manifest and restore it in a `finally`, which made this the only test in the suite that
    wrote a file every other reader of the corpus opens by a fixed path. `write_text`
    truncates before it writes, so for the length of each of these four writes
    `backend/eval/corpus.toml` was zero bytes on disk, and anything reading it in that
    window got `tomllib` returning `{}` and `load_manifest` raising `KeyError: 'document'`.
    Two `make check` runs in one checkout is enough — it failed roughly one run in three,
    always in this test, and passed on the immediate re-run.
    """
    from eval.__main__ import record_checksums
    from eval.corpus import MANIFEST

    manifest = tmp_path / "corpus.toml"
    manifest.write_text(MANIFEST.read_text())

    updates = {
        document.id: (document.sha256 or "x", document.pages or 1, document.bytes or 1)
        for document in load_manifest(manifest)
    }
    written: set[str] = set()
    for _ in range(4):
        record_checksums(updates, manifest)
        written.add(manifest.read_text())
        assert len(load_manifest(manifest)) == len(updates)
    assert len(written) == 1, "recording is not idempotent"
