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

---

## Outcome — 2026-08-26

Done, verified, and the demonstration corpus is untouched.

| | Demonstration tenant | Cyber Standards Archive |
|---|---|---|
| documents | 26 | 15 |
| passages | 8,273 | 5,275 |
| pages | — | 1,493 |
| status | unchanged | all `ready`, none failed |

Row counts for the demonstration tenant are byte-for-byte what they were before the first
download: 4 users, 26 documents, 8,273 chunks, 8,273 embeddings. `zenith diagnose` reports
41 documents across the installation, every file present, nothing stranded.

**Isolation, checked at the level that matters.** The new administrator asking
`GET /documents` over HTTP sees fifteen documents and none of the other tenant's — not a
filtered list, a different one, enforced by the row-level security policy rather than by a
`WHERE` clause in our code.

**Retrieval works on documents nobody wrote questions for.** Four unscripted searches, all
`degraded: false`, 745–1,432 ms:

- *"How quickly must an incident be reported"* → 800-61r2, the incident-handling document.
- *"AC-2 account management"* → 800-53r5 p456 **and** 800-53b p30. The control catalogue and
  the baseline that references it, from one query. That is the cross-document behaviour, on
  an archive that was empty two hours earlier.
- *"what are the zero trust maturity stages"* → the CISA maturity model, pages 9 and 10.
- *"minimum password entropy requirements"* → 800-63b p78.

**Ingestion took about 90 minutes for 1,493 pages**, strictly sequential — the `cpu` profile
sets `ingestion_concurrency=1` so the parser's memory and the embedder's never overlap. The
492-page control catalogue alone was roughly 25 of those minutes. Worth knowing before
anyone uploads a corpus in front of an audience: this is a background job, not a demo step.

**What this corpus is not.** It has no measured recall figure and no verified question set.
Do not quote a percentage against it. The measured baseline belongs to the other tenant, and
Figures below describe one installation's corpus and are not a benchmark of the software.
