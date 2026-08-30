# Labelling with a model — the plan

**Date:** 2026-08-30 · **Status:** todo, stages 1 and 2 done

The request: a button that reads a file and decides its labels, possibly inventing new ones,
with a worry about being slow at two thousand labels.

**Most of it already existed.** `backend/app/features/ingestion/classification.py`,
`POST /labels/suggest`, the `classifying` stage of the pipeline, and provider resolution
through `generation/connector/resolve.provider_for`. This plan is therefore mostly about
**what not to rebuild**, one thing that was quietly broken, one idea that was measured and
refuted, and two things left to build.

---

## The fact that governs every decision here

**A label is not a tag. It is a permission.** `access_labels → role_labels → roles → users`,
and the policy is `label_ids = '{}' OR label_ids && zenith_current_labels()`. A button asking a
model to label a document is a button asking a model **who may read it**.

`label_ids = '{}'` means visible to the whole tenant, so the directions are not symmetric:

| the model does | consequence |
|---|---|
| adds a label to an **unlabelled** document | narrows from tenant-wide to a compartment — safe |
| adds a label **beside an existing one** | labels are a **union**, so this **widens** — unsafe |
| creates a label no role holds | document becomes **invisible to everyone**, uploader included |
| removes or replaces labels | widens — unacceptable |

### The three rules, already implemented, that keep it inside the access model

1. **It only runs on a document with no labels.** Not a preference about manual overrides
   winning — it is what makes the classifier *monotonically narrowing by construction*.
2. **The model chooses from a numbered list and never sees an id.** Anything not a number in
   range is discarded. **It cannot create a label.**
3. **The list is the uploader's own reach**, through `UserRepository.label_ids`.

---

## Stage 1 — the suggestion says which ending it hit · **done**, `1696201`

`classification.py` distinguishes four endings and its own docstring names the bug of
collapsing them. The ingestion path was fixed; `POST /labels/suggest` was not.

`SuggestedLabels` carried `label_ids` alone, so `DECLINED`, `UNAVAILABLE` and `FAILED` all
arrived as an empty list — and `Staging.tsx` added a fifth collapse with `.catch(() => [])`.
The visible consequence: under `FAILED` the interface promised *"no match — server will file
it"* about a document heading for a quarantine label only `admin` reaches.

`Classifier.suggest()` already knew — it called `self.file(...)`, which returns
`Filing(labels, outcome)`, and returned `.labels` off the end.

**Gate met:** four backend tests named for the endings, the deciding one asserting `FAILED`
**and** that it is not `DECLINED`; nine frontend tests including a rejected call, pinning the
exact string against `/server will file it/`.

---

## Stage 2 — can a shortlist lift the 60-label ceiling? · **done: no**, PR #33

`MAX_LABELS = 60`, above which a tenant gets no automatic filing at all.

Proposed: embed the label names, hand the model the nearest twenty-five, so cost stops
depending on the label count. **Measured and refuted** (`backend/eval/label-shortlist.json`):
micro recall of the human's own label at k=25 falls 0.7500 → 0.5227 → **0.1818** across pools
of 60, 200 and 2,000. At two thousand labels, four documents in five would be offered a list
containing no folder anyone chose.

The failure is not a weak embedding: the shortlist finds the right *region* every time and
cannot pick the folder out of its siblings. All three controls passed, so the failure is
believable — random scores 0.0227 against the measured 0.1818.

**The ceiling stays and is now measured rather than assumed.** It does not bind on this
installation: the corpus tenant has 19 labels, the uploader reaches 14.

**Not ruled out:** a shortlist built from the passages already filed under a label rather than
from its name. Different proposal; this run says nothing about it.

---

## Stage 3 — the button, the sparkle, and the review · **in flight, designer**

The mechanism exists and does not look like anything. `suggestLabels` is already called per
file by the staging area, and `noteFor(t, outcome)` renders all five endings in one neutral
treatment, differing only in words.

**The constraint that governs the design: the animation must read as a suggestion awaiting a
person, not as a label that has been applied.** That distinction is the whole safety model. A
sparkle that reads as "done" would be actively harmful.

The durable distinction is **shape, not motion** — nobody watches an animation twice, but
somebody looks at a screen of a hundred rows cold. A dashed outline chip for proposed, the
solid `TagChip` for committed. Legible without colour, survives a screenshot.

Five endings, five looks:

| ending | look |
|---|---|
| `CHOSE` | dashed chips, accept or dismiss per file, accept-all for bulk |
| `DECLINED` | a muted line — a real answer, not an error, not an empty state |
| `UNAVAILABLE` | the button is not offered; the Admin connector is one click away |
| `UNAVAILABLE` **above 60 labels** | its own sentence — the model is fine, the taxonomy is too large |
| `FAILED` | `--zenith-amber`, the same ink as `degraded` in Search, for the same reason: a component is missing. The only ending that leaves a document quarantined |

**The deeper client work is provenance.** `autoTag` puts the model's ids straight into
`row.labelIds` via `tagSelected`, so a label the model chose is byte-for-byte one the uploader
chose. Dashed-versus-solid cannot be drawn on that data; `stagingState.ts` has to carry it.

**Bulk decides the shape.** Per-row state, rows resolving as they land, a running count, and
cancellable — nothing has been written, so abandoning costs nothing. The sequential pass stays:
a hundred parallel calls would take the API's connection pool from everyone else.

---

## Stage 4 — the model may propose a *name*

**The decision, taken.** The user asked for the model to be able to create labels. It cannot
and must not: a label no role holds makes the document invisible to everyone, including the
uploader. **So the model proposes a name; a person creates the label and assigns it to roles in
the same gesture.** Rule 2 survives — the code still cannot create a compartment — and naming a
folder is the one judgement a person is genuinely better at than the model.

**The trigger is `DECLINED`, and it already exists.** It means the model read the document and
no folder fitted. That is precisely the moment a new folder might be warranted, and it is the
model's own judgement rather than a cosine threshold — which matters, because stage 2 killed
the thresholded version of this idea and this one does not need it.

**A third chip state:** proposed-and-does-not-exist. It must be visibly different from a dashed
chip naming a label that already exists, **because accepting one grants access and accepting
the other creates a compartment.**

Gates:

- A proposed name is **never** written by the classifier. The code path that creates a label
  stays the one a person drives, and a test asserts the classifier cannot reach it.
- Accepting a proposed name and assigning no role is either refused or warns plainly, because
  it produces a document nobody can read.
- `DECLINED` with a proposed name must still be distinguishable from `DECLINED` without one —
  the ending count grows, and stage 1 exists because collapsing endings is how this breaks.

---

## Order, and what each depends on

1. ~~Stage 1~~ · done, and it unblocked stage 3.
2. ~~Stage 2~~ · done; the answer was no, and it removed stage 4's original trigger.
3. **Stage 3** · in flight with the designer. Independent of stage 4 for the first four looks.
4. **Stage 4** · backend can start now that the trigger is settled; its chip state waits on 3.

## Numbers quoted here, and one that is not on disk

Everything above comes from `backend/eval/label-shortlist.json`, `latency.json`
(`total_median_ms` 873.44 for retrieval), and the source files named. **A generation median of
5,085 ms was quoted to the designer during this work and is not recorded under
`backend/eval/`** — it was measured over 30 questions and left in `/tmp`. Do not size anything
on it until it is landed as a real artefact or re-run.
