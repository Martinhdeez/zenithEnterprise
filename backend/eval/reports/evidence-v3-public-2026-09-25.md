# Public pair-judgment pilot, 2026-09-25

This is a matched **candidate-ranking** pilot against the current `main` TEI
reranker, not an end-to-end retrieval result or a production provider decision.
The frozen fixture is
[`evidence-v3-public-v1.json`](../fixtures/evidence-v3-public-v1.json), SHA-256
`f87123fa48e11495dbb9d19e63842d089651aaf885da28859e0c5d7e176b4bd8`.
It has ten questions (including one Spanish question) and eight short, public,
source-linked candidates from EPA, USGS, NWS, and CDC. Ordinal relevance labels
were reviewed by the implementing agent, not independently adjudicated. The
fixture is small and comparatively easy, so the ranking metrics are exploratory.

All configurations assessed the same ordered candidate IDs and text. Current
`main` TEI is `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, pinned at Hugging
Face revision `1427fd652930e4ba29e8149678df786c240d8825`. Its CPU service
used the repository's `cpu-1.8` image; a separate GPU trial used the `120-1.9`
image on an RTX 5070 Ti Laptop GPU. Both reported the pinned model identity.
The GPU runtime is a distinct runtime and is not the repository default.
Jev used pinned `jev-1.13.0`, `zenith-contribution-score6-v1` and
`zenith-contribution-noul-v1`, with the tracked rubric/utility definitions.

| Configuration | Mean nDCG@8 | Labeled-best at rank 1 | Mean relevant recall@2 | Median query time |
|---|---:|---:|---:|---:|
| Current-main TEI, CPU 1.8 (matched live run) | 0.968 | 9/10 | 1.00 | 645 ms |
| Same weights, GPU 1.9 (separate run) | 0.968 | 9/10 | 1.00 | 272 ms |
| Jev Score6 | 1.000 | 10/10 | 1.00 | 2,240 ms |
| Jev Noul | 1.000 | 10/10 | 1.00 | 2,154 ms |

The main difference in these labels is the Spanish shaking-intensity question:
TEI chose the magnitude passage first, while both Jev formulations chose the
intensity passage. Both Jev formulations tied on this fixture, so it cannot
select between them. TEI GPU was about 2.4 times faster than the CPU service
in the matched run, with unchanged ranking metrics. Timing includes local or
remote service latency and varies with warmup; a separate CPU-only run measured
889 ms median. The GPU comparison also changes TEI runtime version, not weights.

The successful matched run made 160 Jev pair calls with 73,168 reported input
tokens and 3,280 output tokens. At the provider's published input-token rate
checked on this date, estimated cost was **$0.003073056**, below the user's
$0.50 and 1,000-call limits. Two earlier diagnostic calls included one rejected
response and one numeric-format probe; provider billing for the rejected call
is unknown. These amounts are estimates, not an invoice. No private material
was sent. The first call exposed two-decimal rounding in the API's Score and
distribution display; the adapter now accepts the bounded rounding difference
without treating arbitrary scores as valid.

Raw per-query results and identities:
[`Jev/TEI JSON`](evidence-v3-public-2026-09-25-jev-tei.json),
[`GPU TEI JSON`](evidence-v3-public-2026-09-25-tei-gpu.json).
For a CPU-only replay, run the harness from `backend` with `ZENITH_JWT_SECRET`
set to a disposable valid value:

```powershell
uv run python eval/judge_comparison.py --tei-url http://127.0.0.1:18082 --output ..\Zenith_Evidence_Fork_v3\tei_cpu_replay.json
```

Live Jev replay additionally requires explicit external-processing permission,
an approved public fixture, a secret supplied through the interactive prompt,
and `--live-jev`; the harness enforces the above two caps. It must never be
pointed at private source corpora. This pilot does not assess full retrieval,
calibration, evidence sufficiency, source completeness, sustainable throughput,
or claim support. Keep the local baseline as the default pending a harder,
independently labeled and end-to-end comparison.
