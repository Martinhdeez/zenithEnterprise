# A second corpus, in its own tenant

## What and why

Build a fresh archive of real PDFs on a new topic, **beside** the measured demonstration
corpus rather than in place of it. The product is multi-tenant and enforces that isolation in
Postgres, so a second organisation costs nothing and destroys nothing.

The corpus that is here now is what `live-recall.json` measured: 26 documents, 8,273
passages, Recall@8 90.0%. Those numbers are properties of *those files* and of the thirty
questions in `eval/questions.toml` whose pages are re-derived from their extracted text.
Replacing the corpus would not lower the number, it would delete it, three days out.

A second tenant also demonstrates something the single-tenant install cannot: two archives on
one installation, and a user of one who cannot reach a byte of the other.

## Topic

**Cybersecurity and risk-management standards** — NIST, ENISA, CISA. Chosen for four reasons
and not for variety:

- Freely redistributable. NIST publications are US government work in the public domain;
  ENISA and CISA publish theirs openly. Nothing here needs a licence we do not have.
- Dense in identifiers — `SP 800-53`, `AC-2`, `PR.AA-05`. That is the control family the
  exact-match half of retrieval exists for, and it is the demonstration that lands hardest.
- Genuinely cross-document: the CSF maps to 800-53, which 800-171 derives from. Questions
  that need two documents are the ones a keyword search cannot answer.
- Enterprise-plausible. It is the archive a company evaluating this product might actually
  have.

English, deliberately. The text search configuration is `zenith_text` — unaccent then
`english_stem` (migration 0018) — and language-aware indexing is a written, parked plan. A
Spanish corpus would be demonstrating against a known limitation for no reason.

## Constraints

- **The existing tenant is not touched.** Row counts for it are recorded before and after and
  must be identical. No SQL against it, no purge, no reingest.
- Everything goes through the product: `zenith create-tenant` for the organisation, then the
  HTTP API as that tenant's own administrator. Nothing is inserted behind the application.
- Downloads are named before they happen — source, filename, size.

## Steps

1. Record the current installation state: tenants, documents, chunks per tenant.
2. Assemble the manifest — id, URL, why it is in the set — and fetch each PDF into a scratch
   directory. Verify each is really a PDF and record sha256 and page count.
3. `zenith create-tenant` for the new organisation and its administrator. The password is
   printed once; it goes to the operator, not into a file in the repository.
4. Log in as that administrator over HTTP and upload each document.
5. Wait for ingestion to finish. `zenith diagnose` must show no stranded documents and no
   failures.
6. Verify: document and passage counts for the new tenant, a real search returning cited
   passages, and `degraded: false`.
7. Verify isolation: the new administrator sees none of the demonstration corpus, and the
   old tenant's counts are unchanged.
8. Commit the manifest and a note recording what the new corpus is and what it is not — it
   has **no measured recall figure**, and must never be quoted as if it did.

## What this is not

Not a replacement for the measured corpus, and not something to demonstrate recall numbers
against. It is a live archive to show the product working on documents nobody has seen
before, which is a different and also useful thing.
