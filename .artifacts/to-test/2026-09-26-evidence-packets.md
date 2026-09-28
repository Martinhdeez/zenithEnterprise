# PR 06 evidence packets

This is the dated PR 06 plan and slice result. The later integrated local
`make check` passed at `1862072`; the Windows typing and first model-loading
issues below are historical. Packet and counterevidence flags remain off by
default because the measured source-span trials did not show a net gain.
Generated-answer quality remains unqualified. See
`backend/eval/reports/evidence-v3-final-qualification-2026-09-27.md` for the
current decision and `docs/evidence-v3/release-validation-2026-09-27.md` for
authenticated browser acceptance.

Base: R1 commit `2c08f104b5667fb393391e92f301a43d8c510bf9` on `feat/context-preserving-evidence-packets`. The default generation path still reads the top eight retrieved chunks. No schema or active-index change is planned.

The opt-in packet path selects whole evidence bundles under a conservative rendered-prompt budget. It reads only ready same-document chunks through the application's RLS session. A leading exception/condition or table row can require preceding context; a following exception and an explicit same-document section reference can add context. A fact with a known missing or over-budget dependency is omitted and the query response reports partial evidence. Candidate ranking never acts as a source-access check. Every final constituent is reauthorized before and after model inference. Packet streaming buffers the final answer until the post-inference check. Citation markers are assigned from the final rendered hit list, so an answer using an expanded span cites that span's existing chunk identity.

The separate counterevidence flag proposes at most two same-document exception-like chunks under the same budget. They are tagged as potential exceptions with applicability unassessed; neither a regex nor a ranking score establishes a contradiction. Both flags default off. The entire path remains local and does not enable Jev processing. `ZENITH_EVIDENCE_PACKETS_ENABLED=false` and `ZENITH_EVIDENCE_COUNTEREVIDENCE_ENABLED=false` roll back without a migration or citation rewrite.

Validation includes real PostgreSQL under `zenith_app` RLS, dependency/budget cases, changed-source disclosure, buffer behavior, old default compatibility, and matched source-span evaluations against top-k. The frozen relevant backend suite passed 335 tests. The final code-matched QASPER 206-case and ContractNLI 614-case local GPU runs found no packet recall gain and one QASPER TEI loss; methods and case rows are in `backend/eval/reports/evidence-v3-packets-2026-09-26.md`. These are evidence-visibility tests, not generated-answer quality, and cannot promote the flag or model default. A local Llama 3.1 8B model was downloaded, but its first inference request timed out during model loading; answer-quality and final full-stack gates remain open. Standard Windows `make check` is blocked by the clean-base Pyright `os.uname` error; Linux-target strict Pyright passed with zero errors.
