# Evidence retrieval v3

The [v10 upstream submission index](../evidence-v3-hardening/upstream-submission-2026-09-28.md) distinguishes the merged fork integration from the 13 logical review slices, optional R1, and final reports. Its upstream CI runs are awaiting maintainer approval.

Evidence v3 is an optional extension to Zenith's existing retrieval and
citation flow. It does not change the shipped `legacy` search or local TEI
reranker defaults, create a database migration, or replace the active index.
External processing remains disabled for every purpose by default. The
[operator runbook](operator-runbook.md) documents configuration and rollback.

| Capability | Implementation and operator detail | Current decision |
| --- | --- | --- |
| Bounded Jev pair assessment, purpose-specific egress, quotas and fallback | [Jev provider](jev-provider.md), [hybrid integration](hybrid-provider.md) | Optional; Noul has a positive fixed-candidate English QASPER result, while Score remains experimental. |
| Legacy, hybrid, direct and automatic search with coverage receipts | [Runbook](operator-runbook.md), [retrieval service](../../backend/app/features/retrieval/service.py) | Legacy/TEI default; direct and auto require an explicit feature flag and authorized scope. |
| Lossless source mapping and structural grouping | R1 trial at `fix/evidence-v3-series-r1:backend/eval/reports/evidence-v3-r1-broad-2026-09-26.md` | Research only; no active-index cutover or default promotion. |
| Dependency packets and possible exceptions | [Packet trial](../../backend/eval/reports/evidence-v3-packets-2026-09-26.md), [runbook](operator-runbook.md) | Optional and off by default; measured source-span trials did not establish a gain. |
| Buffered strict claim support and abstention | [SciFact trial](../../backend/eval/reports/evidence-v3-scifact-support-2026-09-26.md), [runbook](operator-runbook.md) | Optional and off by default; false suppression and broad answer quality remain open. |
| Search controls, receipts and original-source viewers | [Search UI](../../frontend/src/features/search/Search.tsx), [text viewer](../../frontend/src/features/documents/viewer/TextViewer.tsx), [PDF viewer](../../frontend/src/features/documents/viewer/PdfViewer.tsx) | Tested with real UI login, application-role permissions, uploads, source downloads and visible highlights. |

The [final qualification report](../../backend/eval/reports/evidence-v3-final-qualification-2026-09-27.md)
states the current engineering and model decisions. The
[held-out 240-paper study](../../backend/eval/reports/evidence-v3-qasper-heldout-2026-09-27.md)
and [new disjoint 176-paper study](../../backend/eval/reports/evidence-v3-qasper-extension-2026-09-27.md)
describe their own frozen candidates, denominators, exclusions, latency and
cost estimates. The combined view reuses saved predictions; it is not a new
independent held-out trial. These studies qualify evidence ranking on the
specified public English paper task, not generated answers or private and
Spanish-domain model quality.

The [local release-validation record](release-validation-2026-09-27.md)
separates the exact code commit tested from later documentation-only commits,
records authenticated browser evidence and integrated gates, and names the
remaining limitations. The [skip audit](skip-audit-2026-09-27.md) accounts for
conditional tests. The [publication plan](publication-plan-2026-09-27.md)
preserves the incremental review order and distinguishes local acceptance
from remote CI, maintainer review, staging and production qualification.
