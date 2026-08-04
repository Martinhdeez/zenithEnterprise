# F16 — Folders, M3 administration, and a status that never existed

Three surfaces, and one bug that had been shipping quietly in two places at once.

---

## 1. Folders are computed by the server

`GET /documents/folders` returns the label structure aggregated into the shape a sidebar
wants. `mvp.md` §2.14 lists folder hierarchies as a non-goal and this does not contradict
it — nothing here can be created, moved or nested. It is the labels the tenant already has.

**The grouping is server-side, and the third reason is the one that settles it:**

1. A client grouping documents itself needs every document to do it — the listing
   endpoint's entire page budget spent to draw a sidebar.
2. Two clients would group differently, and a count that disagrees between the sidebar and
   the list is a bug nobody can reproduce.
3. **A document with no labels is visible to the whole tenant; a labelled one is visible
   only to a role that reaches it.** That rule lives in an RLS policy. A client rebuilding
   the tree from a flat list would have to re-implement it — which is exactly how a folder
   appears in a sidebar for somebody who cannot open anything inside it.

Two consequences worth naming:

- **A folder the caller cannot reach is absent, not empty.** An empty "Finance" tells a
  member that Finance exists and has something in it, which is the inference §3.1 forbids.
  Asserted.
- **A label with no documents is omitted.** An empty folder is a place to click that does
  nothing; `GET /labels` remains the list for screens that *manage* labels.

## 2. M3 administration: roles and the connector

**Labels were already done** — F3 shipped create, list, rename, default, delete, role
assignment and document assignment. They are deliberately absent from the admin router
rather than duplicated: one resource administered from two places is how two screens start
disagreeing about what a label is.

So M3's real gaps were roles and the generation connector.

### The refusal that justifies the roles module existing

`ADMINISTRATION` is the pair of permissions that, if nobody holds them, locks a tenant out
of its own administration. Recovering from that on an on-premise install means somebody in
`psql` on the customer's server. So an edit that would leave the tenant with no
administrator is **rejected, not warned about** — and the check is across *held* roles,
because a role with the permissions and no users protects nobody.

Also: unknown permission codes are refused rather than stored. A permission nobody enforces
is a lie in the administration screen — it appears granted and grants nothing. System roles
cannot be edited at all, which is the same lockout blocked by a shorter route.

### The connector, and the key that is never read back

`GET /llm-config` reports whether a key is stored, **never the key**. An administration
screen that displays a credential turns every support screenshot into a disclosure, and
nobody needs to read it — only to replace it.

Omitting `api_key` on save keeps the stored one; sending `""` clears it. An administrator
editing a model name cannot re-enter a credential they cannot read, and wiping it on a save
that looked harmless would break generation.

**Nothing validates that the endpoint works.** §2.11 wants a "test connection" button
running the RNF-06 suite; pretending a 200 from `/v1/models` is certification would be
worse than admitting there is none yet.

## 3. The bug: a document status that has never existed

`documents.status` is constrained to `pending, parsing, chunking, embedding, ready,
failed`. There is no `processing`.

Both the F16 folder aggregation **and F13's status badge** filtered on one. Neither errored.
The backend count matched zero rows forever; the frontend read `undefined` and displayed
nothing. **The ingestion indicator this project added specifically so users would know why
search was slow has never once appeared.**

It surfaced only because a folder test tried to *insert* the invented status and the check
constraint rejected it — the database catching what neither the type system nor the tests
could, because both sides were consistently wrong.

Fixed in one place per side, derived rather than re-listed:

- backend: `IN_FLIGHT` in `documents/model.py`, computed from `DOCUMENT_STATUSES`
- frontend: an `IN_FLIGHT` constant with a test asserting its contents, because the client
  cannot import Python and a typo there is not a type error — it is a counter that reads
  zero forever

That duplication is real and is documented as such. The alternative is generating the
client's constants from the schema, which is a build step this project does not have.

## 4. Frontend

**History** — keyset pagination, appending pages rather than replacing them. No scope
control: the server decides whose history from the caller's permissions, and a client-side
toggle would imply the choice is the client's. `mine` is shown when a shared history is
being read, because a shared history is only readable if you can tell whose is whose.

**Folders** — a pure presentation layer over §1, with per-folder failed counts. "Which
folder" is the first thing anybody asks after "one document failed".

**Admin** — roles as permission toggles, the connector as a form. Both render the server's
refusals verbatim: *"this would leave nobody in the tenant holding: roles.manage"* tells an
administrator exactly what to do first, and a generic failure would not.

**Navigation is a union type, not a router.** Three screens with no deep links do not need
one, and a router would be the largest dependency in a bundle whose first paint matters on
a box F11 measured at four cores.

## 5. Still open

- **`cross-platform-obligations`** — the last question outside the 96.8% ceiling.
- **BM25** (F15 §5) and the **`queries` row-level policy** (F15 §6).
- **User invitation** — `users.invite` and `users.manage` are enforced nowhere, because
  there is no endpoint that creates a user outside the install CLI.
- **The RNF-06 certification suite** behind a "test connection" button.
