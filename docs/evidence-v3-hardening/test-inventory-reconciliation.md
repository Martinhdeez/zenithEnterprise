# Backend test inventory: historical release to v10

The historical executable release `1862072e55f3d79877334964b0e1b3a8f473a39f` recorded 1,013 passed and nine skipped. The unchanged v10 executable head `910bc125bb6f731ca681f20ac8ae19d97a5e0874` recorded 1,004 passed and 15 skipped on Windows and Linux. These are results from their respective default `make check` environments; skips are not passes.

On 28 September 2026, read-only `pytest --collect-only -q` with the same local Python environment found 1,022 node IDs at the historical commit and 1,019 at v10. Comparing exact IDs found 19 absent and 16 new. Three on each side are the same parameterized system API tests whose randomly generated tenant UUIDs changed in the displayed node IDs. Excluding that display-only churn, 16 historical tests left the core collection and 13 were added, for a net reduction of three collected tests.

The 16 removed IDs belong to the separate optional R1 research branch:

| Historical test module | IDs absent from v10 core |
| --- | ---: |
| `app/features/ingestion/tests/test_lossless.py` | 7 |
| `eval/tests/test_contract_nli_trial.py` | 2 |
| `eval/tests/test_lossless_trial.py` | 3 |
| `eval/tests/test_qasper_judge_trial.py` | 1 |
| `eval/tests/test_qasper_trial.py` | 3 |

The 13 new IDs are one strict decimal-equivalence test, five Jev accounting/diagnostic tests, one opt-in real generator/Jev/support test, two neutral-harness boundary tests, two Jev options-harness tests, and two proxy-guard tests. The R1 tests remain in the isolated R1 branch; their absence from the core is a publication boundary, not a test silently disabled in place.

The six additional default skips account for the rest of the passed-count difference: five BERT-paper column tests that ran in the historical checkout skip in the v10 checkout because its ignored, hash-pinned `bert-paper.pdf` was not provisioned there; the new combined real-model test is opt-in and skips by default. The other nine historical conditional skips remain: two GDPR column tests, one image-only corpus test, two complete-question-corpus tests, two differently purposed live Jev tests, one direct/hybrid pilot, and one local Ollama pilot. The v10 `test_columns.py` module-level marker skips all seven column tests when the pinned BERT file is absent, including the two GDPR tests. No alternate PDF or changed hash was substituted.

The arithmetic is therefore `1,022 - 3 = 1,019` collected and `9 + 6 = 15` skipped; `1,013 - 3 - 6 = 1,004` passed. The exact node IDs can be regenerated with `pytest --collect-only -q` at the two named commits; the full local gate results and earlier failed Vitest attempts are recorded in [per-PR validation](per-pr-validation.md). This reconciliation is a collection comparison plus the recorded run outcomes, not a rerun of the historical full suites.
