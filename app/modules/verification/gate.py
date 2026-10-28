"""Deterministic Verification Gate and bounded Generator -> Verifier loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from app.modules.knowledge.models import Citation, EvidencePackage
from app.modules.query_router.router import detect_required_access_level

SAFE_FALLBACK_TEXT = "Mình chưa có đủ thông tin đã được xác minh để trả lời câu này."
SPOILER_FALLBACK_TEXT = (
    "Mình chưa thể tiết lộ thông tin này ở tiến trình hiện tại của bạn. "
    "Hãy tiếp tục khám phá các chương tiếp theo nhé."
)
CLARIFICATION_FALLBACK_TEXT = (
    "Bạn có thể nói rõ hơn câu hỏi thuộc về thế giới Saigon: Veilwake hay địa điểm ngoài đời không?"
)
GAME_STATE_UNAVAILABLE_TEXT = (
    "Mình chưa kiểm tra được trạng thái tiến trình của bạn từ hệ thống trò chơi lúc này."
)
DIRECT_GREETING_TEXT = (
    "Xin chào Người Lắng Nghe! Bạn muốn tìm hiểu về thế giới Ký Giới hay các địa danh tại Sài Gòn?"
)

ClaimStatus = Literal["SUPPORTED", "UNSUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"]
VerifierDecisionType = Literal["PASS", "REVISE", "REJECT"]
GateStatus = Literal["PASS", "REVISE", "REJECT"]


@dataclass(frozen=True)
class DraftAnswer:
    text: str
    cited_evidence_ids: tuple[str, ...] = ()
    content_mode: str = "FACTUAL_GROUNDED"


class ClaimVerificationSchema(BaseModel):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    status: ClaimStatus
    supporting_evidence_ids: list[str] = Field(default_factory=list)


class VerificationReportSchema(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    decision: VerifierDecisionType
    claims: list[ClaimVerificationSchema] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)
    recommended_action: str = "APPROVE"


@dataclass(frozen=True)
class ApprovedAnswer:
    approved_answer_id: str
    text: str
    content_mode: str
    verification_status: Literal["PASS"]
    citations: tuple[Citation, ...]
    policy_version: str = "v1.3"


@dataclass(frozen=True)
class GateDecision:
    status: GateStatus
    reason: str
    issues: tuple[str, ...] = ()
    approved_answer: ApprovedAnswer | None = None
    fallback_text: str = SAFE_FALLBACK_TEXT


class GroundedGenerator(Protocol):
    async def generate_draft(
        self,
        query: str,
        evidence: EvidencePackage,
        *,
        content_mode: str = "FACTUAL_GROUNDED",
        revision_issues: tuple[str, ...] = (),
    ) -> DraftAnswer: ...


class ClaimVerifier(Protocol):
    async def verify_draft(
        self,
        query: str,
        draft: DraftAnswer,
        evidence: EvidencePackage,
        *,
        allowed_story_access_level: int = 0,
    ) -> dict[str, Any]: ...


class VerificationGate:
    """Server-side deterministic gate that validates Verifier output and spoiler/evidence rules."""

    def __init__(self) -> None:
        self._approved_answers: dict[str, ApprovedAnswer] = {}

    def get_approved_answer(self, approved_answer_id: str) -> ApprovedAnswer:
        if approved_answer_id not in self._approved_answers:
            raise KeyError(f"Approved answer not found or not verified: {approved_answer_id}")
        return self._approved_answers[approved_answer_id]

    def register_direct_approved_answer(
        self, text: str, *, content_mode: str = "FACTUAL_GROUNDED"
    ) -> ApprovedAnswer:
        approved = ApprovedAnswer(
            approved_answer_id=f"ans_{uuid4().hex}",
            text=text,
            content_mode=content_mode,
            verification_status="PASS",
            citations=(),
        )
        self._approved_answers[approved.approved_answer_id] = approved
        return approved

    def evaluate(
        self,
        draft: DraftAnswer,
        raw_report: dict[str, Any] | VerificationReportSchema,
        evidence: EvidencePackage,
        *,
        allowed_story_access_level: int = 0,
        revision_attempt: int = 0,
    ) -> GateDecision:
        if not draft.text or not draft.text.strip():
            return GateDecision(status="REJECT", reason="EMPTY_DRAFT")

        lowered_draft = draft.text.lower()
        if any(
            phrase in lowered_draft
            for phrase in (
                "không có thông tin",
                "không có bằng chứng",
                "bằng chứng được cung cấp không có",
            )
        ):
            return GateDecision(status="REJECT", reason="DRAFT_REPORTS_MISSING_EVIDENCE")

        if not evidence.is_sufficient or not evidence.items:
            return GateDecision(status="REJECT", reason="EVIDENCE_INSUFFICIENT")

        # 1. Deterministic spoiler check on draft text itself.
        if detect_required_access_level(draft.text) > allowed_story_access_level:
            return GateDecision(
                status="REJECT",
                reason="SPOILER_LEAK_IN_DRAFT",
                issues=("Draft contains spoiler terms above caller story_access_level.",),
                fallback_text=SPOILER_FALLBACK_TEXT,
            )

        # 2. Strict JSON Schema validation of Verifier output.
        try:
            report = (
                raw_report
                if isinstance(raw_report, VerificationReportSchema)
                else VerificationReportSchema.model_validate(raw_report)
            )
        except ValidationError as exc:
            return GateDecision(
                status="REJECT",
                reason="INVALID_VERIFIER_SCHEMA",
                issues=(str(exc),),
            )

        evidence_by_id = {item.evidence_id: item for item in evidence.items}
        unsupported_issues: list[str] = list(report.issues)
        referenced_evidence_ids: list[str] = []

        # 3. Deterministic claim-to-evidence and access-level verification.
        for claim in report.claims:
            for ev_id in claim.supporting_evidence_ids:
                ev_item = evidence_by_id.get(ev_id)
                if ev_item is None:
                    return GateDecision(
                        status="REJECT",
                        reason="UNKNOWN_EVIDENCE_ID",
                        issues=(f"Claim {claim.claim_id} cited unknown evidence ID {ev_id}.",),
                    )
                if (
                    ev_item.story_access_level > allowed_story_access_level
                    or ev_item.visibility_scope not in {"player", "public"}
                ):
                    return GateDecision(
                        status="REJECT",
                        reason="UNAUTHORIZED_EVIDENCE_ACCESS",
                        issues=(f"Evidence {ev_id} exceeds allowed story_access_level.",),
                        fallback_text=SPOILER_FALLBACK_TEXT,
                    )
                if ev_id not in referenced_evidence_ids:
                    referenced_evidence_ids.append(ev_id)

            if claim.status == "CONTRADICTED":
                return GateDecision(
                    status="REJECT",
                    reason="CLAIM_CONTRADICTED",
                    issues=(f"Claim {claim.claim_id} is contradicted by evidence.",),
                )

            if claim.status != "SUPPORTED" or not claim.supporting_evidence_ids:
                unsupported_issues.append(
                    f"Claim {claim.claim_id} ({claim.status}) lacks valid supporting evidence."
                )

        # Even if Verifier LLM returned decision="PASS", any unsupported claim blocks approval.
        if unsupported_issues or report.decision == "REVISE":
            if revision_attempt < 1 and report.decision != "REJECT":
                return GateDecision(
                    status="REVISE",
                    reason="UNSUPPORTED_OR_REVISABLE_CLAIMS",
                    issues=tuple(unsupported_issues or ["Revise draft strictly to evidence."]),
                )
            return GateDecision(
                status="REJECT",
                reason="UNSUPPORTED_CLAIMS_AFTER_REVISION"
                if revision_attempt >= 1
                else "VERIFIER_REJECTED",
                issues=tuple(unsupported_issues),
            )

        if report.decision != "PASS":
            return GateDecision(
                status="REJECT",
                reason="VERIFIER_REJECTED",
                issues=tuple(report.issues),
            )

        citations = tuple(
            evidence_by_id[ev_id].citation
            for ev_id in referenced_evidence_ids
            if ev_id in evidence_by_id
        )
        if not citations:
            return GateDecision(
                status="REJECT",
                reason="NO_VALID_CITATIONS",
                issues=("Approved answer must reference at least one valid citation.",),
            )

        approved = ApprovedAnswer(
            approved_answer_id=f"ans_{uuid4().hex}",
            text=draft.text.strip(),
            content_mode=draft.content_mode,
            verification_status="PASS",
            citations=citations,
        )
        self._approved_answers[approved.approved_answer_id] = approved
        return GateDecision(
            status="PASS",
            reason="VERIFIED",
            approved_answer=approved,
        )


@dataclass(frozen=True)
class VerifiedPipelineOutcome:
    passed: bool
    text: str
    verification_status: Literal["PASS", "FALLBACK"]
    approved_answer: ApprovedAnswer | None
    citations: tuple[Citation, ...] = ()
    revision_count: int = 0
    gate_reason: str = "VERIFIED"
    issues: tuple[str, ...] = field(default_factory=tuple)


async def run_verified_answer_pipeline(
    query: str,
    evidence: EvidencePackage,
    *,
    generator: GroundedGenerator,
    verifier: ClaimVerifier,
    gate: VerificationGate,
    allowed_story_access_level: int = 0,
    content_mode: str = "FACTUAL_GROUNDED",
) -> VerifiedPipelineOutcome:
    """Execute Generator -> Verifier -> Gate with at most 1 revision round (fail-closed)."""

    if not evidence.is_sufficient or not evidence.items:
        fallback = (
            SPOILER_FALLBACK_TEXT
            if evidence.insufficiency_reason == "ENTITY_NOT_IN_AUTHORIZED_EVIDENCE"
            and detect_required_access_level(query) > allowed_story_access_level
            else SAFE_FALLBACK_TEXT
        )
        return VerifiedPipelineOutcome(
            passed=False,
            text=fallback,
            verification_status="FALLBACK",
            approved_answer=None,
            gate_reason=evidence.insufficiency_reason or "EVIDENCE_INSUFFICIENT",
        )

    revision_issues: tuple[str, ...] = ()
    for attempt in range(2):
        try:
            draft = await generator.generate_draft(
                query,
                evidence,
                content_mode=content_mode,
                revision_issues=revision_issues,
            )
            raw_report = await verifier.verify_draft(
                query,
                draft,
                evidence,
                allowed_story_access_level=allowed_story_access_level,
            )
        except Exception as exc:
            if attempt == 0:
                continue
            return VerifiedPipelineOutcome(
                passed=False,
                text=SAFE_FALLBACK_TEXT,
                verification_status="FALLBACK",
                approved_answer=None,
                revision_count=attempt,
                gate_reason=f"PROVIDER_ERROR:{type(exc).__name__}",
            )

        decision = gate.evaluate(
            draft,
            raw_report,
            evidence,
            allowed_story_access_level=allowed_story_access_level,
            revision_attempt=attempt,
        )
        if decision.status == "PASS" and decision.approved_answer is not None:
            return VerifiedPipelineOutcome(
                passed=True,
                text=decision.approved_answer.text,
                verification_status="PASS",
                approved_answer=decision.approved_answer,
                citations=decision.approved_answer.citations,
                revision_count=attempt,
                gate_reason=decision.reason,
            )
        if decision.status == "REVISE" and attempt == 0:
            revision_issues = decision.issues
            continue
        return VerifiedPipelineOutcome(
            passed=False,
            text=decision.fallback_text,
            verification_status="FALLBACK",
            approved_answer=None,
            revision_count=attempt,
            gate_reason=decision.reason,
            issues=decision.issues,
        )

    return VerifiedPipelineOutcome(
        passed=False,
        text=SAFE_FALLBACK_TEXT,
        verification_status="FALLBACK",
        approved_answer=None,
        revision_count=1,
        gate_reason="MAX_REVISIONS_EXCEEDED",
    )

