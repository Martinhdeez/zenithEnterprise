# Architecture Decision Records

Each record captures one decision that was expensive to make and would be expensive to
reverse — what was chosen, what it cost, and what evidence settled it.

They are written after the fact, from decisions this codebase actually made, and several
record a **measurement that contradicted the original reasoning**. That is the point: an
ADR that only records successes is a marketing document, and the reversals are the ones a
future maintainer most needs to find before repeating the experiment.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-row-level-security.md) | Isolation enforced by Postgres RLS, not by application filters | Accepted |
| [0002](0002-hybrid-retrieval-and-fusion.md) | Hybrid lexical + dense retrieval fused by RRF | Accepted, amended twice |
| [0003](0003-provider-agnostic-llm-adapter.md) | An ABC in the domain layer, not a vendor SDK | Accepted |
| [0004](0004-citations-enforced-in-code.md) | The zero-fabrication gate is enforced after the model speaks | Accepted |
| [0005](0005-hardware-profiles.md) | One profile table; performance may vary, semantics may not | Accepted, floor measured |
| [0006](0006-circuit-breaker-for-optional-components.md) | Optional components degrade visibly, and stop being paid for | Accepted |
| [0007](0007-backend-driven-aggregation.md) | The server computes groupings the client would get wrong | Accepted |
| [0008](0008-problem-details-for-errors.md) | RFC 7807 for every error response | Accepted |
| [0009](0009-partition-by-tenant.md) | `chunks` and `chunk_embeddings` partitioned by tenant, the only lever that changes N | Accepted, stage 0 landed |

## Format

Context, Decision, Consequences — and, where one exists, **Evidence**. A decision made
against a number cites the number.
