"""Resource measurement: RAM, wall clock and disk per 100 pages.

**This must run on x86_64 Linux with the real TEI image**, not on the development machine.
Its whole output is a sentence for a customer's IT department, and that sentence is
worthless if it was measured under emulation on hardware nobody will deploy.

It runs on a subset of the corpus, deliberately. Peak memory is decided by batch size, not
by corpus size, and time and disk are *rates* — measuring 300 pages and stating a rate is
honest, where extrapolating a total from it would not be. The subset is stated in the
output so nobody has to guess what was measured.
"""

# pyright: reportCallIssue=false, reportArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

import json
import os
import subprocess
import time
from dataclasses import dataclass, field

from eval.corpus import load_manifest
from eval.embedder import DIMENSION, get_embedder
from eval.pipeline import Chunk, chunk_document, connect, index_all, reset_schema

# A spread of categories rather than the smallest files: a born-digital regulation, a
# two-column paper, a scan and a financial report. Roughly 300 pages, which is enough for a
# rate and short enough to finish on four cores of a 2013 microarchitecture.
SUBSET = ("gdpr", "attention-is-all-you-need", "nasa-scanned-report", "boe-monetary-policy")


@dataclass
class Stage:
    name: str
    seconds: float = 0.0
    peak_container_mb: dict[str, float] = field(default_factory=dict)


def container_memory() -> dict[str, float]:
    """Current memory use per container, in MB.

    Read from `docker stats` rather than from inside the process: the embedder runs in the
    TEI container, and the number that matters for a hardware statement is what the whole
    deployment holds, not what one Python process allocated.
    """
    try:
        output = subprocess.run(
            ["sudo", "docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception:
        return {}

    usage: dict[str, float] = {}
    for line in output.strip().splitlines():
        if "\t" not in line:
            continue
        name, memory = line.split("\t", 1)
        raw = memory.split("/")[0].strip()
        for suffix, factor in (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024)):
            if raw.endswith(suffix):
                usage[name] = float(raw[: -len(suffix)]) * factor
                break
    return usage


def _merge_peaks(into: dict[str, float], sample: dict[str, float]) -> None:
    for name, value in sample.items():
        into[name] = max(into.get(name, 0.0), value)


def measure() -> dict[str, object]:
    documents = [d for d in load_manifest() if d.id in SUBSET and d.path.exists()]
    pages = sum(d.pages or 0 for d in documents)
    if not documents:
        raise SystemExit("subset not downloaded — run `python -m eval fetch` first")

    stages: list[Stage] = []
    peaks: dict[str, float] = {}

    # --- parse and chunk ------------------------------------------------------------
    stage = Stage("parse+chunk")
    started = time.perf_counter()
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document.id))
        _merge_peaks(peaks, container_memory())
    stage.seconds = time.perf_counter() - started
    stage.peak_container_mb = dict(peaks)
    stages.append(stage)
    print(f"parse+chunk: {len(chunks)} chunks in {stage.seconds:.0f}s", flush=True)

    # --- embed ----------------------------------------------------------------------
    stage = Stage("embed")
    started = time.perf_counter()
    embedder = get_embedder()
    vectors = embedder.encode([chunk.text for chunk in chunks], batch=4)
    _merge_peaks(peaks, container_memory())
    stage.seconds = time.perf_counter() - started
    stage.peak_container_mb = dict(peaks)
    stages.append(stage)
    print(f"embed: {stage.seconds:.0f}s", flush=True)

    # --- index ----------------------------------------------------------------------
    stage = Stage("index+hnsw")
    started = time.perf_counter()
    connection = connect()
    reset_schema(connection)
    index_all(connection, chunks, vectors)
    _merge_peaks(peaks, container_memory())
    stage.seconds = time.perf_counter() - started
    stage.peak_container_mb = dict(peaks)
    stages.append(stage)
    print(f"index: {stage.seconds:.0f}s", flush=True)

    # --- disk -----------------------------------------------------------------------
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_total_relation_size('eval_chunks'), pg_relation_size('eval_chunks_hnsw')"
        )
        sizes = cursor.fetchone()
        assert sizes is not None, "the table was just written; it exists"
        total_bytes, hnsw_bytes = sizes
    connection.close()

    per_100 = 100 / pages
    return {
        "host": {
            "arch": os.uname().machine,
            "cores": os.cpu_count(),
            "embedder": type(embedder).__name__,
            "dimension": DIMENSION,
        },
        "subset": {
            "documents": [d.id for d in documents],
            "pages": pages,
            "chunks": len(chunks),
        },
        "per_100_pages": {
            "chunks": round(len(chunks) * per_100, 1),
            "parse_chunk_seconds": round(stages[0].seconds * per_100, 1),
            "embed_seconds": round(stages[1].seconds * per_100, 1),
            "index_seconds": round(stages[2].seconds * per_100, 1),
            "total_seconds": round(sum(s.seconds for s in stages) * per_100, 1),
            "disk_mb": round(total_bytes / 1_048_576 * per_100, 2),
            "hnsw_mb": round(hnsw_bytes / 1_048_576 * per_100, 2),
        },
        "peak_container_memory_mb": {k: round(v, 1) for k, v in sorted(peaks.items())},
        "stages": [{"name": s.name, "seconds": round(s.seconds, 1)} for s in stages],
    }


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2))
