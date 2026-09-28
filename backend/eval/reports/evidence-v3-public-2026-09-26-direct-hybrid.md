# Public direct-versus-hybrid pilot, 2026-09-26

The same application-role PostgreSQL snapshot, document scope, and local GPU TEI reranker
were used for both routes. The eight source-grounded public paraphrases and ten queries
are pinned by fixture SHA-256 `f87123fa48e11495dbb9d19e63842d089651aaf885da28859e0c5d7e176b4bd8`.
The reranker was `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, revision
`1427fd652930e4ba29e8149678df786c240d8825`, in TEI 1.9 on an RTX 5070 Ti Laptop GPU.
The [machine-readable report](evidence-v3-public-2026-09-26-direct-hybrid.json) records
every order, coverage receipt, model identity, and per-request latency.

On the complete eight-chunk snapshot, hybrid assessed all eight candidates and direct
assessed all eight source windows. Their full order agreed on all 10 queries. Both had
9/10 labeled-best at rank one and mean nDCG@8 of 0.968. The second, warm run recorded
median hybrid latency of 208 ms and median direct latency of 207 ms. Hybrid was always
run first, so these paired latencies do not establish a general speed advantage.

For a separate, ungraded probe, the embedding row of the lead-boiling passage was removed
while its ready parsed source remained authorized. The agent-authored question was
"Does increasing thermal energy eliminate dissolved Pb?" Hybrid assessed seven candidates
and omitted that passage; direct assessed eight windows and returned it. This demonstrates
the no-vector coverage path, not a measured answer-quality win on normally ingested data.

The fixture uses test-seeded vectors and `WorkingEmbedder`, not the shipped embedding model;
the passages are short paraphrases, not production parser output. Labels are agent-reviewed
against linked public sources, not an independent human test set. This pilot does not
qualify either route for a default change, estimate sustainable throughput, or measure Jev.

Reproduce after starting disposable Docker Desktop/PostgreSQL and a locally cached TEI GPU
container on `127.0.0.1:18083` with the pinned revision above:

```powershell
cd backend
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = (Resolve-Path '..\Zenith_Evidence_Fork_v3').Path
$env:ZENITH_RUN_PUBLIC_DIRECT_HYBRID = '1'
$env:ZENITH_PUBLIC_TEI_URL = 'http://127.0.0.1:18083'
$env:ZENITH_PUBLIC_DIRECT_REPORT = (Join-Path (Resolve-Path '..\Zenith_Evidence_Fork_v3').Path 'pr04_public_direct_hybrid.json')
uv run pytest -q -x eval/test_direct_hybrid_public.py
```

The ignored `sitecustomize.py` in the handoff directory is a Windows Selector-loop
bootstrap for the PostgreSQL test fixture. On Linux, use the repository's normal test
environment. The script requires explicit opt-in and makes no paid model calls.
