# ADR 0005 — One profile table; performance may vary, semantics may not

**Status:** Accepted; floor measured in F11

## Context

The same software runs on a GPU server and on a 7.6 GB VPS. Something must differ. The
question is what, and where the difference is written down.

A boolean spreads: `if gpu:` appears in the compose file, then the worker, then the reranker
client, and each site drifts.

## Decision

`ZENITH_HARDWARE` selects a **profile** — a named row in one table — and nothing else in the
system branches on hardware. A profile may change *performance*; it may never change
*semantics*. Degradations are visible: `zenith diagnose` names what a profile disabled, and
a customer never has to infer it from a recall number.

## Consequences

- The profile and the TEI container are **one setting in two places**, which is a trap. F11
  found `tei-rerank` started with no batch flags at all: TEI kept its small defaults, the
  client sized batches from the profile, and every rerank request was rejected. Search did
  exactly what it was designed to do — caught it, fell back to the fused order, marked it
  `degraded` — so nothing crashed and no test failed. The only symptom was recall fifteen
  points below what F7 measured. Now pinned from shared variables and asserted by
  `test_compose_matches_profiles.py`.
- Compose defaults describe **the smallest supported machine**, because the only safe
  assumption about a box nobody has measured is that it is the smallest one.

## Evidence, and one number that is still a guess

Measured on a 4-core, 7.6 GB VPS (F11):

- `low-spec` disabling the reranker is **correct**, not merely cautious: the cross-encoder
  cannot rerank even *ten* passages inside the 5-second interactive timeout, and with the
  `cpu` profile's own TEI flags the container is OOM-killed at 4.3 GB during warm-up.
- **`cpu`'s `rerank_candidates = 50` remains uncalibrated.** It has never run on hardware
  that can host the profile. Lowering it on the strength of a box that cannot run it at all
  would repeat F7's mistake in the other direction, so it keeps the value and gains a
  documented floor. It is written down as a guess because it is one.
