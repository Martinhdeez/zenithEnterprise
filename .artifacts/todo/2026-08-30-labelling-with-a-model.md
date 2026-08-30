# Labelling with a model — the plan

**Date:** 2026-08-30 · **Status:** todo, stages 1, 2 and 2b done

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

## Stage 2b — the client learns it before the button, not after · **done**

Stage 1 made the suggestion say which ending it hit, and a later branch split `UNAVAILABLE`
into three. Both are *per row, after the pass*. So the first thing a new customer met was: a
prominent accented button, a hundred rows ticking past, and the same note on every one saying
nothing could ever have happened.

**Three of the six endings are settled before a model is spoken to** — `UNAVAILABLE`,
`NO_FOLDERS`, `TOO_MANY_FOLDERS` — so they can be answered in advance. `CHOSE`, `DECLINED`
and `FAILED` describe how a call went and cannot be. And this is not an edge case:
`eval/label-shortlist.json` records six tenants on this installation, **four of which hold
labels of which none is offerable**, which is the ordinary state of a fresh tenant.

The fix is the one already accepted for `UNAVAILABLE` in the table below — **do not offer the
button** — and it applies more strongly here because this is the common case.

### The shape, and the one that was rejected

**Rejected: `LabelResponse.offerable` plus a published `MAX_LABELS`, with the client
counting.** Two reasons.

1. It puts `NOT is_quarantine AND NOT is_default AND reach <= MAX_LABELS` in two places, one
   of which no test on the server side can reach. That is this repository's recurring
   failure, not a hypothetical.
2. **It would count the wrong set.** `GET /labels` returns `all_in_tenant()` to a caller
   holding `labels.manage` and `reachable()` to everyone else, while the classifier offers
   only what that person *reaches*. So an administrator's count would be wrong — for exactly
   the person most likely to press the button, and wrong in the direction that offers a dead
   action.

**Built: `GET /labels/suggest/availability`**, answering `{"reason": Outcome | null}`.

The part that carries the argument is not the endpoint, it is that
**`Classifier.availability` is the front half of `Classifier.file`, not a second opinion
beside it.** `file` calls it and continues from the `Ready(folders, provider)` it returns, so
there is one evaluation of the predicate and the call is what consumes it. A pre-flight that
recomputed the rule — in the browser or on the server — would be free to drift from the pass
it predicts.

`reason` is `Outcome` itself, not a translation, so the pre-flight and `POST /labels/suggest`
are compared value to value rather than through a mapping that could itself be wrong.

**`too_many_folders` is a fact about a person, not an organisation.** `len(reachable) >
MAX_LABELS` measures the reach of whoever would press the button: in one tenant an
administrator reaching 200 is refused while a member reaching 10 is filed normally. Any
sentence written about it has to say *your reach*, never *your tenant's labels*.

Errors are left to fail as errors. A refusal invented out of a broken request is
indistinguishable from a real one and would hide a working button, so `availability` raises
where `file` returns `FAILED`.

**Gates met:** a test per reason, each asserting it is not the other two; the boundary at
exactly `MAX_LABELS`; two people in one tenant answered differently; and agreement with
`POST /labels/suggest` in both directions, at the object level and over HTTP. Both HTTP
agreement tests were confirmed to fail against a router deliberately made to disagree. No
migration — nothing about this is stored.

**Left for the designer:** `suggestionAvailability(token)` in
`frontend/src/features/documents/api.ts`, returning `{ reason: SuggestionRefusal | null }`.
Calling it, deciding what the button does with each of the three, and every sentence a person
reads are stage 3's and untouched here.

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
| `NO_FOLDERS` | the button is not offered; the remedy is an administrator, not an operator |
| `TOO_MANY_FOLDERS` | its own sentence — the model is fine, *this person's reach* is too wide |
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
2b. ~~Stage 2b~~ · done. Three of the endings are now knowable before the button is drawn,
   which is what lets stage 3 not draw it.
3. **Stage 3** · in flight with the designer. Independent of stage 4 for the first four looks.
4. **Stage 4** · backend can start now that the trigger is settled; its chip state waits on 3.

## Numbers quoted here, and one that is not on disk

Everything above comes from `backend/eval/label-shortlist.json`, `latency.json`
(`total_median_ms` 873.44 for retrieval), and the source files named. **A generation median of
5,085 ms was quoted to the designer during this work and is not recorded under
`backend/eval/`** — it was measured over 30 questions and left in `/tmp`. Do not size anything
on it until it is landed as a real artefact or re-run.
