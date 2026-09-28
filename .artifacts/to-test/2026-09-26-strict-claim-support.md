# PR 07 optional strict claim-support assessment

Base: PR 06 commit `e8a61b84aaf09f49428608bd5525da687af9cd32` on
`feat/claim-support-assessment`. The existing citation binder and ordinary
default answer path remain intact. No migration or index change is involved.

The new default-off strict path splits a draft into bounded factual clauses,
checks marker references, exact quoted spans, numeric tokens, and simple
arithmetic independently, then assesses each eligible claim against the whole
rendered source context with a versioned Jev Noul rubric and a distinct
`claim_support` processing permission. It reauthorizes every source under the
application role before outbound dispatch and after inference. A failed,
incomplete, or inaccessible check cannot mark draft prose supported. One
repair is allowed; revised claims are checked again. Streaming is buffered
until the final verdict. HTTP and chat UI show the aggregate support status,
with English and Spanish abstention wording. The internal assessments retain
claim/evidence hashes and assessor provenance without putting source text or
private query content in diagnostics.

The 68-case public SciFact development trial used human rationale labels and
the real Jev adapter. At the trial's frozen 0.6 cutoff, a disjoint 10+10
within-development check accepted 8/10 supported and 0/10 contradicted
claim/document pairs with oracle abstracts. This is a limited support-assessor
comparison, not end-to-end answer quality or a promotion decision. The flag
and conservative 0.8 product cutoff remain unchanged. The local runbook and
`backend/eval/reports/evidence-v3-scifact-support-2026-09-26.md` give the
protocol, case export, limits, configuration, and rollback.

Focused deterministic/application-role and HTTP tests passed. The frozen
relevant backend suite passed 351 tests on real disposable PostgreSQL under
`zenith_app` RLS; the full serial frontend suite passed 449 tests. Ruff,
Linux-target Pyright, both builds, and the production-package licence gate
passed. The standard Windows `make check` still stops on the pre-existing
`eval/resources.py:131 os.uname` Pyright error. A live application-role
public-source Jev smoke passed. To roll back,
set `ZENITH_STRICT_CLAIM_SUPPORT_ENABLED=false` and disable the separate
claim-support egress permission. Existing query/citation rows need no rewrite.
