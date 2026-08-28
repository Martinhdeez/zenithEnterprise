"""`python -m eval fetch` — build the corpus locally.

Separate from the `zenith` CLI on purpose. That one is production code shipped to
customers; this is laboratory tooling that only ever runs on a developer machine, and
mixing them would put an evaluation command in a customer's `--help`.
"""

import sys

import httpx

from eval.corpus import USER_AGENT, checksum, download, load_manifest, page_count, verify


def fetch(record: bool) -> int:
    documents = load_manifest()
    updates: dict[str, tuple[str, int, int]] = {}
    failures = 0

    with httpx.Client(
        follow_redirects=True, timeout=180.0, headers={"User-Agent": USER_AGENT}
    ) as client:
        for document in documents:
            if document.path.exists() and document.sha256:
                ok, detail = verify(document)
                print(f"{'ok  ' if ok else 'FAIL'} {document.id:<28} {detail}")
                failures += not ok
                continue

            try:
                path = download(document, client)
            except Exception as exc:  # noqa: BLE001 - one bad URL must not stop the rest
                print(f"FAIL {document.id:<28} {type(exc).__name__}: {exc}")
                failures += 1
                continue

            digest, pages, size = checksum(path), page_count(path), path.stat().st_size
            updates[document.id] = (digest, pages, size)
            print(f"got  {document.id:<28} {pages} pages, {size / 1_048_576:.1f} MB")

    if record and updates:
        record_checksums(updates)
        print(f"\nRecorded {len(updates)} checksum(s) in the manifest.")

    return 1 if failures else 0


def record_checksums(updates: dict[str, tuple[str, int, int]]) -> None:
    """Write checksums back into the manifest.

    Edited as text rather than re-serialised, because `tomllib` reads but does not write,
    and a round-trip through another library would reformat the comments that carry the
    reason each document is in the set. Those comments are the useful part.
    """
    from eval.corpus import MANIFEST

    recorded = ("sha256 = ", "pages = ", "bytes = ")
    output: list[str] = []
    current: str | None = None
    written = False

    for line in MANIFEST.read_text().splitlines():
        if line.startswith("id = "):
            current = line.split("=", 1)[1].strip().strip('"')
            written = False

        # Drop values recorded by a previous run for this document. Without this the file
        # gains duplicate TOML keys, and duplicates are not merely untidy: parsing fails
        # with "Cannot overwrite a value" and the corpus becomes unloadable.
        #
        # `current` deliberately stays set after the insertion below. The first attempt at
        # this fix cleared it there, which switched the skip off for exactly the lines it
        # was meant to remove — so a second run was clean and a third was corrupt again.
        if current in updates and line.startswith(recorded):
            continue

        # `why` is the last key of every entry, so inserting after its closing delimiter
        # keeps the recorded facts together and below the prose.
        if line == '"""' and current in updates and not written:
            digest, pages, size = updates[current]
            output.extend([line, f'sha256 = "{digest}"', f"pages = {pages}", f"bytes = {size}"])
            written = True
            continue

        output.append(line)

    MANIFEST.write_text("\n".join(output) + "\n")


def layout(limit: int | None) -> int:
    """Score the two-column detector against the corpus.

    Its thresholds were chosen by reasoning rather than by measurement, which is precisely
    the kind of claim this corpus exists to settle.
    """
    from pathlib import Path

    from eval.layout import run, verdict

    reports = run(limit=limit, output=Path(__file__).resolve().parent / "layout-report.json")
    print()
    for line in verdict(reports):
        print(line)
    return 0


def grounding() -> int:
    """Extraction and context rates, and the Docling verdict.

    Needs no language model, which is what makes it runnable anywhere and RNF-07 compliant
    by construction — there is nothing here to send to anyone.
    """
    import asyncio

    from eval.grounding import run

    asyncio.run(run())
    return 0


def answers() -> int:
    """The end-to-end run, through whatever `ZENITH_LLM_PROVIDER` selects."""
    import asyncio

    from eval.answers import run

    asyncio.run(run())
    return 0


def live() -> int:
    """Recall of the running installation, over HTTP — the deployment, not the design."""
    from eval.live import run

    if "--token" not in sys.argv:
        print("usage: python -m eval live --token <jwt> [--url http://localhost:8000]")
        return 2
    token = sys.argv[sys.argv.index("--token") + 1]
    url = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else "http://localhost:8000"
    return run(url, token)


def separation() -> int:
    """Whether retrieval can tell an answerable question from one the corpus cannot answer.

    Read-only and product-neutral: it runs searches and compares two score distributions.
    Needs a token, because it goes over HTTP against the real corpus — synthetic data would
    measure the wrong thing entirely.
    """
    from eval.separation import run

    if "--token" not in sys.argv:
        print("usage: python -m eval separation --token <jwt> [--url http://localhost:8000]")
        return 2
    token = sys.argv[sys.argv.index("--token") + 1]
    url = sys.argv[sys.argv.index("--url") + 1] if "--url" in sys.argv else "http://localhost:8000"
    return run(url, token)


def ef_search() -> int:
    """What `hnsw_ef_search` is worth on this machine. Read-only, needs no token."""
    from eval.ef_search import run

    return run()


def rerank_depth() -> int:
    """How deep the cross-encoder should read on this machine. Read-only, needs no token."""
    from eval.rerank_depth import run

    return run()


def latency(repeats: int | None) -> int:
    """Where the milliseconds of one search go. Read-only, needs no token."""
    from eval.latency import REPEATS, run

    return run(repeats if repeats is not None else REPEATS)


def iterative_scan() -> int:
    """What iterative scan costs and buys end to end, unscoped. Read-only, needs no token."""
    from eval.iterative_scan import run

    return run()


COMMANDS = (
    "fetch",
    "layout",
    "grounding",
    "answers",
    "live",
    "separation",
    "ef-search",
    "rerank-depth",
    "latency",
    "iterative-scan",
)


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(
            "usage: python -m eval fetch [--record]\n"
            "       python -m eval layout [--limit N]\n"
            "       python -m eval grounding\n"
            "       python -m eval answers\n"
            "       python -m eval live --token <jwt> [--url http://localhost:8000]\n"
            "       python -m eval separation --token <jwt> [--url http://localhost:8000]\n"
            "       python -m eval ef-search\n"
            "       python -m eval rerank-depth\n"
            "       python -m eval latency [--repeats N]\n"
            "       python -m eval iterative-scan"
        )
        return 2
    if sys.argv[1] == "iterative-scan":
        return iterative_scan()
    if sys.argv[1] == "separation":
        return separation()
    if sys.argv[1] == "latency":
        repeats = None
        if "--repeats" in sys.argv:
            repeats = int(sys.argv[sys.argv.index("--repeats") + 1])
        return latency(repeats)
    if sys.argv[1] == "rerank-depth":
        return rerank_depth()
    if sys.argv[1] == "ef-search":
        return ef_search()
    if sys.argv[1] == "live":
        return live()
    if sys.argv[1] == "grounding":
        return grounding()
    if sys.argv[1] == "answers":
        return answers()
    if sys.argv[1] == "layout":
        limit = None
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        return layout(limit)
    return fetch(record="--record" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
