# Standalone upstream extraction: local-mcp-agents

Base: 33b48812c95150348c52d2519159780252c92db2.
Fork source: e49fd78e225c4176d4e439b502fa6d5d61f85127.

Extract nine bounded read tools, stdio and optional Keycloak intranet HTTP transports,
capabilities resource, cited-answer prompt and trusted host REST upload client.
Keep fresh per-user authorization, RLS, source coordinates, revocation rechecks and
bounded/cancellable operations; no global admin key, filesystem tool or agent platform.
Reuse upstream local SearchService directly without the fork-only judge_mode parameter.
Add optional MCP SDK 2.2.0 plus dev tests; regenerate lock from upstream dependencies.
Historical 6.89x batch-source improvement is integrated-fork evidence, not a network SLA.

No dependency on the other new slices, the v10 foundation or Jev.
Preserve existing PRs and branches; publication is authorized, upstream merge is not.
Run focused host checks and full independent fork CI; upstream CI may need maintainer
approval. Do not restart Docker/WSL or claim the historical benchmark was rerun here.

Ruff lint/format and strict target Pyright passed. Host reference/identity transport checks and independent full Linux CI are recorded separately.

## 2026-10-04 portable uploader test follow-up

The psycopg-compatible Windows Selector loop cannot create asyncio subprocesses.
Use standard-library Popen in asyncio.to_thread and communicate(timeout=30);
keep actual loopback TCP/CLI upload, deduplication, byte SHA, secret-output and
401/403 assertions, with kill/wait cleanup on failure. No MCP runtime change.
The integrated-fork fresh real-PostgreSQL replay passed 1/1 in 110.91 seconds.
Both full fork PR and main CI passed 1096 backend cases after this correction.
This is an additive follow-up to PR #22; preserve its previous commit and rerun
its own complete CI. Do not rewrite, merge or close upstream history.
