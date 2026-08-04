"""Errors as RFC 7807 Problem Details.

Every error this API returns has had the same `{code, message}` shape since F0, which was
already better than most — one shape, always parseable. RFC 7807 is that discipline with a
registry behind it: a `type` URI a client can switch on and a human can follow, `title` and
`status` for display, `detail` for the specific occurrence, and `instance` for the request
it happened on.

The gain is not tidiness. It is that a generated client gets **one** error model for the
whole surface, and that `type` is a stable identifier which survives the message being
reworded — which the messages here are, often, because they are written for the person
reading them.

**Backwards compatibility is deliberate**: `code` is retained alongside `type`. Every client
written against this API reads `code`, including the one in this repository, and an error
contract is the last thing that should break silently.
"""

from typing import Any

from fastapi.responses import JSONResponse

#: Base for the `type` URIs. Not resolvable today, and that is allowed — RFC 7807 §4.2 is
#: explicit that a `type` need not dereference. It is a stable identifier first; a
#: documentation URL later, when there is documentation to point at.
NAMESPACE = "https://zenith.enterprise/problems"

MEDIA_TYPE = "application/problem+json"

#: The human-readable summary per error code. Separate from `detail`, which describes the
#: single occurrence: RFC 7807 §3.1 requires `title` to stay the same for a given `type`,
#: so that a client may group by it and a UI may translate it.
TITLES: dict[str, str] = {
    "not_found": "Resource not found",
    "authentication_failed": "Authentication failed",
    "permission_denied": "Permission denied",
    "rate_limited": "Too many requests",
    "conflict": "Conflict with the current state",
    "limit_exceeded": "Limit exceeded",
    "invalid_cursor": "Invalid pagination cursor",
    "unsupported_file": "Unsupported file type",
    "generation_unavailable": "No language model available",
    "encryption_unavailable": "Encryption key unavailable",
    "missing_tenant_context": "Internal error",
    "internal_error": "Internal error",
}


def problem(
    status: int,
    code: str,
    detail: str,
    instance: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    """One error, in the one shape.

    `extra` carries members specific to a problem type — RFC 7807 §3.2 permits them, and it
    is where a machine-readable `retry_after` or a field name belongs rather than being
    smuggled into prose the client would have to parse.
    """
    body: dict[str, Any] = {
        "type": f"{NAMESPACE}/{code}",
        "title": TITLES.get(code, "Error"),
        "status": status,
        "detail": detail,
        # Retained deliberately. Every client written against this API reads `code`, and an
        # error contract is the last thing that should break silently. It is also the
        # single token worth logging and grepping for.
        "code": code,
        # Kept because the whole product was built against it and removing it would be a
        # breaking change dressed as a standards upgrade.
        "message": detail,
        **extra,
    }
    if instance:
        body["instance"] = instance

    return JSONResponse(
        status_code=status,
        content=body,
        media_type=MEDIA_TYPE,
        headers=headers,
    )
