"""Cached page text, and a search over it.

Extracting 1,842 pages takes minutes, and writing the question set means reading across the
corpus repeatedly. The cache makes that iteration cheap.

It is also what makes verification mechanical. Every question records the exact sentence its
answer comes from, and a test asserts that sentence really is on the page claimed — so
"hand-verified" is a property the suite re-checks rather than a promise in a commit message.
"""

import json
import warnings

from eval.corpus import EVAL_DIR, load_manifest

CACHE = EVAL_DIR / ".text-cache"


def extract(document_id: str, force: bool = False) -> list[str]:
    """Page text for one document, one string per page, cached on disk.

    The cache is shared on purpose — three tests in `test_questions.py` walk the whole
    corpus through it, and giving each run its own copy would cost minutes of extraction
    per run to avoid a race that a rename closes. So the cache stays where it is and the
    write is made atomic instead.
    """
    import pdfplumber

    CACHE.mkdir(exist_ok=True)
    cached = CACHE / f"{document_id}.json"
    if cached.exists() and not force:
        return json.loads(cached.read_text())

    document = next(entry for entry in load_manifest() if entry.id == document_id)
    with warnings.catch_warnings():
        # pdfplumber is loud about malformed font descriptors in older PDFs, which is
        # exactly the kind of document this corpus is made of.
        warnings.simplefilter("ignore")
        with pdfplumber.open(document.path) as pdf:
            pages = [(page.extract_text() or "") for page in pdf.pages]

    # Written beside the entry and renamed over it, so `exists()` above is only ever true
    # of a complete file. `write_text` truncates first: a second run reading the cache in
    # that window gets `json.loads("")`, and an interruption leaves a truncated entry that
    # every later run accepts — `force` defaults to False, so the corpus tests would stay
    # broken on that machine until someone deleted `.text-cache` by hand.
    partial = cached.with_suffix(".partial")
    partial.write_text(json.dumps(pages))
    partial.replace(cached)
    return pages


def normalise(text: str) -> str:
    """Collapse every run of whitespace to a single space.

    PDF text carries line breaks wherever the original had them, so a phrase a human reads
    as continuous — "this is Bank Rate" — is stored as "this is\nBank Rate". Matching the
    raw text means anchors silently fail whenever they happen to cross a line, which is
    most of the time for anything longer than a few words.

    Found while building the question set: one anchor in twenty-five failed for exactly
    this reason, and it would have failed as a *missing answer* rather than as a bug.
    """
    return " ".join(text.split())


def page_text(document_id: str, page: int) -> str:
    """Text of one page, 1-indexed — page numbers in the question set are what a human
    would type into a viewer, not list offsets."""
    return extract(document_id)[page - 1]


def find(needle: str, document_ids: list[str] | None = None) -> list[tuple[str, int]]:
    """Every (document, page) whose text contains `needle`.

    Used while writing questions: draft the question, search for the phrase, and let this
    say which page to record. Guessing the page and checking later is how a question set
    ends up attributing an answer to the wrong page and quietly measuring nothing.
    """
    ids = document_ids or [document.id for document in load_manifest()]
    hits: list[tuple[str, int]] = []
    for document_id in ids:
        target = normalise(needle).lower()
        for index, text in enumerate(extract(document_id), start=1):
            if target in normalise(text).lower():
                hits.append((document_id, index))
    return hits


def cache_all() -> None:
    for document in load_manifest():
        if document.path.exists():
            pages = extract(document.id)
            print(f"{document.id:<28} {len(pages)} pages cached")


if __name__ == "__main__":
    cache_all()
