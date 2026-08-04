# ADR 0004 — The zero-fabrication gate is enforced after the model speaks

**Status:** Accepted

## Context

`mvp.md` §5.6 has one metric whose target is zero: **fabrication with a false citation**.

A gate of zero cannot be met by a prompt. Prompts are advice, and the fixed development
model is an 8B — chosen deliberately as the *floor*, because a citation format that survives
it survives anything a customer plugs in.

## Decision

The model's output is a **claim to be checked**, not a result to render. Three rules, in
`citations.py`:

1. A marker naming a passage that was not sent is **stripped from the text**.
2. An answer with **no valid citation at all** is discarded and replaced by the abstention.
3. Only surviving markers become rows in `query_citations`.

The model is shown **numbered passages and never a chunk id**, so a fabricated citation is
an integer out of range — checkable without touching the database. Put a chunk id in the
prompt and a fabrication becomes a plausible UUID, checkable only by a lookup, which is a
query for a row the model was never shown.

## Consequences

- **Rule 2 is strict and deliberate.** An uncited answer may well be correct, and there is
  no way to tell it from an invented one without reading the corpus — the work the user came
  to avoid. Shipping it would make the gate measure nothing: fabrications would simply stop
  wearing markers.
- **Streaming keeps rule 1 and cannot keep rule 2.** A marker is short and self-delimiting,
  so `MarkerFilter` validates it in flight and no invalid marker ever reaches a client.
  Whether an answer cites anything *at all* is knowable only at the end, so the stream ends
  with an authoritative `result` event and the client must honour it. That trade is written
  into the endpoint's own OpenAPI description, and `POST /query` keeps the strict behaviour.
- The frontend's state machine lives outside React so "replace, not append" is testable
  without rendering anything.

## Evidence

Four independent full runs of 36 questions through Llama 3.1 8B: **5/5 correct abstentions,
0 invalid citations**, every time. `unanswerable-tungsten` — whose answer is absent from the
corpus and present in the model's weights — abstained in all of them.
