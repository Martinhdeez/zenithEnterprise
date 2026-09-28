"""The exploratory report counts exact source ranges, never repeated overlap."""

import pytest

from eval.lossless_trial import TrialUnit, choose, covers_all, displayed_range, line_ranges, rank
from eval.public_html import MainText


def test_source_line_mapping_and_interval_union() -> None:
    text = "A\r\nB😀\nC"
    ranges = line_ranges(text)
    assert [text[start:end] for start, end in ranges] == ["A\r\n", "B😀\n", "C"]
    overlapping = [TrialUnit("first", 0, 4, "A\r\nB"), TrialUnit("second", 3, 6, "B😀\n")]
    assert covers_all(overlapping, (ranges[0], ranges[1]))
    assert not covers_all(overlapping, (ranges[2],))
    assert displayed_range("  A\r\n", 0, 5) == (2, 3)


def test_equal_budget_selection_and_stable_rank() -> None:
    units = [TrialUnit("b", 0, 3, "one"), TrialUnit("a", 3, 6, "two")]
    assert [item.id for item in rank(units, [[1.0, 0.0], [1.0, 0.0]], [1.0, 0.0])] == ["a", "b"]
    assert [item.id for item in choose(units, budget=3)] == ["b"]
    with pytest.raises(ValueError, match="vectors"):
        rank(units, [[1.0]], [1.0])


def test_public_main_extractor_ignores_navigation_and_keeps_article_text() -> None:
    parser = MainText()
    parser.feed(
        "<nav>hidden</nav><main><h1>Visible</h1><p>One<br>two</p><script>secret</script><p>Three</p></main>"
    )
    assert parser.extracted() == "Visible\nOne two\nThree\n"
