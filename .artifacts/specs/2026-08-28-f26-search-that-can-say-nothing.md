# F26 — Search cannot say "nothing here matches", and it must

**Date:** 2026-08-28 · **Status:** specification, not started

## The problem

Ask Chat something the corpus does not contain and it abstains: `citations.py` discards an
answer with no valid citation and replaces it with the abstention. Ask **Search** the same
thing and it returns passages, confidently, every time.

This is not a bug in the ranking. It is structural: **the dense half returns the k nearest
neighbours no matter how far away they are.** A kNN has no concept of "nothing is close" —
it returns the nearest thing in the index whether that thing is relevant or not. "What is
the melting point of tungsten?" over a corpus of Spanish employment law still yields eight
passages, ranked, with scores.

So the product's central promise — that an answer can be checked against its source — is
carried by Chat and quietly broken by Search, which is the screen users land on.

## What already exists

**Five negatives, already written.** `eval/questions.toml` carries five questions marked
`type = "unanswerable"`, with no document attached because the corpus does not contain the
answer:

- the price of a Zenith licence per seat
- a Bank Rate set in February 2027
- which GDPR article governs quantum computing
- the melting point of tungsten
- (one more of the same shape)

**They are scored for generation and not for retrieval.** `eval/answers.py` counts
`correctly_abstained` over them. The retrieval harness ignores them entirely, and the reason
is recorded in that file: recall over a question with no answer is undefined. That is true,
and it is exactly why this gap survived — the number that would have shown it was never
computed.

## The signals available, and what each is worth

| Signal | What it is | Worth as a relevance gate |
|---|---|---|
| `rerank_score` | a cross-encoder reading query and passage **together** | **the right signal.** It is trained to answer this exact question |
| `dense_score` | cosine similarity of two embeddings | weak alone: not comparable across queries — a three-word question and a twenty-word one have different score distributions |
| `lexical_score` | `ts_rank_cd`, frequency and proximity | presence is informative, magnitude is not — no IDF, so a common word scores like a rare one |
| the score *spread* | how far the top result stands above the tail | relative, so it needs no cross-query calibration — but a corpus of legal text is full of near-identical passages and its distribution is flat even for good matches |

## The decision that matters most

**A false negative here is worse than a false positive**, and by a wide margin.

If the gate hides a passage that was actually there, the user concludes the corpus does not
contain it — and they have no way to find out otherwise, because the screen told them there
was nothing to check. That is the opposite of this product's claim. A false positive, by
contrast, costs them reading two paragraphs and deciding for themselves, which is what they
were going to do anyway.

So the gate is **not** a single threshold that hides things. Three bands:

1. **Confident** — results as they are shown today.
2. **Weak** — results shown, with a line saying no passage strongly matches and these are
   merely the closest. The product already has this vocabulary: `degraded` says a component
   was missing, this says the corpus was.
3. **Nothing** — below the floor, no results, and a sentence saying so.

The middle band is what makes this shippable. Without it, every threshold argument becomes
"how much recall are we willing to destroy", and the answer to that is none.

## Measured, 2026-08-28 — `eval/separation.json`

Stage 1 has been run. **No signal separates perfectly**, and the naive reading of that —
"no threshold exists" — is wrong. The medians are twenty to one:

| Signal | Answerable (median) | Unanswerable (median) | Perfect separation |
|---|---|---|---|
| `rerank` | **0.9455** | **0.0463** | no |
| `dense` | 0.60 | 0.55 | no |
| `spread` | 1.19 | 1.17 | no |

`dense` and `spread` are dead: their medians are nearly identical, which is the outcome the
"not comparable across queries" and "legal text is flat" predictions expected. **The
reranker is the only usable signal**, and the overlap in it comes from two named causes
rather than noise.

### What a threshold actually costs

| Threshold | Negatives caught | Answerable hidden |
|---|---|---|
| **0.02** | 4 / 10 | **0 / 30** |
| 0.05 | 5 / 10 | 1 / 30 |
| **0.15** | **9 / 10** | 1 / 30 |
| 0.20 | 9 / 10 | 4 / 30 |
| 0.50 | 9 / 10 | 5 / 30 |

**A floor at 0.02 is free.** It rejects four of the ten negatives and hides nothing at all.
Between 0.02 and 0.15 the return is steep: another five negatives for one answerable
question. That is the shape the three-band design was written for, and it now has numbers
under it.

### The two causes of the overlap, and only one is fixable

**Cross-document questions score low, and that is structural.** The five `cross-*` questions
have a median rerank of **0.504** against **0.951** for the other twenty-five. A
cross-encoder scores one passage against the question; a question whose answer is assembled
from two documents has no single passage that answers it, so every candidate scores middling
by construction. `cross-payroll-and-return` at 0.0272 is the lowest-scoring answerable
question in the set and it is the one any threshold hits first.

**`unanswerable-future-rate` scores 0.9996 and no threshold will ever catch it.** The
question is "What Bank Rate did the Monetary Policy Committee set in February 2027?" and the
corpus contains a Bank Rate document — `boe-bank-rate` is an answerable question in this same
set. The cross-encoder judges **topical relevance, not factual presence**: a passage about
Bank Rate is a genuinely excellent match for a question about Bank Rate, and nothing in its
training tells it the date is absent.

This is the `temporal` miss, and it is out of reach of any relevance score. It must be named
as a limit rather than tuned at: **the feature can say "this corpus is not about that". It
cannot say "this corpus is about that but does not contain that fact".** Claiming otherwise
would be the overreach this whole specification exists to avoid.

---

## Stages

### Stage 1 — Make the gap measurable *(no product change)*

Nothing can be tuned before it can be scored.

- Extend the retrieval harness to score the `unanswerable` questions: for each, record the
  top `rerank_score`, `dense_score`, whether the lexical half matched at all, and the spread
  between rank 1 and rank 8.
- Record the same four for the 30 answerable questions.
- Output both distributions to a JSON report under `backend/eval/`, like every other figure
  in this repository.

**This stage answers the question the whole feature turns on:** do the two distributions
separate? If a strong match and a nonsense query produce overlapping rerank scores, no
threshold exists and stages 2–4 are the wrong design.

### Stage 2 — Characterise the reranker's scale

`mmarco-mMiniLMv2-L12` emits unbounded logits, not probabilities. Before any constant is
written down, establish what its output range actually is on this corpus and whether a
sigmoid makes it comparable across queries. A threshold on an uncharacterised scale is a
magic number waiting to be wrong on the next model.

**Five negatives is not enough to calibrate a boundary.** Expanding the negative set is part
of this stage, not an afterthought — twenty to thirty, spanning the ways a question misses:
wrong domain entirely, right domain and absent fact, a fact that would exist in a newer
edition of a document that is in the corpus. That last kind is the hard one and the most
realistic.

### Stage 3 — The gate, with the reranker

- Two constants, both derived from stage 1's report and both carrying the run they came
  from in a comment.
- Applied after reranking, in `SearchService`.
- **Acceptance is asymmetric, deliberately:** zero of the 30 answerable questions may fall
  into "nothing", and that is a hard gate. Abstention on the negatives is maximised subject
  to it.

### Stage 4 — The fallback, for when the reranker is absent

`low-spec` sets `rerank_candidates=0` and the reranker is the component most likely to be
down. The gate cannot simply vanish there — a screen that says "nothing matches" on one
machine and returns eight passages on another is the hardware-dependent behaviour ADR 0005
exists to prevent.

Without a reranker, fall back to the two weak signals **in agreement**: no lexical match at
all *and* dense similarity below a floor. Two weak signals agreeing is worth more than
either alone, and requiring both keeps the false-negative rate down, which is the direction
that matters.

Whatever the fallback decides, the response says which rule produced it. The client already
distinguishes "the system is degraded" from "the corpus is empty"; this adds "the corpus
does not contain this", and a reader must be able to tell the three apart.

## What this does not touch

Retrieval itself. Fusion, the three signals, the reranker: unchanged. This reads scores that
are already computed and decides what to do with them. Any diff in how passages are *found*
means the feature has drifted.

## Open questions

1. **Does the same gate belong on the anchored conversation?** Chat abstains through
   citations, which is a different mechanism and a stricter one. Two abstention rules in one
   product need to agree, or a user will see Search say "nothing" and Chat answer anyway.
2. **Does the gate run before or after the reranker's own truncation?** Reranking `cpu`'s 8
   candidates and gating on the best of those is not the same as gating on the best of the
   50 fused.
3. **What does the empty state offer?** Today's "nothing matched" for a zero-result search
   suggests fewer words and a broader folder. "Nothing in the corpus covers this" is a
   different sentence and needs its own.
