"""Synthetic provider fixtures exercise coverage without using live quotas or secrets."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v1.conversations import get_turn_service
from app.core.config import Settings
from app.main import create_app
from app.modules.conversation.landmark_context import resolve_landmark_query
from app.modules.conversation.landmarks import LANDMARK_NAMES
from app.modules.conversation.memory import ConversationMemoryStore
from app.modules.knowledge.models import EvidencePackage
from app.modules.orchestration.service import InMemoryTurnService, build_turn_service
from app.modules.query_router.router import RouteMode, analyze_query
from app.modules.verification.gate import DraftAnswer
from app.modules.web_search.service import (
    BraveWebSearchProvider,
    WebSearchResult,
    WebSearchService,
    WikipediaViWebSearchProvider,
    _extract_infobox_facts,
    sanitize_public_search_query,
)
from app.schemas.conversation import ResponseMode


@pytest.mark.parametrize("place_id", LANDMARK_NAMES)
def test_all_mobile_landmarks_supply_first_question_context(place_id):
    resolved = resolve_landmark_query("Địa chỉ ở đâu?", place_id)
    assert LANDMARK_NAMES[place_id] in resolved
    assert analyze_query(resolved).knowledge_domain == "real_world"


@pytest.mark.parametrize("question", [
    "Địa chỉ?", "Bao nhiêu tuổi?", "Ai thiết kế?", "Có gì thú vị?",
    "Mấy giờ mở cửa?", "Giá vé bao nhiêu?", "Kien truc the nao?", "Xay nam nao?",
])
def test_short_questions_keep_selected_location_and_conversation_topic(question):
    assert "Đức Bà" in resolve_landmark_query(question, "cathedral")
    memory = ConversationMemoryStore()
    memory.append_turn(
        "conv_test", turn_id="turn_1", user_query="Giới thiệu Chợ Bình Tây",
        resolved_query="Giới thiệu Chợ Bình Tây", answer_text="", knowledge_domain="real_world",
        verification_status="FALLBACK",
    )
    assert "Chợ Bình Tây" in memory.rewrite_query("conv_test", question)


@pytest.mark.parametrize("question", [
    "Lịch sử Chợ Bình Tây?", "Giới thiệu về Tháp Eiffel", "Tôi đã hoàn thành nhiệm vụ chưa?",
])
def test_context_does_not_replace_explicit_subject_or_game_state(question):
    assert resolve_landmark_query(question, "cathedral") == question


@pytest.mark.parametrize("question", ["Chào Vara!", "Xin chao Vara", "Cảm ơn Vara"])
def test_vara_greetings_are_direct(question):
    assert analyze_query(question).route_mode == RouteMode.DIRECT


def test_public_questions_and_unaccented_lore_have_distinct_routes():
    assert analyze_query("Gợi ý món ăn đặc sản Sài Gòn").knowledge_domain == "real_world"
    assert analyze_query("Ky Gioi la gi?").knowledge_domain == "game_lore"
    assert analyze_query("Nguoi Giu Im Lang la ai?").route_mode == RouteMode.GAME_LORE_GATED
    assert analyze_query("Nó có gì thú vị?").route_mode == RouteMode.NEEDS_CLARIFICATION
    assert analyze_query("Ai thiết kế?").route_mode == RouteMode.NEEDS_CLARIFICATION
    assert analyze_query("Toi da co bao nhieu huy hieu?").route_mode == RouteMode.GAME_STATE


def test_infobox_date_templates_preserve_the_year():
    facts = _extract_infobox_facts("| construction_start_date = {{start date|1877|10|7}}")
    assert "1877" in facts


@pytest.mark.asyncio
async def test_wikipedia_retries_an_article_outage_instead_of_caching_empty_evidence():
    detail_calls = 0

    def handle(request):
        nonlocal detail_calls
        if request.url.params.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [{"title": "Bưu điện"}]}})
        detail_calls += 1
        if detail_calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"query": {"pages": {"1": {
            "title": "Bưu điện", "fullurl": "https://vi.wikipedia.org/wiki/Test",
            "extract": "Dữ kiện thử nghiệm.",
        }}}})

    provider = WikipediaViWebSearchProvider(transport=httpx.MockTransport(handle))
    results = await provider.search("Bưu điện", max_results=1)
    assert len(results) == 1
    assert detail_calls == 2


def test_search_keeps_question_aspect_instead_of_only_parenthesized_topic():
    address = sanitize_public_search_query("Địa chỉ ở đâu? (Nhà thờ Đức Bà Sài Gòn)")
    date = sanitize_public_search_query("Xây dựng năm nào? (Nhà thờ Đức Bà Sài Gòn)")
    assert "Địa chỉ" in address
    assert "năm nào" in date
    assert address != date
    assert "Đức Bà" in address and "Đức Bà" in date


@pytest.mark.asyncio
async def test_wikipedia_uses_valid_single_page_extracts_and_finds_late_details():
    requests = []

    def handle(request):
        params = request.url.params
        requests.append(params)
        if params.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [
                {"title": "Nhà thờ Đức Bà Sài Gòn", "snippet": "Nhà thờ."},
                {"title": "Trang phụ", "snippet": "Bài khác."},
            ]}})
        assert "exchars" not in params
        assert "|" not in params["titles"]
        title = params["titles"]
        text = ("Đoạn giới thiệu tổng quát về công trình.\n" * 150
                + "Khởi công năm 1877 và khánh thành năm 1880.\n")
        return httpx.Response(200, json={"query": {"pages": {title: {
            "title": title, "fullurl": "https://vi.wikipedia.org/wiki/" + title,
            "extract": text,
        }}}})

    provider = WikipediaViWebSearchProvider(transport=httpx.MockTransport(handle))
    results = await provider.search("Xây dựng năm nào? Nhà thờ Đức Bà Sài Gòn")
    assert requests[0]["srsearch"] == "Nhà thờ Đức Bà Sài Gòn"
    assert len(results) == 2
    assert all("1877" in result.snippet for result in results)
    assert all(len(result.snippet) <= 2800 for result in results)


@pytest.mark.asyncio
async def test_wikipedia_is_not_a_fresh_prices_or_hours_source():
    def handle(request):
        pytest.fail("Time-sensitive questions must not use encyclopedia revision dates")

    provider = WikipediaViWebSearchProvider(transport=httpx.MockTransport(handle))
    assert await provider.search("Giá vé hôm nay", freshness="day") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("search_has_hits", [True, False])
async def test_wikipedia_keeps_redirected_canonical_articles(search_has_hits):
    def handle(request):
        if request.url.params.get("list") == "search":
            hits = [{"title": "Nhà thờ Đức Bà Sài Gòn"}] if search_has_hits else []
            return httpx.Response(200, json={"query": {"search": hits}})
        return httpx.Response(200, json={"query": {"pages": {"1": {
            "title": "Nhà thờ chính tòa Đức Bà Sài Gòn",
            "fullurl": "https://vi.wikipedia.org/wiki/Canonical",
            "extract": "Nhà thờ khánh thành năm 1880.",
        }}}})

    provider = WikipediaViWebSearchProvider(transport=httpx.MockTransport(handle))
    results = await provider.search("Nhà thờ Đức Bà Sài Gòn xây năm nào?", max_results=1)
    assert len(results) == 1
    assert results[0].title == "Nhà thờ chính tòa Đức Bà Sài Gòn"
    assert "1880" in results[0].snippet


@pytest.mark.asyncio
async def test_brave_translates_shared_freshness_to_provider_format():
    def handle(request):
        assert request.url.params["freshness"] == "pd"
        return httpx.Response(200, json={"web": {"results": []}})

    provider = BraveWebSearchProvider("fixture-key", transport=httpx.MockTransport(handle))
    assert await provider.search("Thời tiết Sài Gòn", freshness="day") == []


class EmptyRetriever:
    async def retrieve(self, query, **kwargs):
        return EvidencePackage(query, query, kwargs["knowledge_domain"], 0, (), (), False)


class BrokenRetriever:
    async def retrieve(self, query, **kwargs):
        raise RuntimeError("synthetic internal outage")


class FixtureWebProvider:
    def __init__(self, *, empty=False):
        self.calls = []
        self.empty = empty

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs.get("freshness")))
        return [] if self.empty else [WebSearchResult(
            "Synthetic source", "https://example.org/fixture", "Dữ kiện du lịch thử nghiệm."
        )]


class EvidenceEchoGenerator:
    def __init__(self):
        self.calls = []

    async def generate_draft(self, query, evidence, **kwargs):
        self.calls.append((query, evidence))
        return DraftAnswer(evidence.items[0].text, (evidence.items[0].evidence_id,))


class FixtureVerifier:
    async def verify_draft(self, query, draft, evidence, **kwargs):
        return {"decision": "PASS", "claims": [{
            "claim_id": "c1", "text": draft.text, "status": "SUPPORTED",
            "supporting_evidence_ids": list(draft.cited_evidence_ids),
        }]}


async def ask(service, query, *, location_id=None, conversation=None):
    cid = conversation or service.create_conversation()
    tid = service.accept_turn(cid, "text", query, ResponseMode.TEXT, location_id=location_id)
    await service.process_verified_turn(tid)
    return service.get_turn(tid), tid


@pytest.mark.asyncio
async def test_public_question_survives_rag_outage_but_keeps_verification_and_citations():
    web = FixtureWebProvider()
    service = InMemoryTurnService(
        retriever=BrokenRetriever(), generator=EvidenceEchoGenerator(), verifier=FixtureVerifier(),
        web_search_service=WebSearchService(web),
    )
    response, tid = await ask(service, "Ai thiết kế?", location_id="post_office")
    assert response.answer.verification_status == "PASS"
    assert response.answer.citations[0]["url"] == "https://example.org/fixture"
    assert "Bưu điện" in web.calls[0][0]
    assert service._turns[tid].routing_decision.web_search_used


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "Ky Gioi la gi?", "Nguoi Giu Im Lang la ai?", "Tiến trình của tôi?", "Chỗ đó có gì hay?",
])
async def test_lore_state_and_ambiguous_questions_never_search_public_web(question):
    web = FixtureWebProvider()
    generator = EvidenceEchoGenerator()
    service = InMemoryTurnService(
        retriever=BrokenRetriever(), generator=generator, verifier=FixtureVerifier(),
        web_search_service=WebSearchService(web),
    )
    response, _ = await ask(service, question)
    assert response.answer.verification_status == "FALLBACK"
    assert web.calls == []
    assert generator.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_web", [True, False])
async def test_current_hours_do_not_fall_back_to_static_internal_evidence(with_web):
    old = WebSearchService._normalize_results(
        original_query="", sanitized_query="", knowledge_domain="dynamic_web",
        allowed_story_access_level=0, results=[WebSearchResult(
            "Old schedule", "https://example.org/old", "Giờ mở cửa cũ."
        )],
    )

    class OldRetriever:
        async def retrieve(self, query, **kwargs):
            return old

    web = FixtureWebProvider(empty=True)
    generator = EvidenceEchoGenerator()
    service = InMemoryTurnService(
        retriever=OldRetriever(), generator=generator, verifier=FixtureVerifier(),
        web_search_service=WebSearchService(web) if with_web else None,
    )
    response, _ = await ask(service, "Mấy giờ mở cửa?", location_id="ben_thanh")
    assert response.answer.verification_status == "FALLBACK"
    assert generator.calls == []
    if with_web:
        assert web.calls[0][1] == "day"


@pytest.mark.asyncio
async def test_web_recovery_after_unsupported_rag_answer_records_actual_source():
    internal = WebSearchService._normalize_results(
        original_query="", sanitized_query="", knowledge_domain="real_world",
        allowed_story_access_level=0, results=[WebSearchResult(
            "Synthetic internal fixture", "https://example.org/internal", "Thông tin cũ."
        )],
    )

    class Retriever:
        async def retrieve(self, query, **kwargs):
            return internal

    class Verifier(FixtureVerifier):
        async def verify_draft(self, query, draft, evidence, **kwargs):
            report = await super().verify_draft(query, draft, evidence, **kwargs)
            if evidence.items[0].url.endswith("internal"):
                report["decision"] = "REJECT"
                report["claims"][0]["status"] = "UNSUPPORTED"
            return report

    web = FixtureWebProvider()
    service = InMemoryTurnService(
        retriever=Retriever(), generator=EvidenceEchoGenerator(), verifier=Verifier(),
        web_search_service=WebSearchService(web),
    )
    response, tid = await ask(service, "Nhà thờ Đức Bà xây năm nào?")
    assert response.answer.verification_status == "PASS"
    assert response.answer.citations[0]["url"].endswith("fixture")
    assert service._turns[tid].routing_decision.web_search_used
    assert len(web.calls) == 1


def test_text_public_pipeline_does_not_require_stt_or_embedding_credentials():
    service = build_turn_service(Settings(
        _env_file=None, groq_generator_api_key="fixture-key", groq_generator_model="fixture-model",
        gemini_api_key=None, supabase_url=None, supabase_secret_key=None,
    ))
    assert service._generator is not None
    assert service._verifier is not None
    assert service._web_search_service is not None
    assert service._stt_provider is None


def test_mobile_text_and_voice_api_keep_new_place_context_and_followups(wav_path):
    class Stt:
        def transcribe(self, audio_bytes, **kwargs):
            return "Ai thiết kế?"

    generator = EvidenceEchoGenerator()
    service = InMemoryTurnService(
        retriever=EmptyRetriever(), generator=generator, verifier=FixtureVerifier(),
        web_search_service=WebSearchService(FixtureWebProvider()), stt_provider=Stt(),
    )
    application = create_app()
    application.dependency_overrides[get_turn_service] = lambda: service
    with TestClient(application) as client:
        cid = client.post("/v1/conversations").json()["conversation_id"]
        for route, kwargs in [
            ("turns", {"json": {
                "message": "Địa chỉ?", "location_id": "opera_house", "response_mode": "text",
            }}),
            ("voice-turns", {
                "files": {"audio": ("question.wav", wav_path.read_bytes(), "audio/wav")},
                "data": {"location_id": "opera_house", "response_mode": "text"},
            }),
        ]:
            accepted = client.post(f"/v1/conversations/{cid}/{route}", **kwargs)
            assert accepted.status_code == 202
            result = client.get(accepted.json()["status_url"]).json()
            assert result["answer"]["verification_status"] == "PASS"
            assert result["location_context"]["location_id"] == "opera_house"
            assert result["location_context"]["visual_verified"] is False
        assert result["input"]["transcript"] == "Ai thiết kế?"
        assert all("Nhà hát" in query for query, _ in generator.calls)
