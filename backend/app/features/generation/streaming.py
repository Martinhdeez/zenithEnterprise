"""Streaming a cited answer without letting an invented citation escape.

F8 deferred this for a real reason: the citation binder needs the whole text before it can
strip an invalid marker, so naive streaming ships tokens the validator has not seen. Two
rules enforce the 0% gate, and streaming treats them differently — deliberately, and
visibly.

**Rule 1 — an invalid marker never reaches the client.** A citation marker is short and
self-delimiting, so the stream holds back text from `[` until the matching `]` arrives,
checks the numbers against the shortlist, and emits or drops. The delay is bounded by one
marker's length. This rule survives streaming intact.

**Rule 2 — an answer with no valid citation is discarded.** This is knowable only at the
end, and by then the prose has been sent. The stream therefore ends with an authoritative
`result` event, and a client must not treat a streamed answer as final until it arrives.
That cost is why streaming is opt-in and `POST /query` keeps the strict behaviour.

The buffer is a small state machine rather than a regex over accumulated text, because the
input arrives in arbitrary chunks: `[`, `1`, `2`, `]` can be four tokens, and a regex that
needs the whole marker present would never match.
"""

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field

# A marker that never closes is prose, not a citation. Without a ceiling, a model that emits
# a stray `[` would swallow the rest of the answer into the buffer and the client would
# receive nothing at all — a worse failure than showing the bracket.
MAX_MARKER = 24


@dataclass
class MarkerFilter:
    """Emits text with out-of-range citation markers removed, incrementally.

    `valid` is the set of passage numbers actually sent to the model. Anything else the
    model writes in brackets is an invented source, and the whole point of this class is
    that it is dropped *before* the client sees it rather than after.
    """

    valid: frozenset[int]
    buffer: str = ""
    # Markers the model invented, counted for the same reason `citations.bind` counts them:
    # it is what the RNF-06 certification suite scores a model on.
    fabricated: int = 0
    # Whether anything survived. The caller needs this to know, at the end, whether rule 2
    # has been broken — an answer that cited nothing valid has to be withdrawn.
    cited: set[int] = field(default_factory=set[int])
    # Set when a marker was dropped entirely, so the space that followed it can be dropped
    # too. `citations._tidy` repairs this with a regex over the finished answer; streaming
    # has no finished answer to repair, so the spacing has to be right as it goes.
    # Otherwise "the penalty is 4% [9] of turnover" ships with a visible double space —
    # cosmetic, and exactly the kind of blemish that makes a reader distrust the citation
    # that survived next to it.
    _swallow_space: bool = False

    def feed(self, chunk: str) -> str:
        """Take a token, return the text that is safe to send now.

        Text before a `[` goes out immediately; from `[` onwards it is held until the
        marker resolves. That is the entire trade — a few characters of latency for a
        guarantee that nothing invented is ever displayed.
        """
        out: list[str] = []
        for character in chunk:
            if not self.buffer:
                if character == "[":
                    self.buffer = character
                elif self._swallow_space and character == " ":
                    # The space that trailed a marker nobody will see.
                    self._swallow_space = False
                else:
                    self._swallow_space = False
                    out.append(character)
                continue

            self.buffer += character
            if character == "]":
                resolved = self._resolve(self.buffer)
                self._swallow_space = not resolved
                out.append(resolved)
                self.buffer = ""
            elif len(self.buffer) > MAX_MARKER or not _plausible(self.buffer):
                # Not a marker after all — a bracket in the prose. Release it verbatim
                # rather than holding it, and do not count it as a fabrication.
                out.append(self.buffer)
                self.buffer = ""
        return "".join(out)

    def flush(self) -> str:
        """Whatever is still held when the model stops.

        An unterminated `[` at the end of a stream is text the model meant to write, so it
        is released rather than swallowed.
        """
        remainder, self.buffer = self.buffer, ""
        return remainder

    def _resolve(self, marker: str) -> str:
        numbers = [part.strip() for part in marker[1:-1].split(",")]
        if not all(part.isdigit() for part in numbers) or not numbers:
            return marker

        survivors = [int(part) for part in numbers if int(part) in self.valid]
        self.fabricated += len(numbers) - len(survivors)
        self.cited.update(survivors)
        return "".join(f"[{number}]" for number in survivors)


def _plausible(buffer: str) -> bool:
    """Could this still become a citation marker?

    Only digits, commas and spaces follow a `[` in one. The moment a letter appears the
    buffer is prose — `[see annex]` is not a citation — and holding it back any longer
    delays text for nothing.
    """
    return all(character.isdigit() or character in ", " for character in buffer[1:])


async def filtered(tokens: AsyncIterator[str], valid: Iterable[int]) -> AsyncIterator[str]:
    """The filter as a stream transformer, which is how the router uses it."""
    marker_filter = MarkerFilter(valid=frozenset(valid))
    async for token in tokens:
        text = marker_filter.feed(token)
        if text:
            yield text
    remainder = marker_filter.flush()
    if remainder:
        yield remainder
