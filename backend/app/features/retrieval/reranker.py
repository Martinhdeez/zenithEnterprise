"""The cross-encoder, and why it is worth its latency.

The dense half compares two vectors computed separately, so a passage and a question are
each reduced to 1024 numbers before they ever meet. A cross-encoder reads them **together**
and scores the pair. It cannot be indexed — there is nothing to precompute — which is why it
runs on a shortlist rather than on the corpus, and why it is the last stage rather than the
first.

M0 measured the room it has: Recall@8 80%, Recall@50 95%. Every miss is already in the
candidate set at a rank between 9 and 50. The material is there; the order is wrong.

Same server, same image, same three lessons F5 paid for on real hardware — token-budget
batching, no `--max-concurrent-requests`, sequential requests. This one is additionally on
the interactive path, so it never retries.
"""

from dataclasses import dataclass

import httpx
import structlog

from app.core.config import settings
from app.core.hardware import Profile
from app.core.hardware import active as active_profile

# Reranking is not embedding, and borrowing embedding's 5-second budget was wrong about the
# work. An embedding call is one short forward pass; a rerank batch is a cross-encoder
# reading the question against every passage in it. Measured on a 14-core CPU with
# `bge-reranker-v2-m3`, saturating all of them: **1,785 ms per 400-token passage**, linear —
# a batch of four takes 7.1 s and tripped the 5-second limit every single time, so every
# search paid five seconds to then report `degraded` and use the fused order anyway.
#
# The number below is the whole `rank()` call, not one request, and it is deliberately far
# above the measured cost of `rerank_candidates` passages. It exists to catch a hung or
# swapping service, not to police how long a cross-encoder takes — the profile's candidate
# count is what bounds the wait, because that is the knob with a quality meaning.
RERANK_TIMEOUT = 60.0

log = structlog.get_logger()

MODEL = "BAAI/bge-reranker-v2-m3"


@dataclass(frozen=True, slots=True)
class Scored:
    index: int
    score: float


class RerankerUnavailable(Exception):
    """The reranker could not be reached, or refused the request.

    Not a `ZenithError`: this never becomes an HTTP status. A search whose reranker is down
    still answers, ordered by fusion, and says `degraded`. Turning it into a 503 would take
    a working search away from a customer to report a component they did not know existed.
    """


class TeiReranker:
    def __init__(
        self,
        url: str | None = None,
        profile: Profile | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = (url or settings.tei_rerank_url).rstrip("/")
        self.profile = profile or active_profile()
        self.transport = transport

    async def rank(self, question: str, passages: list[str]) -> list[Scored]:
        """Score every passage against the question, best first.

        Returns indexes rather than reordered text so the caller keeps whatever it attached
        to each passage — chunk id, page, boxes — without this module knowing about any of
        it.
        """
        if not passages:
            return []

        scores: list[Scored] = []
        async with httpx.AsyncClient(timeout=RERANK_TIMEOUT, transport=self.transport) as client:
            for start, batch in self.plan_batches(question, passages):
                response = await client.post(
                    f"{self.url}/rerank",
                    json={"query": question, "texts": batch, "return_text": False},
                )
                if response.status_code >= 400:
                    raise RerankerUnavailable(
                        f"the reranker returned {response.status_code}. Check that "
                        f"ZENITH_HARDWARE matches how the container was started."
                    )
                for item in response.json():
                    scores.append(Scored(index=start + int(item["index"]), score=item["score"]))

        # Ties broken by the incoming order, which is the fused order — so where the
        # cross-encoder is indifferent, the two halves' agreement still decides.
        return sorted(scores, key=lambda scored: (-scored.score, scored.index))

    def plan_batches(self, question: str, passages: list[str]) -> list[tuple[int, list[str]]]:
        """The same token budget as embedding, with the question counted once per pair.

        A rerank request carries the question alongside *every* passage, so a batch of eight
        1,200-character passages costs more than eight chunks did during ingestion. Reusing
        the profile's budget without accounting for that is how the 413 M0 hit comes back
        wearing a different hat.
        """
        from app.features.ingestion.chunking.chunker import CHARACTERS_PER_TOKEN

        question_tokens = len(question) // CHARACTERS_PER_TOKEN + 1
        batches: list[tuple[int, list[str]]] = []
        current: list[str] = []
        start = 0
        tokens = 0

        for offset, passage in enumerate(passages):
            cost = len(passage) // CHARACTERS_PER_TOKEN + 1 + question_tokens
            over_budget = current and tokens + cost > self.profile.max_batch_tokens
            over_count = len(current) >= self.profile.max_client_batch_size
            if over_budget or over_count:
                batches.append((start, current))
                current, tokens, start = [], 0, offset
            current.append(passage)
            tokens += cost

        if current:
            batches.append((start, current))
        return batches
