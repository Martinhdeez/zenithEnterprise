"""Sizes as the person reading them sees them elsewhere on their screen.

`file exceeds the 104857600 byte limit` was what somebody got for dragging a large PDF into
the uploader. Every word true, and none of it answering the only question they have.
"""

import pytest

from app.common.units import bytes_as_text


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (104_857_600, "105 MB"),
        (52_428_800, "52 MB"),
        (2_000_000_000, "2.0 GB"),
        (1_500_000, "1.5 MB"),
        (52_000, "52 kB"),
        (900, "900 bytes"),
        (0, "0 bytes"),
    ],
)
def test_it_reads_the_way_a_file_listing_does(count: int, expected: str) -> None:
    assert bytes_as_text(count) == expected


def test_it_is_decimal_rather_than_binary() -> None:
    """`MB` means a million bytes here, which is what every file manager shows.

    `MiB` is more precise and would be comparing against a figure the user does not have on
    screen — the point of the message is that they can look at their own file listing and see
    whether their file is over the line.
    """
    assert bytes_as_text(1_000_000) == "1.0 MB"


def test_a_round_limit_reads_as_a_round_number() -> None:
    """`104.9 MB` invites somebody to wonder what happens at `104.8`."""
    assert "." not in bytes_as_text(104_857_600)
