# Grouping search results — "folders"

**Date:** 2026-08-01
**Status:** idea, not committed
**Raised by:** the user, checking whether it was already planned. It was not.

---

## What was asked

Whether the product generates folders from labels after a search, produced by the AI.

**It does not, and it never did.** Nothing in `mvp.md`, `iteration-plan.md` or
`technical-decisions.md` describes grouping, clustering or foldering. §2.9 specifies a
flat ranked list of cited passages with bounding boxes. §2.2 explicitly rejects
hierarchical folder inheritance — but that was about *permissions*, which is a different
question from *presentation*, and the rejection does not settle this one.

Recorded here rather than answered in a conversation, because "did we plan that?" having
no written answer is how a specification stops being trusted.

## Two features, often confused

### A. Faceting by label — deterministic, cheap

Results already carry `label_ids`. Grouping them is a `GROUP BY` over data the query
already returned.

> 12 results — 7 Finance, 5 HR

No model call, no latency, no invented groups, and it explains itself: every group maps
to a label the user can see the name of. Probably most of the perceived value.

**Cost:** small. Mostly a response-shape change plus UI.

### B. Semantic clustering — a real feature

The model invents groups that correspond to no label: *"contracts near renewal"*,
*"Q3 budget discussions"*. More useful, and much harder:

- A model call per search, on the latency path RNF-01 constrains.
- Non-deterministic: the same query produces different groups on different runs, which
  makes it untestable in the way the rest of the system is tested.
- Group *names* are generated text over customer content, so they inherit every
  hallucination concern the answer generation has, in a place with no citation to check
  them against.

**Cost:** its own spec, its own evaluation approach.

## The rule either version must obey

**Grouping is presentation. It must never change which documents come back.**

The moment a group decides what is in the result set, it is a filter — and a filter a
model invents is a way to leak a document, or to hide one the user needed. Grouping
applies strictly *after* RLS and *after* ranking, over rows the user was already
entitled to see.

This is the same boundary that keeps `access_labels` administrator-assigned: a model
that hallucinates a tag must never become a model that grants access.

## Where it would land

After **F7 (retrieval)**, because there are no results to group before it. Not in F3 —
F3 is the security mechanism, and conflating the two is exactly the mistake this note
exists to prevent.

Recommended if pursued: build **A** first, measure whether anyone asks for **B**. A
deterministic grouping that ships is worth more than a clever one that needs an
evaluation harness nobody built.
