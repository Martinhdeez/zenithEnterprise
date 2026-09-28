# Conditional-test audit for evidence v3

The historical integrated check at `0a24cc3` passed 1,003 backend tests and
skipped 15. The exact skipped node IDs and messages were extracted from a
fresh JUnit run before local corpus provisioning. A green `make check` did
not count any skip as a pass.

| Original skipped nodes | Original reason | Classification and action |
| --- | --- | --- |
| `app/features/ingestion/parsers/tests/test_columns.py::{test_a_sentence_is_not_spliced_from_two_columns,test_reading_order_follows_the_column_down,test_every_box_stays_inside_its_column,test_the_detector_and_the_extractor_agree,test_a_two_column_page_whose_box_is_offset_still_parses}` | Evaluation corpus not checked out | Missing local public infrastructure. Downloaded and hash-verified the five pinned BERT PDFs into ignored `backend/eval/documents`; all five now run and pass. |
| `app/features/ingestion/parsers/tests/test_columns.py::{test_a_single_column_page_is_not_split,test_parsing_a_long_document_does_not_hold_every_page}` | Evaluation corpus not checked out | Pinned `gdpr.pdf` unavailable at the official EUR-Lex URLs checked; the tests still skip honestly. The long-document memory assertion also uses Unix `ps` and requires Linux or a portable probe once the exact pinned file is recovered. |
| `eval/tests/test_corpus.py::test_the_image_only_fixture_has_no_extractable_text` | Corpus not downloaded | Needs the same pinned GDPR fixture; remains skipped. |
| `eval/tests/test_questions.py::{test_every_anchor_is_on_every_page_it_claims,test_recorded_pages_are_exactly_the_pages_that_contain_the_anchor}` | Corpus not downloaded | Needs every PDF referenced by the committed answer anchors, including the pinned GDPR, EU AI Act, and Digital Services Act files. The marker now checks the full referenced set; these remain skipped. |
| `eval/tests/test_questions.py::test_anchors_are_distinctive_enough_to_be_evidence` | Corpus not downloaded | This reads only committed question metadata. The corpus marker was corrected and the test now runs and passes. |
| `eval/test_live_strict_public.py::test_real_jev_support_assessment_rechecks_application_role_source` | Requires approved public Jev call | Conditional live claim-support study. The user's new authorization covers the frozen remaining-QASPER **reranking** extension only, so this test remains opt-in. Existing historical one-call evidence is retained, not replayed. |
| `eval/test_live_hybrid_public.py::test_live_public_hybrid_comparison` | Live public Jev opt-in | Conditional different public Jev input/purpose. Not covered by the fixed-candidate extension authorization; remains opt-in. |
| `eval/test_direct_hybrid_public.py::test_public_direct_vs_hybrid_same_tei_and_scope` | Public direct/hybrid pilot opt-in | Local TEI feature coverage. Its report was moved to an ignored temp path; the configured isolated run passed. Default suite intentionally skips it. |
| `eval/test_local_ollama_public.py::test_local_model_retrieval_answer_and_abstention` | Local GPU model service opt-in | Local hardware/feature coverage. Run separately after GPU TEI is stopped, since the 12 GB GPU does not support the combined acceptance stack reliably. Default suite intentionally skips it. |

After provisioning BERT and correcting the metadata-only marker, the
configured corpus subset recorded 15 passed and four corpus-dependent skips.
The default full suite at tested code head `1862072` had **nine** remaining
conditional skips:
three GDPR-only cases, two full-question-corpus anchor cases, two differently
scoped live Jev tests, the direct
pilot, and the local Ollama case. The latter two can pass in separate
configured runs without converting their default skip to a normal test.

The GDPR hash remains pinned. Failed EUR-Lex downloads returned empty or
changed content, so no substitute PDF was silently substituted and no pinned
hash was updated. This is an unresolved reproducibility prerequisite, not a
product-path pass. The two live Jev skips also remain deliberate; the 1,396
newly authorized calls were spent only on the preregistered QASPER cohort.
