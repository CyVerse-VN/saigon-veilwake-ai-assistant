"""RAG-first Source Router and conditional Web Search eligibility gate."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from app.modules.knowledge.models import EvidencePackage
from app.modules.knowledge.retrieval import DEFAULT_SEARCH_ALIASES


class RouteMode(str, Enum):
    DIRECT = "DIRECT"
    INTERNAL = "INTERNAL"
    GAME_LORE_GATED = "GAME_LORE_GATED"
    GAME_STATE = "GAME_STATE"
    WEB_FALLBACK = "WEB_FALLBACK"
    HYBRID = "HYBRID"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


class RagEvidenceStatus(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    STALE = "STALE"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    RETRIEVAL_ERROR = "RETRIEVAL_ERROR"
    UNKNOWN_DOMAIN = "UNKNOWN_DOMAIN"


@dataclass(frozen=True)
class SubquestionPlan:
    subquestion: str
    knowledge_domain: str
    route_mode: RouteMode
    allowed_sources: tuple[str, ...]
    requires_freshness: bool
    web_search_eligible_if_rag_insufficient: bool


@dataclass(frozen=True)
class RoutingDecision:
    query: str
    knowledge_domain: str
    route_mode: RouteMode
    allowed_sources: tuple[str, ...]
    rag_evidence_status: RagEvidenceStatus
    web_search_allowed: bool
    fallback_reason: str | None
    web_search_used: bool = False
    public_web_subquery: str | None = None


_GREETING_PATTERNS = re.compile(
    r"^(xin\s+chào|chào\s+bạn|chào\s+ai|hello|hi|hey|cảm\s+ơn|tạm\s+biệt)[!.?\s]*$",
    re.IGNORECASE,
)

_GAME_STATE_KEYWORDS = (
    "tôi có nhận được",
    "tôi đã mở",
    "tôi đang ở chương",
    "nhiệm vụ của tôi",
    "hành trang của tôi",
    "túi đồ của tôi",
    "tiến trình của tôi",
    "tôi có bao nhiêu huy hiệu",
    "tôi đã hoàn thành",
)

_FRESHNESS_KEYWORDS = (
    "hôm nay",
    "bây giờ",
    "hiện tại",
    "đang mở cửa",
    "có mở cửa không",
    "mấy giờ mở cửa",
    "mấy giờ đóng cửa",
    "giá vé hôm nay",
    "lịch sự kiện hôm nay",
)

_REAL_WORLD_KEYWORDS = (
    "bưu điện trung tâm",
    "bưu điện",
    "nhà thờ đức bà",
    "đức bà",
    "nhà thờ tân định",
    "nhà thờ",
    "dinh độc lập",
    "hội trường thống nhất",
    "chợ bến thành",
    "bến thành",
    "bến nhà rồng",
    "nhà rồng",
    "nhà hát thành phố",
    "bảo tàng",
    "địa đạo củ chi",
    "củ chi",
    "chùa ngọc hoàng",
    "thảo cầm viên",
    "phố đi bộ nguyễn huệ",
    "hồ con rùa",
    "chợ lớn",
    "chợ bình tây",
    "kiến trúc",
    "lịch sử",
    "xây dựng năm nào",
    "ở ngoài đời",
    "địa chỉ",
    "nằm ở đâu",
    "ở đâu",
    "quận mấy",
    "đường nào",
    "vũng tàu",
    "bình dương",
)

_LORE_KEYWORDS = (
    "saigon: veilwake",
    "veilwake",
    "ký giới",
    "dư ức",
    "ký vực",
    "nhịp cộng hưởng",
    "người lắng nghe",
    "người kể tiếp",
    "người giữ im lặng",
    "mảnh ký ức",
    "ấn ký",
    "điệp ức",
    "cảnh ký ức",
    "hồ sơ bí ẩn",
    "hộ chiếu ký ức",
    "huy hiệu địa danh",
    "đại huy hiệu",
    "hành trình truy ức",
    "đêm ký giới",
    "đêm cộng hưởng",
    "đại thức tỉnh",
    "nhật ký dẫn lối",
    "phản diện cuối mùa",
    "cốt truyện mùa đầu",
    "cốt truyện",
    "thế giới quan",
    "người chơi",
    "nhân vật",
    "chương 1",
    "chương 2",
    "chương 3",
    "chương 4",
    "chương 5",
    "mùa 1",
    "mùa đầu",
    "trong game",
    "trò chơi",
    "ký ức",
)

_LEVEL_GATED_TERMS: dict[str, int] = {
    "điệp ức": 1,
    "diep uc": 1,
    "những cánh bướm không trở về": 1,
    "những bức thư chưa được gửi": 2,
    "tiếng vọng từ biển": 3,
    "bàn tay cuối cùng": 4,
    "đêm không ai còn nhớ": 5,
    "người giữ im lặng": 5,
    "nguoi giu im lang": 5,
    "phản diện cuối mùa": 5,
    "phản diện": 5,
}


_AMBIGUOUS_DEICTIC_PATTERNS = re.compile(
    r"^(chỗ\s+đó|nơi\s+đó|cái\s+đó|cái\s+này|ở\s+đó)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text.strip()).lower()


def detect_required_access_level(query: str) -> int:
    """Return the minimum story_access_level required by explicit spoiler terms in the query."""

    lowered = _normalize(query)
    required = 0
    for term, level in _LEVEL_GATED_TERMS.items():
        if term in lowered:
            required = max(required, level)
    return required


def _contains_lore_term(text: str) -> bool:
    lowered = _normalize(text)
    if any(keyword in lowered for keyword in _LORE_KEYWORDS):
        return True
    for variants in DEFAULT_SEARCH_ALIASES.values():
        if any(variant.lower() in lowered for variant in variants):
            return True
    return False


def is_exclusive_lore_query(query: str) -> bool:
    """Return True if the query explicitly references Saigon: Veilwake lore or gated concepts."""

    return _contains_lore_term(query) or detect_required_access_level(query) > 0


def analyze_query(query: str, *, allowed_story_access_level: int = 0) -> SubquestionPlan:
    """Classify domain and initial RAG-first retrieval plan before any tool call."""

    cleaned = query.strip()
    lowered = _normalize(cleaned)

    if not cleaned:
        return SubquestionPlan(
            subquestion="",
            knowledge_domain="unknown",
            route_mode=RouteMode.NEEDS_CLARIFICATION,
            allowed_sources=(),
            requires_freshness=False,
            web_search_eligible_if_rag_insufficient=False,
        )

    if _GREETING_PATTERNS.match(cleaned):
        return SubquestionPlan(
            subquestion=cleaned,
            knowledge_domain="direct",
            route_mode=RouteMode.DIRECT,
            allowed_sources=(),
            requires_freshness=False,
            web_search_eligible_if_rag_insufficient=False,
        )

    if any(keyword in lowered for keyword in _GAME_STATE_KEYWORDS):
        return SubquestionPlan(
            subquestion=cleaned,
            knowledge_domain="game_state",
            route_mode=RouteMode.GAME_STATE,
            allowed_sources=("game_backend",),
            requires_freshness=True,
            web_search_eligible_if_rag_insufficient=False,
        )

    required_level = detect_required_access_level(cleaned)
    has_lore = _contains_lore_term(cleaned) or required_level > 0
    has_freshness = any(keyword in lowered for keyword in _FRESHNESS_KEYWORDS)
    has_real_world = any(keyword in lowered for keyword in _REAL_WORLD_KEYWORDS)

    if has_lore:
        if required_level > allowed_story_access_level:
            return SubquestionPlan(
                subquestion=cleaned,
                knowledge_domain="game_lore",
                route_mode=RouteMode.GAME_LORE_GATED,
                allowed_sources=("internal_rag",),
                requires_freshness=False,
                web_search_eligible_if_rag_insufficient=False,
            )
        return SubquestionPlan(
            subquestion=cleaned,
            knowledge_domain="game_lore",
            route_mode=RouteMode.INTERNAL,
            allowed_sources=("internal_rag",),
            requires_freshness=False,
            web_search_eligible_if_rag_insufficient=False,
        )

    if has_freshness:
        return SubquestionPlan(
            subquestion=cleaned,
            knowledge_domain="dynamic_web",
            route_mode=RouteMode.INTERNAL,
            allowed_sources=("internal_rag", "web_search"),
            requires_freshness=True,
            web_search_eligible_if_rag_insufficient=True,
        )

    if has_real_world:
        return SubquestionPlan(
            subquestion=cleaned,
            knowledge_domain="real_world",
            route_mode=RouteMode.INTERNAL,
            allowed_sources=("internal_rag", "web_search"),
            requires_freshness=False,
            web_search_eligible_if_rag_insufficient=True,
        )

    if _AMBIGUOUS_DEICTIC_PATTERNS.match(cleaned):
        return SubquestionPlan(
            subquestion=cleaned,
            knowledge_domain="unknown",
            route_mode=RouteMode.NEEDS_CLARIFICATION,
            allowed_sources=(),
            requires_freshness=False,
            web_search_eligible_if_rag_insufficient=False,
        )

    # Default RAG-first policy: treat general questions in the AI Guide as game_lore
    # queries against internal RAG rather than rejecting before retrieval.
    return SubquestionPlan(
        subquestion=cleaned,
        knowledge_domain="game_lore",
        route_mode=RouteMode.INTERNAL,
        allowed_sources=("internal_rag",),
        requires_freshness=False,
        web_search_eligible_if_rag_insufficient=False,
    )


def evaluate_routing_decision(
    plan: SubquestionPlan,
    evidence: EvidencePackage | None,
    *,
    allowed_story_access_level: int = 0,
    retrieval_error: bool = False,
    evidence_is_stale: bool = False,
    web_budget_available: bool = True,
) -> RoutingDecision:
    """Enforce Proposal v1.3 Section 6.1 RAG-first and conditional web fallback rules."""

    if plan.route_mode == RouteMode.DIRECT:
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain=plan.knowledge_domain,
            route_mode=RouteMode.DIRECT,
            allowed_sources=plan.allowed_sources,
            rag_evidence_status=RagEvidenceStatus.NOT_APPLICABLE,
            web_search_allowed=False,
            fallback_reason=None,
        )

    if plan.route_mode == RouteMode.GAME_STATE:
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain="game_state",
            route_mode=RouteMode.GAME_STATE,
            allowed_sources=("game_backend",),
            rag_evidence_status=RagEvidenceStatus.NOT_APPLICABLE,
            web_search_allowed=False,
            fallback_reason="GAME_STATE_AUTHORITY_ONLY",
        )

    if plan.route_mode == RouteMode.NEEDS_CLARIFICATION or plan.knowledge_domain == "unknown":
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain="unknown",
            route_mode=RouteMode.NEEDS_CLARIFICATION,
            allowed_sources=(),
            rag_evidence_status=RagEvidenceStatus.UNKNOWN_DOMAIN,
            web_search_allowed=False,
            fallback_reason="UNKNOWN_DOMAIN",
        )

    if (
        plan.route_mode == RouteMode.GAME_LORE_GATED
        or detect_required_access_level(plan.subquestion) > allowed_story_access_level
    ):
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain="game_lore",
            route_mode=RouteMode.GAME_LORE_GATED,
            allowed_sources=("internal_rag",),
            rag_evidence_status=RagEvidenceStatus.ACCESS_RESTRICTED,
            web_search_allowed=False,
            fallback_reason="SPOILER_ACCESS_RESTRICTED",
        )

    if retrieval_error:
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain=plan.knowledge_domain,
            route_mode=plan.route_mode,
            allowed_sources=plan.allowed_sources,
            rag_evidence_status=RagEvidenceStatus.RETRIEVAL_ERROR,
            web_search_allowed=False,
            fallback_reason="RETRIEVAL_ERROR_NO_WEB_BYPASS",
        )

    has_sufficient_rag = bool(evidence and evidence.is_sufficient and evidence.items)
    if has_sufficient_rag and not (plan.requires_freshness and evidence_is_stale):
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain=plan.knowledge_domain,
            route_mode=RouteMode.INTERNAL,
            allowed_sources=plan.allowed_sources,
            rag_evidence_status=RagEvidenceStatus.SUFFICIENT,
            web_search_allowed=False,
            fallback_reason=None,
        )

    rag_status = (
        RagEvidenceStatus.STALE
        if (has_sufficient_rag and plan.requires_freshness and evidence_is_stale)
        else RagEvidenceStatus.INSUFFICIENT
    )

    # Exclusive game lore never falls back to public web search.
    if plan.knowledge_domain == "game_lore" or not plan.web_search_eligible_if_rag_insufficient:
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain=plan.knowledge_domain,
            route_mode=RouteMode.INTERNAL,
            allowed_sources=("internal_rag",),
            rag_evidence_status=rag_status,
            web_search_allowed=False,
            fallback_reason="LORE_WEB_FALLBACK_FORBIDDEN",
        )

    if not web_budget_available:
        return RoutingDecision(
            query=plan.subquestion,
            knowledge_domain=plan.knowledge_domain,
            route_mode=RouteMode.INTERNAL,
            allowed_sources=plan.allowed_sources,
            rag_evidence_status=rag_status,
            web_search_allowed=False,
            fallback_reason="WEB_BUDGET_EXHAUSTED",
        )

    # Public domain (real_world / dynamic_web) with missing or stale RAG evidence.
    return RoutingDecision(
        query=plan.subquestion,
        knowledge_domain=plan.knowledge_domain,
        route_mode=RouteMode.WEB_FALLBACK,
        allowed_sources=plan.allowed_sources,
        rag_evidence_status=rag_status,
        web_search_allowed=True,
        fallback_reason=f"RAG_{rag_status.value}_PUBLIC_WEB_ALLOWED",
        public_web_subquery=plan.subquestion,
    )
