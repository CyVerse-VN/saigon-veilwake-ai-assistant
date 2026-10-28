from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v1.conversations import get_turn_service
from app.main import app
from app.modules.knowledge.models import KnowledgeChunk
from app.modules.knowledge.retrieval import (
    InMemoryKnowledgeRetriever,
    SupabaseKnowledgeRetriever,
    normalize_and_expand_query,
)
from app.modules.orchestration.service import InMemoryTurnService
from app.modules.query_router.router import (
    RagEvidenceStatus,
    RouteMode,
    analyze_query,
    evaluate_routing_decision,
)
from app.modules.verification.gate import (
    DraftAnswer,
    VerificationGate,
    run_verified_answer_pipeline,
)


class _DummyQueryEmbedder:
    async def embed_query(self, text: str) -> list[float]:
        lowered = text.lower()
        if "điệp ức" in lowered:
            return [0.0, 1.0] + [0.0] * 766
        if "người giữ im lặng" in lowered:
            return [0.0, 0.0, 1.0] + [0.0] * 765
        return [1.0] + [0.0] * 767


def _sample_chunks() -> list[KnowledgeChunk]:
    return [
        KnowledgeChunk(
            ordinal=0,
            content=(
                "Ký Giới là thế giới siêu nhiên hình thành từ những ký ức đứt mạch. "
                "Người Kể Tiếp là danh xưng cuối mùa của người chơi."
            ),
            section_title="II. Thế giới quan",
            story_access_level=0,
            knowledge_domain="game_lore",
            source_id="game_world_v1_0",
            document_version="1.0",
            source_span="p-0019..p-0020",
            section_path=("II. Thế giới quan",),
            embedding=tuple([1.0] + [0.0] * 767),
        ),
        KnowledgeChunk(
            ordinal=1,
            content=(
                "Điệp Ức là sinh vật hình bướm được hình thành từ ký ức "
                "từng được truyền qua nhiều người."
            ),
            section_title="Điệp Ức — tại sao lại là bướm?",
            story_access_level=1,
            knowledge_domain="game_lore",
            source_id="game_world_v1_0",
            document_version="1.0",
            source_span="p-0068",
            section_path=("VII. Cốt truyện mùa đầu", "Điệp Ức — tại sao lại là bướm?"),
            embedding=tuple([0.0, 1.0] + [0.0] * 766),
        ),
        KnowledgeChunk(
            ordinal=2,
            content="Người Giữ Im Lặng là phản diện phong ấn tất cả ký ức vĩnh viễn.",
            section_title="Phản diện: Người Giữ Im Lặng",
            story_access_level=5,
            knowledge_domain="game_lore",
            source_id="game_world_v1_0",
            document_version="1.0",
            source_span="p-0066",
            section_path=("VII. Cốt truyện mùa đầu", "Phản diện: Người Giữ Im Lặng"),
            embedding=tuple([0.0, 0.0, 1.0] + [0.0] * 765),
        ),
        KnowledgeChunk(
            ordinal=3,
            content="XI. Trạng thái quyết định nội bộ không dành cho người chơi.",
            section_title="XI. Nội bộ",
            story_access_level=0,
            knowledge_domain="game_lore",
            visibility="INTERNAL",
            source_id="game_world_v1_0",
            document_version="1.0",
            source_span="p-0090",
            section_path=("XI. Nội bộ",),
            embedding=tuple([1.0] + [0.0] * 767),
        ),
    ]


@pytest.mark.asyncio
async def test_hybrid_retrieval_enforces_pre_ranking_spoiler_and_visibility_filters() -> None:
    retriever = InMemoryKnowledgeRetriever(_sample_chunks(), embedder=_DummyQueryEmbedder())

    # 1. Public level 0 query succeeds and resolves full citation metadata.
    pkg_lvl0 = await retriever.retrieve(
        "Người Kể Tiếp trong Ký Giới là gì?", allowed_story_access_level=0
    )
    assert pkg_lvl0.is_sufficient is True
    assert len(pkg_lvl0.items) == 1
    assert pkg_lvl0.citations[0].source_id == "game_world_v1_0"
    assert pkg_lvl0.citations[0].document_version == "1.0"
    assert pkg_lvl0.citations[0].source_span == "p-0019..p-0020"
    assert all("XI. Trạng thái quyết định" not in item.text for item in pkg_lvl0.items)

    # 2. Level 1 entity ("Điệp Ức", even via ASCII alias "Diep Uc") is blocked at level 0.
    assert "Điệp Ức" in normalize_and_expand_query("Diep Uc la gi?")
    gated_lvl0 = await retriever.retrieve("Diep Uc la gi?", allowed_story_access_level=0)
    assert gated_lvl0.is_sufficient is False
    assert gated_lvl0.insufficiency_reason == "ENTITY_NOT_IN_AUTHORIZED_EVIDENCE"

    # 3. Unlocking level 1 allows "Điệp Ức" but still blocks level 5 "Người Giữ Im Lặng".
    unlocked_lvl1 = await retriever.retrieve("Điệp Ức là gì?", allowed_story_access_level=1)
    assert unlocked_lvl1.is_sufficient is True
    assert any("Điệp Ức" in item.text for item in unlocked_lvl1.items)

    antagonist_lvl4 = await retriever.retrieve(
        "Người Giữ Im Lặng là ai?", allowed_story_access_level=4
    )
    assert antagonist_lvl4.is_sufficient is False
    assert antagonist_lvl4.insufficiency_reason == "ENTITY_NOT_IN_AUTHORIZED_EVIDENCE"

    antagonist_lvl5 = await retriever.retrieve(
        "Người Giữ Im Lặng là ai?", allowed_story_access_level=5
    )
    assert antagonist_lvl5.is_sufficient is True
    assert any("Người Giữ Im Lặng" in item.text for item in antagonist_lvl5.items)


@pytest.mark.asyncio
async def test_supabase_retriever_calls_rpc_and_rechecks_invariants() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rpc/search_knowledge_chunks")
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["p_allowed_story_access_level"] == 1
        return httpx.Response(
            200,
            json=[
                {
                    "chunk_id": "00000000-0000-0000-0000-000000000010",
                    "document_version_id": "00000000-0000-0000-0000-000000000020",
                    "source_id": "00000000-0000-0000-0000-000000000030",
                    "source_key": "game_world_v1_0",
                    "source_title": "Saigon: Veilwake v1.0",
                    "canonical_url": None,
                    "document_version": "1.0",
                    "source_span": "p-0068",
                    "chunk_ordinal": 35,
                    "section_path": ["VII. Cốt truyện mùa đầu", "Điệp Ức — tại sao lại là bướm?"],
                    "content": "Điệp Ức là sinh vật hình bướm được hình thành từ ký ức.",
                    "knowledge_domain": "game_lore",
                    "story_access_level": 1,
                    "canonical_status": "design_approved",
                    "tags": ["chapter_1"],
                    "metadata": {"visibility": "player"},
                    "semantic_score": 0.88,
                    "lexical_score": 0.45,
                    "semantic_rank": 1,
                    "lexical_rank": 1,
                    "hybrid_score": 0.0327,
                }
            ],
        )

    retriever = SupabaseKnowledgeRetriever(
        supabase_url="https://example.supabase.co",
        supabase_secret_key="secret-key",
        embedder=_DummyQueryEmbedder(),
        transport=httpx.MockTransport(handler),
    )
    pkg = await retriever.retrieve("Điệp Ức là gì?", allowed_story_access_level=1)
    assert pkg.is_sufficient is True
    assert pkg.citations[0].source_span == "p-0068"


@pytest.mark.asyncio
async def test_source_router_rag_first_and_conditional_web_policy() -> None:
    retriever = InMemoryKnowledgeRetriever(_sample_chunks(), embedder=_DummyQueryEmbedder())
    valid_lore_evidence = await retriever.retrieve("Ký Giới là gì?", allowed_story_access_level=0)

    # 1. Lore with sufficient RAG -> INTERNAL, 0 web allowed.
    lore_plan = analyze_query("Ký Giới là gì?", allowed_story_access_level=0)
    lore_decision = evaluate_routing_decision(lore_plan, valid_lore_evidence)
    assert lore_decision.route_mode == RouteMode.INTERNAL
    assert lore_decision.rag_evidence_status == RagEvidenceStatus.SUFFICIENT
    assert lore_decision.web_search_allowed is False

    # 2. Lore missing in RAG -> NEVER falls back to web.
    lore_missing_decision = evaluate_routing_decision(lore_plan, None)
    assert lore_missing_decision.rag_evidence_status == RagEvidenceStatus.INSUFFICIENT
    assert lore_missing_decision.web_search_allowed is False
    assert lore_missing_decision.fallback_reason == "LORE_WEB_FALLBACK_FORBIDDEN"

    # 3. Spoiler-gated lore -> ACCESS_RESTRICTED, NEVER falls back to web.
    spoiler_plan = analyze_query("Người Giữ Im Lặng là ai?", allowed_story_access_level=0)
    spoiler_decision = evaluate_routing_decision(spoiler_plan, None, allowed_story_access_level=0)
    assert spoiler_decision.route_mode == RouteMode.GAME_LORE_GATED
    assert spoiler_decision.rag_evidence_status == RagEvidenceStatus.ACCESS_RESTRICTED
    assert spoiler_decision.web_search_allowed is False

    # 4. Real-world / dynamic_web allows web ONLY when RAG is insufficient or stale.
    rw_plan = analyze_query("Bưu điện Trung tâm Sài Gòn có kiến trúc gì?")
    rw_missing = evaluate_routing_decision(rw_plan, None)
    assert rw_missing.route_mode == RouteMode.WEB_FALLBACK
    assert rw_missing.web_search_allowed is True

    # Retrieval infrastructure error never opens web search bypass.
    rw_error = evaluate_routing_decision(rw_plan, None, retrieval_error=True)
    assert rw_error.rag_evidence_status == RagEvidenceStatus.RETRIEVAL_ERROR
    assert rw_error.web_search_allowed is False

    # 5. Game state and ambiguous domain never call web.
    gs_plan = analyze_query("Tôi có nhận được mảnh ký ức chưa?")
    gs_decision = evaluate_routing_decision(gs_plan, None)
    assert gs_decision.route_mode == RouteMode.GAME_STATE
    assert gs_decision.web_search_allowed is False

    ambiguous_plan = analyze_query("Chỗ đó có gì hay?")
    ambiguous_decision = evaluate_routing_decision(ambiguous_plan, None)
    assert ambiguous_decision.route_mode == RouteMode.NEEDS_CLARIFICATION
    assert ambiguous_decision.web_search_allowed is False


class _ScriptedGenerator:
    def __init__(self, drafts: list[DraftAnswer]) -> None:
        self._drafts = list(drafts)
        self.calls = 0

    async def generate_draft(
        self,
        query: str,
        evidence: Any,
        *,
        content_mode: str = "FACTUAL_GROUNDED",
        revision_issues: tuple[str, ...] = (),
    ) -> DraftAnswer:
        idx = min(self.calls, len(self._drafts) - 1)
        self.calls += 1
        return self._drafts[idx]


class _ScriptedVerifier:
    def __init__(self, reports: list[dict[str, Any]]) -> None:
        self._reports = list(reports)
        self.calls = 0

    async def verify_draft(
        self,
        query: str,
        draft: DraftAnswer,
        evidence: Any,
        *,
        allowed_story_access_level: int = 0,
    ) -> dict[str, Any]:
        idx = min(self.calls, len(self._reports) - 1)
        self.calls += 1
        return self._reports[idx]


@pytest.mark.asyncio
async def test_verification_gate_revises_once_and_overrides_false_pass() -> None:
    retriever = InMemoryKnowledgeRetriever(_sample_chunks(), embedder=_DummyQueryEmbedder())
    evidence = await retriever.retrieve("Ký Giới là gì?", allowed_story_access_level=0)
    gate = VerificationGate()

    # Attempt 0: Verifier falsely says PASS even though c2 is UNSUPPORTED.
    # Gate must override to REVISE, then Attempt 1 succeeds.
    generator = _ScriptedGenerator(
        [
            DraftAnswer(
                text="Ký Giới hình thành từ ký ức đứt mạch và có 100 chương.",
                cited_evidence_ids=("ev_01",),
            ),
            DraftAnswer(
                text="Ký Giới là thế giới siêu nhiên hình thành từ những ký ức đứt mạch.",
                cited_evidence_ids=("ev_01",),
            ),
        ]
    )
    verifier = _ScriptedVerifier(
        [
            {
                "schema_version": "1.0",
                "decision": "PASS",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "Ký Giới hình thành từ ký ức đứt mạch",
                        "status": "SUPPORTED",
                        "supporting_evidence_ids": ["ev_01"],
                    },
                    {
                        "claim_id": "c2",
                        "text": "Có 100 chương",
                        "status": "UNSUPPORTED",
                        "supporting_evidence_ids": [],
                    },
                ],
                "issues": ["c2 không có trong evidence"],
                "recommended_action": "REMOVE_UNSUPPORTED_CLAIM",
            },
            {
                "schema_version": "1.0",
                "decision": "PASS",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "Ký Giới hình thành từ những ký ức đứt mạch",
                        "status": "SUPPORTED",
                        "supporting_evidence_ids": ["ev_01"],
                    }
                ],
                "issues": [],
                "recommended_action": "APPROVE",
            },
        ]
    )

    outcome = await run_verified_answer_pipeline(
        "Ký Giới là gì?",
        evidence,
        generator=generator,
        verifier=verifier,
        gate=gate,
        allowed_story_access_level=0,
    )
    assert outcome.passed is True
    assert outcome.revision_count == 1
    assert outcome.verification_status == "PASS"
    assert outcome.approved_answer is not None
    assert "100 chương" not in outcome.text


def test_v1_api_verified_turn_and_voice_integration() -> None:
    retriever = InMemoryKnowledgeRetriever(_sample_chunks(), embedder=_DummyQueryEmbedder())
    generator = _ScriptedGenerator(
        [
            DraftAnswer(
                text="Ký Giới là thế giới siêu nhiên hình thành từ những ký ức đứt mạch.",
                cited_evidence_ids=("ev_01",),
            )
        ]
    )
    verifier = _ScriptedVerifier(
        [
            {
                "schema_version": "1.0",
                "decision": "PASS",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": (
                            "Ký Giới là thế giới siêu nhiên hình thành từ những ký ức đứt mạch."
                        ),
                        "status": "SUPPORTED",
                        "supporting_evidence_ids": ["ev_01"],
                    }
                ],
                "issues": [],
                "recommended_action": "APPROVE",
            }
        ]
    )

    class _FakeStt:
        def transcribe(self, audio_bytes: bytes, *, mime_type: str = "audio/wav") -> str:
            return "Ký Giới là gì?"

    class _FakeTts:
        def __init__(self) -> None:
            self.synthesized_texts: list[str] = []

        def synthesize(self, text: str) -> bytes:
            self.synthesized_texts.append(text)
            return b"RIFF_VERIFIED_WAV"

    tts = _FakeTts()
    verified_service = InMemoryTurnService(
        retriever=retriever,
        generator=generator,
        verifier=verifier,
        stt_provider=_FakeStt(),
        tts_provider=tts,
    )

    app.dependency_overrides[get_turn_service] = lambda: verified_service
    try:
        client = TestClient(app)
        conv_id = client.post("/v1/conversations").json()["conversation_id"]

        # 1. Verified Voice Turn -> STT -> RAG -> Generator -> Verifier -> Gate -> TTS
        voice_resp = client.post(
            f"/v1/conversations/{conv_id}/voice-turns",
            files={"audio": ("question.wav", b"RIFF_INPUT_WAV", "audio/wav")},
            data={"response_mode": "text_and_voice", "story_access_level": "0"},
        )
        assert voice_resp.status_code == 202
        turn_data = client.get(voice_resp.json()["status_url"]).json()
        assert turn_data["status"] == "COMPLETED"
        assert turn_data["input"]["transcript"] == "Ký Giới là gì?"
        assert turn_data["answer"]["verification_status"] == "PASS"
        assert turn_data["answer"]["citations"][0]["source_span"] == "p-0019..p-0020"
        assert turn_data["audio"]["status"] == "READY"

        audio_resp = client.get(turn_data["audio"]["endpoint"])
        assert audio_resp.status_code == 200
        assert audio_resp.content == b"RIFF_VERIFIED_WAV"
        assert len(tts.synthesized_texts) == 1

        # 2. Spoiler-blocked turn must NOT call TTS or expose audio.
        spoiler_resp = client.post(
            f"/v1/conversations/{conv_id}/turns",
            json={
                "message": "Người Giữ Im Lặng là ai?",
                "story_access_level": 0,
                "response_mode": "text_and_voice",
            },
        )
        assert spoiler_resp.status_code == 202
        spoiler_turn = client.get(spoiler_resp.json()["status_url"]).json()
        assert spoiler_turn["status"] == "COMPLETED_TEXT_ONLY"
        assert spoiler_turn["answer"]["verification_status"] == "FALLBACK"
        assert spoiler_turn["audio"]["status"] == "UNAVAILABLE"
        assert client.get(f"/v1/turns/{spoiler_resp.json()['turn_id']}/audio").status_code == 404
        assert len(tts.synthesized_texts) == 1
    finally:
        app.dependency_overrides.pop(get_turn_service, None)


def test_web_search_eligibility_ssrf_and_conversation_memory_integration() -> None:
    from app.modules.conversation.memory import ConversationMemoryStore
    from app.modules.web_search.service import (
        WebSearchResult,
        WebSearchService,
        sanitize_public_search_query,
    )

    # 1. Query sanitizer strips private lore terms before external search.
    sanitized = sanitize_public_search_query("Nhà thờ Đức Bà trong Ký Giới và Điệp Ức")
    assert "Ký Giới" not in sanitized
    assert "Điệp Ức" not in sanitized
    assert "Nhà thờ Đức Bà" in sanitized

    class _FakeWebProvider:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def search(
            self,
            query: str,
            *,
            freshness: str | None = None,
            locale: str = "vi-VN",
            max_results: int = 5,
        ) -> list[WebSearchResult]:
            self.queries.append(query)
            return [
                # Unsafe URLs must be filtered out by SSRF guard.
                WebSearchResult(
                    title="Internal Admin",
                    url="http://127.0.0.1:8000/secret",
                    snippet="Should never be included.",
                ),
                WebSearchResult(
                    title="Nhà thờ chính tòa Đức Bà Sài Gòn",
                    url="https://vi.wikipedia.org/wiki/Nha_tho_Duc_Ba_Sai_Gon",
                    snippet=(
                        "Nhà thờ chính tòa Đức Bà Sài Gòn là nhà thờ Công giáo nằm tại "
                        "Công trường Công xã Paris, Quận 1, Thành phố Hồ Chí Minh."
                    ),
                    publisher="vi.wikipedia.org",
                ),
            ]

    retriever = InMemoryKnowledgeRetriever(_sample_chunks(), embedder=_DummyQueryEmbedder())
    fake_web_provider = _FakeWebProvider()
    web_service = WebSearchService(fake_web_provider)
    memory = ConversationMemoryStore()

    generator = _ScriptedGenerator(
        [
            DraftAnswer(
                text=(
                    "Nhà thờ chính tòa Đức Bà Sài Gòn nằm tại Công trường Công xã Paris, "
                    "Quận 1, Thành phố Hồ Chí Minh."
                ),
                cited_evidence_ids=("ev_01",),
            )
        ]
    )
    verifier = _ScriptedVerifier(
        [
            {
                "schema_version": "1.0",
                "decision": "PASS",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": (
                            "Nhà thờ chính tòa Đức Bà Sài Gòn nằm tại Công trường Công xã Paris, "
                            "Quận 1, Thành phố Hồ Chí Minh."
                        ),
                        "status": "SUPPORTED",
                        "supporting_evidence_ids": ["ev_01"],
                    }
                ],
                "issues": [],
                "recommended_action": "APPROVE",
            }
        ]
    )

    verified_service = InMemoryTurnService(
        retriever=retriever,
        generator=generator,
        verifier=verifier,
        web_search_service=web_service,
        memory=memory,
    )

    app.dependency_overrides[get_turn_service] = lambda: verified_service
    try:
        client = TestClient(app)
        conv_id = client.post("/v1/conversations").json()["conversation_id"]

        # Turn 1: Ask about "Nhà thờ Đức Bà" -> RAG has 0 real_world chunks -> Web Fallback runs.
        t1_resp = client.post(
            f"/v1/conversations/{conv_id}/turns",
            json={"message": "Nhà thờ Đức Bà ở đâu?", "response_mode": "text"},
        )
        assert t1_resp.status_code == 202
        t1 = client.get(t1_resp.json()["status_url"]).json()
        assert t1["answer"]["verification_status"] == "PASS"
        assert t1["answer"]["citations"][0]["source_type"] == "web_search"
        assert "wikipedia.org" in t1["answer"]["citations"][0]["url"]
        assert len(fake_web_provider.queries) == 1

        # Turn 2: Follow-up "Giới thiệu về địa điểm đó" -> Memory resolves "địa điểm đó"
        # to "Nhà thờ Đức Bà" and answers with PASS!
        t2_resp = client.post(
            f"/v1/conversations/{conv_id}/turns",
            json={"message": "Giới thiệu về địa điểm đó", "response_mode": "text"},
        )
        assert t2_resp.status_code == 202
        t2 = client.get(t2_resp.json()["status_url"]).json()
        assert t2["answer"]["verification_status"] == "PASS"
        assert "Nhà thờ chính tòa Đức Bà Sài Gòn" in t2["answer"]["text"]
        assert t2["answer"]["citations"][0]["source_type"] == "web_search"
    finally:
        app.dependency_overrides.pop(get_turn_service, None)


def test_build_turn_service_wires_elevenlabs_tts_and_verified_providers() -> None:
    from app.core.config import Settings
    from app.modules.orchestration.service import build_turn_service

    cfg = Settings(
        _env_file=None,
        supabase_url="https://example.supabase.co",
        supabase_secret_key="sb-secret",
        gemini_api_key="gem-key",
        groq_generator_api_key="groq-key",
        groq_generator_model="openai/gpt-oss-120b",
        elevenlabs_api_key="sk_el_key",
        elevenlabs_tts_voice_id="voice_abc",
    )
    svc = build_turn_service(cfg)
    assert svc._retriever is not None
    assert svc._generator is not None
    assert svc._verifier is not None
    assert svc._web_search_service is not None
    assert svc._stt_provider is not None
    assert svc._tts_provider is not None
