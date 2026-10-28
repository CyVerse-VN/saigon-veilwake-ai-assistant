"""Hybrid knowledge retrieval (semantic + FTS + RRF) and evidence packaging."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import NAMESPACE_OID, UUID, uuid5

import httpx

from app.core.config import get_settings
from app.integrations.embeddings.gemini import GeminiEmbeddingProvider
from app.modules.knowledge.access import visible_chunks
from app.modules.knowledge.models import (
    Citation,
    EvidenceItem,
    EvidencePackage,
    KnowledgeChunk,
    RetrievedChunk,
)
from app.modules.knowledge.ranking import reciprocal_rank_fusion

DEFAULT_SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "Ký Giới": ("ky gioi", "ký giới"),
    "Dư Ức": ("du uc", "dư ức"),
    "Ký Vực": ("ky vuc", "ký vực"),
    "Nhịp Cộng Hưởng": ("nhip cong huong", "nhịp cộng hưởng"),
    "Người Lắng Nghe": ("nguoi lang nghe", "người lắng nghe"),
    "Người Kể Tiếp": ("nguoi ke tiep", "người kể tiếp"),
    "Người Giữ Im Lặng": ("nguoi giu im lang", "người giữ im lặng"),
    "Mảnh Ký Ức": ("manh ky uc", "mảnh ký ức"),
    "Ấn Ký": ("an ky", "ấn ký"),
    "Điệp Ức": ("diep uc", "điệp ức"),
    "Cảnh Ký Ức": ("canh ky uc", "cảnh ký ức"),
    "Hồ Sơ Bí Ẩn": ("ho so bi an", "hồ sơ bí ẩn"),
    "Hộ Chiếu Ký Ức": ("ho chieu ky uc", "hộ chiếu ký ức"),
    "Huy Hiệu Địa Danh": ("huy hieu dia danh", "huy hiệu địa danh"),
    "Đại Huy Hiệu": ("dai huy hieu", "đại huy hiệu"),
    "Hành Trình Truy Ức": ("hanh trinh truy uc", "hành trình truy ức"),
    "Đêm Ký Giới": ("dem ky gioi", "đêm ký giới"),
    "Đêm Cộng Hưởng": ("dem cong huong", "đêm cộng hưởng"),
    "Đại Thức Tỉnh": ("dai thuc tinh", "đại thức tỉnh"),
    "Nhật Ký Dẫn Lối": ("nhat ky dan loi", "nhật ký dẫn lối"),
}

_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_VIETNAMESE_STOPWORDS = frozenset(
    {
        "la",
        "là",
        "gi",
        "gì",
        "tai",
        "tại",
        "sao",
        "nhu",
        "như",
        "the",
        "thế",
        "nao",
        "nào",
        "co",
        "có",
        "cua",
        "của",
        "trong",
        "va",
        "và",
        "hay",
        "hoac",
        "hoặc",
        "ai",
        "o",
        "ở",
        "dau",
        "đâu",
        "khi",
        "nao",
        "cho",
        "bi",
        "bị",
        "duoc",
        "được",
        "mot",
        "một",
        "nhung",
        "những",
        "cac",
        "các",
        "ve",
        "về",
        "thi",
        "thì",
        "ma",
        "mà",
        "de",
        "để",
        "voi",
        "với",
        "con",
        "còn",
        "khong",
        "không",
        "lai",
        "lại",
    }
)


class QueryEmbedder(Protocol):
    async def embed_query(self, text: str) -> list[float]: ...


def normalize_and_expand_query(
    query: str, aliases: dict[str, Sequence[str]] | None = None
) -> str:
    """Normalize to NFC and append canonical Vietnamese terms when ASCII aliases appear."""

    normalized = unicodedata.normalize("NFC", query.strip())
    if not normalized:
        return ""
    lowered = normalized.lower()
    alias_map = aliases or DEFAULT_SEARCH_ALIASES
    expansions: list[str] = []
    for canonical, variants in alias_map.items():
        if canonical.lower() in lowered:
            continue
        if any(variant.lower() in lowered for variant in variants):
            expansions.append(canonical)
    if not expansions:
        return normalized
    return f"{normalized} ({' '.join(expansions)})"


def extract_content_tokens(text: str) -> list[str]:
    """Extract lowercase content words excluding common Vietnamese question filler words."""

    normalized = unicodedata.normalize("NFC", text).lower()
    tokens = _TOKEN_PATTERN.findall(normalized)
    return [tok for tok in tokens if len(tok) > 1 and tok not in _VIETNAMESE_STOPWORDS]


def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=False))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _lexical_overlap_score(query_text: str, content_text: str) -> float:
    query_tokens = extract_content_tokens(query_text)
    if not query_tokens:
        return 0.0
    normalized_content = unicodedata.normalize("NFC", content_text).lower()
    content_tokens = set(_TOKEN_PATTERN.findall(normalized_content))
    matched = sum(1 for token in query_tokens if token in content_tokens)
    if matched == 0:
        return 0.0

    # Boost exact canonical term matches when present in the chunk.
    phrase_bonus = 0.0
    lowered_query = unicodedata.normalize("NFC", query_text).lower()
    for canonical in DEFAULT_SEARCH_ALIASES:
        canonical_lower = canonical.lower()
        if canonical_lower in lowered_query and canonical_lower in normalized_content:
            phrase_bonus += 0.5
    return (matched / len(query_tokens)) + phrase_bonus


class InMemoryKnowledgeRetriever:
    """Deterministic hybrid retriever over in-memory KnowledgeChunk items for unit tests."""

    def __init__(
        self,
        chunks: Sequence[KnowledgeChunk],
        *,
        embedder: QueryEmbedder | None = None,
        rrf_k: int = 60,
    ) -> None:
        self._chunks = list(chunks)
        self._embedder = embedder
        self._rrf_k = rrf_k

    async def retrieve(
        self,
        query: str,
        *,
        allowed_story_access_level: int = 0,
        knowledge_domain: str = "game_lore",
        language: str = "vi",
        candidate_limit: int = 20,
        top_k: int = 8,
        min_semantic_score: float = 0.55,
    ) -> EvidencePackage:
        normalized_query = normalize_and_expand_query(query)
        if not normalized_query:
            return EvidencePackage(
                query=query,
                normalized_query="",
                knowledge_domain=knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="EMPTY_QUERY",
            )

        # 1. Pre-retrieval filter BEFORE any semantic or lexical ranking.
        domain_filtered = [
            chunk
            for chunk in self._chunks
            if chunk.knowledge_domain == knowledge_domain
            and chunk.language == language
            and chunk.visibility.upper() == "PLAYER"
        ]
        authorized = visible_chunks(domain_filtered, allowed_story_access_level)
        if not authorized:
            return EvidencePackage(
                query=query,
                normalized_query=normalized_query,
                knowledge_domain=knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="NO_AUTHORIZED_CHUNKS",
            )

        query_vector: list[float] | None = None
        if self._embedder is not None:
            query_vector = await self._embedder.embed_query(normalized_query)

        chunk_by_key: dict[str, KnowledgeChunk] = {}
        semantic_scores: dict[str, float] = {}
        lexical_scores: dict[str, float] = {}

        for chunk in authorized:
            chunk_uuid = chunk.chunk_id or uuid5(
                NAMESPACE_OID, f"{chunk.source_id}:{chunk.document_version}:{chunk.ordinal}"
            )
            key = str(chunk_uuid)
            chunk_by_key[key] = chunk
            if query_vector is not None and chunk.embedding is not None:
                semantic_scores[key] = _cosine_similarity(query_vector, chunk.embedding)
            else:
                semantic_scores[key] = 0.0
            lexical_scores[key] = _lexical_overlap_score(normalized_query, chunk.content)

        semantic_ranked = [
            key
            for key, score in sorted(
                semantic_scores.items(),
                key=lambda item: (item[1], -chunk_by_key[item[0]].ordinal),
                reverse=True,
            )[:candidate_limit]
            if score > 0.0
        ]
        lexical_ranked = [
            key
            for key, score in sorted(
                lexical_scores.items(),
                key=lambda item: (item[1], -chunk_by_key[item[0]].ordinal),
                reverse=True,
            )[:candidate_limit]
            if score > 0.0
        ]

        rank_lists = [lst for lst in (semantic_ranked, lexical_ranked) if lst]
        fused = reciprocal_rank_fusion(rank_lists, k=self._rrf_k) if rank_lists else []

        semantic_rank_map = {key: idx for idx, key in enumerate(semantic_ranked, start=1)}
        lexical_rank_map = {key: idx for idx, key in enumerate(lexical_ranked, start=1)}

        retrieved: list[RetrievedChunk] = []
        for key, hybrid_score in fused[:top_k]:
            sem_score = semantic_scores.get(key, 0.0)
            lex_score = lexical_scores.get(key, 0.0)
            if lex_score <= 0.0 and sem_score < min_semantic_score:
                continue
            chunk = chunk_by_key[key]
            section_path = chunk.section_path or (
                (chunk.section_title,) if chunk.section_title else ()
            )
            retrieved.append(
                RetrievedChunk(
                    id=UUID(key),
                    content=chunk.content,
                    source_title=chunk.source_title,
                    source_url=chunk.source_url,
                    document_version=chunk.document_version,
                    section_title=section_path[-1] if section_path else chunk.section_title,
                    story_access_level=chunk.story_access_level,
                    metadata=dict(chunk.metadata),
                    hybrid_score=hybrid_score,
                    source_id=chunk.source_id,
                    source_span=chunk.source_span or f"chunk-{chunk.ordinal:04d}",
                    section_path=section_path,
                    knowledge_domain=chunk.knowledge_domain,
                    canonical_status=chunk.canonical_status,
                    semantic_score=sem_score,
                    lexical_score=lex_score,
                    semantic_rank=semantic_rank_map.get(key),
                    lexical_rank=lexical_rank_map.get(key),
                )
            )

        return build_evidence_package(
            query=query,
            normalized_query=normalized_query,
            knowledge_domain=knowledge_domain,
            allowed_story_access_level=allowed_story_access_level,
            chunks=retrieved,
            min_semantic_score=min_semantic_score,
        )


class SupabaseKnowledgeRetriever:
    """Hybrid retriever backed by Supabase pgvector + FTS RPC (`search_knowledge_chunks`)."""

    def __init__(
        self,
        *,
        supabase_url: str | None = None,
        supabase_secret_key: str | None = None,
        embedder: QueryEmbedder | None = None,
        rrf_k: int = 60,
        timeout_seconds: int = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self._supabase_url = (supabase_url or settings.supabase_url or "").rstrip("/")
        self._supabase_secret_key = supabase_secret_key or settings.supabase_secret_key or ""
        if not self._supabase_url or not self._supabase_secret_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for retrieval.")

        if embedder is None:
            if not settings.gemini_api_key:
                raise ValueError("GEMINI_API_KEY is required for query embedding.")
            embedder = GeminiEmbeddingProvider(
                api_key=settings.gemini_api_key,
                model=settings.gemini_embedding_model,
                dimensions=settings.gemini_embedding_dimensions,
                timeout_seconds=settings.gemini_request_timeout_seconds,
            )
        self._embedder = embedder
        self._rrf_k = rrf_k
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def retrieve(
        self,
        query: str,
        *,
        allowed_story_access_level: int = 0,
        knowledge_domain: str = "game_lore",
        language: str = "vi",
        candidate_limit: int = 20,
        top_k: int = 8,
        min_semantic_score: float = 0.55,
    ) -> EvidencePackage:
        normalized_query = normalize_and_expand_query(query)
        if not normalized_query:
            return EvidencePackage(
                query=query,
                normalized_query="",
                knowledge_domain=knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="EMPTY_QUERY",
            )

        query_vector = await self._embedder.embed_query(normalized_query)
        vector_literal = "[" + ",".join(f"{float(v):.8f}" for v in query_vector) + "]"

        payload = {
            "p_query_text": normalized_query,
            "p_query_embedding": vector_literal,
            "p_knowledge_domain": knowledge_domain,
            "p_allowed_story_access_level": int(allowed_story_access_level),
            "p_language": language,
            "p_candidate_limit": int(candidate_limit),
            "p_match_count": int(max(top_k, candidate_limit)),
            "p_rrf_k": int(self._rrf_k),
        }
        headers = {
            "apikey": self._supabase_secret_key,
            "Authorization": f"Bearer {self._supabase_secret_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            base_url=f"{self._supabase_url}/rest/v1",
            headers=headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post("/rpc/search_knowledge_chunks", json=payload)
            response.raise_for_status()
            rows: list[dict[str, Any]] = list(response.json())

        # Defense-in-depth: re-verify access/domain invariants and recompute RRF in Python.
        safe_rows = [
            row
            for row in rows
            if int(row.get("story_access_level", 0)) <= allowed_story_access_level
            and str(row.get("knowledge_domain", "")) == knowledge_domain
            and str(row.get("metadata", {}).get("visibility", "player")).upper() == "PLAYER"
        ]

        row_by_id = {str(row["chunk_id"]): row for row in safe_rows}
        semantic_ranked = [
            str(row["chunk_id"])
            for row in sorted(
                (r for r in safe_rows if r.get("semantic_rank") is not None),
                key=lambda r: int(r["semantic_rank"]),
            )
        ]
        lexical_ranked = [
            str(row["chunk_id"])
            for row in sorted(
                (r for r in safe_rows if r.get("lexical_rank") is not None),
                key=lambda r: int(r["lexical_rank"]),
            )
        ]
        rank_lists = [lst for lst in (semantic_ranked, lexical_ranked) if lst]
        fused = reciprocal_rank_fusion(rank_lists, k=self._rrf_k) if rank_lists else []

        retrieved: list[RetrievedChunk] = []
        for chunk_id_str, hybrid_score in fused[:top_k]:
            row = row_by_id[chunk_id_str]
            sem_score = float(row.get("semantic_score") or 0.0)
            lex_score = float(row.get("lexical_score") or 0.0)
            if lex_score <= 0.0 and sem_score < min_semantic_score:
                continue
            section_path = tuple(str(part) for part in (row.get("section_path") or []))
            retrieved.append(
                RetrievedChunk(
                    id=UUID(chunk_id_str),
                    content=str(row["content"]),
                    source_title=str(row["source_title"]),
                    source_url=row.get("canonical_url"),
                    document_version=str(row["document_version"]),
                    section_title=section_path[-1] if section_path else None,
                    story_access_level=int(row["story_access_level"]),
                    metadata=dict(row.get("metadata") or {}),
                    hybrid_score=hybrid_score,
                    source_id=str(row["source_key"]),
                    source_span=str(row["source_span"]),
                    section_path=section_path,
                    knowledge_domain=str(row["knowledge_domain"]),
                    canonical_status=str(row.get("canonical_status", "design_approved")),
                    semantic_score=sem_score,
                    lexical_score=lex_score,
                    semantic_rank=row.get("semantic_rank"),
                    lexical_rank=row.get("lexical_rank"),
                )
            )

        return build_evidence_package(
            query=query,
            normalized_query=normalized_query,
            knowledge_domain=knowledge_domain,
            allowed_story_access_level=allowed_story_access_level,
            chunks=retrieved,
            min_semantic_score=min_semantic_score,
        )


def build_evidence_package(
    *,
    query: str,
    normalized_query: str,
    knowledge_domain: str,
    allowed_story_access_level: int,
    chunks: Sequence[RetrievedChunk],
    min_semantic_score: float = 0.55,
) -> EvidencePackage:
    """Convert ranked chunks into an EvidencePackage with traceable Citations."""

    if not chunks:
        return EvidencePackage(
            query=query,
            normalized_query=normalized_query,
            knowledge_domain=knowledge_domain,
            allowed_story_access_level=allowed_story_access_level,
            items=(),
            citations=(),
            is_sufficient=False,
            insufficiency_reason="NO_MATCHING_EVIDENCE",
        )

    # Check if the query asks about a known canonical entity that is absent from all retrieved
    # chunks (e.g., a gated entity like "Người Giữ Im Lặng" or "Điệp Ức" when player level is 0).
    lowered_query = normalized_query.lower()
    combined_text = "\n".join(chunk.content for chunk in chunks).lower()
    for canonical_entity in DEFAULT_SEARCH_ALIASES:
        entity_lower = canonical_entity.lower()
        if entity_lower in lowered_query and entity_lower not in combined_text:
            return EvidencePackage(
                query=query,
                normalized_query=normalized_query,
                knowledge_domain=knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="ENTITY_NOT_IN_AUTHORIZED_EVIDENCE",
            )

    # Check general keyword/semantic coverage so completely unrelated questions fail closed.
    content_tokens = extract_content_tokens(normalized_query)
    combined_tokens = set(_TOKEN_PATTERN.findall(combined_text))
    matched_tokens = [tok for tok in content_tokens if tok in combined_tokens]
    best_semantic = max((chunk.semantic_score for chunk in chunks), default=0.0)
    if content_tokens and not matched_tokens and best_semantic < (min_semantic_score + 0.05):
        return EvidencePackage(
            query=query,
            normalized_query=normalized_query,
            knowledge_domain=knowledge_domain,
            allowed_story_access_level=allowed_story_access_level,
            items=(),
            citations=(),
            is_sufficient=False,
            insufficiency_reason="INSUFFICIENT_RELEVANCE",
        )

    retrieved_at = datetime.now(timezone.utc).isoformat()
    items: list[EvidenceItem] = []
    citations: list[Citation] = []
    for idx, chunk in enumerate(chunks, start=1):
        evidence_id = f"ev_{idx:02d}"
        citation = Citation(
            evidence_id=evidence_id,
            source_id=chunk.source_id,
            source_type=chunk.knowledge_domain,
            title=chunk.source_title,
            document_version=chunk.document_version,
            section_path=chunk.section_path,
            source_span=chunk.source_span,
            url=chunk.source_url,
        )
        items.append(
            EvidenceItem(
                evidence_id=evidence_id,
                chunk_id=chunk.id,
                source_kind="internal_document",
                source_id=chunk.source_id,
                knowledge_domain=chunk.knowledge_domain,
                title=chunk.source_title,
                url=chunk.source_url,
                document_version=chunk.document_version,
                section_path=chunk.section_path,
                source_span=chunk.source_span,
                text=chunk.content,
                story_access_level=chunk.story_access_level,
                canonical_status=chunk.canonical_status,
                trust_tier="canonical" if chunk.knowledge_domain == "game_lore" else "reviewed",
                visibility_scope="player",
                semantic_score=chunk.semantic_score,
                lexical_score=chunk.lexical_score,
                relevance_score=chunk.hybrid_score,
                retrieved_at=retrieved_at,
                citation=citation,
            )
        )
        citations.append(citation)

    return EvidencePackage(
        query=query,
        normalized_query=normalized_query,
        knowledge_domain=knowledge_domain,
        allowed_story_access_level=allowed_story_access_level,
        items=tuple(items),
        citations=tuple(citations),
        is_sufficient=True,
        insufficiency_reason=None,
    )
