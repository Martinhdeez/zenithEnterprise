# Standalone upstream extraction: gpu-local-batches

Base: 33b48812c95150348c52d2519159780252c92db2.
Fork source: e49fd78e225c4176d4e439b502fa6d5d61f85127.

Add only the optional 4096-token/eight-item GPU row and public deployment preset.
Keep CPU/low-spec/gpu rows, sequential client, model and worker count unchanged.
Verify actual Compose interpolation into API/worker and both TEI services.
Historical 53.38% embedding / 36.56% ready reduction is integrated-fork evidence.
The standalone slice does not change persistence or include the Jev evaluation harness.

No dependency on the other new slices, the v10 foundation or Jev.
Preserve existing PRs and branches; publication is authorized, upstream merge is not.
Run focused host checks and full independent fork CI; upstream CI may need maintainer
approval. Do not restart Docker/WSL or claim the historical benchmark was rerun here.

20 focused host tests passed in 9.48 seconds; Ruff lint/format and strict target Pyright passed.
