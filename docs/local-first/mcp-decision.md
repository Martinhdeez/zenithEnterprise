# First MCP client, transport and identity decision

Extraction provenance: this report preserves the measured integrated-fork experiment.
The standalone upstream slice starts from `33b48812c95150348c52d2519159780252c92db2`;
its independent checks are reported in the PR. Historical benchmark and quality
figures have not been rerun on this extraction. Original implementation and
evaluation harness: [frozen fork source](https://github.com/Kripta-Studios/zenithEnterprise/tree/e49fd78e225c4176d4e439b502fa6d5d61f85127).

Status: local-first direction accepted on 2026-10-01. After “continue, do it all,” an
optional same-machine stdio server/reference host was implemented and tested on independent
branch `feat/local-mcp-stdio` at `5f590bb3f1ca24e4e20dafb2ee995636ab25f7eb`.
Follow-up: the full local MCP backend suite completed with 911 passed / 12 skipped in
2,143.39 seconds. A separately invoked Keycloak resource server is implemented on
feat/mcp-intranet-auth. Exact checks are in followup.md. It binds loopback, requires
explicit identity enrollment and preserves current Zenith authority. Production DNS,
TLS, issuer and secret provisioning remain deployment inputs.
The final focused Linux checks passed 29 with one opt-in model skip in 203.96 seconds;
actual stdio and owner-role rejection are included. Complete-suite scope and retained
failures are explicit in the continuation report.

Zenith's optional MCP server exposes its existing retrieval, sources and document status.
It will not become a general agent host or client of arbitrary servers. The first consumer
must run locally with a local model, as selected by the user. The implemented consumer is
a fixed official SDK reference client, using already cached Ollama llama3.2:3b Q4_K_M.
Manifest/tag SHA-256 is a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72;
all six declared blobs were hash/size verified (2,019,393,189 bytes). One public ready-source
proof answered “Treinta días. [1]” and finished normally in 3.203 s (test 18.57 s including
setup). The previously proposed cached Qwen forced a thinking template and exceeded the
output budget; the completion gate correctly withholds that result. These observations
select a working local reference, not the best Spanish model. No weights were downloaded.
The owned Ollama proof server was stopped. Simultaneous model residency and a full
interactive desktop host/network binary-transfer path remain unqualified.
The final guarded commit repeated that answer with valid citations: one test passed in
141.75 s including infrastructure; generation/reauthorization took 18.469 s under concurrent
checks. Both raw results are retained, with no latency-distribution claim.

The tested same-machine transport is stdio with explicit per-user application credentials
and current AccessProfile resolution, without a network auth service. Actual SDK calls,
cancellation and a subprocess handshake were tested. The local host must not send tool results,
telemetry or traces containing sources to a cloud model. A local server alone cannot enforce
the consumer's downstream processing destination.

For a later intranet deployment, propose self-hosted Keycloak rather than building OAuth
inside Zenith. No existing organizational IdP was identified; the user delegated the choice.
Keycloak's OIDC discovery, signing keys, authorization-code flow, introspection and revocation
make it suitable for an on-premises identity boundary. Configure authorization code plus PKCE,
pre-registered clients, resource audience and minimal scopes. An operator must confirm issuer,
principal mapping, deployment ownership and token/revocation policy before production use.

Zenith's current HS256 web tokens contain sub, tid, ver, typ, iat and exp. They do not declare
MCP resource audience or OAuth scopes, and web login/refresh is not a complete MCP OAuth
authorization server. Refresh checks token_version; existing access sessions can survive until
expiry. The new stdio adapter additionally checks the current user's token version on each
operation. Its stronger local revocation check does not change the existing REST window.

For HTTP MCP, use protected Streamable HTTP with TLS at the deployment boundary, Origin
checks, protected resource metadata and audience-validated expiring tokens. Pin an SDK version
that actually supports the selected client protocol. Official Python SDK mcp==2.2.0 is
pinned in an optional extra and the dev group; actual SDK discovery negotiated
2026-07-28. Current local tests cover expiry/type/version rejection, isolation and cancellation.
Web JWTs have no MCP resource audience. The optional HTTP branch uses Keycloak tokens and
tests issuer, resource audience, scope, expiry, bearer type and revocation independently.

Direct MCP service calls must explicitly check operation permissions and refresh AccessProfile.
REST dependency checks do not run automatically outside the router. Use zenith_app for customer
content; never expose queue owner credentials or treat a tenant argument as authority. Recheck
source access after search and after label revocation. Return bounded source IDs/coordinates;
avoid raw filesystem paths, arbitrary URLs, SQL, commands or corpus-export tools.

Binary uploads remain authenticated multipart POST /documents. The trusted host streams a
user-selected file with explicit labels; its helper polls permitted document status. MCP is the
control plane, not a base64 PDF channel. The reference uploader was tested against real
authorized REST handlers and disposable DB: new uploads return 201, duplicates 200, and
revoked upload permission returns 403. Follow-up TCP tests cover the selected-file CLI and
original-file hash through a real loopback listener. Both
acknowledgments precede readiness and duplicates can change labels. Interactive desktop
binary-upload compatibility remains untested; the existing web uploader remains available.

The tested model host uses loopback only, disables cloud, does not follow redirects or use
environment proxies, never pulls weights, and bounds sources/context/output. It binds current
source IDs/coordinates and rechecks cited text/access after generation. This validates reference
identity, not semantic entailment. No general agent, filesystem/URL tool or corpus export exists.
Exact final checks and failed attempts are in [continuation.md](https://github.com/Kripta-Studios/zenithEnterprise/blob/e49fd78e225c4176d4e439b502fa6d5d61f85127/docs/local-first/continuation.md) and its JSON.

Sources checked on 2026-10-01:
- https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports
- https://github.com/modelcontextprotocol/python-sdk
- https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0
- https://www.keycloak.org/securing-apps/oidc-layers
- https://ollama.com/library/qwen3:4b
- upstream backend/app/core/security.py, auth/service.py, auth/access/dependencies.py,
  documents/router.py and retrieval/service.py at the recorded base.
