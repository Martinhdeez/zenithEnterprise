"""The embedding client, and the three ways M0 made it fail.

All three came from the same 8 GB VPS, and each one is a line in this file.

**413 Payload Too Large.** TEI ran with `--max-batch-tokens 2048`; the client sent eight
chunks of roughly 350 tokens, which is 2,800, and every request failed. The fix is not a
smaller constant — that repairs one chunk size and breaks on the next. A batch here is a
**token budget**, so a page of dense tables and a page of sparse prose both produce a legal
request with nobody tuning anything.

**Exit 139.** `--max-concurrent-requests 4` panics TEI `cpu-1.8` outright: *"Queue
background task dropped the receiver... This is a bug."* So concurrency is never pushed
into TEI. It is controlled on our side, by how many documents a worker processes at once,
because a queue we own degrades and a queue inside TEI crashes.

**OOM at 6.59 GB.** TEI holds the model plus the activations for a whole batch, so the
batch size is what decides peak memory. That is the profile's business, not this file's,
and this file reads it rather than guessing.
"""

import asyncio
from collections.abc import Iterator, Sequence

import httpx

from app.common.exceptions import ZenithError
from app.core.config import settings
from app.core.hardware import Profile
from app.core.hardware import active as active_profile

MODEL = "BAAI/bge-m3"
VERSION = "1"
DIMENSION = 1024

# Retries are for a busy server, not for a rejected request. A 413 will be a 413 every time
# — retrying it just fails more slowly.
RETRYABLE = (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)
ATTEMPTS = 3


class EmbeddingServiceError(ZenithError):
    """The embedding service could not be reached, or refused the request.

    A domain error rather than a bare `HTTPStatusError`, so the ingestion pipeline can put
    something an operator understands into `status_detail` instead of a driver traceback.
    """

    status_code = 503
    code = "embedding_service_unavailable"


class TeiClient:
    def __init__(
        self,
        url: str | None = None,
        profile: Profile | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = (url or settings.tei_embed_url).rstrip("/")
        self.profile = profile or active_profile()
        # Injected only by tests. Running the batching logic against a real TEI would mean
        # a container and a model download in the unit suite, and the behaviour worth
        # testing here is ours, not the server's.
        self.transport = transport

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed in profile-sized batches, sequentially.

        Sequentially on purpose, including on `gpu`: parallel requests are what the
        concurrency flag was for, and that flag is the one that panicked the server. The
        throughput knob we do use is the batch size, which TEI handles internally and
        safely.
        """
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=600.0, transport=self.transport) as client:
            for batch in self.plan_batches(texts):
                vectors.extend(await self.send_batch(client, batch))
        return vectors

    def plan_batches(self, texts: Sequence[str]) -> Iterator[list[str]]:
        """Accumulate until the token budget or the client batch limit is reached.

        Both bounds are real and neither implies the other: `max_batch_tokens` is what the
        server will process at once, `max_client_batch_size` is how many items it accepts
        per request. M0 hit the first; the second is TEI's own guard.

        A single text over budget is still yielded, alone. It will fail, and it should:
        silently truncating a customer's paragraph would produce an embedding for text
        nobody wrote, and the passage would then be retrievable by content it does not
        contain. The chunker's size bound is what prevents this from arising.
        """
        batch: list[str] = []
        tokens = 0
        for text in texts:
            estimate = _estimate_tokens(text)
            over_budget = batch and tokens + estimate > self.profile.max_batch_tokens
            over_count = len(batch) >= self.profile.max_client_batch_size
            if over_budget or over_count:
                yield batch
                batch, tokens = [], 0
            batch.append(text)
            tokens += estimate
        if batch:
            yield batch

    async def send_batch(self, client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            try:
                response = await client.post(f"{self.url}/embed", json={"inputs": batch})
                response.raise_for_status()
                return list(response.json())
            except httpx.HTTPStatusError as exc:
                # 4xx is our mistake and will not improve with time; 5xx may be a server
                # still loading its model, which is the one status worth waiting out.
                if exc.response.status_code < 500:
                    raise EmbeddingServiceError(
                        f"the embedding service rejected a batch of {len(batch)} "
                        f"({exc.response.status_code}). Check that ZENITH_HARDWARE matches "
                        f"the --max-batch-tokens the container was started with."
                    ) from exc
                last = exc
            except RETRYABLE as exc:
                last = exc
            await asyncio.sleep(2**attempt)

        raise EmbeddingServiceError(
            f"the embedding service at {self.url} did not respond after {ATTEMPTS} attempts"
        ) from last


def _estimate_tokens(text: str) -> int:
    """Characters ÷ 3, deliberately crude and deliberately pessimistic.

    A real tokeniser means loading one into the API process for a number used only to
    decide where to split a list. Overestimating costs a slightly smaller batch;
    underestimating costs a failed request and a document stuck in `embedding`.
    """
    from app.features.ingestion.chunking.chunker import CHARACTERS_PER_TOKEN

    return len(text) // CHARACTERS_PER_TOKEN + 1
