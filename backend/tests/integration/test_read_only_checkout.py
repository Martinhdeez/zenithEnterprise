"""The guard that keeps a test from writing into the checkout, held to the same standard.

`read_only_checkout` in `conftest.py` compares the tree before and after the session and
fails if anything moved. It exists because the rule it enforces — write to `tmp_path`, never
to a path derived from a module constant — was broken twice in two days, in
`backend/eval/corpus.toml` and then in `backend/eval/documents/image-only.pdf`. Neither was
caught by a test, both were caught by two `make check` runs sharing a checkout, and by then
the symptom was a corrupt file in an unrelated test on someone else's machine.

A guard that has never been seen to fail is a guard nobody should trust, and this one runs
at session teardown where a silent break would be invisible for months. So the walk and the
diff are exercised here against a tree built for the purpose — in `tmp_path`, which is the
whole point.
"""

from pathlib import Path

from conftest import PERMITTED_WRITES, tree, written_between


def populate(root: Path) -> None:
    (root / "app").mkdir(parents=True, exist_ok=True)
    (root / "app" / "service.py").write_text("x = 1\n")
    (root / "corpus.toml").write_text("[[document]]\n")


def test_a_new_file_is_seen(tmp_path: Path) -> None:
    """The image-only fixture's case: a path that did not exist before the run does after."""
    populate(tmp_path)
    before = tree(tmp_path)

    (tmp_path / "app" / "documents.pdf").write_bytes(b"%PDF-1.7")

    assert written_between(before, tree(tmp_path)) == [Path("app/documents.pdf")]


def test_a_rewritten_file_is_seen_even_at_the_same_length(tmp_path: Path) -> None:
    """The corpus manifest's case, and the one a size comparison alone would miss.

    `fetch --record` put the manifest back byte for byte in a `finally`, so the file ended
    the run exactly as long as it started — and for the length of each write it was empty.
    A run that restores what it damaged has still damaged it.
    """
    populate(tmp_path)
    before = tree(tmp_path)
    original = (tmp_path / "corpus.toml").read_text()

    (tmp_path / "corpus.toml").write_text("")
    (tmp_path / "corpus.toml").write_text(original)

    assert written_between(before, tree(tmp_path)) == [Path("corpus.toml")]


def test_a_deleted_file_is_seen(tmp_path: Path) -> None:
    populate(tmp_path)
    before = tree(tmp_path)

    (tmp_path / "corpus.toml").unlink()

    assert written_between(before, tree(tmp_path)) == [Path("corpus.toml")]


def test_a_run_that_touches_nothing_reports_nothing(tmp_path: Path) -> None:
    """The other half. A guard that fires on an untouched tree would be turned off within
    a week, and then it would be a comment."""
    populate(tmp_path)
    before = tree(tmp_path)

    (tmp_path / "corpus.toml").read_text()

    assert written_between(before, tree(tmp_path)) == []


def test_bytecode_and_caches_are_not_writes(tmp_path: Path) -> None:
    """Every run writes these, so a guard that counted them would never be green."""
    populate(tmp_path)
    before = tree(tmp_path)

    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "service.pyc").write_bytes(b"\x00")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "lastfailed").write_text("{}")

    assert written_between(before, tree(tmp_path)) == []


def test_a_declared_exception_is_not_a_write(tmp_path: Path) -> None:
    """`PERMITTED_WRITES` is the escape hatch, and it is a dictionary rather than a habit:
    every entry carries the reason it is there, so an exception is something a reviewer
    reads rather than something they never see."""
    permitted = next(iter(PERMITTED_WRITES))
    populate(tmp_path)
    before = tree(tmp_path)

    (tmp_path / permitted).mkdir()
    (tmp_path / permitted / "gdpr.json").write_text("[]")

    assert written_between(before, tree(tmp_path)) == []
    assert all(reason for reason in PERMITTED_WRITES.values()), "an exception with no reason"
