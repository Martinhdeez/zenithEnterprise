"""The scoring rules behind every number F9 reports.

A measurement harness gets exactly as much trust as its arithmetic, and these are the two
places a bug would be invisible: an anchor check that silently never matches would report
0% and look like a finding, and one that matched too easily would report 100% and look like
success. Both are quiet failures, so both get a test.
"""

from eval.grounding import Grounded, contains, normalise, report, verdict


def grounded(kind: str, extracted: bool, in_context: bool) -> Grounded:
    return Grounded(
        question_id=f"{kind}-{extracted}-{in_context}",
        type=kind,
        extracted=extracted,
        in_context=in_context,
    )


def test_an_anchor_matches_across_a_line_break() -> None:
    """The failure this would otherwise cause is a silent 0%.

    A PDF extractor decides for itself where line breaks and double spaces land, so an
    anchor recorded as one phrase routinely comes back split. Comparing raw strings would
    measure the extractor's typography and call it a missing fact.
    """
    page = "the report states that CPI\n  inflation   is projected to fall"

    assert contains(page, "CPI inflation is projected")


def test_matching_ignores_case_but_not_content() -> None:
    assert contains("Form 1545-0074 must be filed", "form 1545-0074")
    assert not contains("Form 1545-0075 must be filed", "1545-0074")


def test_normalise_collapses_whitespace_without_removing_it() -> None:
    """Removing whitespace entirely would make `28.4` match inside `128.45`-style runs of
    digits split across a table cell. Collapsing keeps word boundaries intact."""
    assert normalise("  a   b\n\tc ") == "a b c"


def test_the_docling_verdict_counts_anchors_rather_than_opinions() -> None:
    """The business case for a heavy dependency, stated as a number.

    Every table anchor lost at extraction is a question no reranker, no larger context and
    no better model can ever answer. That count is the only thing that justifies putting
    Docling on the ingestion path of a product that runs on a 7.6 GB VPS.
    """
    results = [
        grounded("table", extracted=False, in_context=False),
        grounded("table", extracted=True, in_context=True),
        grounded("factual", extracted=False, in_context=False),
    ]

    summary = report(results)
    docling = summary["docling"]

    assert isinstance(docling, dict)
    assert docling["table_questions"] == 2
    # The factual loss is real and is reported separately — it is not evidence for Docling,
    # which is a table extractor.
    assert docling["anchors_lost_in_extraction"] == 1
    assert "JUSTIFIED" in "\n".join(verdict(summary))


def test_no_lost_table_anchor_argues_against_installing_docling() -> None:
    """The opposite verdict has to be stated as plainly as the supporting one.

    A harness that can only ever say "yes, buy it" is a harness that decided in advance.
    """
    results = [grounded("table", extracted=True, in_context=True)]

    lines = "\n".join(verdict(report(results)))

    assert "NOT JUSTIFIED" in lines


def test_context_is_scored_only_where_extraction_succeeded() -> None:
    """The chain is ordered for a reason: extraction bounds context, which bounds the
    answer. A question whose anchor never survived parsing cannot reach the model, so a
    report showing context above extraction would mean the anchor check is matching
    something it should not."""
    results = [
        grounded("factual", extracted=True, in_context=True),
        grounded("factual", extracted=True, in_context=False),
        grounded("factual", extracted=False, in_context=False),
    ]

    summary = report(results)

    assert summary["extraction_rate"] == round(2 / 3, 4)
    assert summary["context_rate"] == round(1 / 3, 4)
    assert summary["context_rate"] <= summary["extraction_rate"]  # type: ignore[operator]
