# The Spanish corpus, measured over HTTP

Run of record: `backend/eval/spanish-corpus.json`. Measured 2026-08-31 against the live
installation on `localhost:8000`, model `gemini-3.1-flash-lite`, lexical engine `tsvector`,
hardware profile `cpu`, reranker up. 45 calls, no errors, two full passes with **zero**
disagreement in abstention outcome between them.

This run exists because `backend/eval/live-recall.json` was measured against the 26 English
documents (GDPR, IRS, NASA) that this installation no longer holds. Any figure quoted from
that file about this installation is describing something else. Quote this file instead.

## What was not measured

**Recall@8 was not measured, and no recall figure may be taken from this run.** Recall needs a
labelled question set with verified `(document, page)` anchors. `backend/eval/questions.toml`
holds 43 questions anchored to the old English corpus, and `eval/live.py` drops any question
whose document is absent — against this corpus it would score nothing at all. Building an
anchored Spanish set is separate work. `questions.toml`, `live.py`, `corpus.toml` and the
tests were left untouched, as instructed.

Also not measured: answer correctness, mechanical grounding faithfulness, ingestion time, and
`alembic current` against head.

## The corpus

59 documents, 4,739 pages, 20,476 chunks, 20,476 embeddings, all `ready`, across two tenants.
Empresa Demo holds 55 documents / 4,568 pages / 19,738 chunks over seven labels; Organización Demo 2
holds 4 environmental documents / 171 pages / 738 chunks. Largest label is *Normativa
académica* (13 documents), heaviest is *General* (1,194 pages, carrying the Civil and Penal
codes and the two procedural laws).

## The account that could not be used

**`direccion@empresademo.es` does not authenticate.** `POST /auth/login` with the documented
password returns 401. Its `users.token_version` is 3 where all three other seeded accounts are
2, so its password was changed after seeding and the documented credential is stale. It is the
only account reaching all 55 documents. It was **not** reset — that is a write to a shared
installation and it signs out every live session.

Everything was therefore run as `juridico@` (reach 42 of 55), with a seven-question subset as
`secretaria@` (reach 20). Both reaches were confirmed against the database and match what was
expected. This should be fixed before any demo: it is the account a presenter would log in as.

## Headline numbers, as `juridico@`

| | paraphrase | exact-term | unanswerable | unreachable |
|---|---|---|---|---|
| questions | 12 | 14 | 6 | 6 |
| abstained | 2 (17%) | 5 (36%) | 6 (100%) | 5 (83%) |
| citations per answer, median | 2 | 1 | — | 1 |
| retrieval ms, median / p95 | 1002 / 1093 | 1121 / 1410 | 1009 / 1072 | 989 / 1111 |
| generation ms, median / p95 | 2251 / 4103 | 2525 / 4246 | 2471 / 4175 | 1214 / 3046 |

Overall: retrieval median **1024 ms**, p95 **1409 ms**; generation median **2380 ms**, p95
**4175 ms** over the 29 calls where generation ran. **Degraded rate 0/45.** Retrieval latency is
strikingly flat — every call landed between 831 and 1410 ms regardless of question or reach.
p95 on n this small is indicative only.

Abstention on genuinely answerable, in-reach questions: **7 of 26 (27%)** — that is the
false-abstention rate and it is too high. On the unanswerable set: **6 of 6**, no fabrication.

## Isolation: clean

Across all 45 calls, **every one of the 47 citations and every consulted passage was inside the
asking identity's reachable document set**. Zero violations. `secretaria@`, with 20 documents,
never saw a passage from the other 35, and never from the other tenant.

## What surprised me, and what should be looked at

**The lexical half is analysing Spanish with an English stemmer.** `zenith_text` maps to
`unaccent` + `english_stem` — correct when migration 0018 was written and the corpus was
English, wrong now. Spanish stopwords are not English stopwords, so `el`, `la`, `del`, `es`,
`cual`, `según`, `sobre` survive tokenisation, and `lexical.py` ORs the lexemes. Measured: the
query the application actually builds for "¿Cuál es el tipo general del IVA según la Ley
37/1992?" matches **20,384 of 20,476 chunks — 99.6% of the corpus**. Under the `spanish`
configuration the same construction matches 12,454 (60.8%). Across six real questions the live
analyser selected 99.5–99.9% every time. The lexical half is not discriminating; it hands
fusion the whole corpus ranked by `ts_rank_cd`. This is consistent with exact-term questions
abstaining at twice the paraphrase rate. Note that `ix_chunks_bm25` **is** built with Spanish
stemming — the correctly-configured index exists and is not the one serving queries, because
`ZENITH_LEXICAL_ENGINE` is `tsvector`. Changing the analyser rebuilds a generated column, so
it is a migration, not a setting.

**Citation markers point into the wrong array.** The `[N]` markers in `answer` are indices into
`consulted[]`, not `citations[]`. `citations[]` is the cited subset, renumbered from 1. **12 of
the 24 answered responses** carry markers that mis-resolve against `citations[]`; all 24 resolve
exactly against `consulted[]`. `e10` cites five documents and its answer contains `[8]`; `e14`
cites three and contains `[6]`. Anything resolving a marker against `citations[]` — a reader, or
the frontend — shows the wrong source or nothing.

**An out-of-reach question can be answered from an unrelated in-reach document.** `x02` asked
what paid leave the EBEP grants public employees. The EBEP sits under *Empleo y personas*,
outside `juridico@`'s labels. Instead of abstaining the system answered — "El Estatuto Básico
del Empleado Público reconoce permisos por parto, adopción o guarda y paternidad" — citing
`ley-35-2006-irpf.pdf` p15, a tax law. `secretaria@` did the same on `p07`: asked about erasing
personal data with the LOPDGDD out of reach, it answered from
`ley-enjuiciamiento-criminal-1882.pdf` about deleting records after a firm judgment. Isolation
is not breached and invariant 5 is satisfied in the letter — there is a citation — but the
citation does not support the claim. This is the failure a narrow-reach demo user is most
likely to hit, and it looks entirely credible on screen.

**Five exact-term questions abstained reproducibly** on both passes despite the document being
in reach: `e04` (Ley 9/2017, 273 pages), `e06` (the general VAT rate), `e08` (Ley 34/2002),
`e09` (art. 66 LGT), `e12` (LOSU governing bodies). These are the questions a buyer asks first.

**The abstention message is in English.** Every abstention returns "The documents provided do
not contain an answer to this question." on a Spanish corpus, on a Spanish-language installation — and
at a 40% abstention rate it is the string they will see most often.
