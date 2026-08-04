# F18 — BM25 attempted and rejected, history privacy moved into Postgres, invitations shipped

Three items. One of them failed, and the failure is the most valuable result in this
milestone because it is now impossible to repeat by accident.

---

## 1. BM25 via `pg_search`: attempted, measured, reverted

ADR 0002 recorded BM25 as the principled successor to F15's identifier query. It was built
— extension, `bm25` index on `chunks`, `lexical()` rewritten around `@@@` and
`paradedb.score()` — and it works, in the sense that mattered:

| Case | `ts_rank_cd` | **BM25** |
|---|---|---|
| `Catalog Number 10000W` | rank 52 (outside the candidate set) | **rank 1** |
| `citation 23 U.S.C. 101` | not found | **rank 40** |

Both land inside `CANDIDATES = 50` **with no custom identifier query at all**. IDF is
exactly the missing property, and BM25 supplies it systematically.

### Why it was reverted: it cannot score under row-level security

`paradedb.score(id)` returns a value only when ParadeDB's custom scan executes. With the RLS
policies on `chunks` in force, the planner never chooses it:

```
Bitmap Heap Scan on chunks
  Filter: (id @@@ '{"with_index":...}'::paradedb.searchqueryinput)
    Bitmap Index Scan on ix_chunks_tenant_id
    Bitmap Index Scan on ix_chunks_label_ids
```

The tenant and label indexes drive the scan and `@@@` is applied as an ordinary **filter**.
Rows come back correctly isolated — this was never a security problem — but **every score is
`NULL`**.

Ranking by a NULL score ranks everything equally. Search keeps answering, answers worse, and
nothing indicates it: the silent degradation this project refuses everywhere else.

Coaxing the planner with `enable_bitmapscan = off` is not an answer. Correctness would then
depend on a plan choice, and when the plan changes the failure is the silent one above.

**This is the first measured cost of ADR 0001.** RLS-first is not free. The isolation
guarantee is worth more than the ranker, so the ranker went. Revisiting BM25 needs either a
`pg_search` whose custom scan composes with RLS quals, or a design where the lexical index is
queried without policies to satisfy — and the second means a new bypass route, which ADR 0001
permits for a security guarantee and never for ergonomics.

Recorded in ADR 0002 with the plan output, so the next person to have this idea finds the
experiment already run.

**Retrieval is unchanged:** extraction 100%, context ceiling **96.8%**, identifier questions
100%. F15's identifier query stands as the shipped answer — narrower, and working inside the
architecture rather than against it.

## 2. Query history privacy is now enforced by Postgres

F15 shipped a `WHERE` clause in Python and flagged it as the one place application code did
security work. Migration 0005 closes it.

`zenith.user_id` and `zenith.reads_all_history` join the RLS context, and the policy becomes:

```sql
tenant_id = zenith_current_tenant()
AND (zenith_reads_all_history() OR user_id = zenith_current_user_id())
```

**`reads_all_history` is a boolean the application binds from the permission catalogue**, not
a permission string parsed in SQL. The catalogue already lives in one place; asking Postgres
to re-derive authority would put it in two that can disagree.

**It fails closed.** An unset `zenith.user_id` is `NULL`, and `user_id = NULL` is never true —
a session that forgets to bind the caller sees *nothing*, which is the same direction every
other policy here fails in. Asserted.

Three consequences fell out, and each is a test:

- A raw `SELECT * FROM queries`, written by somebody who never read `history.py`, now returns
  nothing for another user's rows. That is the difference between a rule and a convention.
- `query_citations` needed **no change**: its policy is `EXISTS (SELECT 1 FROM queries …)`,
  which applies the parent's policy in turn. Migration 0001 wrote derived policies that way
  on purpose, and this is the first time it has paid off.
- The policy has `WITH CHECK` as well as `USING`, so `AnswerService` must bind the author when
  it writes a query row. The insert was refused until it did — the policy working exactly as
  intended, since a row nobody could subsequently read is not a row worth writing.

## 3. `POST /users/invite`

`users.invite` has been in the catalogue since F2 and enforced nowhere, because the only way
to create a user was `zenith create-user` on the customer's server.

**No email is sent, and the password is returned exactly once.** This product ships into
networks that frequently have no outbound SMTP; requiring a mail server to add a colleague
would make the feature undeployable precisely where the product is sold. The API generates
the password, returns it in the response, and never stores or logs it in the clear — the
same decision the CLI made, for the same reason.

The cost is written down rather than glossed: **a password passed through a chat message is
a password in a chat log.** The proper answer is an invitation token and a set-password
page, which needs a public unauthenticated route and a token table. That is a milestone, not
a line.

Two details that are security decisions rather than ergonomics:

- Roles are validated **before** the user is created, so a bad request cannot leave a user
  with no roles and an administrator unsure whether the invitation half-worked.
- A duplicate address is a conflict *within the tenant only*. `users` is unique per
  `(tenant_id, email)` precisely so two customers may employ the same person, and an error
  distinguishing "taken here" from "taken elsewhere" would leak one customer's staff list to
  another.

The admin UI shows the password in a panel that says plainly it cannot be retrieved, with an
"I have copied it" acknowledgement — because an administrator who closes it without copying
has to invite again, and that is only cheap if they know.

## 4. Still open

- **`cross-platform-obligations`** — the last question outside the 96.8% ceiling, and a
  cross-document question rather than a retrieval-mechanics one.
- **BM25**, blocked on §1 rather than on effort.
- **Invitation tokens** with a set-password page, replacing the password-in-response.
- **The RNF-06 certification suite** behind §2.11's "test connection" button.
