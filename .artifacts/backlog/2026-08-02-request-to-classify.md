# Request-to-classify

**Status:** deferred from F4, deliberately. Recorded so the gap is a known one.

## The situation

Postgres unique constraints see every row; row-level security does not. So when Finance
holds a document and someone in HR uploads the same bytes:

- `SELECT ... WHERE sha256 = ?` returns nothing — the row is behind a label HR does not
  reach.
- The `INSERT` still collides with `UNIQUE (tenant_id, sha256)`.

The deduplication rule agreed for F4 says the labels are unioned. That cannot be done from
inside the caller's transaction, because the row to update is invisible to them. Reaching
it would mean a fifth entry on the RLS bypass surface (`technical-decisions.md` §5.1),
which is currently four named routes audited by two greps.

## What F4 does instead

Refuses, with a message that says nothing about an existing document:

> this document cannot be stored under the labels requested. Ask an administrator to
> classify it.

The message is deliberately uninformative. Confirming that these exact bytes are already
held would tell HR that Finance holds this specific file — in a compartmentalised
installation that is classified metadata, and an opaque error is better than a metadata
leak. Widening the `SECURITY DEFINER` surface to smooth out the ergonomics of a duplicate
upload is the wrong trade.

## The flow this should become

The uploader records an *intent* rather than a document: "these bytes, this label". An
administrator holding both labels — the only person who can see both sides — approves it,
and the union happens under their authority, in their own context, with no bypass.

Properties worth keeping when it is built:

1. The requester learns nothing from making the request. A pending request must look the
   same whether or not a matching document exists.
2. The approver sees both the existing document and the requested label, because they
   reach both. No elevated session is needed at any point.
3. The approval is an event worth logging: it is the moment access widened, and who
   decided it.

## Why not the alternatives

- **A `SECURITY DEFINER` merge function.** Works, and adds a fifth bypass route for a
  convenience rather than for a security guarantee. It also still discloses the existing
  document's filename, uploader and date to the second uploader once the union succeeds.
- **Drop the unique constraint, store per-label copies.** Duplicate chunks in every search
  result, and the ingestion cost of a second copy against a hardware budget M0 measured at
  roughly 5 minutes per 100 pages.
