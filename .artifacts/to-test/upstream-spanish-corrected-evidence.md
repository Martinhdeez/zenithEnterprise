# Standalone upstream extraction: spanish-corrected-evidence

Base: 33b48812c95150348c52d2519159780252c92db2.
Fork source: e49fd78e225c4176d4e439b502fa6d5d61f85127.

Publish complete public Spanish generated-answer/citation evaluation with a separate automatic NLI judge.
Corrected BGE 34/128 versus Jev 31/128; the measured difference does not justify promotion.
Preserve frozen protocol, public-only aggregate provenance, confidence interval, cached-score
latency limits and the distinction between automatic judging and external human adjudication.
Documentation-only: no provider code, private-corpus processing or evaluator threshold change.

No dependency on the other new slices, the v10 foundation or Jev.
Preserve existing PRs and branches; publication is authorized, upstream merge is not.
Run focused host checks and full independent fork CI; upstream CI may need maintainer
approval. Do not restart Docker/WSL or claim the historical benchmark was rerun here.

Documentation-only. JSON is parsed, CSV trial counts and relative artifact references are checked; no new quality experiment is claimed.
