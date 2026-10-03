# Standalone upstream extraction: embedding-final-backoff

Base: 33b48812c95150348c52d2519159780252c92db2.
Fork source: e49fd78e225c4176d4e439b502fa6d5d61f85127.

Skip only the pointless delay after the final failed attempt.
Keep attempt counts, intermediate exponential backoff and exceptions unchanged.
Test timeout and HTTP 503 in interactive and ingestion modes with an observed sleep recorder.

No dependency on the other new slices, the v10 foundation or Jev.
Preserve existing PRs and branches; publication is authorized, upstream merge is not.
Run focused host checks and full independent fork CI; upstream CI may need maintainer
approval. Do not restart Docker/WSL or claim the historical benchmark was rerun here.

10 host embedding tests passed in 7.82 seconds; Ruff lint/format and strict target Pyright passed.
