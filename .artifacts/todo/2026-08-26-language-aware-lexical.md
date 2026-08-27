# Language-aware lexical indexing

**Status:** planned, not started. Measured; the shortcuts are ruled out with evidence.
**Why it is here:** it is worth four times the cost driver behind the scaling limit, and it
cannot be done by choosing a better single configuration.

---

## The measurement

On the corpus of 25 August, one ordinary Spanish question:

| analyser | passages matched | share of corpus |
|---|---|---|
| `english` | 5,273 | 24.8% |
| `spanish` | 1,245 | 5.8% |

`ts_rank_cd` has **no IDF** and must score *every* matching row before taking the top 50. The
size of that match set is the lexical wall — measured at 5,953 ms over 300,000 passages under
real RLS. A four-fold reduction in it is four times off the cost of the thing that does not
scale, and it arrives as a quality improvement rather than a rewrite.

The difference is almost entirely stop-words: `de`, `la`, `el`, `los` are content words to an
English analyser, and they are the most common tokens in the corpus.

## Why the cheap versions do not work

**Switch to `'spanish'`.** The corpus is 77% English by passage — 16,355 against 4,940 before
the leftovers were removed. This would fix Spanish and take English stemming and stop-words
away from three quarters of the passages.

**Chain both dictionaries**, `unaccent, spanish_stem, english_stem`. A dictionary chain stops
at the first dictionary that recognises the token, and Snowball recognises everything — so
`english_stem` would never run. Measured:

```
spanish_stem on English:  'quickly' 'report' 'running' 'the' 'through'
english_stem on English:  'quick'   'report' 'run'
```

English loses its stemming *and* its stop-words. Strictly worse than today.

**A custom stop-word list** covering both languages. Postgres reads stop-words from a file on
the database server, which makes it a deployment artifact that must exist before the schema
does — awkward for an on-premise product, and it still leaves Spanish unstemmed.

Accent folding — migration 0018, already shipped — is the part of this that *could* be done
without knowing the language. It is not a substitute for the rest.

## What it actually needs

**A language per document, decided at ingestion**, and a `tsvector` generated with the matching
configuration:

```sql
tsv tsvector GENERATED ALWAYS AS (to_tsvector(text_config::regconfig, text)) STORED
```

`to_tsvector(regconfig, text)` is IMMUTABLE, so a generated column may take the configuration
from a column of its own row.

Detection does not need a dependency. Classifying the corpus by stop-word frequency —
`\m(de|la|el|que|los|para)\M` against `\m(the|of|and|for|with)\M` — separated all 39 documents
correctly on the first attempt, and the answer only has to be right per document, not per
sentence.

**The query side is the hard half, and it is where this stops being a small change.** Lexemes
from different analysers do not match: a Spanish question stemmed as Spanish produces `detencion`
where an English-indexed chunk holds `detención`. So the query has to be tokenised once per
configuration present in the corpus and the halves OR'd:

```sql
WHERE tsv @@ to_tsquery('zenith_es', :q_es)
   OR tsv @@ to_tsquery('zenith_en', :q_en)
```

The GIN index serves both, so this costs an extra `to_tsquery` per language rather than an
extra scan. `lexical.py`'s rule — *never tokenise a query with anything but the analyser that
built the index* — becomes *tokenise it with each analyser that built part of the index*, which
is the same rule and needs saying again in the new shape.

## Order of work

1. `documents.language`, detected at ingestion, defaulting to the installation's own guess for
   rows that predate it.
2. `chunks.text_config`, copied from the document, and the generated column rebuilt against it.
   The rebuild is the whole corpus; 0018 did the same in 2.4 seconds at 21,295 passages.
3. `lexical.to_tsquery` returns one query per configuration; `search.lexical` ORs them.
4. Re-measure Recall@8 *and* the match set. The second is the number this exists for.

## What has to hold afterwards

- Recall@8 does not regress from 90.0%; identifier questions stay at 100%.
- The match set for the same Spanish question drops from ~25% of the corpus towards ~6%.
- An English question against an English document is byte-for-byte what it is today. This is
  the one that would be easy to lose and hard to notice.

## Why it is not started

It rewrites the lexical half of retrieval days before a demonstration, and the current state is
measured, documented and honest: accents work, the analysis is English, and the runbook says so.
A search path changed in a hurry is the opposite of the thing this week has been for.
