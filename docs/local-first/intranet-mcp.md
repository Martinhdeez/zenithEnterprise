# Optional Keycloak resource server

Extraction provenance: this report preserves the measured integrated-fork experiment.
The standalone upstream slice starts from `33b48812c95150348c52d2519159780252c92db2`;
its independent checks are reported in the PR. Historical benchmark and quality
figures have not been rerun on this extraction. Original implementation and
evaluation harness: [frozen fork source](https://github.com/Kripta-Studios/zenithEnterprise/tree/e49fd78e225c4176d4e439b502fa6d5d61f85127).

This branch extends `feat/local-mcp-stdio` at 5f590bb3f1ca24e4e20dafb2ee995636ab25f7eb.
The two independent judge/proxy extractions remain unchanged and do not depend on it.
Local stdio remains the first transport for private documents and a local model.

The optional entry point is `uv run --extra mcp python -m app.features.mcp.network`.
It exposes the existing three bounded read tools through the pinned official MCP SDK,
with protected-resource discovery and stateless Streamable HTTP. It binds 127.0.0.1:19080.
No route is added to the product API, frontend proxy or production Compose files.

Configuration is explicit; credentials belong in the operator's secret environment:

| Environment variable | Meaning |
| --- | --- |
| ZENITH_MCP_KEYCLOAK_ISSUER | Exact HTTPS realm issuer |
| ZENITH_MCP_RESOURCE | Exact public HTTPS resource URL ending in /mcp |
| ZENITH_MCP_INTROSPECTION_CLIENT | Confidential resource-server client ID |
| ZENITH_MCP_INTROSPECTION_SECRET | Its secret, never a user's tool argument |
| ZENITH_MCP_SUBJECT_BINDINGS | JSON: subject to user_id, tenant_id, token_version |
| ZENITH_MCP_ALLOWED_ORIGINS | Optional JSON list of explicit browser origins |
| ZENITH_MCP_HTTP_PORT | Optional loopback listener port, default 19080 |

Use the existing application database configuration with role zenith_app. Startup rejects
owner/platform roles and disabled RLS. Bind `(configured issuer, subject)` to an existing
Zenith user and tenant explicitly. Email, source text, requested tenant IDs and Keycloak
roles do not enroll users or grant Zenith permissions. Provision users through the existing
Zenith administration path. The binding's integer token_version must match the current
user; Zenith sign-out invalidates reads until an operator intentionally updates that binding.

Configure Keycloak authorization code with PKCE S256, a pre-registered public local client,
an exact loopback callback, and minimal scope zenith:read. Disable implicit/password grants
and Full Scope Allowed. Separate audience mappers must include BOTH the canonical MCP
resource URL and the confidential introspection client ID. Keycloak 26.6.2+ requires the
authenticated introspection client in aud. Keep that check enabled. The resource server
also independently checks the canonical resource, issuer, active bearer type, expiry,
linked subject and scope. Include an explicit subject mapper in access and introspection tokens;
missing subjects are rejected. Discovery advertises the configured issuer only.

Introspection occurs on every HTTP request and again before a content read. A fixed issuer
endpoint, no environment proxies/redirects, a five-second timeout and a 64 KiB streaming
response bound constrain provider failures. No token is forwarded to downstream tools.
Current Zenith permissions, labels and token_version are resolved with application-role
RLS. Post-search/post-generation source reauthorization remains in the reused local service.
Transport guards reject wrong Host/Origin and oversized bodies before sending a token to
Keycloak. Access logs are disabled; tokens and provider secrets are excluded from outputs.

For an intranet deployment, place a TLS reverse proxy on the same host in front of the
loopback listener. Preserve the configured public Host, stream /mcp, and expose
/.well-known/oauth-protected-resource/mcp. Restrict the proxy to the intended intranet.
Do not enable trust of arbitrary forwarded headers. A real DNS name, certificate, issuer,
identity enrollment and secret provisioning are deployment inputs, not supplied by this
disposable local proof. Private tool results still require a local consumer/model: a server
cannot prevent an authorized client from subsequently sending its results to a cloud model.

Binary uploads continue through existing authenticated multipart POST /documents. The
selected-file CLI was tested over actual TCP, including deduplication, original-file hash,
missing authentication and current upload-permission revocation. No binary MCP channel,
filesystem browsing tool, new OAuth server or general agent platform was introduced.

## Reproduce the disposable identity proof

`backend/app/features/mcp/tests/fixtures/keycloak-public.json` contains intentionally public
synthetic credentials. It creates only a loopback research realm, never production identity.
Start Keycloak 26.8.0 with that file mounted read-only at
/opt/keycloak/data/import/zenith.json, then `start-dev --import-realm --hostname
http://127.0.0.1:8080 --hostname-strict true`. Publish only 127.0.0.1:19081:8080 and cap it
at two CPUs/768 MiB. Wait for realm discovery rather than assuming a short startup.

Run the optional pytest `app/features/mcp/tests/test_keycloak.py` with
ZENITH_TEST_PUBLIC_KEYCLOAK=1 inside the disposable Keycloak container's network namespace.
The test uses real authorization code/PKCE, rejects a wrong verifier, validates issuer/state
and resource audience, calls an actual SDK client over TCP under real Zenith RLS, checks
Zenith token-version invalidation and then Keycloak logout/introspection/HTTP 401.
HTTPX's lack of a Secure-cookie loopback exception is handled only inside this synthetic
fixture. Production requires HTTPS. Default unit tests do not contact an identity provider.

Sources: [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization),
[official SDK v2.2.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0),
[Keycloak 26.8 migration guide](https://www.keycloak.org/docs/26.8.0/upgrading/).
