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

    # How many candidates the cross-encoder reads. Zero where the reranker is off. The
    # cost is real and interactive: 50 pairs is 50 forward passes, and on four CPU cores
    # that is not free — `cpu` reranks half the union and lets the fused order choose which
    # half, which is the one job RRF keeps.
    rerank_candidates: int

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
        rerank_candidates=100,
    ),
    # Requires **more than 4 cores and more than 7.6 GB**, and that floor is measured
    # rather than estimated. F11 started TEI with these flags on a 4-core, 7.6 GB VPS and
    # the reranker container was OOM-killed during warm-up at 4.3 GB RSS, before serving a
    # single request. On the same box the cross-encoder could not rerank even *ten*
    # passages inside the 5-second interactive timeout.
    #
    # `rerank_candidates = 50` therefore remains **uncalibrated**. It has never run on
    # hardware that can host this profile, and lowering it on the strength of a machine
    # that cannot run the profile at all would repeat F7's mistake in the other direction.
    # A box that meets the floor is what settles it. Until then the honest statement is
    # that this number is a guess, and it is written down as one.
    "cpu": Profile(
        name="cpu",
        max_batch_tokens=8192,
        max_client_batch_size=16,
        ingestion_concurrency=1,
        reranker=True,
        ocr=True,
        hnsw_ef_search=100,
        rerank_candidates=50,
    ),
    # Measured the hard way on a 4-core, 7.6 GB VPS. With these two flags TEI loads and
    # serves at 3.71 GB; with the defaults the kernel kills it at 6.59 GB. The difference
    # between "does not run at all" and "runs" is two numbers, and no amount of design
    # review would have produced them.
    #
    # Tried at 16 once the parser fix had freed 2.8 GB, on the theory that the round trips
    # were the reason a 3,179-chunk document took an hour. They are not, and the number
    # cannot move: `plan_batches` fills a request until *either* bound is reached, and at
    # 400-token chunks the token budget is reached first, at five. Raising the client limit
    # to 16 changed nothing about what was sent — 555 requests, every one a 200, none of
    # them larger than the budget already allowed.
    #
    # So 4 is already within one chunk of the ceiling, and the only lever that would
    # actually widen a request is `max_batch_tokens` — which is the number keeping TEI
    # alive at 3.71 GB on this hardware, and is not available to spend.
    "low-spec": Profile(
        name="low-spec",
        max_batch_tokens=2048,
        max_client_batch_size=4,
        ingestion_concurrency=1,
        reranker=False,
        ocr=False,
        hnsw_ef_search=40,
        rerank_candidates=0,
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
