# Contributing

## Language

**Everything in this repository is in English, without exception**: identifiers, comments, docstrings, error messages, commit messages, branch names, documentation and CI text. See `.artifacts/specs/2026-07-27-mvp.md` §5.2 for the reasoning.

---

## Branching model

GitHub Flow. `main` is always green and always deployable; work happens on short-lived branches.

| Purpose | Branch name |
|---|---|
| Feature | `feat/f1-tenancy` |
| Bug fix | `fix/rls-label-recursion` |
| Tooling, deps, config | `chore/upgrade-checkout-action` |
| Documentation only | `docs/iteration-2-scope` |

**Branches live days, not weeks.** This is not a style preference here: two branches each adding an Alembic migration with the same `down_revision` produce two heads, and Alembic refuses to run until one is rewritten by hand. Keeping one feature in flight at a time avoids the problem entirely.

Git Flow (`develop`, `release/*`, `hotfix/*`) is deliberately **not** used. It exists to support several released versions in parallel, which this product does not have.

---

## Definition of done

A feature is not finished until all six hold:

1. A plan note exists in `.artifacts/in-progress/` — model, endpoints, business rules, what gets tested.
2. Implementation passes `pyright` in strict mode.
3. Tests live inside the feature (`app/features/<name>/tests/`). Tests that cross features go in `tests/integration/`.
4. `make check` is green locally.
5. An Alembic migration exists if the schema changed, with a `downgrade` that actually works.
6. Commits follow the convention below, and the plan note has moved to `.artifacts/to-test/`.

### `make check`

```
make check     # lint + format-check + types + tests + licenses
```

It mirrors the CI job exactly. A green `check` locally means a green pipeline — so run it before pushing, not after CI tells you.

---

## Commits

[Conventional Commits](https://www.conventionalcommits.org). The scope is the feature name.

```
feat(auth): role-based permission checks
fix(ingestion): retry no longer duplicates chunks
test(labels): cross-label leak coverage
chore(docker): pin ParadeDB image
docs(specs): record the physical-delete decision
ci: run licence check on pull requests
refactor(retrieval): extract RRF fusion
```

**The body explains why, not what.** The diff already says what changed. The body should say what would otherwise have to be rediscovered: the constraint that forced the design, the alternative that was rejected, the failure mode being avoided. Anything surprising in the diff needs a sentence.

Commits are not squashed on merge, so each one has to stand on its own.

---

## Pull requests

One PR per feature. CI must pass — `main` is protected and the `backend` check is required.

**Merge with "Rebase and merge".** Squash and merge commits are disabled at the repository level: squashing would collapse a feature's commits into one and destroy the reasoning they carry. Rebasing keeps every commit and keeps history linear.

Branches are deleted automatically after merge.

---

## Migrations

- One migration per feature, from `0002` onwards. The full initial schema lives in `0001` on purpose — foreign keys and RLS policies cross features, and a half-built schema cannot be tested for leaks.
- Every migration needs a working `downgrade`. If a deployment fails, that is the only way back short of restoring a backup.
- `tests/integration/test_migrations.py` verifies that the models and the database do not drift. If you change a model without generating a migration, that test fails.

---

## Testing

- **Real Postgres, never SQLite.** SQLite has no pgvector, no RLS and no `tsvector`; testing against it would be testing a different product.
- Integration tests run as `zenith_app`, the role the application uses in production. Querying as the owner proves nothing: the owner bypasses the RLS policies.
- The two leak tests (cross-tenant and cross-label) are MVP acceptance gates. They do not get skipped, and they do not get weakened to make a change pass.
