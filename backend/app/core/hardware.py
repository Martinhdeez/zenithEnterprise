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
    #
    # **This field does not start the worker.** Procrastinate's concurrency is a command-line
    # argument, fixed when the process launches and long before any profile is resolved, so
    # `docker-compose.yml` passes `ZENITH_WORKER_CONCURRENCY` and defaults it to 1. The two
    # agree today — 1 is what both measured profiles want — and the number here is what an
    # operator should set that variable to, reported by `tasks.worker_concurrency()`. Said
    # plainly because the alternative is a reader assuming the profile is enforced, which is
    # how `ocr=True` came to promise an engine that did not exist.
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
    # A box that meets the floor has now settled `max_batch_tokens`, and the answer was not
    # 8192. Measured on a 14-core, 24 GB machine with 11.7 GB given to the container VM:
    #
    #     max_batch_tokens=8192  ->  tei-embed alone reaches 10.44 GB and the pair are
    #                                OOM-killed during warm-up, both at once
    #     max_batch_tokens=2048  ->  tei-embed serves at 2.66 GB
    #
    # TEI reserves activation memory in proportion to this number, so two models at 8192
    # want upwards of 20 GB between them — more than raising the VM's allocation could
    # reasonably supply, on a machine whose real constraint is that the embedder and the
    # cross-encoder must live side by side.
    #
    # **Lowering it costs throughput, not ranking.** What orders results is the reranker
    # itself, the 50 candidates it reads, and `hnsw_ef_search`; the token budget decides
    # only how many passages ride in one request. Trading batch size for a cross-encoder
    # that actually runs is the whole point of this profile — a profile that OOMs reranks
    # nothing at all.
    #
    # `max_client_batch_size` follows it down to 4, and not by choice: this module's own
    # consistency test requires `max_client_batch_size * 400 <= max_batch_tokens`, because
    # a full batch of target-sized chunks that exceeds the server's budget is a request
    # guaranteed to be rejected. 16 × 400 is 6400 against a 2048 budget. Memory is not what
    # rules the number out — the token budget is.
    #
    # `rerank_candidates` is no longer a guess either, and 50 was never reachable. Measured
    # on the same machine, with all 14 cores saturated at 1415%: **1,785 ms per 400-token
    # passage**, and linear — batching buys nothing, because one batch already occupies
    # every core. So the depth *is* the latency, directly:
    #
    #     50 candidates -> 89 s      8 candidates -> 14 s      4 candidates -> 7 s
    #
    # 50 was not a slow setting, it was an impossible one: no timeout accommodates 89
    # seconds on an interactive path, so every search spent five seconds failing and fell
    # back to the fused order. A number that never runs is worse than a smaller one that
    # does — it costs the latency and delivers none of the ranking.
    #
    # **8, because 8 is the page.** `SearchService` returns eight passages, and the
    # complaint that sent us here was that the right passage was on the page but not at the
    # top. Reranking exactly the shortlist that gets shown fixes that, and reranking deeper
    # buys reach into ranks nobody reads at 1.8 s per rank.
    #
    # The honest limit of this profile: 14 seconds is not interactive, and the way out is a
    # smaller cross-encoder, not a bigger number here. That was then taken —
    # `TEI_RERANK_MODEL` now defaults to `mmarco-mMiniLMv2-L12`, and the measurements behind
    # the choice are in `docker-compose.yml` beside it. **784 ms against 13,731, for
    # identical Recall@8.**
    #
    # `rerank_candidates` stays at 8 with that model, and the reason is measured rather than
    # inherited: raising it to 32 made ranking *worse* — Recall@1 66.7% -> 53.3%, mean rank
    # 1.35 -> 1.62 — because a weaker judge given four times the candidates is wrong four
    # times as often, and RRF's own top-eight beat its reordering of thirty-two. The latency
    # headroom the smaller model bought cannot be spent on depth. It could be spent on a
    # better cross-encoder, which is what a GPU deployment should do.
    "cpu": Profile(
        name="cpu",
        max_batch_tokens=2048,
        max_client_batch_size=4,
        ingestion_concurrency=1,
        reranker=True,
        ocr=True,
        hnsw_ef_search=100,
        rerank_candidates=8,
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
