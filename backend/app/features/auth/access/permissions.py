"""The permission catalogue (mvp.md 2.1).

Defined by the software, not by the customer: every code here maps to something the
code knows how to check, and a permission nobody enforces would be a lie in the UI.

This module is the readable copy. The authoritative copy lives in migration `0002`,
because a migration must never import application code — it has to keep applying
unchanged after this file is refactored. The duplication is deliberate and guarded:
`test_catalogue_matches_the_database` fails the moment the two drift apart.
"""

from typing import Final

CATALOGUE: Final[dict[str, str]] = {
    "documents.upload": "Upload documents",
    "documents.delete.own": "Delete documents uploaded by oneself",
    "documents.delete.any": "Delete any document in the tenant",
    "query.execute": "Ask questions of the corpus",
    "query.history.own": "Read one's own query history",
    "query.history.any": "Read the query history of the whole tenant",
    "users.invite": "Invite new users",
    "users.manage": "Edit and deactivate users",
    "roles.manage": "Create and edit roles",
    "labels.manage": "Create and assign access labels",
    "llm_config.manage": "Configure the generation connector",
    # Held apart from `query.history.any`, and the distinction is the point: reading what
    # colleagues asked and reading who granted whom access to what are different powers,
    # and an organisation that separates them should be able to.
    "audit.read": "Read the record of who changed access to what",
}

# The pair that, if nobody holds it, locks the tenant out of its own administration.
# Recovering from that on-premise means someone in `psql`, so role management (M3)
# has to refuse the operation that would produce it.
ADMINISTRATION: Final[frozenset[str]] = frozenset({"users.manage", "roles.manage"})

# Seeded per tenant at creation time, marked `is_system` so they cannot be deleted.
SYSTEM_ROLES: Final[dict[str, frozenset[str]]] = {
    "admin": frozenset(CATALOGUE),
    "member": frozenset({"query.execute", "query.history.own"}),
}
