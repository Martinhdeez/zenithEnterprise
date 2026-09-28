# PR 06 evidence packets: matched local source-span trials

The opt-in packet selector preserves known local conditions, exceptions, table context, and explicit same-document section references. It does not improve complete annotated-evidence retrieval on these two development benchmarks. Keep both packet and counterevidence flags disabled by default. This is a source-visibility result, not an answer-quality or production qualification.

## Fixed setup

The trial uses [QASPER v0.3](https://huggingface.co/datasets/allenai/qasper) development JSON SHA-256 `2ae7ee62a65b1c4225791c70de80c2aad4e8998cf1fd4f09a53103db4f21af93` and [ContractNLI](https://stanfordnlp.github.io/contract-nli/) development JSON SHA-256 `310af7d661d2ab50ee3700169cef524c75f39fb296bbf5a515c229eb0f42e68e`. Selection and human evidence-span interpretation are described in [the R1 report](evidence-v3-r1-broad-2026-09-26.md). The 64 selected QASPER papers yield 206 eligible questions across 61 papers with exact evidence; all 61 ContractNLI development contracts yield 519 entailment and 95 contradiction cases. `NotMentioned` has no positive evidence target and is excluded. Contract text and dataset archives remain in the ignored local trial folder and were not sent to an external provider.

Every route uses the same current main-line chunks, BGE-M3 embedding revision `5617a9f61b028005a4858fdac845db406aefb181`, and dense top-eight candidates. The local GPU TEI reranker `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` reorders that fixed shortlist for TEI routes. Plain top-k and the actual production packet selector receive the same 1,100 BGE tokenizer source-token budget. The optional lane may add at most two exception-like candidates. The benchmark replaces database neighbor/reference lookups with in-memory lookups over the same public source units; real PostgreSQL/RLS behavior is separately tested. The source-token cap excludes generator prompt, metadata, and role-tag tokens, so it is an **equal source-context comparison**, not an exact production LLM-token budget. Production enforces a conservative full-prompt UTF-8-byte bound.

Complete evidence means at least one human-annotated alternative evidence set is entirely covered by selected source spans. This does not measure factual correctness, exception applicability, answer support, or abstention. Intervals are paired equal-paper/document-weight 2,000-resample percentile 95% bootstrap intervals (seed 1729). Paired case outcomes and pinned identities are [QASPER cases](evidence-v3-qasper-packets-206-gpu.csv) / [manifest](evidence-v3-qasper-packets-206-gpu-manifest.json) and [ContractNLI cases](evidence-v3-contract-nli-packets-dev-gpu.csv) / [manifest](evidence-v3-contract-nli-packets-dev-gpu-manifest.json). The full raw JSON stays in the ignored local handoff folder.

| Dataset, route | Plain top-k complete | Packet complete | Packet plus counter complete | Packet minus top-k, macro 95% interval |
| --- | ---: | ---: | ---: | ---: |
| QASPER, dense | 53.40% | 53.40% | 53.40% | 0.00 pp [0.00, 0.00] |
| QASPER, local TEI | 50.97% | 50.49% | 50.49% | −0.82 pp [−2.46, 0.00] |
| ContractNLI, dense | 69.06% | 69.06% | 69.06% | 0.00 pp [0.00, 0.00] |
| ContractNLI, local TEI | 77.04% | 77.04% | 77.04% | 0.00 pp [0.00, 0.00] |

The single QASPER TEI loss is an annotated span displaced when packet context is selected under the cap. It is a negative finding, even though the selector retained source identity. The optional counterevidence lane proposed four dense/two TEI additional QASPER chunks and six/four ContractNLI chunks; it changed no complete-evidence outcome. ContractNLI packet selection flagged 22 dense and 15 TEI cases with a known unresolved dependency, without silently presenting those selected facts as complete. Those flags are heuristic and do not measure all possible dependencies.

An earlier exploratory ContractNLI pass lost seven dense and nine TEI cases because textual mentions of section numbers were mistaken for section headings. The selector now resolves only anchored same-document headings, with a real application-role PostgreSQL regression test. The table is from the corrected final selector. One dense ContractNLI case changed top-k outcome between exploratory and final local GPU runs despite identical sources; near-tie/model execution sensitivity is a limit on fine-grained interpretations. Neither experimental flag is promoted.

Run from `backend/` with the pinned local TEI embedding and rerank services at `127.0.0.1:18081` and `:18083`, the datasets in ignored `../Zenith_Evidence_Fork_v3/datasets/`, and a disposable JWT secret:

```powershell
$env:PYTHONUTF8='1'
$env:ZENITH_JWT_SECRET=(& python -c 'import secrets; print(secrets.token_urlsafe(48))')
uv run python -m eval.qasper_packet_trial --dev-json ../Zenith_Evidence_Fork_v3/datasets/qasper/qasper-dev-v0.3.json --output ../Zenith_Evidence_Fork_v3/datasets/qasper/packet-206-v4.json
uv run python -m eval.contract_nli_packet_trial --dev-json ../Zenith_Evidence_Fork_v3/datasets/contract-nli-dev.json --output ../Zenith_Evidence_Fork_v3/datasets/contract-nli-packet-dev-v3.json
uv run python -m eval.packet_case_export --input ../Zenith_Evidence_Fork_v3/datasets/qasper/packet-206-v4.json --csv-output eval/reports/evidence-v3-qasper-packets-206-gpu.csv --manifest-output eval/reports/evidence-v3-qasper-packets-206-gpu-manifest.json
uv run python -m eval.packet_case_export --input ../Zenith_Evidence_Fork_v3/datasets/contract-nli-packet-dev-v3.json --csv-output eval/reports/evidence-v3-contract-nli-packets-dev-gpu.csv --manifest-output eval/reports/evidence-v3-contract-nli-packets-dev-gpu-manifest.json
```

The trial makes no Jev calls; the [40-query matched Jev versus current TEI study](evidence-v3-qasper-judge-live-2026-09-26.md) evaluates the ranking primitive separately. A generator answer-quality ablation remains needed before enabling packets. No result here justifies a default judge, index, or context-policy change.
