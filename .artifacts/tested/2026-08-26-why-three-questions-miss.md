# Why the three questions miss

The measured corpus scores Recall@8 of 90.0%. Three questions fail, and until now the honest
answer to "why?" was that we did not know. Traced stage by stage — lexical, dense,
exact-match, fusion, the cross-encoder shortlist, the returned page — they turn out to be
three unrelated defects with three different answers. Read-only; the retrieval path was not
changed to find this out.

A working question was traced alongside them as a control, because a stage report means
nothing without one: `gdpr-breach-deadline` is rank 1 at every single stage.

## 1. `boe-bank-rate` — the cross-encoder demotes it

*"What is Bank Rate?"* — answer on page 2 of the Bank of England document.

| stage | where the correct passage sits |
|---|---|
| lexical | rank 18 of 50 |
| dense | rank 13 of 50 |
| fused | rank 12 of 50 |
| shortlist (8) | **absent** |
| returned | absent |

It looked like a depth failure: both halves find it, fusion keeps it at 12, and a shortlist
of 8 cuts it before the cross-encoder ever reads it. So the shortlist depth was swept — and
the sweep disproved its own premise.

**At depth 32 the passage is in the shortlist, at rank 12, and the cross-encoder pushes it
out of the top eight anyway.** Depth is not what is wrong. The judge is. And depth costs:
Recall@8 is flat at every value from 8 to 32 while Recall@1 falls from 66.7% to 50.0% and
median latency goes from 965 ms to 3,222 ms (`eval/rerank-depth.json`).

**Fix:** a better cross-encoder, which means the GPU profile. Nothing in the `cpu` profile
recovers this question, and `rerank_candidates` stays at 8. This is the one of the three
that costs a headline point.

## 2. `cross-platform-obligations` — not a passage question

*"Which EU regulations here impose duties specifically on online platforms?"*

Absent from **every** stage: lexical, dense and exact all fail to propose any credited page.
That is not a ranking failure, and no amount of tuning addresses it.

The question asks which *documents* in the corpus have a property. No single passage answers
it; the credited pages are anchors inside two regulations that each demonstrate the property
separately. Answering it means reading both documents and reporting a set — an aggregation
over the archive, which is a different capability from ranking passages.

**Fix:** none that belongs in retrieval. The honest options are to build corpus-level
aggregation, or to accept that this class of question is outside what the product does. What
is *not* an option is deleting the question to raise the number — a question set edited until
it passes measures nothing.

## 3. `table-form-1040-status` — the form is flattened

*"What filing statuses does Form 1040 offer?"* — page 1 of the form itself.

Absent from every stage, and page 1 **is** in the corpus and chunked. The content is there;
it is not retrievable, because the filing statuses on a 1040 are a row of checkboxes, and
pdfplumber flattens that into disconnected tokens with no sentence to embed or match.

This is the known, documented limitation: `questions.py` records that `table` questions are
*"expected to score near zero: the tracer bullet runs pdfplumber with no Docling routing, so
tables are flattened by design"*, and excludes them from the headline for that reason.

**Fix:** table-aware extraction. Real work, already understood, and it does not move the
headline because this question type is deliberately not in it.

## What this changes

Nothing in the code, and that is the point. Two of the three are outside what this build
does and are documented as such; the third needs hardware the demonstration will not have.

What it changes is what can be said out loud. "Ninety percent, and here is which three fail
and why — one needs a stronger reranker, one asks a question about the archive rather than
about a passage, and one is a checkbox form our parser flattens by design" is a stronger
position than a number with three unexplained holes, and it is true.
