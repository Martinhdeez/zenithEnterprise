# R1 lossless segmentation: two public, human-annotated benchmarks

This is an isolated research index, based on PR 05 commit
`1867532d3f04c632bb932a03e9e573b118d7143b`. The active ingestion
chunks, database vectors, source IDs, and citations were not changed. The
comparison uses the current main-line `chunk_stream` output as its legacy
baseline, then (a) expands fixed legacy retrieval leaders to structural
groups and (b) re-embeds the structural groups into a separate in-memory
index. These are different experiments.

## Pinned sources and protocol

| Item | Identity |
| --- | --- |
| QASPER development archive | [AllenAI QASPER v0.3](https://huggingface.co/datasets/allenai/qasper), train/dev archive SHA-256 `a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a`; extracted development JSON SHA-256 `2ae7ee62a65b1c4225791c70de80c2aad4e8998cf1fd4f09a53103db4f21af93` |
| ContractNLI development archive | [Stanford ContractNLI](https://stanfordnlp.github.io/contract-nli/), official ZIP SHA-256 `e03fc77bbf8b53e2976a250e81d8a294bc3d5e5fb014521e477dee9340d6287b`; extracted development JSON SHA-256 `310af7d661d2ab50ee3700169cef524c75f39fb296bbf5a515c229eb0f42e68e` |
| Embedding and counting | BGE-M3 revision `5617a9f61b028005a4858fdac845db406aefb181` via TEI 1.9 GPU; `/tokenize` includes special tokens |
| Container | `ghcr.io/huggingface/text-embeddings-inference:120-1.9` digest `sha256:bd8e5b1954146f7fe8590b64b959bc194433c6c38c036592a84d736841ca9400` |
| Hardware | NVIDIA RTX 5070 Ti Laptop GPU, 12 GB; local disposable TEI at `127.0.0.1:18081` |
| Selection and budget | QASPER: 64 smallest SHA-256 paper IDs on dev, 206 eligible questions; ContractNLI: all 61 dev documents, 614 positive/contradiction cases. Dense top 8, 1,100 BGE tokenizer tokens rendered per route. |
| Uncertainty | Paired, equal-document-weight bootstrap of complete-evidence difference; 2,000 document-cluster resamples with seed 1729 and percentile 95% interval. |

QASPER provides practitioner-written questions and supporting passages.
Only answer evidence matching exactly one full-text paragraph is scored;
empty, unanswerable, figure-only, and ambiguous paragraph references are
excluded. Of 250 questions in the selected papers, 206 are eligible. Multiple
human annotations are alternative *complete* evidence sets. Original
paragraphs are rendered with section headings and exact character offsets.
This is not answer correctness evaluation.

ContractNLI provides human hypothesis labels and exact character-index
evidence spans. The 61 development documents contain 519 entailments, 95
contradictions, and 423 `NotMentioned` labels. The last class has no target
evidence and is excluded from retrieval recall; this experiment does not
measure abstention. One annotated span was whitespace only and is explicitly
skipped. The real contract text is processed locally only and is not included
in this repository. Both datasets are attributed to their original authors
under the respective published CC BY 4.0 terms.

Complete evidence means every gold range in at least one human annotation is
visible in the selected, deduplicated source-offset union. The top-1 measure
asks only whether its range intersects any gold span; it does not establish
answer correctness. The context budget uses the embedding tokenizer because
the generator tokenizer is not pinned in this isolated R1 trial.

## Observations

| Dataset and route | Complete evidence | Mean gold recall | Top-1 intersects | Mean rendered tokens |
| --- | ---: | ---: | ---: | ---: |
| QASPER, current chunks | 53.40% | 58.01% | 39.32% | 1,011.9 |
| QASPER, fixed leaders + grouping | 56.31% | 60.09% | 26.21% | 1,008.3 |
| QASPER, resegmented + re-embedded | 52.91% | 57.08% | 33.01% | 1,000.3 |
| ContractNLI, current chunks | 69.06% | 77.31% | 55.37% | 1,007.6 |
| ContractNLI, fixed leaders + grouping | 72.64% | 77.06% | 39.90% | 1,004.0 |
| ContractNLI, resegmented + re-embedded | 74.59% | 80.40% | 47.56% | 998.8 |

For QASPER, grouping minus current chunks is +3.80 percentage points by
equal-paper macro average, with a 95% interval of −2.12 to +9.81; re-embedding
is +0.26 points, interval −6.52 to +6.77. For ContractNLI, grouping is +3.33
points, interval −0.10 to +6.94; re-embedding is +4.89 points, interval +1.31
to +8.47. ContractNLI entailment complete-evidence rates are 69.56%, 73.60%,
and 75.34% respectively; contradiction rates are 66.32%, 67.37%, and 70.53%.
Top-1 intersections fall with grouping in both domains even where complete
evidence improves. The group can straddle a leading legacy chunk, so its
first selected group can differ from the exact leading chunk.

The trial counted 1,933 legacy and 1,472 structural QASPER units, and 842
legacy and 663 structural ContractNLI units. Observed one-time embedding
durations were 89.2 versus 74.4 seconds for QASPER and 44.4 versus 36.4
seconds for ContractNLI, legacy first in each case. Warm-up and order confound
these timings; they are not a throughput or cost advantage. Production
re-indexing, storage duplication, update frequency, and citation migration
were not measured.

The five-question [EPA exploratory trial](evidence-v3-r1-epa-gpu.json) found
4/5 complete spans for each route. In a separate public EPA boundary probe,
[eight Jev Noul assessments](evidence-v3-r1-epa-jev-boundaries.json) were valid,
but changed zero of 17 structural groups. Eight proposals do not qualify Jev
segmentation or establish a quality gain. The probe preserved exact source
coverage. Its raw report predates usage capture, so no actual Jev spend is
claimed from that file.

A broader [QASPER boundary probe](evidence-v3-r1-qasper-boundaries-8x4.json)
assessed 32 positions across eight public papers under the distinct
segmentation permission and rubric. All 32 calls were valid, reported 20,393
input tokens, and retained exact extracted-text coverage; no group IDs
changed. **The selected positions were existing structural cuts**, however,
so this is a check that Jev does not disrupt those cuts, not a fair test of
whether it finds better alternative cuts. A no-egress inspection found that
the nearest other atomic boundary is often only one whitespace character
away; more substantive alternatives are frequently hundreds of characters
away, beyond the current modest boundary bonus. This algorithm and selection
need a separately frozen alternative-cut trial before any claim about Jev
segmentation quality. No active-index promotion is justified by these calls.

A separate [40-paper live ranking comparison](evidence-v3-qasper-judge-live-2026-09-26.md)
compared local TEI with Jev Noul and Score on identical current-chunk
candidates. It informs judge quality, not the segmentation-by-judge
interaction matrix.
The [full 206-question current-TEI baseline](evidence-v3-qasper-main-tei-206-gpu.json)
found 51.0% complete evidence for TEI versus 53.4% for dense order, with a
paired equal-paper interval spanning zero. Its 40 overlapping cases exactly
match the live Jev trial's dense and TEI metrics. The 40-case Jev comparison
cannot be extrapolated to all 206 questions without more live Jev calls.

## Decision and limits

Keep the active index and default retrieval unchanged. Structural grouping is
a candidate for optional packet context, but its gains are uncertain and its
top-1 behavior needs examination. Re-embedding improves ContractNLI evidence
completeness in this local setup, while QASPER does not replicate that gain.
Do not promote a new index without dual-index/cutover, migration, old-citation,
storage, and rollback tests. Human evidence annotations provide a stronger
reference than model self-grades, but these runs do not test answer generation,
claim support, query abstention, hybrid retrieval, or all languages and tenant
policies. A segmentation-by-judge interaction matrix remains separate work.

## Reproduce without sending source text externally

From the repository root, create only an ignored dataset directory. Confirm
the SHA-256 values above before evaluation. The archives and extracted corpora
must remain outside Git.

```powershell
New-Item -ItemType Directory -Force Zenith_Evidence_Fork_v3/datasets/qasper | Out-Null
Invoke-WebRequest https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz -OutFile Zenith_Evidence_Fork_v3/datasets/qasper/qasper-train-dev-v0.3.tgz
tar -xf Zenith_Evidence_Fork_v3/datasets/qasper/qasper-train-dev-v0.3.tgz -C Zenith_Evidence_Fork_v3/datasets/qasper
Invoke-WebRequest https://raw.githubusercontent.com/stanfordnlp/contract-nli/gh-pages/resources/contract-nli.zip -OutFile Zenith_Evidence_Fork_v3/datasets/contract-nli.zip
python -c "import zipfile; z=zipfile.ZipFile('Zenith_Evidence_Fork_v3/datasets/contract-nli.zip'); open('Zenith_Evidence_Fork_v3/datasets/contract-nli-dev.json','wb').write(z.read('contract-nli/dev.json'))"
Get-FileHash Zenith_Evidence_Fork_v3/datasets/qasper/qasper-dev-v0.3.json -Algorithm SHA256
Get-FileHash Zenith_Evidence_Fork_v3/datasets/contract-nli-dev.json -Algorithm SHA256
docker volume create zenith-v3-r1-embed-cache
docker run --name zenith-v3-r1-embed-gpu --gpus all -p 127.0.0.1:18081:80 -v zenith-v3-r1-embed-cache:/data ghcr.io/huggingface/text-embeddings-inference:120-1.9 --model-id BAAI/bge-m3 --revision 5617a9f61b028005a4858fdac845db406aefb181 --port 80 --max-client-batch-size 8 --max-batch-tokens 4096
```

Run the container in a separate terminal. Then, from `backend/`:

```powershell
uv sync --group dev
uv run pytest -q eval/tests/test_qasper_trial.py eval/tests/test_contract_nli_trial.py app/features/ingestion/tests/test_lossless.py
uv run python -m eval.qasper_trial --dev-json ../Zenith_Evidence_Fork_v3/datasets/qasper/qasper-dev-v0.3.json --output eval/reports/evidence-v3-r1-qasper-dev-64-gpu.json
uv run python -m eval.contract_nli_trial --dev-json ../Zenith_Evidence_Fork_v3/datasets/contract-nli-dev.json --output eval/reports/evidence-v3-r1-contract-nli-dev-gpu.json
```

Per-case outcomes, identities, timings, and failure accounting are in the
[QASPER JSON](evidence-v3-r1-qasper-dev-64-gpu.json) and
[ContractNLI JSON](evidence-v3-r1-contract-nli-dev-gpu.json). No private
source corpus, model weights, or credentials are tracked.
