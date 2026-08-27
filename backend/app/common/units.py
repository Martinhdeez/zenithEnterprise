"""Numbers as a person reads them.

One function, in `common/`, because the alternative is each call site formatting bytes its own
way and a product that says `52428800` in one place, `50.0 MB` in another and `50 MiB` in a
third.

The reason it exists at all: `file exceeds the 104857600 byte limit` was the message somebody
got for dragging a large PDF into the uploader. Every word of it is true and none of it
answers the only question they have, which is how much smaller the file needs to be.
"""

#: Decimal, not binary. `MB` here means 1,000,000 bytes, which is what every file manager and
#: every operating system's file listing shows — so the number in this message matches the
#: number the user is looking at in their own window. `MiB` is more precise and would be
#: comparing against a figure nobody has on screen.
_UNITS = (("GB", 1_000_000_000), ("MB", 1_000_000), ("kB", 1_000))


def bytes_as_text(count: int) -> str:
    """`104857600` -> `105 MB`.

    No decimal place unless the number needs one: a limit reads as a round figure and
    `104.9 MB` invites somebody to wonder what happens at `104.8`.
    """
    for unit, size in _UNITS:
        if count >= size:
            value = count / size
            return f"{value:.0f} {unit}" if value >= 10 else f"{value:.1f} {unit}"
    return f"{count} bytes"
