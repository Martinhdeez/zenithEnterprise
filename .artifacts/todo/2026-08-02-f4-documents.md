# F4 — Documents

Upload, deduplication, label-at-upload, physical deletion. The feature that puts a
customer's real bytes on our disk for the first time, which is why most of the difficulty
here is not the happy path.

F4 stops at `status='pending'`. Parsing, chunking and embedding are F5. What F4 owns is
the record, the file, and the labels — everything that decides *who may ever see it*.

---

## 1. Where the bytes live

**Content-addressed on the filesystem**, not in the database.

```
${ZENITH_STORAGE_DIR}/<tenant_id>/<sha256>.pdf
```

Three reasons, in order of weight:

1. **The filename never touches the path.** A customer PDF called `../../etc/passwd.pdf`
   is a path-traversal attempt, and the whole class disappears when the path is derived
   from a digest we computed ourselves. The original filename is stored in a column,
   displayed, and never resolved.
2. `bytea` in Postgres would put a 100 MB file through the WAL, the backup, and the
   replication stream. On the low-spec profile that is the difference between a working
   install and an install nobody can restore.
3. Content addressing makes the dedup check and the storage key the same fact.

New setting: `ZENITH_STORAGE_DIR`, validated at startup and reported by
`zenith diagnose` (writable? how much free space?). Free space is a real operational
question now that M0 measured ~5 MB per 100 pages of derived data on top of the original.

## 2. Upload

`POST /documents` — multipart, `documents.upload`.

**Streamed to a temporary file while hashing, never buffered in memory.** The service
runs with 8 GB total; `await file.read()` on a 100 MB upload from four concurrent users is
a straightforward way to reproduce the OOM M0 already hit once on the VPS. The digest is
computed on the same pass as the write, so the file is read exactly once.

Order of operations, and it matters:

1. Stream to `tmp`, hashing as we go, aborting past `max_file_bytes`.
2. Sniff the magic bytes. `%PDF-` for now — the declared `Content-Type` is a client
   claim, and F5's parser routing assumes what it is handed is really a PDF.
3. Insert the row inside the tenant's RLS session, labels included.
4. Move the temp file into place — `rename` on the same filesystem, so it is atomic.

Row first, file second. Inverted, a crash between the two leaves a blob nothing points at;
this way it leaves a row whose file is missing, which the status field can express and an
operator can see. Silent orphans are worse than visible inconsistency.

Limits enforced: `max_file_bytes` (100 MB), `max_documents_per_tenant` (5,000).

## 3. Deduplication

`UniqueConstraint("tenant_id", "sha256")` already exists in the schema.

The check is not "select then insert" — two simultaneous uploads of the same file both
pass the select. **Insert and catch the unique violation**, then return the existing
document with `200` instead of `201`. The database is the only place the race can be
settled.

Scope is deliberately per tenant: identical bytes in two tenants are two documents, on two
paths, with no shared row. Cross-tenant dedup would save disk and create a channel where
one customer's storage cost reveals another customer's content.

**Open decision — the one I want your call on.** A file already present is uploaded again
by someone from a different department, with a different label:

- **Union the labels (my recommendation).** The document gains the second label. The
  second uploader intended exactly that, and they already hold the bytes, so nothing leaks
  *toward them*. The cost is that people holding the second label can now see a document
  the first department uploaded — which is the honest meaning of "this file belongs to
  both departments".
- **Dedup only within an identical label set.** Stricter, simpler to reason about, and it
  stores the same bytes twice. It also produces the surprising result that uploading a
  file you can already see creates a second copy.

I recommend the union, with the response stating plainly that the document already existed
and which labels it now carries. Silent widening would be the wrong version of this; a
visible one is fine.

## 4. Labels at upload

**A document is never stored without at least one label.** This is the same hole
migration `0003` closed for chunks: an empty `label_ids` array satisfies no policy that
grants by label, and an empty array under a permissive read policy is visible to the whole
tenant. A Finance contract readable by everyone, with nothing in any log saying so.

- Caller supplies `label_ids` → validated against the labels the caller actually reaches.
  A user cannot file a document under a label they cannot read; that would let someone
  write into a compartment they are locked out of.
- Caller supplies none → `access_labels.is_default` for the tenant, which F3 guarantees
  exists and guarantees is unique via the partial unique index.
- Neither available → the upload **fails**. Not "store it unlabelled and fix it later".

The write goes through `document_labels`; F3's `zenith_sync_document_labels` trigger keeps
`documents.label_ids` in step. F4 adds no new denormalisation and no new
`SECURITY DEFINER` — the bypass surface stays at the four routes listed in
`technical-decisions.md` §5.1, and the two audit greps stay valid.

## 5. Deletion

`DELETE /documents/{id}` — `documents.delete.own` (only where `uploaded_by` is the
caller) or `documents.delete.any`.

Physical, per RF-03. The cascades already in the schema do the work: `pages`, `chunks`,
`chunk_embeddings` and `query_citations` all fall with the document. Then the blob is
unlinked.

Database first, file second, and this time for the opposite reason to the upload: unlink
first and a failed transaction leaves a row pointing at nothing that no code path can
repair. Deleting the row first can only leave an orphan blob, which a later sweep — or a
person — can reclaim.

**One consequence to state out loud rather than discover in iteration 4:**
`query_citations` cascades. Deleting a document erases the link between past answers and
the passages that produced them. The answer text survives in `queries`; the evidence does
not. That is the correct behaviour for a deletion the customer asked for (GDPR erasure
means erasure), but it is in tension with the audit trail planned for iteration 4, and the
audit design has to account for it rather than assume citations are permanent. Recorded
now, decided there.

## 6. Reading

- `GET /documents` — paginated, newest first, filterable by status. RLS does the access
  control; no `WHERE tenant_id` in application code, ever.
- `GET /documents/{id}` — metadata.
- `GET /documents/{id}/file` — the original bytes, streamed, for the citation viewer F7
  will need. Authorised by the same RLS read that any other access uses; a document
  invisible in the list is a 404 here, not a 403, because the distinction leaks existence.

## 7. What M0 obliges us to carry into F4's neighbourhood

- **`status='ready'` with zero chunks is forbidden.** F5 enforces it, but F4 owns the
  status column and the `pending` starting point, so the constraint is written here and
  the test that an image-only PDF cannot reach `ready` lands with F5's parser.
- `max_pages_per_document` (3,000) cannot be checked at upload without opening the PDF.
  Checked in F5, after parsing, where the page count is a fact rather than a guess.

## 8. Tests (real Postgres, testcontainers, never SQLite)

1. Upload creates a row, a file, and a label — and never zero labels.
2. The same file twice yields one row, one file, `200` on the second.
3. Two concurrent uploads of the same bytes yield one row (the unique violation path is
   exercised, not assumed).
4. A label the caller does not reach is rejected.
5. No label supplied → the tenant default is applied.
6. Tenant A cannot see, download, or delete tenant B's document — through RLS, with A's
   own session, not a mocked filter.
7. `delete.own` cannot delete another user's upload; `delete.any` can.
8. Deletion removes the row, the pages, the chunks and the file.
9. A filename containing `../` cannot escape the storage directory.
10. A file that is not a PDF is rejected before anything is stored.
11. Over `max_file_bytes` is rejected without the whole file reaching memory.

**Sabotage check**: remove the default-label fallback and confirm test 5 fails; remove the
magic-byte sniff and confirm test 10 fails.

## 9. Order of work

1. `ZENITH_STORAGE_DIR` + the storage module (path derivation, atomic write, unlink) and
   its `zenith diagnose` check.
2. Repository and service — upload, dedup, label resolution.
3. Router, schemas, permission wiring.
4. Deletion.
5. Read endpoints and the file stream.

One PR. No migration is needed: the schema for all of this landed in `0001` and `0003`.
