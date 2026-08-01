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
        _record(updates)
        print(f"\nRecorded {len(updates)} checksum(s) in the manifest.")

    return 1 if failures else 0


def _record(updates: dict[str, tuple[str, int, int]]) -> None:
    """Write checksums back into the manifest.

    Edited as text rather than re-serialised, because `tomllib` reads but does not write,
    and a round-trip through another library would reformat the comments that carry the
    reason each document is in the set. Those comments are the useful part.
    """
    from eval.corpus import MANIFEST

    lines = MANIFEST.read_text().splitlines()
    output: list[str] = []
    current: str | None = None

    for line in lines:
        if line.startswith("id = "):
            current = line.split("=", 1)[1].strip().strip('"')
        # `why` is the last key of every entry, so appending after its closing delimiter
        # keeps the recorded facts together and below the prose.
        if line == '"""' and current in updates:
            digest, pages, size = updates[current]
            output.append(line)
            output.append(f'sha256 = "{digest}"')
            output.append(f"pages = {pages}")
            output.append(f"bytes = {size}")
            current = None
            continue
        output.append(line)

    MANIFEST.write_text("\n".join(output) + "\n")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "fetch":
        print("usage: python -m eval fetch [--record]")
        return 2
    return fetch(record="--record" in sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
