# The Spanish demo corpus

**Date:** 2026-08-31 · **Status:** in-progress

The installation is being emptied and rebuilt in Spanish for a demo. This file is the
agreement the seeding, the demo script and the isolation proof all read from; if one of them
disagrees with this file, one of the two is wrong and it is worth finding out which.

## What is being thrown away, and what was kept

`./scripts/backup.sh ~/zenith-backups` was run first: **290 tables, 43 document rows, 42 PDF
files, 83 MB of database and 55 MB of documents**, at `2026-08-31T11-34-06Z`. The backup
reported one row without a file, which is `runbook.md` — a `.md`, so not counted by a check
that counts `*.pdf`. Benign, and pre-existing.

The wipe is `down -v`, so it destroys the volume and re-runs all 27 migrations from zero.
That is deliberate beyond tidiness: **it is the only thing that exercises the migration chain
against an empty cluster**, which is the state a customer installs into and the state nothing
in CI covers.

Two steps are easy to forget after a fresh volume and both are silent-ish failures:

1. `zenith install-queue` — Procrastinate's tables are outside our migrations. Without it,
   uploads return 201 and stay `pending` for ever.
2. `ALTER ROLE zenith_app LOGIN PASSWORD` **and** `zenith_platform`. Without the first the
   API refuses to start, which is the correct outcome and is `verify_rls_active()` doing its
   job.

## The two organisations

| Tenant | Why it exists |
|---|---|
| **the client** | the demo. Everything is driven here. |
| **Grupo Ardena** | exists only to be a second tenant. Four documents, one user, never logged into during the demo — its whole purpose is that the same question returns nothing from it. |

## The labels, and why these

A label is a permission, not a tag. So the taxonomy is chosen for what it lets a person
*demonstrate*, not for how tidily it files.

| Label | Docs | What it demonstrates |
|---|---|---|
| *(none — `General`, the default)* | ~20 | the tenant-wide floor: the big codes, readable by everyone |
| `Normativa académica` | ~10 | the buyer asked for university requirements; this is that |
| `Empleo y personas` | ~8 | |
| `Contratación y subvenciones` | ~5 | |
| `Protección de datos y digital` | ~7 | |
| `Fiscal y presupuestario` | ~5 | |
| `Dirección — confidencial` | 3 | **the compartment.** See below. |

`General` is already Spanish and keeps its name. The quarantine label ships as
`Unclassified` and is renamed **`Sin clasificar`** — safe, because access turns on the
`is_quarantine` flag and the name is explicitly a display string.

### The trap in this taxonomy, written down before it is fallen into

**A role created after the tenant does not reach `General`.** `seed_default_label` grants it
to the *system* roles only, and the comment there says so on purpose. So every custom role
below must be granted `General` explicitly, or the demo opens on a user who can see none of
the twenty documents everyone is supposed to see, and it will look like the product is broken
rather than like the seeding is.

### Roles

| Role | Reaches |
|---|---|
| `admin` (system) | everything, including `Dirección — confidencial` |
| `Servicios jurídicos` | `General`, Normativa académica, Contratación, Protección de datos, Fiscal |
| `Secretaría académica` | `General`, Normativa académica |

Three users, one per role, so the demo can sign in as each.

## The three confidential documents, and why they are invented

The other forty-odd are real Spanish law from the BOE. **A public law cannot demonstrate a
confidential compartment**, because nothing about it is confidential and the audience knows
it. So `Dirección — confidencial` holds three short synthetic internal documents — an acta
del consejo de dirección, an informe de auditoría interna, a convenio — each visibly marked
as demonstration material.

They carry the load of two demonstrations at once:

- **Cross-label isolation.** `secretaria@` asks what the consejo approved and gets an
  abstention; `direccion@` asks the same question and gets an answer with a citation. Same
  installation, same question, same corpus, two answers. This is the second MVP acceptance
  gate, and until now **it had no live evidence on this installation** — the three labels
  that existed and were not reachable all held zero documents.
- **Abstention.** They contain specific figures that appear nowhere else, so a question about
  one is answerable from exactly one passage or not at all.

## What is measured before this is called ready

- `alembic current == head`, and `zenith diagnose` clean.
- Every document `ready`. Not `pending`, which is the `install-queue` failure, and not
  `embedding`, which is just unfinished.
- A real question answered with a citation, in Spanish.
- The isolation pair above, run as both users.
- `demo-check` green, with the label-isolation warning **gone** — it has been a standing
  warning precisely because no document lived in an unreachable compartment.

## What the rebuild actually cost, including the parts I got wrong

**`down -v` destroys `hf_cache`, not just `db_data`.** I reasoned about the database volume
and forgot the model cache shares the same `-v`. So `tei-embed` came back empty and spent
several minutes re-downloading bge-m3's ONNX weights from HuggingFace — and because I began
uploading immediately, the worker consumed five documents against an embedder that was not
there and marked them `failed`:

    EmbeddingServiceError: the embedding service at http://tei-embed:80 did not respond

No data was lost and `zenith reingest --status failed --apply` put all five back. But the
lesson is the one worth keeping: **stop the worker, or wait on `tei-embed`'s health, before
uploading into a stack that has just been rebuilt.** Nothing warns you; the uploads answer
201 and fail a minute later.

**Ingestion is slow here for a reason that is not the product.** TEI publishes no arm64 CPU
image — `docker manifest inspect` on `cpu-1.8` returns amd64 only — so the embedder runs
under emulation on this machine at roughly 130 chunks a minute. That is a property of the
laptop, not of the installation, and it does not affect a single search once the corpus is
in.

**The quarantine label cannot be renamed through the API**, which contradicts its own model:
`AccessLabel.is_quarantine`'s docstring says "the name is a display string an administrator
may rename", and `PATCH /labels/{id}` answers 409, *"'Unclassified' is where unfiled uploads
wait to be classified and cannot be renamed."* The router is what runs, so it wins. It was
renamed to `Sin clasificar` in SQL, which is safe for precisely the reason the docstring
gives — every access decision reads the flag, never the string — and was done only because a
demo billed as Spanish should not show an English word in its label list. **The two should be
made to agree**; that is a real inconsistency, not a papered-over one.

**Four BOE identifiers I guessed for the second tenant were all wrong**, one of them off by a
single digit (`BOE-A-1999-21568` where the law is `-21567`), and two silently resolved to
laws the client already held. I had told the harvesting agent not to guess identifiers; I then
guessed four. Every id in both tenants is now confirmed against `act.php` before download.

## A gap in the recovery story, found while rebuilding this

`zenith reingest` is the one tool that exists to put stranded documents back in the queue,
and **it cannot see the documents most likely to need it.** `find_stranded` excludes anything
holding a `todo` or `doing` job — sound for `todo`, wrong for `doing`: a worker killed
mid-document leaves its job in `doing` for ever, and the document is then invisible to the
repair tool while never being retried. Two documents ended up in exactly that state during
this rebuild, from a `docker compose up -d worker` that replaced a worker holding jobs. They
had to be repaired with `procrastinate_retry_job_v1` by hand.

The command's own docstring names two paths that strand a document — a failed enqueue and a
relabel after enqueue — and says "this is the fix for both". A third path exists and is not
covered.

**The safe way to restart a worker is `docker compose stop -t 1800 worker`**, which lets
procrastinate drain. `up -d` and `restart` both kill in flight work and produce the invisible
state above.

## The classifier refiled the corpus, and that is the most interesting thing that happened

Sixteen documents were uploaded with no label, intending them to be tenant-wide. **They were
not tenant-wide when the dust settled.** An upload naming no compartment lands in quarantine
and the model files it, which is the product working exactly as designed — and it filed most
of them well: three more into `Normativa académica`, several into `Empleo y personas`.

**It also filed `ley-50-1997-del-gobierno.pdf` — a public law, published in the BOE — into
`Dirección — confidencial`.** Harmless to access control and fatal to the demonstration: the
whole point of that compartment is that it holds three invented internal documents, and a
public statute sitting beside them makes the story false the moment anyone opens it. Moved
back to `General` with `PUT /documents/{id}/labels`.

The lesson generalises past this demo. **A seeding script that leaves the label field empty is
not choosing "visible to everyone" — it is delegating the choice to a model.** If a document's
compartment matters, name it at upload; the classifier is for documents nobody has filed, not
for documents somebody has decided about.

## Measured, on the finished installation

| | |
|---|---|
| documents | **59** — 55 in Empresa Demo, 4 in Grupo Ardena |
| pages | **4,739** |
| chunks | **20,476** |
| retrieval | **~0.9 s** |
| generation | **~2.7 s**, `gemini-3.1-flash-lite`, `degraded=false` |
| reach | 55 / 42 / 20 documents for direccion / juridico / secretaria |

| Compartment | Docs |
|---|---|
| Normativa académica | 13 |
| Protección de datos y digital | 11 |
| Empleo y personas | 11 |
| Fiscal y presupuestario | 8 |
| General | 7 |
| Contratación y subvenciones | 6 |
| **Dirección — confidencial** | **3** |

`verify.py` passes **11 of 11**, `demo-check` reports **Ready with 2 warnings** — OCR disabled
by the hardware profile, and the label-isolation check, which cannot be satisfied by an
administrator because an administrator reaches every label. Run as `secretaria@`, the same
check reports the evidence in full:

> *secretaria@empresademo.es is refused a document filed under Fiscal y presupuestario, which
> none of its labels open — row and bytes both 404, no passage of it back from /search, and
> /query refuses to be scoped to it.*

**That closes the second MVP acceptance gate, which had no live evidence on this installation
before today.** It also exposes a tension inside `demo-check` worth naming: its corpus-reach
check wants the demonstrated account to see everything, and its label-isolation check needs
that account to be refused something. **No single account satisfies both**, so the tool cannot
report green on both halves in one run, and the warning it prints for an administrator reads
like a defect in the installation rather than a property of the account it was given.
