# QASPER test extension: 176 newly assessed papers

**Decision:** The separately frozen, unused English QASPER test cohort confirms
a positive fixed-candidate evidence-ranking result for Jev Noul with TEI
fallback. TEI remains the application default. Jev is optional, requires
purpose-specific approval, and has a measured latency cost. This study does
not qualify generated answers or enterprise/Spanish data.

The [preregistered protocol](evidence-v3-qasper-extension-prereg-2026-09-27.md)
fixed the new candidate manifest at SHA-256
`2d5f23e9a955eca1f6cf19d65140e90e4aabf33631d6acb1e9e69e3bea0160b4`
before live Jev dispatch or examination of the new gold labels. It excludes
every paper in the earlier 240-paper manifest; the paper-ID overlap is zero.
Both judges ranked the same frozen BGE-M3 dense candidates from current-main
chunks. The new run used the pinned local GPU TEI reranker and Jev
`jev-1.13.0` with the unchanged Noul rubric. Failed Jev batches would have
fallen back to TEI; none failed. No Score, packet, segmentation, or strict
support inference was run for this extension.

| Measure | New 176: TEI | New 176: Jev Noul with TEI fallback | Combined 416: TEI | Combined 416: Jev with fallback |
| --- | ---: | ---: | ---: | ---: |
| Complete accepted evidence | 75/147 = 51.0% | 93/147 = 63.3% | 181/340 = 53.2% | 221/340 = 65.0% |
| Paired difference, 95% paper bootstrap interval | | +12.2 points [6.1, 18.4] | | +11.8 points [7.9, 15.6] |
| Binary nDCG@8 on candidate-hit cases | 0.734 | 0.887 | 0.756 | 0.864 |
| Paired nDCG difference, 95% interval | | +0.153 [0.107, 0.196] | | +0.108 [0.081, 0.133] |
| Top-one candidate intersects accepted evidence | 64/147 | 106/147 | 172/340 | 241/340 |
| Rerank batch p50 / p95 | 46 / 315 ms | 2,308 / 2,611 ms | 217 / 309 ms | 2,049 / 2,472 ms |

The new cohort contains 147 evidence-addressable questions; 29 of the 176
selected questions have no usable annotated source span. The frozen candidate
set intersects accepted evidence in 132/147 addressable cases. The remaining
15 candidate misses stay in the complete-evidence denominator and are excluded
from binary nDCG. Jev won 20 new complete-evidence cases and lost two; 125
addressable cases tied. Across the descriptive 416-paper join, 340 cases are
addressable, 316 have candidate hits, and Jev won 45 and lost five.

All 176 new paper batches returned validated judgments. The pre-dispatch
manifest planned and the ledger reserved **1,396 new calls**, below the
authorized 1,408. Jev reported 847,579 input and 30,712 output tokens. At the
[published input rate](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
of $0.042/million tokens, reported new input implies about **$0.036**;
the 9,869,151-token pre-run conservative reservation implied about $0.415.
The full runtime reservation cap was 20 million tokens, or $0.84 at that rate,
below the user's $1 additional cap. These are estimates, not an invoice. The
combined ledger has 3,312 calls and 2,012,042 reported input tokens, including
the historical 1,916 calls and 1,164,463 input tokens; **the old calls were
reused, not repeated**.

The combined 416 result is descriptive reuse. The first 240 labels had already
been inspected before the new cohort was frozen, so the combined interval is
not a fresh independent held-out experiment. The new 176 result was computed
once with the previously fixed candidate and scoring policy; the rubric and
threshold were not tuned on these labels. The score uses human accepted
evidence, which can miss useful unannotated passages. It does not measure
answer correctness, the entire hybrid retrieval pipeline, multi-user provider
throughput, or invoice spend. The unusual new-cohort local TEI median is an
observed warm-service batch latency, not a service-level guarantee.

The regenerated descriptive combined manifest has SHA-256
`b3bcdad1093c4dacbd7e2d0d5e0d5c354529b068ffc786604cc9a4786e930377`.
It records 3,312 planned calls and 23,419,306 conservatively estimated input
tokens as sums of the two component manifests, with each component's own
manifest hash and historical or new authorization cap. There is no single
combined spending authorization. The component manifests and prediction
ledgers were unchanged; offline rescoring left all metrics above unchanged.

## Reproducibility

The source-text-free [new case CSV](evidence-v3-qasper-heldout-new176-2026-09-27.csv)
and [descriptive combined CSV](evidence-v3-qasper-heldout-combined416-2026-09-27.csv)
include all selected cases, availability, exclusions, paired outcomes, and
ranking measures. The ignored local manifest/ledger retain source text and
predictions; the ledger SHA-256 after completion is
`6f0fd1a9ac7a8d8407baad831b2a943b3130ed72d41142bd7665ada81bb1584f`.
Offline rescoring of the original saved 240 predictions produced a byte-identical
JSON report, SHA-256
`f92f1f012adf5f4f62fc0f7a67721d9bf127ca724563c3e23ed6d6bdb6163f29`.
The combined scorer's first 240 case rows exactly match that saved report and
its remaining 176 exactly match the new report. This is reproducibility of
saved predictions, not a second live test.

From `backend/`, run `python -m eval.qasper_extension_jev score` with the
ignored QASPER dataset directory as `--baseline-dir`, the ignored extension
directory as `--output-dir`, and the manifest hash above as
`--manifest-sha256`. Live `run` additionally requires the approved public
reranking processing flag, a locally supplied key, the exact hash, and
`--allow-public-live`; **do not rerun** this completed ledger to manufacture
more calls. The runner refuses changed manifests and preserves uncertain
reservations without replay.
