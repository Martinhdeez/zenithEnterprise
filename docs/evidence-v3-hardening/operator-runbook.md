# Hardening operator notes

Keep the existing [v3 operator runbook](../evidence-v3/operator-runbook.md) as the source for product controls and authenticated acceptance commands. Jev remains off by default. The shared quota and breaker are process-local; use the existing single-worker topology guard. Changing a credential requires the documented restart and a new budget/account review. Do not enable multiworker through an acknowledgement flag.

For local no-egress checks, from `backend/` run `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, and `uv run pytest` with a disposable application-role PostgreSQL test container. From `frontend/`, run `npm ci`, `npm run lint`, `npm run test`, `npm run build`, and `npm audit --omit=dev`. `make check` combines repository gates. On this Windows host use `PYTHONUTF8=1`, a disposable `ZENITH_JWT_SECRET`, and `PYTHONPATH=scripts/windows_selector_bootstrap` for the backend suite.

The Linux qualification used an archive of the exact candidate under ignored `.scratch/evidence-v3-hardening/linux-backend-candidate/`, WSL Ubuntu 24.04 with Docker socket access, and task-local `uv` 0.10.10. Its venv and cache were outside the Windows checkout. From the archived `backend/`, with `uv` and the venv `bin` directory on `PATH`, the equivalent commands are:

```bash
export UV_PROJECT_ENVIRONMENT=/tmp/zenith-v3-linux-venv
export UV_CACHE_DIR=/tmp/zenith-v3-uv-cache
uv sync --group dev
ruff check .
ruff format --check .
pyright --pythonpath "$UV_PROJECT_ENVIRONMENT/bin/python"
PYTHONUTF8=1 ZENITH_JWT_SECRET=local-test-only-placeholder-secret-1234567890 python -m pytest
```

The explicit Pyright Python path is needed when the venv lives outside the archive; pytest fixtures also invoke `uv` as a subprocess. The Windows selector bootstrap must not be applied to Linux.

The three live pilots are one-shot, bounded diagnostic artifacts. Their tracked scripts are `backend/eval/hardening_live_pilot.py`, `backend/eval/score_historical_reprobe.py`, and `backend/eval/jev_options_pilot.py`; their executed manifests/ledgers and raw Score bodies are local under ignored `.scratch/evidence-v3-hardening/`. Do not rerun a ledger: a reserved call with uncertain result may already have been billed. The scripts disable automatic retries so a future separately authorized manifest can account for each dispatch. Never place keys, private text, raw captures, browser credentials, or unreviewed corpora in Git.

For fallback triage, inspect requested versus actual judge provider, `degraded` and reason fields, complete assessment status, and source recheck. Ranking may fall back to a whole TEI ordering. A strict-support assessor failure must withhold the unassessed draft. Capture is opt-in and limited to approved public/synthetic Score diagnostics; no production raw logging is enabled.
