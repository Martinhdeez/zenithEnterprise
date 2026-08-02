"""Does the two-column detector work on real documents?

M0 found that §7's routing rule catches two of three extraction failures and misses column
interleaving entirely — the IRS case, where every word is present and correct and only the
meaning is wrong. F5 added a detector for it, with thresholds chosen by reasoning rather
than by measurement, which is exactly the kind of thing this corpus exists to check.

The corpus supplies ground truth for free: the arXiv papers are two-column throughout, the
EU regulations are single-column throughout, and the difference between them is what tells
us whether the detector has found layout or merely found something.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from eval.corpus import load_manifest

# What each document really is, read off the PDFs rather than inferred from a metric. This
# is the ground truth the numbers are scored against.
TRUE_LAYOUT: dict[str, str] = {
    # NeurIPS format is single-column. Recorded wrongly the first time — the prediction
    # that arXiv papers would be 85–95% flagged rested on that error, not on the detector.
    "attention-is-all-you-need": "single-column",
    "rag-paper": "single-column",
    # NAACL format, genuinely two-column.
    "bert-paper": "two-column",
    "gdpr": "single-column",
    "eu-ai-act": "single-column",
    "eu-digital-services-act": "single-column",
    "infrastructure-act": "single-column",
    "boe-monetary-policy": "single-column",
    "irs-form-1040": "tabular",
    "irs-1040-instructions": "tabular",
    "irs-pub-15": "tabular",
    "nasa-technical-report": "single-column",
    # M0 found this one *has* an OCR layer, just a poor one: 925 characters per page
    # against the ~4,000 an equivalent text page carries. It is a degraded scan, not an
    # absent text layer, and labelling it the latter was the second error in this table.
    "nasa-scanned-report": "degraded-scan",
    "image-only": "no-text-layer",
}


@dataclass
class DocumentReport:
    document: str
    true_layout: str
    pages: int
    fast: int
    layout: int
    unreadable: int
    spacing_warnings: int
    layout_pages: list[int]

    @property
    def flagged_ratio(self) -> float:
        readable = self.fast + self.layout
        return self.layout / readable if readable else 0.0


def analyse(document_id: str, path: Path, limit: int | None = None) -> DocumentReport:
    from app.features.ingestion.parsers.pdfplumber_parser import PdfPlumberParser
    from app.features.ingestion.routing import Route, decide

    pages = PdfPlumberParser().parse(path)
    if limit:
        pages = pages[:limit]

    report = DocumentReport(
        document=document_id,
        true_layout=TRUE_LAYOUT.get(document_id, "unknown"),
        pages=len(pages),
        fast=0,
        layout=0,
        unreadable=0,
        spacing_warnings=0,
        layout_pages=[],
    )

    for page in pages:
        # `ocr_available=False` so an un-OCR'd scan reports as unreadable rather than being
        # folded in with the pages that genuinely need layout parsing. The two are different
        # findings and averaging them would hide both.
        decision = decide(page, ocr_available=False)
        if decision.route is Route.UNREADABLE:
            report.unreadable += 1
        elif decision.route is Route.LAYOUT:
            report.layout += 1
            report.layout_pages.append(page.page_num)
        else:
            report.fast += 1
        if any("spacing" in warning for warning in decision.warnings):
            report.spacing_warnings += 1

    return report


def run(limit: int | None = None, output: Path | None = None) -> list[DocumentReport]:
    reports: list[DocumentReport] = []
    for document in load_manifest():
        if not document.path.exists():
            print(f"skip {document.id}: not downloaded")
            continue
        report = analyse(document.id, document.path, limit)
        reports.append(report)
        print(
            f"{report.document:<28} {report.true_layout:<14} "
            f"{report.pages:>4}p  layout={report.layout:>4} "
            f"fast={report.fast:>4} unreadable={report.unreadable:>4} "
            f"({report.flagged_ratio:.0%} flagged)",
            flush=True,
        )

    if output:
        output.write_text(json.dumps([asdict(report) for report in reports], indent=2))
    return reports


def verdict(reports: list[DocumentReport]) -> list[str]:
    """Score against the thresholds committed before the run.

    Printed rather than asserted: this is a measurement, and a measurement that fails should
    produce a number to think about, not a stack trace.
    """
    by_layout: dict[str, list[DocumentReport]] = {}
    for report in reports:
        by_layout.setdefault(report.true_layout, []).append(report)

    lines: list[str] = []
    for name, expectation in (("two-column", "≥ 70%"), ("single-column", "≤ 5%")):
        group = by_layout.get(name, [])
        if not group:
            continue
        readable = sum(item.fast + item.layout for item in group)
        flagged = sum(item.layout for item in group)
        ratio = flagged / readable if readable else 0.0
        target = ratio >= 0.70 if name == "two-column" else ratio <= 0.05
        lines.append(
            f"{'PASS' if target else 'FAIL'}  {name}: {flagged}/{readable} flagged "
            f"({ratio:.1%}), threshold {expectation}"
        )

    scans = by_layout.get("no-text-layer", [])
    if scans:
        readable = sum(item.fast + item.layout for item in scans)
        lines.append(
            f"{'PASS' if readable == 0 else 'FAIL'}  no-text-layer: "
            f"{readable} page(s) treated as readable, expected 0"
        )
    return lines
