from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class KnowledgeChunk:
    """A chunk that is safe to index only after its source is approved."""

    ordinal: int
    content: str
    section_title: str | None
    language: str = "vi"
    story_access_level: int = 0
    knowledge_domain: str = "game_lore"
    metadata: dict[str, Any] = field(default_factory=dict)
    source_id: str = "game_world_v1_0"
    source_title: str = "Saigon: Veilwake — Bản chốt ý tưởng, thế giới quan và cơ chế trò chơi"
    source_url: str | None = None
    document_version: str = "1.0"
    source_span: str = ""
    section_path: tuple[str, ...] = ()
    canonical_status: str = "design_approved"
    visibility: str = "PLAYER"
    chunk_id: UUID | None = None
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    id: UUID
    content: str
    source_title: str
    source_url: str | None
    document_version: str
    section_title: str | None
    story_access_level: int
    metadata: dict[str, Any]
    hybrid_score: float
    source_id: str = "game_world_v1_0"
    source_span: str = ""
    section_path: tuple[str, ...] = ()
    knowledge_domain: str = "game_lore"
    canonical_status: str = "design_approved"
    semantic_score: float = 0.0
    lexical_score: float = 0.0
    semantic_rank: int | None = None
    lexical_rank: int | None = None


@dataclass(frozen=True)
class Citation:
    """Traceable reference required by retrieval_invariants and public turn answers."""

    evidence_id: str
    source_id: str
    source_type: str
    title: str
    document_version: str
    section_path: tuple[str, ...]
    source_span: str
    url: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "title": self.title,
            "document_version": self.document_version,
            "section_path": list(self.section_path),
            "source_span": self.source_span,
            "url": self.url,
        }


@dataclass(frozen=True)
class EvidenceItem:
    """Normalized evidence item passed to Answer Generator and Claim Verifier."""

    evidence_id: str
    chunk_id: UUID
    source_kind: str
    source_id: str
    knowledge_domain: str
    title: str
    url: str | None
    document_version: str
    section_path: tuple[str, ...]
    source_span: str
    text: str
    story_access_level: int
    canonical_status: str
    trust_tier: str
    visibility_scope: str
    semantic_score: float
    lexical_score: float
    relevance_score: float
    retrieved_at: str
    citation: Citation


@dataclass(frozen=True)
class EvidencePackage:
    """Aggregated, spoiler-filtered evidence bundle for a query or subquestion."""

    query: str
    normalized_query: str
    knowledge_domain: str
    allowed_story_access_level: int
    items: tuple[EvidenceItem, ...]
    citations: tuple[Citation, ...]
    is_sufficient: bool
    insufficiency_reason: str | None = None
