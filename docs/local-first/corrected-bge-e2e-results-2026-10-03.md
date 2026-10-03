# Corrected BGE removes the demonstrated Jev advantage

Extraction provenance: this report preserves the measured integrated-fork experiment.
The standalone upstream slice starts from `33b48812c95150348c52d2519159780252c92db2`;
its independent checks are reported in the PR. Historical benchmark and quality
figures have not been rerun on this extraction. Original implementation and
evaluation harness: [frozen fork source](https://github.com/Kripta-Studios/zenithEnterprise/tree/e49fd78e225c4176d4e439b502fa6d5d61f85127).

The complete paired repeat yields **BGE 34/128 (26.5625%) versus Jev 31/128
(24.21875%)** on strict referenced, cited and independently entailed answers.
Jev minus BGE is **-2.34375 percentage points**, article-cluster bootstrap 95%
interval **[-9.02256, +3.96825] points**. Both preregistered improvement criteria fail.
This does not demonstrate superiority for either provider: the interval includes zero.
It does invalidate using the old positive comparison as evidence of a Jev advantage
over the corrected local baseline.

## Complete outcomes

| Outcome on the same 128 questions | Corrected BGE | Jev |
|---|---:|---:|
| Strict independently entailed grounded reference match | 34 | 31 |
| Grounded reference match before NLI | 76 | 80 |
| Answer contains a published reference | 90 | 92 |
| Delivered non-abstained answers | 117 | 123 |
| Pre-generation abstentions | 8 | 0 |
| Gold source in the reranked eight | 116 | 117 |
| Invalid bound citations | 0 | 0 |
| Fabricated raw numeric citation markers | 0 | 0 |
| Fresh local completions | 77 | 52 |
| Exact complete-request generation cache reuse | 43 | 76 |

Paired strict outcomes: 23 successes shared by both, 11 BGE-only, 8 Jev-only and
86 failures shared by both. The independent model checked 140 BGE and 158 Jev claims.
Zero invalid source references is an identity/range result, not zero hallucinations.

In the original run BGE achieved 10 strict successes and Jev 20. The corrected gate
raises those to 34 and 31 without changing the frozen ranking inputs or generator.
Ranking metrics remain identical: BGE nDCG@8 0.844154, Jev 0.883364; gold source
hit@8 differs by only one question. More delivered answers and slightly better ranking
do not translate into better strict generated-answer quality here.

## What was actually repeated

Code head: `ec84211741dd57601885d0aab4b503195e40bafd`
(full tree/archive hashes are in the aggregate JSON).
Actual integration command: `uv run python -m pytest -q --tb=short
eval/tests/test_spanish_e2e_pipeline.py`. Result: **1 passed**, wrapper exit **0**,
**859.36 seconds** including runner setup. Real upload/dedup, application-role RLS
retrieval, 256 paired query outcomes, mandatory citation binding and persisted citation
audit counts all completed. The separate independent entailment stage completed all 128
pairs and produced summary SHA-256
`7ae11c04babedbea73aa6df5e34bf1da46936d55cfa1858152252eefb882e8c2`.

The cohort remains the original 128 public SQAC test questions from 114 article families,
16 development cases, 650 contexts, 14-file layout and 939 stored chunks. All 144
preparation queries matched their historical candidate capture exactly. Only the new
internal source-version hash is excluded from historical serialization; source IDs,
text, coordinates, ranks, scores and order remain exact. The first failed capture
attempt is retained: it rejected that additional metadata before any scoring/generation.

Embedding: BAAI/bge-m3 revision `5617a9f61b028005a4858fdac845db406aefb181`.
Local reranker: BAAI/bge-reranker-v2-m3 revision
`953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`.
External ranking inputs: frozen Jev 1.13.0 Noul rubric.
Generator: Llama 3.1 8B instruct Q4_K_M, digest
`46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e`,
8192 context, temperature 0, seed 20261002, output cap 256, thinking false.
Independent evaluator: mDeBERTa-v3-base-mnli-xnli revision
`8adb042d524ecd5c26d3e3ba0e3fbcf7e2d0864c`, CPU float32, cutoff 0.8,
overlapping source windows and no silent claim truncation.

There were **zero new provider calls and zero additional paid spend**. Scores were reused
only for exactly matching question/passage inputs. There were 129 new local completions
and 119 exact complete-request cache reuses across 248 generation calls; eight outcomes
abstained before generation. Historical upload timing remains historical: this repeat
deduplicated existing documents. Query replay timings exclude cached provider inference
and cannot establish online latency superiority.

Both arms traverse the corrected shared local relevance boundary, with injected recorded
scores. This is a controlled reranker replacement in the existing query pipeline, not a
benchmark of Jev-native production policy/calibration. The cohort has been observed
before: this corrective repeat is not a new untouched holdout. Reference-span exactness
and an automatic NLI model can reject valid paraphrases; its independence is model and
published-dataset independence, not external human adjudication.

## Product implication and next quality experiment

Retain local BGE as the default and do not present the old +7.8125-point result as a
reason to send private source passages to Jev. The user's optional private-corpus product
direction remains recorded, but its quality rationale is now unconfirmed. Martin's
privacy and multi-process deployment objections remain separate from accuracy.

The next observed quality bottleneck is answer generation/support: 42 BGE and 49 Jev
outcomes pass the reference/citation proxy but fail the strict independent entailment
check. Diagnose unsupported additions, missing per-sentence citations, reference-span
paraphrases and judge disagreements separately. Compare a concise Spanish cited-answer
prompt and available local generators on development data first; freeze the candidate,
then evaluate new published-reference Spanish questions and negative controls. Keep
the same quality thresholds and compare answerability, source identity, claim support,
latency and cost separately. Do not tune a model or evaluator to recover a Jev win on
these 128 cases.

[Frozen repeat protocol](corrected-bge-repeat-protocol-2026-10-03.md),
[source-free aggregates and bindings](corrected-bge-e2e-results-2026-10-03.json),
[paired diagnostics](corrected-bge-e2e-results-2026-10-03.csv),
[historical comparison](https://github.com/Kripta-Studios/zenithEnterprise/blob/e49fd78e225c4176d4e439b502fa6d5d61f85127/docs/local-first/spanish-e2e-results-2026-10-03.md).
