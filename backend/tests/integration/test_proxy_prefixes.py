"""Every route the API serves must be reachable through the two proxies in front of it.

This is the third time the same bug shipped, and it is invisible in a way that guarantees a
fourth without this test. A prefix missing from nginx's location regex does **not** 404: the
request falls through to the SPA and is answered with `index.html`, so the browser reports
`Unexpected token '<', "<!doctype "... is not valid JSON` on a screen whose backend is
perfectly healthy. Nothing else catches it — the API tests call the app directly, and the
component tests mock the client — so it reaches a person every time.

`vite.config.ts` had the identical gap on the identical routes, and its own comment records
the same failure happening once before that.

Read as text rather than parsed, deliberately. A regex-aware nginx parser or a TypeScript
evaluator would be a dependency bought to check one line, and the failure this catches is a
missing word, not a malformed grammar.
"""

import re
from pathlib import Path

from app.main import app

ROOT = Path(__file__).resolve().parents[3]
NGINX = ROOT / "docker" / "nginx.frontend.conf"
VITE = ROOT / "frontend" / "vite.config.ts"

#: Served by the SPA itself or by nginx, never proxied.
NOT_PROXIED = {"health", "openapi.json", "docs", "redoc"}


def served_prefixes() -> set[str]:
    """The first path segment of every route the application serves.

    Read from the OpenAPI document rather than from `app.routes`. This version of FastAPI
    keeps included routers as `_IncludedRouter` objects that expand lazily, so walking
    `app.routes` finds four built-in paths and nine opaque wrappers — which is exactly how
    the first version of this test passed while `analytics` was missing from nginx. The
    generated schema is the API's own account of what it serves, and it cannot disagree with
    itself.
    """
    prefixes = {
        segment[0]
        for path in app.openapi()["paths"]
        if (segment := [part for part in path.split("/") if part])
        and not segment[0].startswith("{")
    }
    return prefixes - NOT_PROXIED


def test_nginx_proxies_every_prefix_the_api_serves() -> None:
    """The one that has shipped broken three times: `groups`, `system`, `analytics`."""
    location = re.search(r"location ~ \^/\(([^)]+)\)", NGINX.read_text())
    assert location, "the proxy location block is not where this test expects it"
    proxied = set(location.group(1).split("|"))

    missing = sorted(served_prefixes() - proxied)

    assert not missing, (
        f"{missing} are served by the API and absent from {NGINX.name}. Unmatched paths do "
        f"not 404 — they fall through to the SPA and answer with index.html, which the "
        f"client reports as a JSON parse error on a screen whose backend is fine."
    )


def test_the_dev_proxy_matches_the_production_one() -> None:
    """Same gap, different file, and development is where it is found last: the two configs
    cannot import each other's route table, so they drift silently."""
    proxied = set(re.findall(r'"/([a-z0-9-]+)":\s*"http', VITE.read_text()))

    missing = sorted(served_prefixes() - proxied)

    assert not missing, f"{missing} are served by the API and absent from vite.config.ts"


def test_no_route_repeats_its_router_prefix() -> None:
    """`/auth/auth/credential/{token}` — a real mistake, made by adding a route to a router
    that is already mounted under a prefix and writing the prefix again in the path.

    The proxy tests above cannot catch it: `/auth/auth/...` still starts with `auth`, so
    nginx forwards it happily and the API answers 404 because nothing is registered there.
    The symptom is a screen reporting an invalid link while the database, the function and
    the token are all perfectly correct — which is a long way to walk for a doubled word.
    """
    doubled = [
        path
        for path in app.openapi()["paths"]
        if (segments := [segment for segment in path.split("/") if segment])
        and len(segments) >= 2
        and segments[0] == segments[1]
    ]

    assert not doubled, (
        f"{doubled} repeat their router's prefix. The router is already mounted under it, "
        f"so the path in the decorator must be relative to it."
    )
