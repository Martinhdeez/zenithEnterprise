# Standalone upstream extraction: atomic-ingestion

Base: 33b48812c95150348c52d2519159780252c92db2.
Fork source: e49fd78e225c4176d4e439b502fa6d5d61f85127.

Preserve one atomic replacement transaction, RLS, labels, source ranges and full vectors.
Use 128-row chunk VALUES statements with assigned UUIDs and bounded 64-vector flushes.
Check late failure rollback, first failure, idempotent retry and tenant/label isolation.
Historical 4.14x persistence evidence is measured on the integrated fork, not this extraction.

No dependency on the other new slices, the v10 foundation or Jev.
Preserve existing PRs and branches; publication is authorized, upstream merge is not.
Run focused host checks and full independent fork CI; upstream CI may need maintainer
approval. Do not restart Docker/WSL or claim the historical benchmark was rerun here.

Ruff lint/format and strict target Pyright passed. Real database rollback/isolation checks are the independent Linux CI gate.
