"""Versioned evidence status, independent of the legacy TEI relevance bands."""

from enum import StrEnum


class EvidenceStatusV1(StrEnum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    CONFLICTING = "conflicting"
    NONE_FOUND = "none_found"
    NOT_ASSESSED = "not_assessed"


POLICY_ID = "evidence-status-v1"
