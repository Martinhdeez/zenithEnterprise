# Evidence v3 final qualification

PR 08 validates the stacked PR 01–07 implementation without changing the
legacy default. The public QASPER test split is a locked ranking comparison:
select 240 source-bearing papers by SHA-256 paper ID and one question per paper
by SHA-256 question ID, discard answer annotations during preparation, freeze
current-main chunks and BGE-M3 dense top-eight candidates, then compare the
current TEI reranker with Jev Noul on identical candidates. The frozen manifest
SHA-256 is `083ef6880efaac1307c6bedd3ab3b29d3038132370780cf54bce1610aa2a26c7`.
Each Jev request is restricted to public source text and the reranking purpose;
the additional authorization is at most 10,000 calls and $5.00. A ledger
reserves calls before dispatch and never replays an uncertain reservation.

The primary quality metric is complete human-annotated evidence under the
existing fixed rendered-token budget, scored on all addressable questions with
TEI fallback for failed Jev batches. Secondary metrics are binary span-overlap
nDCG@8 among candidate-hit questions, top-one intersection, failure rate,
reported usage, and observed serial latency. Resample papers with replacement
for 2,000 paired bootstrap draws (seed 1729). Any apparent gain is exploratory
without an approved practical threshold and end-to-end answer-risk evidence;
do not promote a default from this ranking study alone.

The engineering gates are a fresh application-role PostgreSQL upload/readiness,
scoped query/citation/source/history flow, schema upgrade/downgrade/re-upgrade
from a clean worktree, local GPU generation on the integrated branch and current
main, provider-disabled compatibility, static checks, backend/frontend tests,
licence check, build, and proxy smoke. Record the unavailable rendered-browser
gate and any missing deployed or sustained-load evidence. Keep text-bearing
dataset snapshots and raw Jev replies in the ignored handoff folder; publish a
source-text-free case CSV and decision report. No schema or production index
change is part of PR 08. Rollback is disabling optional flags and reverting the
stack in reverse dependency order; test historical source/history readability.
