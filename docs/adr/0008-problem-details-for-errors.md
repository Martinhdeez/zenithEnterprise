# ADR 0008 — Errors are RFC 7807 Problem Details

**Status:** Accepted

## Context

Every error this API returns has had the same `{code, message}` shape since F0 — already
better than most, because there is exactly one shape and it always parses. What it lacked
was a *registry*: a stable identifier a client can switch on, separate from a message
written for a human and reworded whenever the wording improves.

## Decision

Every error response is `application/problem+json` with `type`, `title`, `status`, `detail`
and `instance`.

- **`type`** is `https://zenith.enterprise/problems/{code}`. Not resolvable today, which
  RFC 7807 §4.2 explicitly permits — it is a stable identifier first and a documentation
  URL when there is documentation to point at.
- **`title`** is fixed per type, so clients may group by it and interfaces may translate it.
  **`detail`** describes the single occurrence.
- **Extension members** carry machine-readable specifics: `retry_after` on a 429, alongside
  the header. RFC 7807 §3.2 exists for exactly this, and a client that already parsed the
  body should not have to reach back into headers.

**`code` and `message` are retained.** Every client written against this API reads them,
including the one in this repository. Adopting a standard is not a licence to break the
callers who trusted the previous contract, and an error shape is the last thing that should
change silently.

## Consequences

- One error model for the entire surface in a generated client.
- The frontend reads `detail ?? message`, so it works against this server and any older one
  still deployed — which on an on-premise product is a real situation, not a hypothetical.
- A 404 rather than a 403 for another tenant's resources remains the rule; the standard
  changes the envelope, not the decision about what to disclose.

## What this audit found while doing it

F14's commit message and PR both claimed `Retry-After` was forwarded by the error handler.
**It never was.** The edit ran from the wrong working directory, wrote nothing, and the
throttle tests asserted on the raised exception rather than on the response — so nothing
failed and the claim went unchallenged into `main`.

It is implemented now, and `test_a_rate_limit_carries_retry_after_in_both_places` asserts
the *response*. The lesson is narrower than "test more": a test that asserts on the object
a handler receives has not tested the handler.
