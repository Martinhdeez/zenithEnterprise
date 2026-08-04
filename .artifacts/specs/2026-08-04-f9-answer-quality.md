# F9 — Answer quality, and finally settling the Docling question

Two milestones deferred this. M0 wrote it down, F7 confirmed it a second time:

> Recall cannot measure comprehension. Retrieval finds the page whether or not the table on
> it survived extraction, so no retrieval metric will ever show what Docling would buy.

F7 proved the point by measurement — table questions scored **80% before reranking and 80%
after**, unmoved by the best ordering the system can produce. The passage is found. Whether
anything readable is *in* it has never been measured, and that is what F9 measures.

---

## 1. The metric that does not need a model

The obvious way to score an answer is to ask another model whether it is right. That has
three problems here: it needs a judge on the machine running the eval, it makes the number
depend on the judge's mood, and under RNF-07 it can never be a cloud model anyway.

The question set already contains something better. Every answerable question records an
**anchor** — the exact phrase its answer comes from, verified by `tests/test_questions.py`
against the extracted text on every run:

```toml
id = "table-attention-bleu"
question = "What BLEU score did the big Transformer reach on English-to-German?"
anchor = "28.4"
pages = [1, 8]
```

That anchor makes three things deterministic, in a chain where each stage bounds the next:

| Stage | Question | What it bounds |
|---|---|---|
| **Extraction** | does the anchor survive parsing at all? | everything |
| **Context** | does it reach the model, in the top 8? | what the model can possibly say |
| **Answer** | does the model state it, with a citation? | what the user gets |

**The middle row is the number this milestone exists to produce.** It is the ceiling on
answer quality, and it is measurable with no language model whatsoever: no model can state a
fact that was not in its context, however good it is. If the ceiling is 60%, buying a better
model is money spent on the wrong stage.

### What the anchor check is not

> **Corrected after measurement — see `2026-08-04-f9-results.md` §4.**
>
> This section originally predicted containment would be an **upper bound on correctness**,
> on the reasoning that it is lenient. The first run disproved that: an anchor of
> `DensePassageRetriever` scored the answer "Dense Passage Retriever (DPR)" as a miss, and
> ten of sixteen misses turned out to be correct answers on inspection. Containment
> measures **quotation**, not correctness, and is neither an upper nor a lower bound on it.
> The metric is now named `quotation_rate` for that reason.

Containment is lenient in one direction and strict in the other. An answer holding `28.4`
scores as a quote even if the surrounding sentence is nonsense — but a correct answer that
paraphrases scores as nothing at all, and paraphrase is what good prose does.

Typography is handled (the check retries on alphanumerics only, withheld from anchors
containing digits, since squashing `1.45%` to `145` would match page numbers). Paraphrase
cannot be, and an anchor lifted from a section heading will never appear verbatim.

What survives is the direction this project actually fears: a fabricated answer does not
contain the anchor, because the anchor is a string lifted from the document.

## 2. The Docling verdict, decided by measurement rather than by faith

Docling is a heavy dependency: its own model weights, its own inference, on the ingestion
path of a product that already runs on a 7.6 GB VPS. M0 routed pages to `Route.LAYOUT` and
then had `pipeline.py` parse them with pdfplumber anyway, with a warning, precisely so this
decision could be made with evidence instead of taste.

The evidence is the **extraction** row above, restricted to table questions:

- **If the anchor survives pdfplumber**, Docling cannot help that question. Retrieval finds
  the page, the fact is in the text, and whatever is wrong is downstream.
- **If the anchor is lost**, that is a question no reranker, no better model and no larger
  context can ever answer — and it is exactly what Docling exists to fix.

The count of lost anchors *is* the business case. Installing Docling before running this
would be adding a dependency on faith, which is the thing M0 refused to do twice.

**This measurement runs with no Docling installed**, because it measures what pdfplumber
loses. Only if it loses something does the comparison run become worth building — and at
that point the comparison is a second parser over the same pages, scored the same way.

## 3. Abstention, scored for the first time

Five `unanswerable` questions have been in the set since M0 and have never been scored.
They could not be: recall over a question with no correct passage is undefined, so the
retrieval harness recorded them and moved on.

Generation makes them scorable, and they measure the guarantee this product is sold on:

| Behaviour | Verdict |
|---|---|
| Abstains | correct |
| Answers, citing a passage | **fabrication** — the 0% gate, at its exact target |
| Answers with no citation | the binder already turned it into an abstention; counted as correct, and logged |

`unanswerable-tungsten` is the sharpest of the five. The melting point of tungsten is not in
the corpus and *is* in the model's weights, so answering it is not a hallucination in the
usual sense — it is the model being helpful with knowledge it was told not to use. A system
that answers it will answer a question about a customer's contract the same way.

## 4. What ships

```
eval/grounding.py   the model-free chain: extraction → context → the Docling verdict
eval/answers.py     the end-to-end run, through AnswerService and a real provider
```

`eval/answers.py` builds its provider through F8's registry rather than constructing one.
That is the abstraction earning its keep on its first outing: the harness runs against
Ollama, vLLM or anything else by changing `ZENITH_LLM_PROVIDER`, and the eval code contains
no vendor name at all.

It is **gated on a provider being reachable**, like `ZENITH_EVAL_RERANK` before it. A
harness that fails when a model is absent would make the model-free measurements — the
valuable half — unrunnable on a machine that has no GPU and no 5 GB to spare.

## 5. Hardening, which belongs here rather than in its own milestone

The API grew a generation endpoint, and generation is the first path that calls a system the
customer configured. Three gaps, all of them the same shape — an internal detail reaching a
user:

1. **No handler for unexpected exceptions.** `ZenithError` is handled; everything else
   becomes Starlette's default 500, whose body depends on how the app is served. A
   deliberate handler logs with a traceback and returns the same opaque JSON shape as every
   other error.
2. **`/query` has no rate limit.** Out of scope as a feature (M4 owns limits), but recorded
   here because generation is the expensive endpoint and it is now reachable.
3. **Fixtures.** `WorkingEmbedder` is defined in `test_rerank.py` and imported by the
   generation tests, which makes a retrieval test file a dependency of a generation one.
   It moves to `conftest.py` where shared fixtures belong.

## 6. What "done" means

1. `make check` green.
2. `python -m eval grounding` reports extraction and context rates per question type, and a
   **verdict on Docling** stated as a count of anchors pdfplumber loses.
3. The answer harness runs end to end against a real local model, or reports clearly that
   no provider is reachable and skips — never silently passes.
4. An unhandled exception returns the standard error shape, asserted by a test.
5. No new RLS bypass.

## 7. Recorded as out of scope

- **Installing Docling.** Gated on §2 producing a non-zero count. If pdfplumber loses
  nothing, adding it is a dependency with no measured benefit.
- **A judge model.** §1 explains why. If the anchor check ever proves too lenient to be
  useful, the replacement is a local judge under RNF-07, not a cloud one.
- **Rate limits.** M4.
- **Streaming.** Still F8's deferral, unchanged.
