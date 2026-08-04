# ADR 0001 — Isolation is enforced by Postgres, not by application code

**Status:** Accepted

## Context

Zenith is multi-tenant and, within a tenant, compartmentalised by access label. Two things
must never happen: one customer reading another's documents, and an employee reading a
compartment they are locked out of.

The usual approach is a `WHERE tenant_id = ...` on every query. It works until somebody
writes the query that forgets it — and that query looks exactly like the one that does not.
The failure is silent, the symptom is a leak, and no test catches it because the test author
also forgot.

## Decision

Isolation is enforced by **Row Level Security policies in Postgres**, keyed on two session
variables: `zenith.tenant_id` and `zenith.label_ids`. Application code never filters by
tenant. `tenant_session()` is the only supported way to reach the database.

Two levels:

- **tenant** — the row carries its own `tenant_id`
- **label** — a document with no labels is visible tenant-wide; with labels, only to a role
  reaching at least one

The bypass surface is **four named routes** (install CLI, diagnostics, the ingestion
requeue, and tenant creation), audited by grepping for `owner_session` and
`SECURITY DEFINER`. It grows for a security guarantee, never for ergonomics.

## Consequences

- A forgotten filter returns *nothing* instead of everything. The failure mode inverts.
- `chunks.tenant_id` and `chunk_embeddings.tenant_id` are denormalised, because the tenant
  filter must apply *inside* the vector query and a join there penalises the HNSW index.
- `documents.label_ids` duplicates `document_labels` — not for speed, but because policies
  reading each other abort on mutual recursion.
- **RLS models tenant and label, and nothing else.** Query history needed "mine versus my
  colleagues'", which neither level expresses, so `history.py` filters in application code.
  That exception is documented at the top of the module it lives in, and tested like the
  security control it is. See ADR 0004's principle applied in reverse: when the database
  cannot enforce it, say so loudly rather than quietly.
- Tests run against **real Postgres via testcontainers**. SQLite has no RLS, so a green
  suite against it would prove nothing about the only thing that matters here.

## Evidence

`verify_rls_active()` refuses to serve if policies are not in force on the connection —
because pointing the application at the owner credentials would disable every guarantee
above with no symptom whatsoever.
