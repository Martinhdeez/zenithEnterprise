# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
#
# Laboratory code, suppressed once per file as elsewhere under `eval/`. Nothing under
# `app/` relaxes strictness.

"""How many candidates the cross-encoder should read, measured rather than argued.

`rerank_candidates` is 8 because 8 is the page: `SearchService` returns eight passages, and
reranking exactly the shortlist that gets shown was what fixed "the right passage is on the
page but not at the top". 32 was then tried and made ranking *worse* — Recall@1 66.7% ->
53.3% — because a weaker cross-encoder given four times the candidates is wrong four times
as often.

So the two ends are measured and the middle is not, and the middle is where a known miss
lives. Tracing `boe-bank-rate` stage by stage put its correct passage at **fused rank 12**:
found by the lexical half at 18 and the dense half at 13, surviving fusion at 12, and then
cut by a shortlist of 8 before the cross-encoder ever saw it. That is a depth failure, not a
ranking failure, and depth is this number.

The question this answers is not "does 12 rescue that question" — it is whether any depth
rescues it *without* costing more elsewhere than it recovers. A sweep that reports only the
question it was built to fix would be the metric gaming itself.

    docker compose exec -T api python -m eval rerank-depth
"""

import asyncio
import json
from pathlib import Path

from eval.harness import installation, score

REPORT = Path(__file__).parent / "rerank-depth.json"

#: 8 is what ships; 32 is the measured-worse end, re-run here rather than quoted so the
#: whole curve comes from one machine on one day. The interesting values are between them.
DEPTHS = (8, 12, 16, 24, 32)


async def _run() -> int:
    where = await installation()
    if not where.questions:
        print("No question's document is in this corpus — nothing to measure.")
        return 1

    print(f"{where.space.n} embeddings, {len(where.questions)} scorable questions\n")
    results: dict[str, object] = {}
    for depth in DEPTHS:
        measured = await score(where, rerank_candidates=depth)
        results[str(depth)] = measured
        print(f"  depth {depth:<3} {json.dumps(measured)}", flush=True)

    REPORT.write_text(
        json.dumps({"scored": len(where.questions), "by_depth": results}, indent=2) + "\n"
    )
    print(f"\nWritten to {REPORT.name}")
    return 0


#: How `python -m eval` finds this sweep. Declared here rather than listed in
#: `__main__.py`, so adding a measurement is adding a file and nothing else.
COMMAND = "rerank-depth"
USAGE = "rerank-depth"


def run() -> int:
    return asyncio.run(_run())
