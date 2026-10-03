# Corrected BGE paired repeat (frozen before generation)

Extraction provenance: this report preserves the measured integrated-fork experiment.
The standalone upstream slice starts from `33b48812c95150348c52d2519159780252c92db2`;
its independent checks are reported in the PR. Historical benchmark and quality
figures have not been rerun on this extraction. Original implementation and
evaluation harness: [frozen fork source](https://github.com/Kripta-Studios/zenithEnterprise/tree/e49fd78e225c4176d4e439b502fa6d5d61f85127).

Base fork main: d12620a8cd973e5f2d1012900a63f1726b225e05.
Original fixture SHA-256: 2c99e0e8982aaa467d1b90ea084e5747bd1d4c1f70968c191a1d95a4951d61dc.

Reuse the 128 test questions / 114 article families, original 14-file corpus and
16 development cases from `spanish-e2e-protocol.json`. Preserve the BGE-M3 embedding,
BGE-reranker-v2-M3, Jev 1.13.0 Noul rubric, Llama 3.1 8B Q4_K_M generation parameters
and multilingual independent NLI model/revision/cutoff. No test-set tuning.

Run actual upload/dedup, RLS retrieval, both `/query` arms, citation binding and audit
writes from the corrected fork. Cached embeddings require exact authorized source and
candidate equality. Reuse reranker scores only for identical question/passages.
Reused completions require identical complete prompts and model parameters; record
new versus reused completions. Query replay timings exclude cached reranker inference.

Both score arms traverse the same corrected local candidate-pool relevance gate.
This tests whether Jev's old advantage survives the corrected local baseline; it does
not establish provider-native calibration or results on unseen questions. Existing
internal source hashes are excluded only from historical capture serialization;
original IDs, coordinates, text, ranks, scores and candidate order must remain exact.

Primary strict success and article-cluster bootstrap remain unchanged. Meaningful
criterion: mean paired gain >= 0.02 and 95% lower bound > 0. Clearly larger criterion:
mean gain >= 0.05 and 95% lower bound >= 0.02. Report failures and counterexamples.
Mandatory citations are validated for identity/range and separately for NLI support.
An independent automatic model is not independent human adjudication.

No new provider calls are allowed in this repeat. Existing results remain historical.
Retain failed attempts and exact code/tree/artifact hashes.
