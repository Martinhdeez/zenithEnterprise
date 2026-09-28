# Held-out QASPER: Jev Noul against current-main TEI reranking

The original frozen 240-paper result below is preserved as a distinct run.
The later [preregistered remaining-176-paper extension](evidence-v3-qasper-extension-2026-09-27.md)
used new public test papers and reused these saved predictions for a descriptive
416-paper join. It did not repeat or replace this experiment.

**Decision:** Jev Noul improved evidence ranking on this public, English research-paper task. Keep it as an **optional, explicitly authorized reranking provider**, with the TEI fallback. Do not change the default: this study does not measure complete generated-answer correctness, Spanish or enterprise documents, sustainable concurrent throughput, or the full hybrid retrieval path. The model is slower per eight-candidate batch in the tested serial topology.

## Frozen method

- Data: [QASPER v0.3 test release](https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-test-and-evaluator-v0.3.tgz), archive SHA-256 `72a52a41193e2838b8074f80ac074b94f956b84886c36a61c58a7df4171bdd72`, extracted JSON SHA-256 `6e29ad410e6e39aa1936017fb965b30a20eb2e7751997f55b97c9d281aa884e5`. Human answer-evidence annotations are the truth, not Jev output.
- Selection before opening labels: first 240 SHA-256-ranked source-bearing paper IDs, then the lowest SHA-256-ranked question ID per paper. Preparation projected away all answers. The frozen text-bearing candidate manifest stays ignored locally; SHA-256 `083ef6880efaac1307c6bedd3ab3b29d3038132370780cf54bce1610aa2a26c7` is enforced by the live runner. Predictions were written before the test labels were opened for scoring. There were 193 evidence-addressable questions; the other 47 remain in the assessed-call denominator but cannot have a source-span quality score.
- Both judges received the same up-to-eight dense candidates: current-main source chunking, `BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181` embedding, and a maximum 1,100 rendered BGE tokens. Local TEI was `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1@1427fd652930e4ba29e8149678df786c240d8825` in `ghcr.io/huggingface/text-embeddings-inference:120-1.9`; Jev was `jev-1.13.0` with Noul rubric `zenith-contribution-noul-v1`, SHA-256 `ea794cf80e72da58ace8bae07b33ec9164aed4e274a5a941ced3329d2c625dac`. The TEI ordering is the baseline used by upstream main. Valid Jev results retain source IDs; an unavailable batch falls back to that TEI ordering. No threshold or lexical veto was applied to the Jev ranking.
- Primary measure: at least one complete accepted human evidence set visible within the same rendered-token budget. Secondary binary nDCG@8 marks a candidate relevant when its source span intersects any accepted annotated evidence; candidate-miss queries are excluded from nDCG and reported separately. One selected question per paper permits paper-level paired bootstrap resampling. There were 2,000 draws with seed 1729, and the intervals below are percentile 95% intervals. This is an initial held-out quality comparison, not an answer-support probability calibration.
- Execution: Windows, RTX 5070 Ti laptop GPU (12 GB), pinned local GPU TEI models, Jev public API, public source text only, reranking permission enabled for this trial. A per-paper ledger pre-reserved calls and refused replay of unknown partial dispatch. The local runner used one concurrent Jev request at a time within a 90-second batch deadline. Raw requests, responses, dataset, and manifest remain in the ignored handoff directory; the [source-text-free per-case CSV](evidence-v3-qasper-heldout-240-2026-09-27.csv) is tracked.

## Results

| Measure | Current-main TEI | Jev Noul with TEI fallback | Paired difference |
|---|---:|---:|---:|
| Complete evidence, 193 addressable questions | 106/193 = 54.9% | 128/193 = 66.3% | +11.4 points; 95% CI +6.7 to +16.6 |
| Binary nDCG@8, 184 candidate-hit questions | 0.773 | 0.848 | +0.076; 95% CI +0.045 to +0.107 |
| Top-one candidate intersects evidence | 55.96% | 69.95% | +13.99 points, descriptive |
| Rerank batch p50 / p95 | 224 / 296 ms | 1,962 / 2,087 ms | Jev is slower here |

Jev gained complete evidence on 25 questions and lost it on three; 165 tied. At least one of the frozen eight candidates intersected human evidence on 184/193 addressable questions (95.3%); neither reranker can recover the other nine from this candidate set. All 240 Jev batches returned validated results; 1,916 calls were reserved and used out of the 10,000 **additional** authorized calls. Jev reported 1,164,463 input and 42,152 output tokens. At the provider's [published $0.042 per million input tokens and free output rate](https://typesafe.ai/blog/introducing-system-one-models-and-jev), the input-token estimate is about **$0.049** for this run; this is not an invoice. The pre-run 20-million-input-token reservation implied at most $0.84 at that rate, below the additional $5.00 cap. Rate changes, request fees, and billing differences remain unverified.

Across the sustained **serial** 240-batch run, recorded reranker call time summed to 470.93 s for Jev and 54.00 s for local TEI: approximately 0.51 versus 4.44 completed paper batches/s in this one-worker comparison. These rates exclude dense candidate construction, ingestion, answer generation, UI time, cold starts, and multi-user queueing; they are not a measured production throughput ceiling.

The ranking result supports offering Jev when an operator has authorized **reranking** of public or otherwise approved text and values evidence completeness enough to accept about two seconds of serial reranking. It does not establish that Jev is faster, cheaper than local GPU TEI, or more accurate for final answers. The QASPER source-span labels are not exhaustive judgments of every useful unannotated passage; binary nDCG inherits that limit. This is a fixed-candidate comparison, not an end-to-end comparison of changed embeddings, hybrid channel selection, packets, strict support, and answer generation. No default or production processing permission changes follow from it.

## Reproduce

The runner is `backend/eval/qasper_heldout_jev.py`. After downloading the pinned public release into an ignored local directory, start the two pinned TEI containers and run `prepare` with its `--test-json`, `--manifest`, `--embed-url`, and `--rerank-url` arguments. Verify the frozen manifest SHA before live work. `run` requires `ZENITH_EXTERNAL_PROCESSING_FOR_RERANKING=true`, a secret `ZENITH_JEV_API_KEY`, `--ledger`, and `--allow-public-live`; it refuses a changed manifest. `score` takes `--test-json`, `--manifest`, `--ledger`, `--output`, and optional `--cases-csv`, and only then reads human labels. Do not replay a ledger with uncertain reservations. Unit coverage is in `backend/eval/tests/test_qasper_heldout_jev.py`. The tracked CSV includes every selected paper/question ID, availability and paired outcome without source text or private queries.
