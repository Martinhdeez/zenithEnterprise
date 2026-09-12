# Open-source release

What has to be true before this repository can be made public, in the order it has to
become true. Written after a full pre-publication sweep of the tracked tree, the git
history and the dependency closure.

Decisions already taken by the author, and the rest of this plan assumes them:

- **Ownership**: personal project. No employer, no client, no academic submission holds
  exploitation rights, so no written permission is a precondition.
- **Licence**: **Apache-2.0** for the project's own code.
- **History**: rewritten rather than discarded. The 513 commits are part of what the
  repository is worth; the handful of facts that must not ship come out of them.

---

## 1. What the sweep found

**Clean.** No credential, key or token has ever been committed. The history was searched
for `sk-`, `gh[pousr]_`, JWT-shaped strings, 40–64 character hex and base64 keys: every hit
is a test fixture (`sk-liveUpstreamKeyABCDEF`), a corpus SHA-256 in `eval/corpus.toml`, or
an npm `integrity` hash. `.env` was never tracked. No customer document was ever committed —
the largest blobs in the whole history are lockfiles and eval reports. `git count-objects`
reports 648 KiB packed.

**Not clean**, in four groups:

| Group | Where | Scale |
|---|---|---|
| A real host, its user and its domain | `.artifacts/specs/2026-08-04-ops-runbook.md`, `docs/deployment.md`, `docker/docker-compose.prod.yml`, `.claude/skills/run-app/SKILL.md` | `operator@host.internal`, `/home/operator`, `zenith.example.com`, `/path/to/...`, OVH/Coolify/Tailscale topology, `/opt/other-project/.env` — a third party's path |
| A named prospect | `.artifacts/todo/2026-08-21-lexical-scalability-bm25.md:373`, `scripts/demo-check.sh:1522`, three commit messages | 2 tracked lines, 3 commits |
| Sales material, not software | `.artifacts/specs/2026-08-26-what-this-demo-claims.md`, `.artifacts/todo/2026-08-21-demo-runbook.md`, `.artifacts/specs/cheatsheet.md`, pricing in `2026-07-27-technical-decisions.md` | 3 files whole, ~8 files partly |
| Figures measured on one private installation, published as properties of the software | `docs/deployment.md`, `docs/adr/0009`, `docs/partitioning-*.md`, ~10 `.artifacts` files | Recall@8 90.0%, 26 documents, 8,273 passages, 13,549 passages, 21,295 chunks |

The fourth group is the one that is easy to get wrong. The numbers are not secret; they are
simply **not reproducible by a reader**, and a repository that states them as product
properties is making a claim it cannot support. Two options per figure: requalify it in
place ("measured on one installation of N documents; not a benchmark") or drop it.

Two further facts, neither blocking:

- `.claude/skills/run-app/SKILL.md` publishes working literal role passwords
  (`ALTER ROLE zenith_app LOGIN PASSWORD 'zenith_app'`). They are development defaults, but
  they must ship as placeholders.
- Four tracked files are wholly in Spanish, against the rule `CONTRIBUTING.md` states for
  the whole repository. Three of them are also sales material and are deleted anyway.

---

## 2. Licence: why Apache-2.0

| | MIT | Apache-2.0 | AGPL-3.0 |
|---|---|---|---|
| Permission to use, modify, sell | yes | yes | yes, if the user's own source is released |
| Express patent grant | no | **yes** | yes |
| Patent retaliation (a suer loses the licence) | no | **yes** | yes |
| Requires the contributor to state changes | no | yes | yes |
| Trademark reservation | no | **yes** | no |
| A company's legal review | trivial | trivial | slow, often refused |
| Protects against a SaaS fork | no | no | **yes** |

**Apache-2.0**, because the risk an individual author actually carries is not that somebody
sells the code — it is that somebody uses it and then claims a patent over what it does.
MIT is silent on patents; Apache-2.0 grants them explicitly and withdraws the grant from
anyone who sues. It is also the licence a hiring engineer expects to see and the one a
company's counsel clears without a conversation. AGPL would protect against a closed SaaS
fork, which is not a risk this project faces, at the cost of the audience it does want.

Apache-2.0 does **not** require per-file headers. A `LICENSE` file, a `NOTICE` file and one
line in the README are the whole obligation.

### Third-party licences this ships against

| Component | Licence | What it means here |
|---|---|---|
| ParadeDB / `pg_search` (Docker image) | **AGPL-3.0** | A separate process, reached over the wire and referenced by image tag; the repository distributes no part of it. Zenith's own Apache-2.0 is unaffected. Anyone redistributing a bundled image has ParadeDB's obligations, not ours. Say so in NOTICE. |
| `text-embeddings-inference` | **HFOIL 1.0** — not an OSI licence | Permits internal and commercial use. Forbids offering TEI itself as a hosted paid service. A reader who plans to sell Zenith as SaaS must read it. Must be called out; it is the only non-open component in the stack. |
| `BAAI/bge-m3` | MIT | No obligation. |
| `BAAI/bge-reranker-v2-m3` | Apache-2.0 | No obligation. |
| `psycopg`, `psycopg-binary`, `psycopg-pool`, `psycopg2-binary` | LGPL-3.0 | Imported, not modified, and replaceable. Compatible with an Apache-2.0 application. Note it; change nothing. |
| `certifi` | MPL-2.0 | File-level copyleft on an unmodified dependency. Nothing to do. |
| npm closure | 682 MIT, 25 ISC, 12 MPL-2.0, 10 Apache-2.0, 13 BSD, 5 OFL-1.1, rest permissive | The five OFL fonts are `@fontsource` packages, not vendored files. Nothing to do. |

`scripts/check-licences.sh` already fails the build on GPL/AGPL/proprietary **Python**
dependencies. It has no npm equivalent. Adding one is optional and is not a release blocker.

---

## 3. The work, in order

### Phase 1 — delete what is not software

```
.artifacts/specs/2026-08-04-ops-runbook.md            # real host, real leak, log-wiping procedure
.artifacts/todo/2026-08-21-demo-runbook.md            # 420 lines of sales choreography
.artifacts/specs/2026-08-26-what-this-demo-claims.md  # what may be said to a buyer
.artifacts/specs/cheatsheet.md                        # investor pitch, Spanish
.artifacts/pages/2026-08-30-chat-retrieving-indicator.html  # design scratch, Spanish
```

Per the folder convention nothing is deleted, it is moved — but that convention governs a
private working tree. These five leave the repository.

### Phase 2 — redact the working tree

1. `docs/deployment.md` — rewrite against a generic host. No `/home/operator`, no OVH, no
   Coolify, no Tailscale, no `example.com`, no "28 containers already on it", no dated
   outage narrative, no backup timestamp. Keep every instruction that is true of any host.
2. `docker/docker-compose.prod.yml` — the Traefik `Host()` rules become
   `${ZENITH_DOMAIN:?set ZENITH_DOMAIN}`.
3. `.claude/skills/run-app/SKILL.md` — placeholders for both role passwords; `/path/to/...`
   becomes a variable.
4. `scripts/demo-check.sh:1522` and `.artifacts/todo/2026-08-21-lexical-scalability-bm25.md:373`
   — the two remaining mentions of the prospect.
5. `scripts/schedule-backup.sh`, `scripts/demo-check.sh` — incident narratives about one
   installation become statements about the failure mode.
6. Pricing and go-to-market paragraphs out of `2026-07-27-technical-decisions.md`,
   `2026-07-27-mvp.md`, `2026-07-27-iteration-plan.md`, `project-state.md`,
   `2026-08-25-aspects-to-improve-codex.md`.
7. Every figure in group four either requalified in place or dropped. `docs/adr/0009` and
   `docs/partitioning-*.md` keep their reasoning; what changes is that the corpus behind the
   numbers is described as one installation rather than as the product.
8. `CLAUDE.md:98` — the sentence about what decides an enterprise sale.

### Phase 3 — rewrite the history

`git-filter-repo` is not installed (`brew install git-filter-repo`). Then, on a **clone**,
never on the working repository:

- `--replace-text` with a rules file covering `host.internal`, `operator`, `example.com`,
  `/path/to`, `other-project`, `the client`.
- `--message-callback` for the three commit messages that name the prospect
  (`8950c95`, `a507055`, `f58a8f6`).
- Optionally `--path-rm` for the five files in Phase 1, so they are absent from every commit
  rather than only from the tip.
- Author identity: 519 commits carry `m.hernandezg@udc.es`. Publishing that is a choice, not
  a leak. If it should become a GitHub no-reply address, `--mailmap` does it in the same
  pass — decide before running, because a second rewrite is a second force push.

Then verify against the rewritten clone before anything is pushed:

```bash
git log --all -p | grep -nEi "$(cat redaction-terms.txt | paste -sd'|' -)"
git log --all --format='%s%n%b' | grep -nEi "$(cat redaction-terms.txt | paste -sd'|' -)"
```

Both must print nothing. The force push needs the author's explicit word; it rewrites every
commit id, and the eleven remote branches have to go or be rewritten with it.

### Phase 4 — what a public repository has to carry

| File | Status | Content |
|---|---|---|
| `LICENSE` | **missing** | Apache-2.0 verbatim, `Copyright 2026 Martín Hernández González` |
| `NOTICE` | **missing** | Attribution plus the third-party table from §2 |
| `README.md` | **missing — the repository has never had one** | What it is, the five invariants, install in ten lines, what is measured and what is not, licence |
| `SECURITY.md` | missing | Where to report; that this is not operated as a service |
| `CONTRIBUTING.md` | exists, clean | Add a licence-of-contributions line (inbound = outbound) |
| `.env.example` | exists, clean | No change; it already ships no secret |
| `.github/workflows/ci.yml` | exists, clean | Runs on any fork as-is |

The README is the release's main risk and its main opportunity. It is the file that decides
whether a reader believes the rest. It must not quote a single figure that the reader cannot
reproduce — which today means it quotes none of the corpus numbers, because
`backend/eval/live-recall.json` describes a corpus that no longer exists. Either regenerate a
recall figure against the public `eval/corpus.toml` set, or state the mechanism and skip the
number. The mechanism is publishable; the installation's numbers are not.

### Phase 5 — flip and check

1. `gh repo edit --visibility public`.
2. Re-run the greps of Phase 3 against the public clone.
3. Confirm GitHub's licence detection shows Apache-2.0 (it reads `LICENSE`).
4. The 30 merged pull requests become public with the repository. Their titles are technical
   and were checked; their bodies were not. Read them before the flip.
5. The live instance at the redacted domain keeps running. Nothing in the repository points
   at it any more, but its secrets were written when the repository was private: rotate
   `ZENITH_JWT_SECRET` and both role passwords at the flip, on the principle that a secret
   whose blast radius changed should be replaced.

---

## 4. What this is not

Not a promise that the project is maintained, and the README should not imply one. It is a
single-author repository that reasons in public about isolation, retrieval and measurement.
The value on offer to a reader is the reasoning — the ADRs, the retractions, the measurements
that contradicted the plan — and that value survives publication only if every number in it
is either reproducible or labelled as not being so.
