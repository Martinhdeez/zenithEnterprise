# Iteration Plan — Zenith Enterprise

**Date:** 2026-07-27
**Status:** Iteration 1 (MVP) converged and detailed in its own document. Iterations 2-4 sketched.
**Related documents:** `2026-07-27-mvp.md`, `2026-07-27-technical-decisions.md`

> **Consolidation note.** The first version of this plan included a separate **iteration 0**, a throwaway spike up front. It has been removed: the Zenith project from HackUDC 2026 already validated the idea and the stack, and the retrieval-quality measurement that justified the spike has been folded into the MVP as its first milestone (M0), with a permanent harness instead of disposable code. What used to be "iteration 0 + iteration 1" is now **iteration 1: the MVP**.

---

## 1. Working framework

### 1.1 Why not a monolithic V-model

The classic V-model assumes requirements are fully known before design, and that validation happens at the end. Zenith Enterprise does not meet that premise, because its dominant risks are not specification risks but **empirical** ones: the quality of semantic retrieval over real corporate documents cannot be predicted on paper, and citation accuracy depends on the chunking strategy, which is only validated by measuring.

### 1.2 Adopted framework: incremental V

The rigour of the V-model is kept, applied **per iteration** rather than to the whole product. Each iteration walks its own thin V:

```
Iteration requirements     <-->     Acceptance criteria
        │                                  ▲
        ▼                                  │
   Design                    <-->     Integration tests
        │                                  ▲
        └──────────► [ CODE ] ─────────────┘
```

Practical consequences:

- The **system SRS** is written in full. It is the project's stable contract.
- **Detailed design** happens per iteration. Nothing is designed in detail if it is not touched until iteration 3.
- Each iteration declares **which requirement IDs it closes**. That traceability delivers most of the V-model's value at a fraction of its cost.

**Accepted cost:** we give up being able to claim the system is fully specified before starting. If a customer appears who demands strict V documentation (defence, healthcare), that narrative will have to be reconstructed from the ID traceability. It is doable, but it is extra work.

### 1.3 Ordering principle

> Each iteration retires the biggest remaining risk, not the flashiest features.

### 1.4 Operational management

- One artifact per iteration, moving through the pipeline: `todo/` → `in-progress/` → `to-test/` → `tested/` → `shipped/`.
- This document lives in `specs/` and does not move through the pipeline.

### 1.5 How acceptance criteria are written

An acceptance criterion needs five elements. Missing any of them, it is not a criterion: it is a wish.

1. **What is measured** — the exact metric.
2. **On what** — which dataset and how many cases.
3. **What threshold** — the number.
4. **How it is measured** — a reproducible procedure, same result if someone else runs it.
5. **What happens if it fails** — the consequence.

#### Gates and measurements

A central distinction in this project. Putting a threshold on something never measured is fake rigour: either it is met by accident, or the threshold ends up lowered until it fits.

- **Gate** — a hard threshold, justifiable *before* measuring. If it is not met, the iteration does not close. There should be few of them.
- **Measurement** — the number is recorded with no threshold. It becomes the **baseline** the next iteration is judged against.

**The MVP's M0 milestone has few gates and many measurements, and that is correct**: its entire purpose is to generate the baselines that make legitimate thresholds possible afterwards.

#### Where a legitimate number comes from

There are only three valid sources:

1. **Business consequence** — a wrong citation destroys customer trust, so the threshold is high. A slow answer is annoying but tolerated, so it is looser.
2. **Architectural constraint** — if 8 chunks are sent to the LLM, anything outside those 8 is unrecoverable. The threshold derives from the pipeline's structure.
3. **Measured baseline** — you measure, and the next threshold is "do not get worse".

If a number cannot be justified by one of the three, write **"measure in M0, set the threshold afterwards"**. That is better than inventing it.

---

## 2. AI architecture: what is fixed and what is pluggable

A structural decision that shapes the whole plan.

### 2.1 Fixed local AI — zero marginal cost and total privacy

All processing and indexing always runs locally. These are mechanical tasks requiring no reasoning:

- **Embedding generation** — documents never leave the server.
- **Audio transcription** — Whisper, locally.
- **Re-ranking** — the precision filter over search results.

### 2.2 Free connector (model-agnostic) — final wording only

Only the generation of the final answer is interchangeable. The customer plugs in whatever their policy allows: a local open-source model, or an enterprise cloud API.

### 2.3 Consequences

**On cost (RNF-02):** variable cost scales with **queries**, not with document volume. Indexing 100,000 documents costs the same in API spend as indexing 100: zero. That is what makes RNF-02 sustainable.

**On quality risk:** in RAG, answer quality is dominated by **retrieval**, not by generation. If the retriever hands over the wrong chunks, no model fixes it — it produces a fluent, confident, false answer. The components pinned to local (embeddings + re-ranking) therefore carry the most weight in correctness **and have no escape hatch**.

**On the reversibility of the embedding model.** Changing embedding model invalidates every existing vector: the vector spaces of two different models are not comparable. Without planning ahead, that turns the embedder choice into a one-way door.

> **Adopted mitigation.** A **hot reindexing pipeline** is built from day one (see technical decisions §4): the schema allows several vector spaces to coexist, the new one is generated in the background while the old one keeps serving queries, and the switchover is a reversible change of state.
>
> The effect is strategic: **getting the model right first time stops being mandatory.** Changing embedder costs GPU hours instead of a rewrite and a downtime window. The same applies to the chunking strategy.

**On the connector:** being pluggable is not enough. Citation faithfulness (RF-03.2) depends on the model respecting attribution instructions, and that varies enormously between models. A **certification suite** is required, which every model must pass before counting as supported (RNF-06).

---

## 3. Settled decisions

| Decision | Value |
|---|---|
| Deployment model | Hybrid (SaaS + on-premise option) |
| Hybrid strategy | Architecture ready for both from day 1, but **only one mode deployed** until iteration 3 |
| Development and demo environment | **Our own VPS**, Docker Compose. This is where the product is built and shown |
| Deployment at design partners | **TBD — pending a commercial conversation.** Not fixed until there is a yes |
| Design partners | **TBD.** Candidates sounded out, none confirmed |
| MVP language | **English** |
| Embedding model | Multilingual despite the English start, to avoid reindexing when Spanish opens up |
| Embedder reversibility | Hot reindexing pipeline from day 1 |
| Code starting point | **New repository.** The hackathon project is consulted as reference, not inherited (see `mvp.md` §1.3) |
| Evaluation corpus | Mixed: ~40 real public corporate documents + ~10 synthetic with adversarial cases |
| Latency commitment | Only over the phase we control (retrieval). Generation depends on the model the customer plugs in (see §8) |
| SRS scope | Complete (RF-01 to RF-04, RNF-01 to RNF-07) |
| Technical stack | See `2026-07-27-technical-decisions.md` |

---

## 4. Requirements added during convergence

**RNF-05 — Ingestion idempotency and resilience.**
Every ingestion job must be retryable without duplicate effects. If processing a large file fails partway, the retry must not produce duplicate chunks or inconsistent state.
*Satisfied by the Postgres-backed queue: queue state rolls back in the same transaction as the chunk inserts.*

**RNF-06 — Certification of pluggable models.**
No generation model counts as supported until it passes a suite measuring source faithfulness, citation accuracy and abstention rate on questions the corpus cannot answer.

**RNF-07 — Data governance in testing.**
Using third-party cloud APIs to generate evaluation or test datasets from real customer documents is strictly prohibited. All testing with customer documents runs exclusively on the local stack.
*Frontier cloud models may be used on our own or public development corpora.*

**RNF-08 — Vector space reversibility.**
The system must be able to generate a new vector space in the background, evaluate it against the harness, and switch to it without service interruption, keeping the previous one available for rollback.

**Pending decisions:** data lifecycle beyond cascading delete, query audit as an exposed feature, feedback on answers. See §9.

---

## 5. Iterations

### Iteration 1 — MVP

**Estimated duration:** ~5-6 months
**Requirements closed:** RF-01.1, RF-03.1, RF-03.2, RF-03.3, RF-04.1, **RF-04.2**, RNF-04, RNF-05, RNF-06, RNF-07, RNF-08

**Full specification in `2026-07-27-mvp.md`.** Summary:

A product installable at 2-3 design partners, not sellable over the web. English PDF documents, natural-language querying, answers with verifiable page-level citations, multi-tenancy with isolation guaranteed by Row-Level Security, RBAC with configurable roles and label-based document scope, and a configurable LLM connector with certification.

> **RF-04.2 is pulled forward from iteration 4.** A design partner uploads their real corpus from day one, and it contains payroll, board minutes and personnel files. A system where every employee sees everything does not survive the start of the pilot. The original plan assumed a pilot with a narrow document set, and that is not realistic. Cost: 2-3 weeks.

Six milestones: M0 pipeline and evaluation harness · M1 production ingestion · M2 querying and citations · M3 multi-tenancy and authentication · M4 administration and operation · M5 packaging.

**Critical gate:** M0 ends with the four retrieval-quality gates. If they do not pass, no UI gets built on top.

**Out of scope:** audio, Spanish, automatic categorisation, facets, per-document permissions, folder hierarchies, SSO, external connectors, conversational memory.

---

### Iteration 2 — Audio and meetings

**Estimated duration:** to be estimated after the MVP
**Requirements closed:** RF-01.2, extension of RF-03.2 to the audio medium

**Scope**

- Ingestion of voice files and meeting recordings.
- Transcription with **word-level timestamps** — indispensable, because segment-level timestamps drift by several seconds and make the citation useless.
- Indexing of the transcribed text.

**Known risks**

- Transcription quality with several speakers and overlapping voices.
- Computational cost of transcription against RNF-02.
- Computational cost of diarisation, which adds to transcription.

**Diarisation — approved as a requirement.** Identifying who is speaking (`pyannote.audio`) is no longer optional: in corporate meeting minutes, "who said what" is exactly what gets asked. It enters the iteration's scope from now.

**Acceptance criteria**

- [ ] A meeting recording is transcribed and becomes queryable.
- [ ] An audio-based answer cites the exact second, verifiable by playing the recording.
- [ ] Contributions are attributed to distinguishable speakers.
- [ ] A long transcription that is interrupted resumes without duplicating content.

---

### Iteration 3 — Organisation and exploration

**Estimated duration:** to be estimated
**Requirements closed:** RF-02.1, RF-02.2, RF-02.3

Mostly conventional work, and considerably easier once the real data from earlier iterations is known.

**Scope**

- Automatic metadata extraction.
- Virtual groupings by customer, date and department.
- Combinable faceted filtering.
- **Ingestion connectors** (SharePoint, network drives, Drive) — the first predictable request from any real customer.
- **Soft delete with scheduled purge** (`deleted_at` + 30 days). The MVP accepts physical delete as a conscious risk (mvp.md §2.7); here the extra filter is designed alongside the rest of document management instead of being bolted onto read paths already protected by RLS.

**Acceptance criteria**

- [ ] A newly uploaded file is classified without manual intervention.
- [ ] The user combines at least three filters and gets coherent results.
- [ ] Navigation stays usable over a large test corpus.

---

### Iteration 4 — Permissions, scale and hardening

**Estimated duration:** to be estimated
**Requirements closed:** RNF-03 and the compliance requirements in §9

**Scope**

- Scale validation up to the RNF-03 target.
- Query audit exposed as a feature (the structured log already exists from the MVP).
- SSO (SAML/OIDC), predictably demanded by the first mid-sized customer.
- Data lifecycle beyond cascading delete: retention and scheduled purge.

*RF-04.2 is no longer here: it was pulled forward into the MVP.*

**Acceptance criteria**

- [ ] The system holds the latency target over a corpus the size of RNF-03.
- [ ] An administrator can audit what each user queried and against which documents.

---

## 6. Requirement traceability

| Requirement | Iteration | Note |
|---|---|---|
| RF-01.1 Documents | 1 | PDF only, English only |
| RF-01.2 Audio | 2 | |
| RF-02.1 Auto-categorisation | 3 | |
| RF-02.2 Dynamic views | 3 | |
| RF-02.3 Faceted navigation | 3 | |
| RF-03.1 Semantic search | 1 | Validated in M0 |
| RF-03.2 Traceability and sources | 1 (docs), 2 (audio) | Validated in M0 |
| RF-03.3 Deduplication | 1 | |
| RF-04.1 Tenant isolation | 1 | Guaranteed by RLS, not by application code |
| RF-04.2 Role-based permissions | 1 | **Pulled forward from 4.** Configurable RBAC + access labels, also via RLS |
| RNF-01 Privacy and sovereignty | Cross-cutting | Guaranteed by design in indexing |
| RNF-02 Economic efficiency | 1 (validation), cross-cutting | |
| RNF-03 Scalability | 4 | |
| RNF-04 Model-agnostic | 1 | |
| RNF-05 Ingestion idempotency | 1 | |
| RNF-06 Model certification | 1 | |
| RNF-07 Testing governance | 1 | In force from day one |
| RNF-08 Vector reversibility | 1 | Schema from the first migration |

---

## 7. Recommendation on hybrid deployment

Hybrid mode is the most expensive to sustain. Therefore:

1. The architecture allows it from day 1, avoiding coupling to any single cloud provider's proprietary services. RNF-04 and the choice of Postgres push in the same direction.
2. **Only one mode is deployed and maintained** until iteration 3. Sustaining two production deployment paths from the start multiplies operations work without adding product validation.

---

## 8. Latency commitment

The latency target is split in two, because **it is impossible to commit to the response time of a model the customer plugs in**.

| Phase | Who controls it | Criterion |
|---|---|---|
| Retrieval + RRF fusion + re-ranking | **Us** | **Gate: p95 < 500 ms** |
| Generation (to first token) | The customer's model | Reference: p95 < 3 s with the reference model |
| End to end | Mixed | Reference: p95 < 4 s to first token |

Measurement rules:

- **Always percentiles, never averages.** The average hides exactly the cases that drive users away. The p95 is the user who is leaving.
- **With the full corpus loaded.** Measuring latency with 50 documents indexed says nothing about behaviour with 100,000. In M0 latency is a measurement; the real scale gate arrives in iteration 4 with RNF-03.

This split also has commercial value: we commit contractually to the phase we control and document the rest as dependent on the model the customer chooses.

---

## 9. Open questions

**Decided** (kept as a record):

- ~~Audio in the MVP~~ → **out**, enters iteration 2.
- ~~Diarisation~~ → **approved** as an iteration 2 requirement.
- ~~Fixed or configurable roles~~ → **configurable RBAC with document scope**, inside the MVP.

**Open.** None of these blocks the start.

1. **Design partners and deployment mode at the customer** — **TBD, pending a commercial conversation.** Not fixed in the document until there is an explicit yes. In the meantime, development runs on **our own VPS** with Docker Compose, which is the same artifact that would be installed at a customer. The M0 corpus uses public equivalents, depending on no specific partner.
2. **Support role** — does it exist, and does it see customer content or only state and logs? Recommendation: never content, and in writing. With no partner confirmed it is not blocking, but the permission catalogue in mvp.md §2.1 closes in M3.
3. **Outstanding compliance requirements**: retention and scheduled purge, exposed audit, feedback on answers. Needed before iteration 4.
4. **Duration estimates** for iterations 2 to 4.

> **Note on the corpus and RNF-07.** If the design partner is identified, the M0 corpus must **resemble their documentation in type and structure**, using public equivalents. Using their real documents to generate the synthetic question set with a cloud model would violate RNF-07.
