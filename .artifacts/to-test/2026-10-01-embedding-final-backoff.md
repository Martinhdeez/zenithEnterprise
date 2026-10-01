# Embedding failure latency

Independent extraction from Martinhdeez main 33b48812c95150348c52d2519159780252c92db2.

The measured CPU ingestion trial exhausted the five-second interactive embedding budget
on six of twenty queries. The client then slept for another second after its only attempt.
Backoff now happens only when another attempt remains. Ingestion keeps three attempts
and the existing one- and two-second delays between them; terminal failures skip the
previous four-second delay. Timeout, batching and fallback policies stay unchanged.

Regression cases exercise the public query and ingestion methods against both HTTP 503
and transport timeout failures. They verify request counts, delays and the domain error.
Run the embedding and retrieval tests on Linux, backend Ruff and strict Linux-target
pyright. Aggregate exact results are recorded in docs/local-first/followup.md on the
measurement branch. This change does not resolve CPU queue contention or claim a new
measured upload baseline; those measurements precede this fix.
