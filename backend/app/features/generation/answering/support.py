"""Optional claim-support checks, separate from citation validity and ranking.

The complete rendered source bundle is the assessor's evidence, while each
claim's markers identify the spans it may cite. A model verdict is an
assessment, never a proof or a permission grant. No draft is disclosed by this
module; the caller buffers it until every check and current-source recheck ends.
"""

import asyncio
import hashlib
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from app.features.auth.service import AccessProfile
from app.features.generation.answering.citations import MARKER
from app.features.retrieval.direct import fingerprint, verify_current
from app.features.retrieval.judging.jev import Purpose, configured_jev_judge
from app.features.retrieval.judging.protocol import Candidate, Judge, Outcome, ScoreKind
from app.features.retrieval.judging.rubrics import CLAIM_SUPPORT_NOUL, Formulation
from app.features.retrieval.search import Hit
from app.features.tenancy.context import TenantContext

SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÜ¿])|[;\n]+")
CONJUNCTION = re.compile(r"\s+(?:and|but|y|pero)\s+", re.I)
QUOTED = re.compile(r'"([^"\n]+)"|“([^”\n]+)”')
NUMBER = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?%?(?!\w)")
ARITHMETIC = re.compile(r"\b(\d+)\s*([+*/-])\s*(\d+)\s*=\s*(\d+)\b")


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"
    NOT_ASSESSED = "not_assessed"


@dataclass(frozen=True, slots=True)
class ClaimAssessment:
    claim_hash: str
    evidence_hash: str
    reference_valid: bool
    span_valid: bool
    values_valid: bool
    support_status: SupportStatus
    assessor_provider: str | None = None
    assessor_model: str | None = None
    rubric_id: str | None = None
    rubric_hash: str | None = None
    input_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedClaim:
    text: str
    markers: tuple[int, ...]
    candidate: Candidate
    assessment: ClaimAssessment


@dataclass(frozen=True, slots=True)
class SupportReview:
    status: SupportStatus
    assessments: tuple[ClaimAssessment, ...]
    # None means the authorization check could not complete, not a proved change.
    source_current: bool | None


def _evidence_hash(hits: list[Hit], markers: tuple[int, ...]) -> str:
    return fingerprint(
        [
            f"{position}:{hit.chunk_id}:{hit.source_sha256}:{hit.text}"
            for position, hit in enumerate(hits, 1)
        ]
        + ["cited:" + ",".join(map(str, markers))]
    )


def _render_evidence(hits: list[Hit], markers: tuple[int, ...]) -> str:
    cited = set(markers)
    return "\n\n".join(
        f"[{index}] {hit.filename} page {hit.page_num} "
        f"({'cited span' if index in cited else 'surrounding context'})\n{hit.text}"
        for index, hit in enumerate(hits, 1)
    )


def _arithmetic_valid(claim: str) -> bool:
    for left, operator, right, result in ARITHMETIC.findall(claim):
        a, b, actual = int(left), int(right), int(result)
        expected = {"+": a + b, "-": a - b, "*": a * b}.get(operator)
        if operator == "/":
            expected = a / b if b else None
        if expected is None or expected != actual:
            return False
    return True


def prepare(
    draft: str, hits: list[Hit], *, max_claims: int, max_input_bytes: int
) -> tuple[PreparedClaim, ...]:
    """Cover each sentence and conjunction; fail closed on a truncated review."""
    if max_claims < 1 or max_input_bytes < 1:
        raise ValueError("invalid support bounds")
    pieces: list[tuple[str, tuple[int, ...]]] = []
    for sentence in SENTENCE.split(draft):
        if not sentence.strip():
            continue
        markers = tuple(
            dict.fromkeys(
                int(number)
                for match in MARKER.finditer(sentence)
                for number in match.group(1).split(",")
            )
        )
        bare = re.sub(r"^[\s•-]+|[\s•.-]+$", "", MARKER.sub("", sentence))
        if not bare:
            continue
        clauses = [part.strip() for part in CONJUNCTION.split(bare) if part.strip()]
        pieces.extend((part, markers) for part in clauses)
    if not pieces or len(pieces) > max_claims:
        identity = hashlib.sha256(draft.encode()).hexdigest()
        evidence = _evidence_hash(hits, ())
        return (
            PreparedClaim(
                draft,
                (),
                Candidate(uuid5(NAMESPACE_URL, identity + evidence), ""),
                ClaimAssessment(
                    identity, evidence, False, False, False, SupportStatus.NOT_ASSESSED
                ),
            ),
        )
    prepared: list[PreparedClaim] = []
    for clause, markers in pieces:
        claim_hash = hashlib.sha256(clause.encode()).hexdigest()
        evidence_hash = _evidence_hash(hits, markers)
        referenced = bool(markers) and all(1 <= marker <= len(hits) for marker in markers)
        cited_text = "\n".join(
            hits[marker - 1].text for marker in markers if 1 <= marker <= len(hits)
        )
        quotes = [next(value for value in groups if value) for groups in QUOTED.findall(clause)]
        span_valid = referenced and all(quote in cited_text for quote in quotes)
        numbers = NUMBER.findall(clause)
        values_valid = (
            referenced
            and _arithmetic_valid(clause)
            and all(number in NUMBER.findall(cited_text) for number in numbers)
        )
        evidence = _render_evidence(hits, markers)
        candidate = Candidate(uuid5(NAMESPACE_URL, claim_hash + evidence_hash), evidence)
        input_fits = len((clause + evidence).encode()) <= max_input_bytes
        status = (
            SupportStatus.CONTRADICTED
            if not _arithmetic_valid(clause)
            else SupportStatus.INSUFFICIENT
            if not (referenced and span_valid and values_valid)
            else SupportStatus.NOT_ASSESSED
        )
        if not input_fits:
            status = SupportStatus.NOT_ASSESSED
            candidate = Candidate(candidate.id, "")
        prepared.append(
            PreparedClaim(
                clause,
                markers,
                candidate,
                ClaimAssessment(
                    claim_hash,
                    evidence_hash,
                    referenced,
                    span_valid,
                    values_valid,
                    status,
                ),
            )
        )
    return tuple(prepared)


def _overall(assessments: tuple[ClaimAssessment, ...]) -> SupportStatus:
    if any(item.support_status is SupportStatus.CONTRADICTED for item in assessments):
        return SupportStatus.CONTRADICTED
    if any(item.support_status is SupportStatus.NOT_ASSESSED for item in assessments):
        return SupportStatus.NOT_ASSESSED
    if any(item.support_status is SupportStatus.INSUFFICIENT for item in assessments):
        return SupportStatus.INSUFFICIENT
    return SupportStatus.SUPPORTED


async def review(
    draft: str,
    hits: list[Hit],
    profile: AccessProfile,
    context: TenantContext,
    *,
    max_claims: int,
    max_input_bytes: int,
    min_noul: float,
    judge: Judge | None = None,
) -> SupportReview:
    """Assess cited claims with current authority before dispatch and disclosure."""
    if not 0 <= min_noul <= 1:
        raise ValueError("invalid support threshold")
    claims = prepare(draft, hits, max_claims=max_claims, max_input_bytes=max_input_bytes)
    if not await verify_current(profile, context, tuple(hits)):
        return SupportReview(SupportStatus.NOT_ASSESSED, tuple(c.assessment for c in claims), False)
    allowed = {claim.candidate.id: claim for claim in claims if claim.candidate.text}

    async def permitted(question: str, candidate: Candidate, purpose: Purpose) -> bool:
        original = allowed.get(candidate.id)
        return bool(
            purpose is Purpose.CLAIM_SUPPORT
            and original is not None
            and question == original.text
            and candidate.text == original.candidate.text
            and await verify_current(profile, context, tuple(hits))
        )

    client = None
    if judge is None and allowed:
        try:
            client = configured_jev_judge(
                formulation=Formulation.NOUL,
                rubric=CLAIM_SUPPORT_NOUL,
                purpose=Purpose.CLAIM_SUPPORT,
                authorize=permitted,
            )
            judge = client
        except Exception:  # noqa: BLE001 - unavailable assessor means no supported prose
            judge = None
    assessed: list[ClaimAssessment] = []
    try:
        for claim in claims:
            prior = claim.assessment
            if prior.support_status is not SupportStatus.NOT_ASSESSED or not claim.candidate.text:
                assessed.append(prior)
                continue
            if judge is None:
                assessed.append(prior)
                continue
            try:
                batch = await judge.assess(claim.text, [claim.candidate])
                item = batch.judgments[0]
                valid = (
                    batch.requested_ids == (claim.candidate.id,)
                    and len(batch.judgments) == 1
                    and item.candidate_id == claim.candidate.id
                    and item.outcome is Outcome.ASSESSED
                    and item.score_kind is ScoreKind.MODEL_PROBABILITY
                    and item.rank_value is not None
                    and 0 <= item.rank_value <= 1
                    and item.rubric_id == CLAIM_SUPPORT_NOUL.id
                    and item.rubric_hash == CLAIM_SUPPORT_NOUL.hash
                )
                score = item.rank_value
                if not valid or score is None:
                    assessed.append(prior)
                    continue
                assessed.append(
                    replace(
                        prior,
                        support_status=(
                            SupportStatus.SUPPORTED
                            if score >= min_noul
                            else SupportStatus.INSUFFICIENT
                        ),
                        assessor_provider=item.provider,
                        assessor_model=item.reported_model,
                        rubric_id=item.rubric_id,
                        rubric_hash=item.rubric_hash,
                        input_fingerprint=item.input_fingerprint,
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - provider details may contain source text
                assessed.append(prior)
    finally:
        if client is not None:
            await client.aclose()
    current = await verify_current(profile, context, tuple(hits))
    results = tuple(assessed)
    return SupportReview(
        _overall(results) if current else SupportStatus.NOT_ASSESSED, results, current
    )
