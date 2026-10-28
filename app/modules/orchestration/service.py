from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from app.core.config import Settings, get_settings
from app.modules.conversation.memory import ConversationMemoryStore, normalize_stt_transcript
from app.modules.knowledge.models import EvidencePackage
from app.modules.query_router.router import (
    RagEvidenceStatus,
    RouteMode,
    RoutingDecision,
    analyze_query,
    evaluate_routing_decision,
    is_exclusive_lore_query,
)
from app.modules.verification.gate import (
    CLARIFICATION_FALLBACK_TEXT,
    DIRECT_GREETING_TEXT,
    GAME_STATE_UNAVAILABLE_TEXT,
    SAFE_FALLBACK_TEXT,
    SPOILER_FALLBACK_TEXT,
    ClaimVerifier,
    GroundedGenerator,
    VerificationGate,
    run_verified_answer_pipeline,
)
from app.modules.web_search.service import WebSearchService, merge_evidence_packages
from app.schemas.conversation import (
    Answer,
    Audio,
    AudioStatus,
    ResponseMode,
    TurnInput,
    TurnResponse,
    TurnStatus,
)

SAFE_FALLBACK = SAFE_FALLBACK_TEXT


class KnowledgeRetrieverProtocol(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        allowed_story_access_level: int = 0,
        knowledge_domain: str = "game_lore",
        language: str = "vi",
        candidate_limit: int = 20,
        top_k: int = 8,
    ) -> EvidencePackage: ...


class SttAdapterProtocol(Protocol):
    def transcribe(self, audio_bytes: bytes, *, mime_type: str = "audio/wav") -> str: ...


class TtsAdapterProtocol(Protocol):
    def synthesize(self, text: str) -> bytes: ...


class WebSearchAdapterProtocol(Protocol):
    async def search(self, query: str) -> EvidencePackage: ...


@dataclass
class TurnRecord:
    conversation_id: str
    input_type: str
    transcript: str | None
    response_mode: ResponseMode
    story_access_level: int = 0
    audio_input_bytes: bytes | None = None
    audio_input_mime: str = "audio/wav"
    status: TurnStatus = TurnStatus.PENDING
    answer_id: str | None = None
    answer_text: str = SAFE_FALLBACK
    content_mode: str = "FACTUAL_GROUNDED"
    verification_status: str = "FALLBACK"
    citations: list[dict[str, Any]] = field(default_factory=list)
    audio_output_bytes: bytes | None = None
    routing_decision: RoutingDecision | None = None


class InMemoryTurnService:
    """Turn orchestrator supporting both fail-closed skeleton and full verified RAG+Voice flow."""

    def __init__(
        self,
        *,
        retriever: KnowledgeRetrieverProtocol | None = None,
        generator: GroundedGenerator | None = None,
        verifier: ClaimVerifier | None = None,
        stt_provider: SttAdapterProtocol | None = None,
        tts_provider: TtsAdapterProtocol | None = None,
        web_search_provider: WebSearchAdapterProtocol | None = None,
        web_search_service: WebSearchService | None = None,
        memory: ConversationMemoryStore | None = None,
        gate: VerificationGate | None = None,
    ) -> None:
        self._conversations: set[str] = set()
        self._turns: dict[str, TurnRecord] = {}
        self._retriever = retriever
        self._generator = generator
        self._verifier = verifier
        self._stt_provider = stt_provider
        self._tts_provider = tts_provider
        self._web_search_provider = web_search_provider
        self._web_search_service = web_search_service
        self._memory = memory or ConversationMemoryStore()
        self._gate = gate or VerificationGate()

    @property
    def gate(self) -> VerificationGate:
        return self._gate

    def create_conversation(self) -> str:
        conversation_id = f"conv_{uuid4().hex}"
        self._conversations.add(conversation_id)
        return conversation_id

    def require_conversation(self, conversation_id: str) -> None:
        if conversation_id not in self._conversations:
            raise KeyError(conversation_id)

    def accept_turn(
        self,
        conversation_id: str,
        input_type: str,
        transcript: str | None,
        response_mode: ResponseMode,
        *,
        story_access_level: int = 0,
        audio_input_bytes: bytes | None = None,
        audio_input_mime: str = "audio/wav",
    ) -> str:
        self.require_conversation(conversation_id)
        turn_id = f"turn_{uuid4().hex}"
        self._turns[turn_id] = TurnRecord(
            conversation_id=conversation_id,
            input_type=input_type,
            transcript=transcript,
            response_mode=response_mode,
            story_access_level=story_access_level,
            audio_input_bytes=audio_input_bytes,
            audio_input_mime=audio_input_mime,
        )
        return turn_id

    def process_mock_turn(self, turn_id: str) -> None:
        """Process turn synchronously; uses verified pipeline if configured, else fail-closed."""

        if (
            self._retriever is not None
            and self._generator is not None
            and self._verifier is not None
        ):
            asyncio.run(self.process_verified_turn(turn_id))
            return

        record = self._turns[turn_id]
        record.status = TurnStatus.VERIFYING
        record.answer_id = f"ans_{turn_id}"
        record.answer_text = SAFE_FALLBACK
        record.verification_status = "FALLBACK"
        record.status = (
            TurnStatus.COMPLETED_TEXT_ONLY
            if record.response_mode == ResponseMode.TEXT_AND_VOICE
            else TurnStatus.COMPLETED
        )

    async def process_verified_turn(self, turn_id: str) -> None:
        """Execute STT -> Router -> RAG-first -> Generator -> Verifier -> Gate -> Verified TTS."""

        record = self._turns[turn_id]
        wants_audio = record.response_mode == ResponseMode.TEXT_AND_VOICE

        # 1. Optional Voice Input -> STT
        if record.input_type == "voice" and not record.transcript:
            if self._stt_provider is None or not record.audio_input_bytes:
                self._complete_with_fallback(record, turn_id, SAFE_FALLBACK_TEXT)
                return
            record.status = TurnStatus.TRANSCRIBING
            try:
                transcript = await asyncio.to_thread(
                    self._stt_provider.transcribe,
                    record.audio_input_bytes,
                    mime_type=record.audio_input_mime,
                )
            except Exception:
                self._complete_with_fallback(record, turn_id, SAFE_FALLBACK_TEXT)
                return
            record.transcript = normalize_stt_transcript(transcript or "") or None
            if not record.transcript:
                self._complete_with_fallback(record, turn_id, SAFE_FALLBACK_TEXT)
                return

        raw_query = normalize_stt_transcript(record.transcript or "")
        if not raw_query:
            self._complete_with_fallback(record, turn_id, SAFE_FALLBACK_TEXT)
            return

        # 2. Multi-turn coreference resolution + Query Analysis & Source Routing
        record.status = TurnStatus.ROUTING
        query = self._memory.rewrite_query(record.conversation_id, raw_query)
        plan = analyze_query(query, allowed_story_access_level=record.story_access_level)

        if plan.route_mode == RouteMode.DIRECT:
            record.routing_decision = evaluate_routing_decision(
                plan, None, allowed_story_access_level=record.story_access_level
            )
            approved = self._gate.register_direct_approved_answer(DIRECT_GREETING_TEXT)
            await self._finalize_approved_turn(record, approved.approved_answer_id, wants_audio)
            return

        if plan.route_mode in {
            RouteMode.GAME_LORE_GATED,
            RouteMode.NEEDS_CLARIFICATION,
            RouteMode.GAME_STATE,
        }:
            decision = evaluate_routing_decision(
                plan, None, allowed_story_access_level=record.story_access_level
            )
            record.routing_decision = decision
            fallback_map = {
                RouteMode.GAME_LORE_GATED: SPOILER_FALLBACK_TEXT,
                RouteMode.NEEDS_CLARIFICATION: CLARIFICATION_FALLBACK_TEXT,
                RouteMode.GAME_STATE: GAME_STATE_UNAVAILABLE_TEXT,
            }
            self._complete_with_fallback(record, turn_id, fallback_map[plan.route_mode])
            self._memory.append_turn(
                record.conversation_id,
                turn_id=turn_id,
                user_query=raw_query,
                resolved_query=query,
                answer_text=record.answer_text,
                knowledge_domain=plan.knowledge_domain,
                verification_status=record.verification_status,
            )
            return

        if self._retriever is None or self._generator is None or self._verifier is None:
            self._complete_with_fallback(record, turn_id, SAFE_FALLBACK_TEXT)
            return

        # 3. Internal RAG Retrieval (first priority)
        record.status = TurnStatus.RETRIEVING
        try:
            evidence = await self._retriever.retrieve(
                query,
                allowed_story_access_level=record.story_access_level,
                knowledge_domain=plan.knowledge_domain,
            )
            retrieval_error = False
        except Exception:
            evidence = None
            retrieval_error = True

        decision = evaluate_routing_decision(
            plan,
            evidence,
            allowed_story_access_level=record.story_access_level,
            retrieval_error=retrieval_error,
        )

        can_try_public_web = (
            self._web_search_service is not None
            and not retrieval_error
            and not is_exclusive_lore_query(query)
        )
        web_searched = False

        # 4. Conditional Web Search fallback ONLY when gate/non-lore rules allow it.
        if (decision.web_search_allowed and decision.public_web_subquery) or (
            can_try_public_web and (evidence is None or not evidence.is_sufficient)
        ):
            web_decision = (
                decision
                if (decision.web_search_allowed and decision.public_web_subquery)
                else RoutingDecision(
                    query=query,
                    knowledge_domain="real_world",
                    route_mode=RouteMode.WEB_FALLBACK,
                    allowed_sources=("internal_rag", "web_search"),
                    rag_evidence_status=RagEvidenceStatus.INSUFFICIENT,
                    web_search_allowed=True,
                    fallback_reason="RAG_INSUFFICIENT_PUBLIC_WEB_ALLOWED",
                    public_web_subquery=query,
                )
            )
            if self._web_search_service is not None:
                web_ev = await self._web_search_service.search_if_eligible(
                    web_decision,
                    allowed_story_access_level=record.story_access_level,
                )
                web_searched = True
                if web_ev.is_sufficient:
                    evidence = merge_evidence_packages(
                        evidence if (evidence and evidence.is_sufficient) else None,
                        web_ev,
                        query=query,
                        knowledge_domain=web_decision.knowledge_domain,
                        allowed_story_access_level=record.story_access_level,
                    )
                    decision = RoutingDecision(
                        query=web_decision.query,
                        knowledge_domain=web_decision.knowledge_domain,
                        route_mode=web_decision.route_mode,
                        allowed_sources=web_decision.allowed_sources,
                        rag_evidence_status=web_decision.rag_evidence_status,
                        web_search_allowed=True,
                        fallback_reason=web_decision.fallback_reason,
                        web_search_used=True,
                        public_web_subquery=web_decision.public_web_subquery,
                    )
            elif self._web_search_provider is not None and decision.public_web_subquery:
                try:
                    evidence = await self._web_search_provider.search(decision.public_web_subquery)
                    decision = RoutingDecision(
                        query=decision.query,
                        knowledge_domain=decision.knowledge_domain,
                        route_mode=decision.route_mode,
                        allowed_sources=decision.allowed_sources,
                        rag_evidence_status=decision.rag_evidence_status,
                        web_search_allowed=True,
                        fallback_reason=decision.fallback_reason,
                        web_search_used=True,
                        public_web_subquery=decision.public_web_subquery,
                    )
                except Exception:
                    evidence = None

        record.routing_decision = decision

        if evidence is None or not evidence.is_sufficient:
            fallback_text = (
                SPOILER_FALLBACK_TEXT
                if decision.rag_evidence_status == RagEvidenceStatus.ACCESS_RESTRICTED
                else SAFE_FALLBACK_TEXT
            )
            self._complete_with_fallback(record, turn_id, fallback_text)
            self._memory.append_turn(
                record.conversation_id,
                turn_id=turn_id,
                user_query=raw_query,
                resolved_query=query,
                answer_text=record.answer_text,
                knowledge_domain=plan.knowledge_domain,
                verification_status=record.verification_status,
            )
            return

        # 5. Generator -> Verifier -> Deterministic Verification Gate
        record.status = TurnStatus.GENERATING
        record.status = TurnStatus.VERIFYING
        outcome = await run_verified_answer_pipeline(
            query,
            evidence,
            generator=self._generator,
            verifier=self._verifier,
            gate=self._gate,
            allowed_story_access_level=record.story_access_level,
        )
        if not outcome.passed and can_try_public_web and not web_searched:
            assert self._web_search_service is not None
            web_decision = RoutingDecision(
                query=query,
                knowledge_domain="real_world",
                route_mode=RouteMode.WEB_FALLBACK,
                allowed_sources=("internal_rag", "web_search"),
                rag_evidence_status=RagEvidenceStatus.INSUFFICIENT,
                web_search_allowed=True,
                fallback_reason="RAG_INSUFFICIENT_PUBLIC_WEB_ALLOWED",
                public_web_subquery=query,
            )
            web_ev = await self._web_search_service.search_if_eligible(
                web_decision,
                allowed_story_access_level=record.story_access_level,
            )
            if web_ev.is_sufficient:
                outcome = await run_verified_answer_pipeline(
                    query,
                    web_ev,
                    generator=self._generator,
                    verifier=self._verifier,
                    gate=self._gate,
                    allowed_story_access_level=record.story_access_level,
                )

        if not outcome.passed or outcome.approved_answer is None:
            self._complete_with_fallback(record, turn_id, outcome.text)
            self._memory.append_turn(
                record.conversation_id,
                turn_id=turn_id,
                user_query=raw_query,
                resolved_query=query,
                answer_text=record.answer_text,
                knowledge_domain=plan.knowledge_domain,
                verification_status=record.verification_status,
            )
            return

        # 6. Persist Approved Answer & optional Verified TTS
        await self._finalize_approved_turn(
            record, outcome.approved_answer.approved_answer_id, wants_audio
        )
        self._memory.append_turn(
            record.conversation_id,
            turn_id=turn_id,
            user_query=raw_query,
            resolved_query=query,
            answer_text=record.answer_text,
            knowledge_domain=plan.knowledge_domain,
            verification_status=record.verification_status,
        )

    async def _finalize_approved_turn(
        self, record: TurnRecord, approved_answer_id: str, wants_audio: bool
    ) -> None:
        # Gate invariant: only an approved_answer_id registered by VerificationGate is allowed.
        approved = self._gate.get_approved_answer(approved_answer_id)
        record.answer_id = approved.approved_answer_id
        record.answer_text = approved.text
        record.content_mode = approved.content_mode
        record.verification_status = approved.verification_status
        record.citations = [citation.to_public_dict() for citation in approved.citations]

        if not wants_audio:
            record.status = TurnStatus.COMPLETED
            return

        if self._tts_provider is None:
            record.status = TurnStatus.COMPLETED_TEXT_ONLY
            return

        record.status = TurnStatus.SYNTHESIZING
        try:
            audio_bytes = await asyncio.to_thread(self._tts_provider.synthesize, approved.text)
        except Exception:
            record.audio_output_bytes = None
            record.status = TurnStatus.COMPLETED_TEXT_ONLY
            return

        if not audio_bytes:
            record.audio_output_bytes = None
            record.status = TurnStatus.COMPLETED_TEXT_ONLY
            return

        record.audio_output_bytes = audio_bytes
        record.status = TurnStatus.COMPLETED

    def _complete_with_fallback(
        self, record: TurnRecord, turn_id: str, fallback_text: str
    ) -> None:
        record.answer_id = f"ans_{turn_id}"
        record.answer_text = fallback_text
        record.verification_status = "FALLBACK"
        record.citations = []
        record.audio_output_bytes = None
        record.status = (
            TurnStatus.COMPLETED_TEXT_ONLY
            if record.response_mode == ResponseMode.TEXT_AND_VOICE
            else TurnStatus.COMPLETED
        )

    def get_turn(self, turn_id: str) -> TurnResponse:
        try:
            record = self._turns[turn_id]
        except KeyError:
            raise KeyError(turn_id) from None

        completed = record.status in {TurnStatus.COMPLETED, TurnStatus.COMPLETED_TEXT_ONLY}
        wants_audio = record.response_mode == ResponseMode.TEXT_AND_VOICE
        has_ready_audio = bool(
            wants_audio
            and completed
            and record.audio_output_bytes
            and record.verification_status == "PASS"
        )
        if not wants_audio:
            audio_status = AudioStatus.NOT_REQUESTED
        elif has_ready_audio:
            audio_status = AudioStatus.READY
        elif completed:
            audio_status = AudioStatus.UNAVAILABLE
        else:
            audio_status = AudioStatus.PENDING

        return TurnResponse(
            turn_id=turn_id,
            conversation_id=record.conversation_id,
            status=record.status,
            input=TurnInput(type=record.input_type, transcript=record.transcript),
            answer=(
                Answer(
                    answer_id=record.answer_id or f"ans_{turn_id}",
                    text=record.answer_text,
                    content_mode=record.content_mode,
                    verification_status=record.verification_status,
                    citations=list(record.citations),
                )
                if completed
                else None
            ),
            audio=Audio(
                status=audio_status,
                endpoint=f"/v1/turns/{turn_id}/audio" if has_ready_audio else None,
            ),
        )

    def get_turn_audio(self, turn_id: str) -> bytes:
        try:
            record = self._turns[turn_id]
        except KeyError:
            raise KeyError(turn_id) from None
        if (
            record.verification_status != "PASS"
            or not record.answer_id
            or not record.audio_output_bytes
        ):
            raise KeyError(turn_id)
        # Re-verify that the answer_id exists in the VerificationGate registry.
        self._gate.get_approved_answer(record.answer_id)
        return record.audio_output_bytes


class _GeminiBytesSttAdapter:
    """Adapt GeminiSttProvider.transcribe_bytes to SttAdapterProtocol for /v1 mobile turns."""

    def __init__(self, settings: Settings) -> None:
        from app.integrations.stt_providers.gemini_stt import GeminiSttProvider

        self._provider = GeminiSttProvider(
            settings.gemini_api_key or "",
            settings.gemini_stt_model,
            settings.gemini_request_timeout_seconds,
        )

    def transcribe(self, audio_bytes: bytes, *, mime_type: str = "audio/wav") -> str:
        raw = self._provider.transcribe_bytes(
            audio_bytes,
            mime_type=mime_type,
            language_code="vi-VN",
        )
        return normalize_stt_transcript(raw) or raw


class _ElevenLabsPrimaryBytesTtsAdapter:
    """Adapt ElevenLabsTtsProvider (primary) to TtsAdapterProtocol for /v1 mobile turns."""

    def __init__(self, settings: Settings) -> None:
        from app.integrations.tts_providers.elevenlabs_tts import ElevenLabsTtsProvider

        self._provider = ElevenLabsTtsProvider(
            api_key=settings.elevenlabs_api_key or "",
            voice_id=settings.elevenlabs_tts_voice_id or "",
            model=settings.elevenlabs_tts_model,
            sample_rate=settings.elevenlabs_tts_sample_rate,
            timeout_seconds=settings.elevenlabs_tts_timeout_seconds,
            base_url=settings.elevenlabs_api_base_url,
        )

    def synthesize(self, text: str) -> bytes:
        return self._provider.synthesize_bytes(text)


def build_turn_service(settings: Settings | None = None) -> InMemoryTurnService:
    """Construct production /v1 turn service with Verified RAG, Web Search & ElevenLabs TTS."""

    cfg = settings or get_settings()
    if not (
        cfg.supabase_url
        and cfg.supabase_secret_key
        and cfg.gemini_api_key
        and cfg.groq_generator_api_key
        and cfg.groq_generator_model
    ):
        return InMemoryTurnService()

    from app.integrations.embeddings.gemini import GeminiEmbeddingProvider
    from app.integrations.llm.groq_verified import GroqClaimVerifier, GroqGroundedGenerator
    from app.modules.knowledge.retrieval import SupabaseKnowledgeRetriever
    from app.modules.web_search.service import (
        BraveWebSearchProvider,
        TavilyWebSearchProvider,
        WebSearchProvider,
        WikipediaViWebSearchProvider,
    )

    embedder = GeminiEmbeddingProvider(
        api_key=cfg.gemini_api_key,
        model=cfg.gemini_embedding_model,
        dimensions=cfg.gemini_embedding_dimensions,
        timeout_seconds=cfg.gemini_request_timeout_seconds,
    )
    retriever = SupabaseKnowledgeRetriever(
        supabase_url=cfg.supabase_url,
        supabase_secret_key=cfg.supabase_secret_key,
        embedder=embedder,
    )
    generator = GroqGroundedGenerator(
        cfg.groq_generator_api_key,
        cfg.groq_generator_model,
        cfg.groq_request_timeout_seconds,
        max_retries=cfg.groq_max_retries,
    )
    verifier = GroqClaimVerifier(
        cfg.groq_verifier_api_key or cfg.groq_generator_api_key,
        cfg.groq_verifier_model or cfg.groq_generator_model,
        cfg.groq_request_timeout_seconds,
        max_retries=cfg.groq_max_retries,
    )
    if cfg.web_search_provider == "tavily" and cfg.tavily_api_key:
        web_provider: WebSearchProvider = TavilyWebSearchProvider(
            cfg.tavily_api_key,
            timeout_seconds=cfg.web_search_timeout_seconds,
        )
    elif cfg.web_search_provider == "brave" and cfg.brave_search_api_key:
        web_provider = BraveWebSearchProvider(
            cfg.brave_search_api_key,
            timeout_seconds=cfg.web_search_timeout_seconds,
        )
    else:
        web_provider = WikipediaViWebSearchProvider(
            timeout_seconds=cfg.web_search_timeout_seconds,
        )
    web_search_service = WebSearchService(web_provider)
    stt_adapter: SttAdapterProtocol | None = (
        _GeminiBytesSttAdapter(cfg) if cfg.gemini_api_key else None
    )
    tts_adapter: TtsAdapterProtocol | None = (
        _ElevenLabsPrimaryBytesTtsAdapter(cfg)
        if (cfg.elevenlabs_api_key and cfg.elevenlabs_tts_voice_id)
        else None
    )
    return InMemoryTurnService(
        retriever=retriever,
        generator=generator,
        verifier=verifier,
        stt_provider=stt_adapter,
        tts_provider=tts_adapter,
        web_search_service=web_search_service,
    )
