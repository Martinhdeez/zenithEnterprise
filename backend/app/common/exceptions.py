class ZenithError(Exception):
    """Root of all domain errors. Everything the API turns into a response."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(ZenithError):
    status_code = 404
    code = "not_found"


class AuthenticationError(ZenithError):
    """Credentials missing, malformed, expired or wrong.

    Deliberately one error for all of those: telling the caller which one it was is
    what turns a login form into an account-enumeration tool.
    """

    status_code = 401
    code = "authentication_failed"


class PermissionDeniedError(ZenithError):
    status_code = 403
    code = "permission_denied"


class RateLimitedError(ZenithError):
    status_code = 429
    code = "rate_limited"


class ConflictError(ZenithError):
    status_code = 409
    code = "conflict"


class LimitExceededError(ZenithError):
    """Limits from mvp.md 2.12."""

    status_code = 413
    code = "limit_exceeded"


class InvalidCursorError(ZenithError):
    """A pagination cursor this system did not issue, or one that has been altered."""

    status_code = 400
    code = "invalid_cursor"


class UnsupportedFileError(ZenithError):
    """Not a file this system can ingest.

    Judged on the bytes, never on the declared content type: the header is a claim the
    client makes, and F5's parser routing assumes what it is handed is really a PDF.
    """

    status_code = 415
    code = "unsupported_file"


class MissingTenantContextError(ZenithError):
    """A session tried to query without an RLS context set.

    This is a programming error, not a user condition: it means someone opened a
    session without going through `tenant_session`.
    """

    code = "missing_tenant_context"
