"""Snapshot a public HTML article for an isolated, reproducible R1 trial.

This is an evaluation extractor, not a production parser. Its output is the
declared trial source representation and is stored outside Git with its hashes.
"""

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path

import httpx

EXTRACTOR_VERSION = "zenith-eval-html-main-v1"
USER_AGENT = "zenith-evidence-v3-eval/1.0 (public retrieval research)"
BLOCKS = {"h1", "h2", "h3", "h4", "p", "li", "tr", "td", "th", "blockquote"}
SKIP = {"script", "style", "nav", "form", "noscript", "svg"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}


class MainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main" and not self.depth:
            self.depth = 1
        elif self.depth and tag not in VOID:
            self.depth += 1
        if self.depth and tag in SKIP:
            self.skip_depth += 1
        if self.depth and not self.skip_depth and tag == "br":
            self.parts.append(" ")
        if self.depth and not self.skip_depth and tag in BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if not self.depth:
            return
        if not self.skip_depth and tag in BLOCKS:
            self.parts.append("\n")
        if tag in SKIP and self.skip_depth:
            self.skip_depth -= 1
        if tag not in VOID:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.depth and not self.skip_depth:
            self.parts.append(data)

    def extracted(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip() + "\n"


def fetch_public_snapshot(url: str, destination: Path) -> dict[str, str | int]:
    if not url.startswith("https://www.epa.gov/"):
        raise ValueError("trial fetch is restricted to the approved public EPA host")
    with httpx.Client(
        timeout=30.0,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        response = client.get(url)
        response.raise_for_status()
    if response.url.host != "www.epa.gov" or "text/html" not in response.headers.get(
        "content-type", ""
    ):
        raise ValueError("unexpected snapshot source")
    if len(response.content) > 500_000:
        raise ValueError("public article exceeds trial fetch bound")
    parser = MainText()
    parser.feed(response.text)
    text = parser.extracted()
    if len(text) < 2_000:
        raise ValueError("article extraction produced too little text")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8", newline="")
    manifest: dict[str, str | int] = {
        "url": url,
        "extractor": EXTRACTOR_VERSION,
        "html_sha256": hashlib.sha256(response.content).hexdigest(),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "characters": len(text),
    }
    destination.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
