"""Optional Keycloak-protected MCP resource server; no authorization server is built here."""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import (
    RequestBodyLimitMiddleware,
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from pydantic import AnyHttpUrl
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.common.exceptions import AuthenticationError
from app.core.database import dispose_engines, tenant_session
from app.features.auth.access.dependencies import requires
from app.features.auth.model import User
from app.features.auth.service import AccessProfile, AuthService
from app.features.mcp.server import create_server
from app.features.mcp.service import LocalReads

SCOPE = "zenith:read"


def token_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("identity version must be a nonnegative integer")
    return value


@dataclass(frozen=True)
class Binding:
    user_id: UUID
    tenant_id: UUID
    token_version: int

    def __post_init__(self) -> None:
        token_version(self.token_version)


def destination(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"})
    ):
        raise ValueError("use a configured HTTPS destination or explicit loopback HTTP")
    return value.rstrip("/")


@dataclass(frozen=True)
class KeycloakConfiguration:
    issuer: str
    resource: str
    client_id: str
    client_secret: str = field(repr=False)
    bindings: dict[str, Binding] = field(repr=False)
    allowed_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if destination(self.issuer) != self.issuer or destination(self.resource) != self.resource:
            raise ValueError("issuer/resource must use exact canonical URLs")
        if urlsplit(self.resource).path != "/mcp":
            raise ValueError("this standalone server's canonical resource must end with /mcp")
        if not self.client_id or not self.client_secret or not self.bindings:
            raise ValueError("explicit introspection credentials and subject bindings are required")
        if any(not subject for subject in self.bindings):
            raise ValueError("invalid identity binding")
        for origin in self.allowed_origins:
            if destination(origin) != origin or urlsplit(origin).path:
                raise ValueError("allowed origins must be explicit canonical origins")

    @classmethod
    def environment(cls) -> "KeycloakConfiguration":
        raw = json.loads(os.environ["ZENITH_MCP_SUBJECT_BINDINGS"])
        bindings = {
            subject: Binding(
                UUID(row["user_id"]), UUID(row["tenant_id"]), token_version(row["token_version"])
            )
            for subject, row in raw.items()
        }
        return cls(
            os.environ["ZENITH_MCP_KEYCLOAK_ISSUER"],
            os.environ["ZENITH_MCP_RESOURCE"],
            os.environ["ZENITH_MCP_INTROSPECTION_CLIENT"],
            os.environ["ZENITH_MCP_INTROSPECTION_SECRET"],
            bindings,
            tuple(json.loads(os.environ.get("ZENITH_MCP_ALLOWED_ORIGINS", "[]"))),
        )


class KeycloakVerifier(TokenVerifier):
    def __init__(
        self,
        configuration: KeycloakConfiguration,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.configuration = configuration
        self.transport = transport

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 8192:
            return None
        cfg = self.configuration
        try:
            # The destination never comes from a JWT, source passage, tool or client argument.
            # Introspection on every request/read honors Keycloak session revocation.
            async with (
                httpx.AsyncClient(
                    timeout=5,
                    trust_env=False,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    "POST",
                    cfg.issuer + "/protocol/openid-connect/token/introspect",
                    auth=httpx.BasicAuth(cfg.client_id, cfg.client_secret),
                    data={"token": token, "token_type_hint": "access_token"},
                ) as response,
            ):
                if response.status_code != 200:
                    return None
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    content.extend(chunk)
                    if len(content) > 65536:
                        return None
                body = json.loads(content)
            expiry = body.get("exp")
            subject = body.get("sub")
            audience = body.get("aud")
            audiences = cast(list[object], audience) if isinstance(audience, list) else [audience]
            scope = body.get("scope")
            client_id = body.get("client_id") or body.get("azp")
            if (
                body.get("active") is not True
                or body.get("iss") != cfg.issuer
                or cfg.resource not in audiences
                or not isinstance(expiry, int)
                or isinstance(expiry, bool)
                or expiry <= time.time()
                or not isinstance(subject, str)
                or subject not in cfg.bindings
                or not isinstance(scope, str)
                or SCOPE not in scope.split()
                or not isinstance(client_id, str)
                or not client_id
                or body.get("token_type", "").casefold() != "bearer"
            ):
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scope.split(),
                expires_at=expiry,
                resource=cfg.resource,
                subject=subject,
                claims={"iss": cfg.issuer},
            )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None


class BoundaryMiddleware:
    """Apply configured transport guards before even sending a token to Keycloak."""

    def __init__(self, app: ASGIApp, security: TransportSecuritySettings) -> None:
        self.app = app
        self.security = TransportSecurityMiddleware(security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            response = await self.security.validate_request(
                Request(scope), is_post=scope["method"] == "POST"
            )
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class NetworkReads(LocalReads):
    def __init__(self, verifier: KeycloakVerifier) -> None:
        super().__init__("")
        self.verifier = verifier

    async def profile(self, permission: str | None = None) -> AccessProfile:
        incoming = get_access_token()
        verified = await self.verifier.verify_token(incoming.token) if incoming else None
        if verified is None or verified.subject is None:
            raise AuthenticationError("invalid credentials")
        binding = self.verifier.configuration.bindings[verified.subject]
        # No local token minting, passthrough, email linking or owner-role content reads.
        profile = await AuthService().profile(binding.user_id, binding.tenant_id)
        async with tenant_session(profile.context) as session:
            version = await session.scalar(
                select(User.token_version).where(User.id == binding.user_id)
            )
        if version is None or version != binding.token_version:
            raise AuthenticationError("invalid credentials")
        if permission is not None:
            await requires(permission)(profile)
        return profile


def create_application(
    configuration: KeycloakConfiguration,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    verifier = KeycloakVerifier(configuration, transport)
    server = create_server(
        "",
        reads=NetworkReads(verifier),
        verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(configuration.issuer),
            resource_server_url=AnyHttpUrl(configuration.resource),
            required_scopes=[SCOPE],
            validate_token_resource=True,
        ),
    )
    parsed = urlsplit(configuration.resource)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[parsed.netloc],
        allowed_origins=list(configuration.allowed_origins) or [origin],
    )
    application = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=65536,
        transport_security=security,
    )
    application.add_middleware(BoundaryMiddleware, security=security)
    application.add_middleware(RequestBodyLimitMiddleware, max_body_size=65536)
    return application


def main() -> None:
    configuration = KeycloakConfiguration.environment()
    # Only a separately configured TLS reverse proxy can expose this listener.
    # No route is mounted in the product API, Vite or nginx by default.
    application = create_application(configuration)

    async def run() -> None:
        try:
            await uvicorn.Server(
                uvicorn.Config(
                    application,
                    host="127.0.0.1",
                    port=int(os.environ.get("ZENITH_MCP_HTTP_PORT", "19080")),
                    access_log=False,
                    log_level="warning",
                    proxy_headers=False,
                )
            ).serve()
        finally:
            await dispose_engines()

    asyncio.run(run())


if __name__ == "__main__":
    main()
