# F8 — Generation and citations, and the one rule that makes them worth anything

Retrieval is finished enough to build on: Recall@8 95%, equal to Recall@50. The passage the
answer needs is in front of the model. Everything in this feature exists to make sure the
answer it writes is **traceable back to that passage, or not shown at all**.

The gate is in `mvp.md` §5.6 and it is the only one with a value of zero:

| Metric | Gate |
|---|---|
| Fabrication with a false citation | **0%** |

A gate of zero cannot be met by a prompt. Prompts are advice; an 8B model takes advice
about as well as an intern on their second day. So the rule here is **enforced in code
after the model has spoken**, and the model's output is treated as a claim to be checked
rather than a result to be rendered.

---

## 1. The shape of it

```
question ─► SearchService ─► hits[1..n]  ─► prompt ─► connector ─► raw answer
                 │                                                     │
                 │                                              bind citations
                 │                                                     │
                 └──────────────► queries + query_citations ◄──────────┘
                                    (real scores, all four phases)
```

Four new modules under `features/generation/`, one new router under `features/query/`.
Nothing in `retrieval/` changes behaviour — it changes what it *reports*, which is §4.

## 2. The connector is pluggable, and the folder is not named after a vendor

`mvp.md` §5.1 settled this in advance: the package is `generation/` with
`adapters/openai_compatible.py` inside it. A folder called `openai/` starts lying the day a
customer plugs in Bedrock, and it drags vendor names into code that should not know any.

```python
class Connector(Protocol):
    async def complete(self, system: str, user: str) -> Completion: ...
```

`Completion` carries `text` and `model`. That is the whole interface. It is deliberately
narrower than any vendor's API: everything the SDKs offer beyond this — tools, JSON mode,
logprobs — is a thing one vendor has and another does not, and the moment the interface
carries it, "pluggable" stops being true.

**Resolution order**, per request:

1. The tenant's `llm_config` row, read inside `tenant_session` — its RLS policy is
   `tenant_id = zenith_current_tenant()`, so no bypass is needed and the bypass surface
   stays at four routes.
2. Environment defaults (`ZENITH_LLM_*`) when the tenant has not configured one. This is
   the development and CI path, and it is what §5.4 fixed to **Llama 3.1 8B Instruct via
   Ollama** — the floor, not the ceiling. A citation format that survives an 8B model
   survives anything a customer plugs in; the reverse is how you ship prompts that only
   work against GPT-4o.
3. Neither: a 503 naming `llm_config.manage`. Not an abstention — abstention means *the
   corpus does not answer this*, and saying that when nobody configured a model would be a
   lie about the documents.

### The API key, and the promise `config.py` made

`ZENITH_ENCRYPTION_KEY` has carried this comment since F0:

> it gets the same guard as `jwt_secret` **when the generation connector is built**, and
> refusing to start over a feature that does not exist would be theatre.

The connector is now built, so the guard arrives — but **not as a startup requirement**.
The key protects one thing: a remote provider's API key at rest. An installation running
the local Ollama baseline stores no key, and refusing to boot such an installation would
be the same theatre in a different costume.

What ships instead:

- If `ZENITH_ENCRYPTION_KEY` is set, it must be a valid Fernet key. A malformed one fails
  at settings construction, exactly like `jwt_secret`.
- If it is unset and a tenant tries to **store** a key, that operation fails with the
  generation instructions. The failure lands on the person configuring the connector, who
  is the only person who can act on it.

## 3. Citation binding: the model proposes, the code disposes

The prompt numbers the passages `[1]`…`[n]` and asks for a marker after every claim. Then:

| The model wrote | What ships |
|---|---|
| `[3]`, and 3 is in the shortlist | a citation bound to that chunk id, page, and boxes |
| `[9]`, with 6 passages | **stripped from the text**, counted, logged as fabrication |
| no valid citation at all | the answer is **discarded** and replaced by the abstention |
| the abstention sentence | passed through, with the documents consulted |

The third row is the strict one and it is deliberate. An uncited answer may well be
correct — and there is no way to tell it apart from an invented one without reading the
corpus, which is what the user came here to avoid doing. Shipping it would mean the 0% gate
measures nothing, because the fabrications would simply arrive without markers.

Citations are bound to **chunk ids from the shortlist that was actually sent**, never to
anything looked up afterwards. That is what makes the isolation argument short: the model
only ever sees passages RLS already released, and a citation cannot name a passage the
model did not see.

## 4. Real scores, because `query_citations` has four columns and F6 filled none of them

`query_citations` was designed with `score_bm25`, `score_vector`, `score_rrf` and
`score_rerank` so that six months from now someone can answer *why did this query return
garbage*. Retrieval currently reports **ranks**, not scores — a deliberate F6 choice for
the API response, where two incomparable scales would imply they were comparable.

For the log, ranks are not enough. A rank tells you a passage came third; a score tells you
whether third was a strong third or the least bad of a bad set. So:

- `lexical()` and `dense()` now return `(chunk_id, score)` — `ts_rank_cd` and cosine
  **similarity** (`1 - distance`, so bigger is better in both columns).
- `Hit` gains `lexical_score`, `dense_score`, `rerank_score`, alongside the ranks it
  already carried. The response keeps both: ranks for the UI, scores for the debugger.
- `score_bm25` holds the `ts_rank_cd` value. The column is misnamed for what F6 shipped —
  `pg_search`'s BM25 is still a candidate for the lexical half — and renaming a column
  across a migration to fix a word is not worth it. Written down here instead.

Only cited chunks get a row, and `rank` is the **retrieval** rank, not the order of
citation in the prose. The scores in that row describe how the passage was retrieved, so
the rank in the same row has to mean the same thing.

## 5. What "done" means

1. `make check` green.
2. A question over a seeded corpus returns an answer whose every citation resolves to a
   chunk that was in the shortlist.
3. A model that cites `[9]` out of six passages produces an answer with no `[9]` in it.
4. A model that cites nothing abstains.
5. `queries` and `query_citations` carry the four scores, and RLS keeps them tenant-scoped
   — asserted from another tenant's session.
6. No new RLS bypass. The grep for `owner_session` returns the same four routes.

## 6. Recorded as out of scope

- **Streaming.** §2 shows the query endpoint streaming. `Completion` is a single response
  today; the citation binder needs the whole text before it can strip an invalid marker,
  so streaming means shipping tokens the validator has not seen yet. That is a design
  problem, not a plumbing one, and it belongs with the frontend that consumes it.
- **The RNF-06 certification suite** and the "test connection" button (§2.11). The
  connector has to exist before something can certify it.
- **Rate limits** (§2.12: 30/min per user, 120/min per tenant). Generation is the
  expensive endpoint and the limits are M4's, enforced in middleware over Postgres.
- **Answer-quality measurement.** F8 builds the path; the numbers — and with them the
  Docling evidence M0 and F7 both deferred — are F9's.
