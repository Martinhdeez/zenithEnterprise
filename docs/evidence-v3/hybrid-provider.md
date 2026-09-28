# Hybrid evidence provider (PR 03)

Hybrid retrieval still uses the current lexical, dense, and exact-identifier
channels, reciprocal-rank fusion, channel-leader promotion, active embedding
space, and source hydration. `EVIDENCE_JUDGE_PROVIDER` defaults to `tei`, so
existing ranking and relevance behavior stays in place. An operator may set
`jev_score6` or `jev_noul` only with the distinct reranking processing flag,
credential, single-worker acknowledgment, and bounded quotas documented in
[`jev-provider.md`](jev-provider.md). Setting a provider without those controls
falls back to local TEI and marks the search degraded.

Before any external pair leaves the process, the service re-resolves the
requesting user's current permissions and labels and checks the exact candidate
text, document SHA-256/version identity, ready state, and original document scope through a short
application-role PostgreSQL transaction under RLS. It closes that transaction
before calling Jev. Every candidate is checked again at dispatch. Selected
sources are checked once more before disclosure. Changed or inaccessible
sources are withheld with a retry notice; no stale candidate is returned.
There remains an unavoidable interval between the last check and network
transmission. An already transmitted request cannot be recalled. Operators
must approve the complete source/query processing scope before enabling the
global purpose flag; mere read permission does not set that flag.

Jev's Score utility and Noul probability order a comparable candidate set.
They do not use the legacy TEI numeric bands or lexical-share veto. With no
calibration for absolute sufficiency, the response gives
`relevance=not_assessed`, `evidence_status=not_assessed`, and
`evidence_policy=evidence-status-v1`. Search displays the ranked passages with
an explicit coverage caveat. This is not a claim that the evidence suffices for
an answer. Legacy TEI response values retain their previous meaning.

An incomplete or unavailable Jev batch triggers a complete local TEI rerank
within the remaining request budget. The response reports the local provider,
`requested_judge_provider`, `fallback_provider=tei`, and a degraded notice.
If local ordering cannot complete, it returns the existing fused order with a
degraded notice; it never combines TEI scores with Jev utilities or treats a
failed judgment as zero. Source access is rechecked after this path too.

The real-PostgreSQL integration tests in `test_jev_hybrid.py` cover dense-only
evidence with zero lexical overlap, whole-order TEI fallback, and source
mutation during a queued assessment. The public fixed-candidate live comparison
is in `backend/eval/reports/`; it does not validate full retrieval quality.
Rollback sets `EVIDENCE_JUDGE_PROVIDER=tei` and disables the external
reranking purpose flag. No migration is involved.
