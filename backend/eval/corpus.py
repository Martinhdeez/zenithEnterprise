"""Fetching and verifying the evaluation corpus.

The documents are not committed. Ten public PDFs are hundreds of megabytes, they belong to
their publishers, and a repository is a poor place to store them. What is committed is the
manifest: where each one came from, why it is in the set, and the checksum of the bytes the
baseline was measured against.

**The checksum is the point.** Public URLs are not immutable — a regulator reissues a PDF,
an agency re-scans an archive — and a corpus that silently changed would move the baseline
without anyone noticing. A number that drifts for unknown reasons is worse than no number.
So a changed file fails loudly and someone decides what to do about it.
"""

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

EVAL_DIR = Path(__file__).resolve().parent
MANIFEST = EVAL_DIR / "corpus.toml"
DOCUMENTS = EVAL_DIR / "documents"

# Several of these hosts reject the default client string, and the SEC asks callers to
# identify themselves. Saying who we are is both the polite and the working option.
USER_AGENT = (
    "zenith-eval/0.1 (retrieval evaluation; https://github.com/Martinhdeez/zenithEnterprise)"
)


@dataclass(frozen=True, slots=True)
class Document:
    id: str
    url: str
    category: str
    difficulty: tuple[str, ...]
    why: str
    sha256: str | None = None
    pages: int | None = None
    bytes: int | None = None

    @property
    def path(self) -> Path:
        return DOCUMENTS / f"{self.id}.pdf"


def load_manifest(manifest: Path = MANIFEST) -> list[Document]:
    """Read the corpus manifest. The path is a parameter so that a test can exercise the
    recording code against a copy — writing the repository's own manifest makes every other
    reader in the run race a truncated file."""
    raw: dict[str, Any] = tomllib.loads(manifest.read_text())
    return [
        Document(
            id=entry["id"],
            url=entry["url"],
            category=entry["category"],
            difficulty=tuple(entry["difficulty"]),
            why=entry["why"].strip(),
            sha256=entry.get("sha256"),
            pages=entry.get("pages"),
            bytes=entry.get("bytes"),
        )
        for entry in raw["document"]
    ]


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def download(document: Document, client: httpx.Client) -> Path:
    """Fetch one document, streaming so a thousand-page bill does not sit in memory."""
    DOCUMENTS.mkdir(parents=True, exist_ok=True)
    partial = document.path.with_suffix(".partial")

    with client.stream("GET", document.url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type:
            raise ValueError(f"{document.id}: expected a PDF, got {content_type!r}")
        with partial.open("wb") as handle:
            for block in response.iter_bytes(1 << 20):
                handle.write(block)

    # Renamed only once complete, so an interrupted download can never be mistaken for a
    # valid file and checksummed into the manifest.
    partial.replace(document.path)
    return document.path


def page_count(path: Path) -> int:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return len(pdf.pages)


def verify(document: Document) -> tuple[bool, str]:
    """Check a local file against the manifest."""
    if not document.path.exists():
        return False, "not downloaded"
    if document.sha256 is None:
        return False, "no checksum recorded"
    actual = checksum(document.path)
    if actual != document.sha256:
        return False, f"checksum mismatch: expected {document.sha256[:12]}, got {actual[:12]}"
    return True, f"{document.pages} pages"
