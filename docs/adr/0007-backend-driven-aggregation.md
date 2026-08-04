# ADR 0007 — The server computes groupings the client would get wrong

**Status:** Accepted

## Context

The document sidebar needs a folder tree. The obvious implementation is to fetch the
document list and group it in the browser.

## Decision

`GET /documents/folders` returns the tree already grouped. The client is a presentation
layer.

## Consequences

Three reasons, and the third is the one that settles it:

1. A client grouping documents itself needs **every** document to do it — the listing
   endpoint's entire page budget spent to draw a sidebar.
2. Two clients would group differently. A count that disagrees between the sidebar and the
   list is a bug report nobody can reproduce.
3. **A document with no labels is visible to the whole tenant; a labelled one is visible
   only to a role that reaches it.** That rule lives in an RLS policy. A client rebuilding
   the tree from a flat list has to re-implement it — which is exactly how a folder appears
   in a sidebar for somebody who cannot open anything inside it.

Consequently: a folder the caller cannot reach is **absent, not empty**. An empty "Finance"
tells a member that Finance exists and has something in it, which is the inference the
non-inference requirement forbids.

The same argument applies to `GET /tenant/status`, whose counts are label-scoped for
identical reasons, and it is why `searchable` is computed server-side rather than derived
from `chunks > 0` in three places that will eventually disagree.
