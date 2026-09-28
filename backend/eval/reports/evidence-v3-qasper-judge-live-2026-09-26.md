# Matched public QASPER judge trial — 26 September 2026

The local current-main-compatible TEI reranker and the optional Jev Noul and
Score adapters ranked the *same* dense top-eight legacy chunks for each of 40
public QASPER development questions. The papers were the first 40 eligible
documents from the frozen 64-paper R1 selection, with the first SHA-256-ranked
eligible question per document. Labels are practitioner-selected evidence
paragraphs, not model-generated judgments. The 1,100-token context budget
uses the pinned BGE-M3 tokenizer. This isolates last-stage ranking; it does
not compare the full database hybrid pipeline or answer generation.
The legacy `chunk_stream` is unchanged from `upstream/main`. The TEI wrapper
adds response validation and deadlines while retaining the main-line model
scoring and score-descending, incoming-order tie rule for valid responses;
the reranker endpoint reported the pinned local model below.

To check whether the 40-paper slice represents the broader selected QASPER
scope, the existing TEI reranker was also run on **all 206 eligible questions**
from the frozen 64-paper sample, without Jev calls. At the same top-eight
candidate and 1,100-token budgets, dense order exposed complete evidence in
53.4% and TEI in 51.0%; the equal-paper macro difference was −0.9 percentage
points, with a paired 95% bootstrap interval of −6.2 to +4.7. TEI's higher
67.5% on the Jev-matched 40-paper slice is therefore not representative of a
reliable improvement over dense order across all 206 questions. All 40 dense
and TEI case metrics matched exactly between the two runs. The [full TEI raw
report](evidence-v3-qasper-main-tei-206-gpu.json) gives the IDs, per-case
scores, model and dataset identities. This larger run cannot establish how
Jev would perform on the other 166 questions; its 40-case paired Jev result
remains the only live Jev quality comparison on this dataset.

| Route | Complete evidence | Mean gold recall | Cases with complete judgments | Median elapsed per eight candidates |
| --- | ---: | ---: | ---: | ---: |
| Dense order | 57.5% | 63.9% | 40/40 | — |
| Local TEI `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | 67.5% | 71.2% | 40/40 | 971 ms |
| Jev 1.13.0 Noul | 75.0% | 79.9% | 40/40 | 2,607 ms |
| Jev 1.13.0 Score6 | 75.0% | 81.1% | **32/40** | 2,595 ms |

The Score percentages are conditional on its 32 fully assessed cases and must
not be compared as if the other eight succeeded. Eight Score batches had at
least one `probability_mass` failure. The adapter rejected those candidates
and kept the batch partial; no failed judgment was assigned a zero score.
The official [Score documentation](https://docs.typesafe.ai/primitives/score)
says probabilities sum to one. A bounded public replay of the eight failed
questions (64 additional Score calls) produced masses of exactly 1.0 on every
response, so the original provider inconsistency could not be reproduced.
We retain strict validation and local fallback. The replay is diagnostic,
not an extra independent quality sample.

On 40 paired, distinct papers, Noul minus local TEI complete-evidence rate is
+7.5 percentage points: five Noul wins and two losses. The prespecified
2,000-resample, seed-1729 paper bootstrap interval is −5.0 to +20.0 points.
It does not establish that Noul is better. TEI minus dense order is +10.0
points, interval −7.5 to +27.5. On the 32 fully assessed Score cases, Score
minus TEI is +9.4 points, interval −3.1 to +21.9; Score's eight unavailable
cases are a separate operational disadvantage. These are evidence-visibility
metrics, not answer correctness probabilities.

The 640 comparison calls reported 374,920 input and 11,904 output tokens in
complete batches. At the [published input price](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
of $0.042 per million tokens, **$0.01575 is a lower-bound estimate**, not an
invoice: partial batches did not yield aggregate usage. The preflight's
conservative 4,523,112-token ceiling corresponds to under $0.19 for this
trial. The user approved no more than $0.50 or 1,000 Jev requests overall;
the trial used 640 calls, following 186 recorded earlier calls. Eight initial
and 64 additional public diagnostic calls bring the recorded total to 898
before any later boundary trial. No private corpus was sent. The credential
was provided only to the process environment, removed by the script before
dispatch, and is absent from the tracked artifacts.
The later 32-call QASPER segmentation probe brought the cumulative recorded
session total to **930 calls**, leaving at most 70 under the approved call cap.
The live trial script now uses 930 as its prior-call lower bound and fails
closed for another 640-call run.

Jev Noul used rubric `zenith-contribution-noul-v1` at hash
`ea794cf80e72da58ace8bae07b33ec9164aed4e274a5a941ced3329d2c625dac`.
Score used rubric `zenith-contribution-score6-v1` at hash
`a92f8c743324675cee016f29dce551ff49c28a0754ce9010e27787b360073756`
and utility map `grade-index-linear-v1`. The Score distribution and rank
utility are preserved separately in the [raw outcomes](evidence-v3-qasper-judge-live-2026-09-26.json).
The local TEI model was served at `127.0.0.1:18083`, model revision
`1427fd652930e4ba29e8149678df786c240d8825`; the BGE-M3 embedder
revision and dataset hashes are in the [R1 report](evidence-v3-r1-broad-2026-09-26.md).

The result supports keeping the current local default while conducting larger,
independently reviewed and operationally complete trials. The 40-case paired
intervals are wide, the Score distribution failures need investigation with
the provider, and no full hybrid/answer-quality qualification was run here.
The local and Jev comparisons ran on different hosts and include network and
serial pair-processing time; their timings describe this setup only.

To reproduce, acquire the QASPER development data and local TEI services using
the [R1 instructions](evidence-v3-r1-broad-2026-09-26.md). From `backend/`,
run the no-egress preflight first, then set `ZENITH_JEV_API_KEY` only in the
process environment and run the live trial. The program enforces the recorded
cumulative call cap and a conservative token reservation before dispatch.

```powershell
uv run python -m eval.qasper_judge_trial --dev-json ../Zenith_Evidence_Fork_v3/datasets/qasper/qasper-dev-v0.3.json --output ../Zenith_Evidence_Fork_v3/datasets/qasper/jev-preflight.json --preflight-only
uv run python -m eval.qasper_main_tei_trial --dev-json ../Zenith_Evidence_Fork_v3/datasets/qasper/qasper-dev-v0.3.json --output eval/reports/evidence-v3-qasper-main-tei-206-gpu.json
```

Reproducing the Jev run requires a new explicit public-data authorization and
budget ledger entry beyond the current 930 calls; this report's raw Jev data
is the recorded run, not an instruction to rerun it with a stale allowance.
