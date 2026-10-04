"""Opt-in real Keycloak fixture: public credentials, PKCE and current Zenith authority.

Run in the disposable Keycloak container's network namespace. This is not an intranet
deployment or a replacement for an operator's realm/client/account provisioning.
"""

import asyncio
import base64
import hashlib
import os
import secrets
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text

from app.core.database import owner_session
from app.features.mcp.network import (
    Binding,
    KeycloakConfiguration,
    KeycloakVerifier,
    create_application,
)
from conftest import Account

ISSUER = "http://127.0.0.1:8080/realms/zenith-lf-public"
RESOURCE = "http://127.0.0.1:19080/mcp"
REDIRECT = "http://127.0.0.1:19082/callback"
CLIENT = "zenith-public-client"
SUBJECT = "11111111-1111-4111-8111-111111111111"
pytestmark = pytest.mark.skipif(
    not os.environ.get("ZENITH_TEST_PUBLIC_KEYCLOAK"), reason="opt-in disposable public Keycloak"
)


class LoginForm(HTMLParser):
    action: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "form" and fields.get("id") in {"kc-form-login", "kc-update-profile-form"}:
            self.action = fields.get("action")


async def grant(client: httpx.AsyncClient, *, wrong_verifier: bool = False) -> httpx.Response:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(24)
    response = await client.get(
        ISSUER + "/protocol/openid-connect/auth",
        params={
            "client_id": CLIENT,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "openid zenith:read",
            "resource": RESOURCE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
        },
    )
    assert response.status_code == 200
    form = LoginForm()
    form.feed(response.text)
    assert form.action is not None
    assert urlsplit(form.action).netloc == "127.0.0.1:8080"
    # HTTPX lacks browsers' Secure-cookie loopback exception. Forward only the disposable
    # realm's synthetic cookies to its asserted loopback origin. Production requires TLS.
    cookies = "; ".join(
        cookie.name + "=" + cookie.value
        for cookie in client.cookies.jar
        if cookie.domain == "127.0.0.1"
        and cookie.path == "/realms/zenith-lf-public/"
        and cookie.value is not None
    )
    login = await client.post(
        form.action,
        headers={"Cookie": cookies},
        data={"username": "public-fixture", "password": "public-fixture-password"},
    )
    assert login.status_code == 302, "public fixture login must redirect, without printing tokens"
    if urlsplit(login.headers["location"]).path.endswith("/required-action"):
        # Seeded fixtures from before Keycloak's required profile fields need one local
        # completion. This synthetic user's profile is never used for Zenith linking.
        profile_url = login.headers["location"]
        assert urlsplit(profile_url).netloc == "127.0.0.1:8080"
        profile = await client.get(profile_url, headers={"Cookie": cookies})
        form = LoginForm()
        form.feed(profile.text)
        assert form.action is not None and urlsplit(form.action).netloc == "127.0.0.1:8080"
        login = await client.post(
            form.action,
            headers={"Cookie": cookies},
            data={"firstName": "Public", "lastName": "Fixture", "email": "public@example.invalid"},
        )
        assert login.status_code == 302
    callback = urlsplit(login.headers["location"])
    assert callback.scheme + "://" + callback.netloc + callback.path == REDIRECT
    parameters = parse_qs(callback.query)
    assert parameters["state"] == [state] and parameters["iss"] == [ISSUER]
    return await client.post(
        ISSUER + "/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT,
            "redirect_uri": REDIRECT,
            "code": parameters["code"][0],
            "resource": RESOURCE,
            "code_verifier": secrets.token_urlsafe(48) if wrong_verifier else verifier,
        },
    )


async def test_real_pkce_tool_permission_and_provider_revocation(
    account: Account,
    labelled_document: UUID,
) -> None:
    cfg = KeycloakConfiguration(
        ISSUER,
        RESOURCE,
        "zenith-introspection-fixture",
        "public-fixture-introspection-secret",
        {SUBJECT: Binding(account.admin_id, account.tenant_id, 0)},
    )
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=15) as browser:
        discovery = await browser.get(ISSUER + "/.well-known/openid-configuration")
        assert discovery.status_code == 200 and discovery.json()["issuer"] == ISSUER
        assert "S256" in discovery.json()["code_challenge_methods_supported"]
        refused = await grant(browser, wrong_verifier=True)
        assert refused.status_code == 400 and "access_token" not in refused.json()
        granted = await grant(browser)
        assert granted.status_code == 200
        tokens = granted.json()
        bearer = tokens["access_token"]
        verifier = KeycloakVerifier(cfg)
        verified = await verifier.verify_token(bearer)
        assert (
            verified is not None and verified.subject == SUBJECT and verified.resource == RESOURCE
        )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 19080))
        listener.listen(128)
        listener.setblocking(False)
        app = create_application(cfg)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        worker = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(90):
                while not server.started:
                    if worker.done():
                        await worker
                        pytest.fail("MCP listener failed")
                    await asyncio.sleep(0.01)
                async with (
                    httpx2.AsyncClient(headers={"Authorization": "Bearer " + bearer}) as http,
                    Client(streamable_http_client(RESOURCE, http_client=http)) as connected,
                ):
                    result = await connected.call_tool(
                        "zenith_get_document", {"document_id": str(labelled_document)}
                    )
                    assert not result.is_error and result.structured_content is not None
                    assert result.structured_content["id"] == str(labelled_document)
                    async with owner_session() as session:
                        await session.execute(
                            text("UPDATE users SET token_version=token_version+1 WHERE id=:u"),
                            {"u": account.admin_id},
                        )
                    denied = await connected.call_tool(
                        "zenith_get_document", {"document_id": str(labelled_document)}
                    )
                    assert denied.is_error and denied.structured_content is None
                logout = await browser.post(
                    ISSUER + "/protocol/openid-connect/logout",
                    data={"client_id": CLIENT, "refresh_token": tokens["refresh_token"]},
                )
                assert logout.status_code == 204
                assert await verifier.verify_token(bearer) is None
                rejected = await browser.post(
                    RESOURCE, headers={"Authorization": "Bearer " + bearer}, json={}
                )
                assert rejected.status_code == 401
        finally:
            server.should_exit = True
            async with asyncio.timeout(10):
                await worker
            listener.close()
