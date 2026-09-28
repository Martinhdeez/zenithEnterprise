# Public hybrid Jev smoke, 2026-09-25

This one-run smoke exercised the optional Jev Score6 route through disposable
real PostgreSQL under the application role, current RLS, hybrid candidate
selection, source reauthorization, the live TypeSafe API, and the unchanged
local TEI route. `backend/eval/test_live_hybrid_public.py` is the reproducible
test. It is skipped unless explicitly enabled with a securely supplied key.

Eight short public source-grounded paraphrases from the frozen
[`evidence-v3-public-v1`](../fixtures/evidence-v3-public-v1.json) fixture were
loaded as ready test chunks with synthetic embeddings. This is **not** an
upload/parser/real-embedding end-to-end result. The candidate IDs compared by
the two routes matched exactly for each question. The TEI route used the
repository's effective current-main model, revision
`1427fd652930e4ba29e8149678df786c240d8825`, on the isolated CPU 1.8
service. The Jev route used pinned `jev-1.13.0` and Score6.

| Public question | Legacy TEI response | Optional Jev response | TEI / Jev elapsed |
|---|---|---|---:|
| Will boiling tap water remove lead? | 0 hits; `relevance=none` after lexical-share veto | 8 ranked hits; EPA boiling passage first; `not_assessed` absolute evidence status | 324 / 2,803 ms |
| Does shaking severity change from place to place in one earthquake? | 8 hits; USGS intensity passage first | 8 hits; same first passage; `not_assessed` absolute evidence status | 150 / 2,607 ms |

This demonstrates one false suppression in the legacy policy on this small
fixture and confirms that the new route does not apply that lexical veto.
It does not prove Jev is generally more accurate: these are two agent-reviewed
questions with synthetic embedding vectors and short paraphrases. The Jev route
was about an order of magnitude slower in this run. It made 16 live pair calls
and reported 8,072 input tokens, an estimated **$0.000339024** at the
published input-token rate used in the pilot; the account invoice and any
unreported charges are unknown. This is in addition to the earlier 162 Jev
calls in the fixed-candidate pilot/diagnostics, still below the user's $0.50
and 1,000-call ceilings. No private source text was sent.

The exact result rows are in
[`evidence-v3-public-2026-09-25-live-hybrid.json`](evidence-v3-public-2026-09-25-live-hybrid.json).
Keep the current local route as default until a larger independently reviewed,
end-to-end evaluation and capacity study justify a configuration decision.
