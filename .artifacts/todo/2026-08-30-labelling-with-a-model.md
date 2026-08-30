# Labelling with a model — what is missing, and what must not be built

**Date:** 2026-08-30 · **Status:** todo

The request was a button that reads a file and decides its labels, possibly inventing new
ones, with a worry about being slow at two thousand labels.

**Most of it exists.** `backend/app/features/ingestion/classification.py`,
`POST /labels/suggest`, and the `classifying` stage of the ingestion pipeline already do the
work, already use the tenant's configured provider through
`generation/connector/resolve.provider_for`, and already hold the safety rules this feature
needs. This note is therefore mostly about **what not to rebuild**, and about the three gaps
that are real.

## The fact that governs every decision here

**A label is not a tag. It is a permission.** `access_labels → role_labels → roles → users`,
and the policy is `label_ids = '{}' OR label_ids && zenith_current_labels()`. A button that
asks a model to label a document is a button that asks a model **who may read it**.

`label_ids = '{}'` means visible to the whole tenant. So the directions are not symmetric:

| the model does | consequence |
|---|---|
| adds a label to an **unlabelled** document | narrows from tenant-wide to a compartment — safe |
| adds a label **beside an existing one** | labels are a **union**, so this **widens** — unsafe |
| creates a label no role holds | document becomes **invisible to everyone**, uploader included |
| removes or replaces labels | widens — unacceptable |

## What already exists and must not be re-derived

Three rules, already implemented, and better formulated in the source than in this note:

1. **It only runs on a document with no labels.** Not a preference about manual overrides
   winning — it is what makes the classifier *monotonically narrowing by construction*, and
   therefore what stops a model being able to widen access at all.
2. **The model chooses from a numbered list and never sees an id.** Anything it writes that
   is not a number in range is discarded, the same reasoning `generation/prompt.py` records
   for citations. **It cannot create a label**; that is not something the code can do.
3. **The list is the uploader's own reach**, resolved through `UserRepository.label_ids` — so
   it can never file a document under a compartment that person could not have chosen by hand.

Also already right: the three endings are distinguished rather than collapsed into an empty
list — *found nothing that fits*, *no model configured*, and *the model broke* — and only the
last leaves the document in quarantine.

## Gap 1 — the ceiling, and it is the user's actual worry

```python
MAX_LABELS = 60   # above this a tenant gets no automatic filing rather than bad filing
```

**Above sixty labels the feature switches itself off**, and a customer does not necessarily
know it has. The source already rejects the naive fix, correctly: taking the first sixty by
name "would file everything under whatever begins with 'a'".

**Proposed: a semantic shortlist** — embed the label names once, embed the document, hand the
model the ~25 nearest by cosine, so the cost stops depending on the label count.

**Measured, and refuted.** `backend/eval/label-shortlist.json`. Recall of the label a human
actually chose, at k = 25:

| pool | micro recall | any true label |
|---|---|---|
| 14 (this installation) | 1.0000 *(meaningless — k exceeds the pool)* | 1.0000 |
| 60 | 0.7500 | 0.9231 |
| 200 | 0.5227 | 0.6538 |
| **2,000** | **0.1818** | **0.2308** |

At two thousand labels **four documents in five would be offered a list containing no folder a
human chose.** Keeping 95% needs k = 1,822 of 2,000, which is not a shortlist.

**The failure is not a weak embedding, and that is what kills the idea rather than sending it
back for tuning.** The shortlist finds the right *region* every time and cannot pick the folder
out of its own siblings: `codigo-civil.pdf` puts `Legal & Regulatory` at rank 53, behind
`Legal/manuals/2024`, `Legal/claims/signed` and `legal/policies`. A large taxonomy has far more
than twenty-five plausible legal folders. All three controls passed — random scores 0.0227
against the shortlist's 0.1818 — so it is doing real work, about eight times chance, and
nowhere near enough.

**The cost claim was entirely right and does not help:** the document vector is free (the
pipeline embeds before it classifies), a label name costs 51 ms once, and the query is 3.14 ms
at 2,000 rows.

**Why this is worse than the ceiling rather than merely no better.** Filing only ever narrows.
A declined document reaches the tenant default, where it already was — visible, no harm. A
document filed into a topically plausible *wrong* compartment is hidden from the people who
should have it. Trading no filing for a 23% chance that the right folder is even on offer is a
trade against the product.

**So `MAX_LABELS = 60` stays, and is now measured rather than assumed.** It also does not bind
on this installation: the corpus tenant has 19 labels and the uploader reaches 14. Everything
about 2,000 is a projection onto a synthetic pool.

**What this does not rule out:** a shortlist built not from a label's *name* but from the
passages already filed under it. Different proposal, different cost; this run says nothing
about it.

**A consequence for gap 2:** above 60 labels the button is not offered, permanently, and the
reason is not "no model configured" — the model is there and the taxonomy is too large for it
to choose well. If those two share a look, an operator goes and checks a connector that is
working fine.

## Gap 2 — it does not look like anything

`POST /labels/suggest` writes nothing and is already called per file by the staging area. What
is missing is the visible, deliberate action the request asked for: a button, a sparkle, an
animation.

Owned by the designer. The constraint handed over with it: **the animation must read as a
suggestion awaiting a human, not as a label that has been applied.** That distinction is the
entire safety model. A sparkle that reads as "done" would be actively harmful.

Two states that need their own look, because the code already distinguishes them and the UI
currently does not: the **empty answer**, which is a correct answer and not a failure, and the
**model broke**, which is the only case that leaves a document in quarantine.

The wait is real and must be designed for: generation runs a **median 5,085 ms with a tail to
12,673** on this installation, and a bulk upload multiplies that by the file count.

## Gap 3 — creating new labels, which is the one the user should decide

Asked for, and **deliberately impossible today**. A brand-new label that no role holds makes
the document invisible to everyone, including the person who uploaded it.

**Recommendation: the model may propose a *name*; a person creates the label and assigns it to
roles in the same gesture.** The code still never creates a compartment, rule 2 survives
intact, and the user gets what they asked for. A third UI state follows from it — a suggested
label that does not exist yet — and it should not be built until this is decided.

Note that unrestrained label creation is also what *produces* the two thousand labels the
request was worried about. A threshold — propose a new name only when nothing existing is close
enough — is the same measurement gap 1 is already taking.

## Order

0. **Measure the shortlist.** In flight. Decides gap 1 and informs gap 3's threshold.
1. **Lift the ceiling** if the measurement supports it, behind the existing scoping.
2. **The button and its animation**, with the designer.
3. **Proposed new labels**, only if the user takes the recommendation in gap 3.
