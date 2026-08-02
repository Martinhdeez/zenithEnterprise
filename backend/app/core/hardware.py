"""One codebase, three deployments.

We sell on-premise. A customer with an A100 and a customer with a four-core VPS get the
same artifact, and maintaining a branch per customer server is how a product becomes
unsupportable. But the two cannot run the same batch sizes, and M0 found out what happens
when you assume otherwise: TEI serving BGE-M3 with default batching was killed by the
kernel at 6.59 GB during warm-up, before it had processed a single document.

`ZENITH_HARDWARE` selects a **profile** — a named set of values — and nothing else in the
system branches on hardware. A boolean would spread: `if gpu:` appears in the compose file,
then in the worker, then in the reranker client, and each site drifts. A table is read in
one place.

**The flag may change performance. It must never change semantics.** Same corpus, same
query, same documents retrieved; RLS, label filtering, deduplication and idempotency are
identical on every profile. A flag that quietly changes what a user can see is a flag that
causes a leak. The one place a profile changes an *outcome* is OCR, and that change is a
refusal — a scanned document fails visibly rather than ingesting as empty.
"""

from dataclasses import dataclass
from typing import Final

from app.core.config import settings


@dataclass(frozen=True, slots=True)
class Profile:
    name: str

    # Must match the `--max-batch-tokens` TEI is started with. The two are one setting in
    # two places: M0 hit `413 Payload Too Large` because the client sent eight chunks of
    # roughly 350 tokens to a server configured for 2048.
    max_batch_tokens: int
    max_client_batch_size: int

    # Documents processed at once. One, on `low-spec`, so the parser's memory and the
    # embedder's never overlap.
    ingestion_concurrency: int

    # Off on `low-spec`. M0 measured what that costs: Recall@8 of 75% against a Recall@50
    # ceiling of 95%, so up to 20 points. A customer running without it is getting a
    # materially worse product and has to be able to tell.
    reranker: bool

    # Off on `low-spec`: the OCR path loads a second model, and there is no room for it
    # beside TEI in 8 GB. A scanned document then *fails* — see `ingestion/pipeline.py`.
    ocr: bool

    # How hard the HNSW index works per query. A speed/recall trade, which is exactly what
    # a profile is for: at pgvector's default the index caps recall below what the data
    # supports, and raising it costs latency the `low-spec` box does not have to give.
    hnsw_ef_search: int

    # Components this profile disabled, in the words `zenith diagnose` prints. Degradations
    # must be visible; a customer should never have to infer them from a recall number.
    @property
    def disabled(self) -> list[str]:
        missing: list[str] = []
        if not self.reranker:
            missing.append("reranker (costs up to 20 points of Recall@8)")
        if not self.ocr:
            missing.append("OCR (scanned documents will fail rather than ingest empty)")
        return missing


# `gpu` is inherited from TEI's defaults and carries no evidence behind it. It gets numbers
# when there is a GPU to measure on; until then it is marked as unmeasured rather than
# presented as a finding.
PROFILES: Final[dict[str, Profile]] = {
    "gpu": Profile(
        name="gpu",
        max_batch_tokens=16384,
        max_client_batch_size=32,
        ingestion_concurrency=4,
        reranker=True,
        ocr=True,
        hnsw_ef_search=200,
    ),
    "cpu": Profile(
        name="cpu",
        max_batch_tokens=8192,
        max_client_batch_size=16,
        ingestion_concurrency=1,
        reranker=True,
        ocr=True,
        hnsw_ef_search=100,
    ),
    # Measured the hard way on a 4-core, 7.6 GB VPS. With these two flags TEI loads and
    # serves at 3.71 GB; with the defaults the kernel kills it at 6.59 GB. The difference
    # between "does not run at all" and "runs" is two numbers, and no amount of design
    # review would have produced them.
    "low-spec": Profile(
        name="low-spec",
        max_batch_tokens=2048,
        max_client_batch_size=4,
        ingestion_concurrency=1,
        reranker=False,
        ocr=False,
        hnsw_ef_search=40,
    ),
}

UNMEASURED: Final[frozenset[str]] = frozenset({"gpu"})


def active() -> Profile:
    """The configured profile, or a startup failure.

    An operator who types `ZENITH_HARDWARE=CPU2` gets an error naming the valid values, not
    a silent fallback to defaults that will OOM at three in the morning. Same reasoning as
    the JWT secret: a misconfiguration that still starts is worse than one that does not.
    """
    try:
        return PROFILES[settings.hardware]
    except KeyError:
        raise ValueError(
            f"ZENITH_HARDWARE is {settings.hardware!r}, which is not a profile. "
            f"Valid values: {', '.join(sorted(PROFILES))}."
        ) from None
