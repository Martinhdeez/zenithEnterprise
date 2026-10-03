# Standalone upstream extraction: candidate-lexical-gate

Base: 33b48812c95150348c52d2519159780252c92db2.
Fork source: e49fd78e225c4176d4e439b502fa6d5d61f85127.

Fix a local relevance false abstention when semantic selected hits lose candidate lexical evidence.
Keep no-lexical-match abstention and tenant isolation. The test uses real exact dense SQL
under RLS to avoid ANN recall variability on tied synthetic vectors. No Jev integration.

No dependency on the other new slices, the v10 foundation or Jev.
Preserve existing PRs and branches; publication is authorized, upstream merge is not.
Run focused host checks and full independent fork CI; upstream CI may need maintainer
approval. Do not restart Docker/WSL or claim the historical benchmark was rerun here.

Ruff lint/format and strict target Pyright passed. Real application-role search/isolation tests are the independent Linux CI gate.
