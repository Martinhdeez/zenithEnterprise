# Table extraction, and the audit-trail trap

**Date:** 2026-08-01
**Status:** ideas, not committed
**Origin:** an external review proposing three "innovative" features. Two were already in
the specification — bounding-box citation highlighting (§2.9, F5 + F9) and the query audit
trail (`mvp.md` line 261, iteration 4). This file records the third, plus a design trap in
the second that the review did not mention.

---

## 1. Tables — split it in two

Conventional RAG handles tables badly. A financial table flattened into a paragraph of
numbers is unreadable to a model, and "what was total revenue across Q1 and Q2?" produces
a confident guess.

### 1a. Structured tables at ingestion — cheap, worth doing

Render detected tables as Markdown (or JSON) inside the chunk text rather than as
flattened prose. The model then sees rows and columns instead of a smear of digits.

No new architecture, no new service. It belongs in **F5**, alongside the per-page parser
routing that already exists in the plan — docling detects table structure, so the
information is available and currently discarded.

Probably 80% of the perceived value.

### 1b. An agent executing code for exact arithmetic — post-MVP

Letting a model emit Python and running it to compute sums is a different product.

On an on-premise installation, executing model-generated code needs a real sandbox. This
entire project has been *removing* trust from components — RLS instead of `WHERE` clauses,
triggers instead of application discipline, a startup guard instead of a warning. Adding
"and then we run whatever the model wrote" cuts against all of it.

Not in the MVP. Revisit when there is a customer asking, and design the sandbox first.

---

## 2. The audit trail is a label bypass unless it is built label-aware

The review proposed an administrator panel showing "which text fragments were delivered to
the AI". That panel, built the obvious way, **defeats the access-label model entirely**.

An administrator who does not hold the Finance label could read Finance content through
the audit log. Three triggers, a `SECURITY DEFINER` bypass list and an entire RLS level
exist to prevent precisely that, and an audit view walks around all of them — through a
feature whose whole purpose is compliance.

**The rule, decided now so nobody has to decide it under deadline:**

| Audit data | Who sees it |
|---|---|
| Who asked, when, which document ids, which labels were in scope | `query.history.any` |
| **The retrieved text itself** | Only a caller whose own labels reach those documents |

Metadata is not content. An auditor can verify *that* a query touched a confidential
document without being shown what it said — which is what a compliance department actually
asks for, and it happens to be the safe design.

The `queries` and `query_citations` tables already store what is needed. The decision is
about the read path, and it is the read path that would leak.

---

## 3. What none of this changes

All three ideas depend on retrieval working, and retrieval quality is still unmeasured.
Adding features to a system whose core is unvalidated is how the F8 problem got created in
the first place. **M0 first.**
