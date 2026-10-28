"""Provider-agnostic orchestration for a single voice/text turn (with optional Verified RAG)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol

from app.core.config import Settings
from app.integrations.embeddings.gemini import GeminiEmbeddingProvider
from app.integrations.llm.groq_generator import GroqGeneratorProvider
from app.integrations.llm.groq_verified import GroqClaimVerifier, GroqGroundedGenerator
from app.integrations.stt_providers.gemini_stt import GeminiSttProvider
from app.integrations.tts_providers.elevenlabs_tts import ElevenLabsTtsProvider
from app.integrations.tts_providers.gemini_tts import GeminiTtsProvider
from app.integrations.tts_providers.vieneu_tts import VieneuTtsProvider
from app.modules.conversation.memory import ConversationMemoryStore, normalize_stt_transcript
from app.modules.knowledge.retrieval import SupabaseKnowledgeRetriever
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
    VerificationGate,
    run_verified_answer_pipeline,
)
from app.modules.web_search.service import (
    BraveWebSearchProvider,
    TavilyWebSearchProvider,
    WebSearchCache,
    WebSearchProvider,
    WebSearchService,
    WikipediaViWebSearchProvider,
    merge_evidence_packages,
)

ProgressCallback = Callable[[str, str | None, float | None], None]
VerificationState = Literal["NOT_RUN", "PASS", "FALLBACK"]

_SHARED_CONVERSATION_MEMORY = ConversationMemoryStore()
_SHARED_WEB_CACHE = WebSearchCache()


def _no_progress(event: str, text: str | None, seconds: float | None) -> None:
    pass


class SttProvider(Protocol):
    def transcribe(self, audio_path: Path, *, language_code: str = "vi-VN") -> str: ...


class GeneratorProvider(Protocol):
    def generate(self, transcript: str) -> str: ...


class TtsProvider(Protocol):
    def synthesize(self, text: str, output_path: Path) -> Path: ...


class VoiceLoopConfigurationError(ValueError):
    """Required local voice-loop configuration is missing."""


@dataclass(frozen=True)
class RagTurnOutput:
    text: str
    verification_status: VerificationState
    citations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class VoiceLoopResult:
    transcript: str
    answer: str
    stt_seconds: float
    llm_seconds: float
    tts_seconds: float | None
    audio_path: Path | None
    tts_error: str | None = None
    verification_status: VerificationState = "NOT_RUN"
    citations: tuple[dict[str, Any], ...] = ()


class RagVerifiedGeneratorProvider:
    """RAG-first + Conditional Web Search + Conversation Memory + Verified Gate adapter."""

    def __init__(
        self,
        *,
        retriever: SupabaseKnowledgeRetriever,
        generator: GroqGroundedGenerator,
        verifier: GroqClaimVerifier,
        web_search: WebSearchService | None = None,
        memory: ConversationMemoryStore | None = None,
        gate: VerificationGate | None = None,
    ) -> None:
        self._retriever = retriever
        self._generator = generator
        self._verifier = verifier
        self._web_search = web_search
        self._memory = memory if memory is not None else _SHARED_CONVERSATION_MEMORY
        self._gate = gate or VerificationGate()

    def generate(self, transcript: str) -> str:
        return self.generate_with_rag(transcript, story_access_level=0).text

    def generate_with_rag(
        self,
        transcript: str,
        *,
        story_access_level: int = 0,
        conversation_id: str | None = None,
    ) -> RagTurnOutput:
        return asyncio.run(
            self._generate_async(
                transcript,
                story_access_level=story_access_level,
                conversation_id=conversation_id,
            )
        )

    async def _generate_async(
        self,
        transcript: str,
        *,
        story_access_level: int = 0,
        conversation_id: str | None = None,
    ) -> RagTurnOutput:
        raw_query = normalize_stt_transcript(transcript)
        if not raw_query:
            return RagTurnOutput(text=SAFE_FALLBACK_TEXT, verification_status="FALLBACK")

        cleaned = (
            self._memory.rewrite_query(conversation_id, raw_query)
            if conversation_id
            else raw_query
        )
        plan = analyze_query(cleaned, allowed_story_access_level=story_access_level)
        if plan.route_mode == RouteMode.DIRECT:
            self._gate.register_direct_approved_answer(DIRECT_GREETING_TEXT)
            return RagTurnOutput(text=DIRECT_GREETING_TEXT, verification_status="PASS")

        if plan.route_mode in {
            RouteMode.GAME_LORE_GATED,
            RouteMode.NEEDS_CLARIFICATION,
            RouteMode.GAME_STATE,
        }:
            fallback_map = {
                RouteMode.GAME_LORE_GATED: SPOILER_FALLBACK_TEXT,
                RouteMode.NEEDS_CLARIFICATION: CLARIFICATION_FALLBACK_TEXT,
                RouteMode.GAME_STATE: GAME_STATE_UNAVAILABLE_TEXT,
            }
            out_text = fallback_map[plan.route_mode]
            if conversation_id:
                self._memory.append_turn(
                    conversation_id,
                    turn_id="dev",
                    user_query=raw_query,
                    resolved_query=cleaned,
                    answer_text=out_text,
                    knowledge_domain=plan.knowledge_domain,
                    verification_status="FALLBACK",
                )
            return RagTurnOutput(text=out_text, verification_status="FALLBACK")

        try:
            evidence = await self._retriever.retrieve(
                cleaned,
                allowed_story_access_level=story_access_level,
                knowledge_domain=plan.knowledge_domain,
            )
            retrieval_error = False
        except Exception:
            evidence = None
            retrieval_error = True

        decision = evaluate_routing_decision(
            plan,
            evidence,
            allowed_story_access_level=story_access_level,
            retrieval_error=retrieval_error,
        )

        can_try_public_web = (
            self._web_search is not None
            and not retrieval_error
            and not is_exclusive_lore_query(cleaned)
        )
        web_searched = False

        # Conditional Web Search fallback when public domain RAG is missing/insufficient.
        if can_try_public_web and (
            (decision.web_search_allowed and decision.public_web_subquery)
            or (evidence is None or not evidence.is_sufficient)
        ):
            web_decision = (
                decision
                if (decision.web_search_allowed and decision.public_web_subquery)
                else RoutingDecision(
                    query=cleaned,
                    knowledge_domain="real_world",
                    route_mode=RouteMode.WEB_FALLBACK,
                    allowed_sources=("internal_rag", "web_search"),
                    rag_evidence_status=RagEvidenceStatus.INSUFFICIENT,
                    web_search_allowed=True,
                    fallback_reason="RAG_INSUFFICIENT_PUBLIC_WEB_ALLOWED",
                    public_web_subquery=cleaned,
                )
            )
            assert self._web_search is not None
            web_ev = await self._web_search.search_if_eligible(
                web_decision,
                allowed_story_access_level=story_access_level,
            )
            web_searched = True
            if web_ev.is_sufficient:
                evidence = merge_evidence_packages(
                    evidence if (evidence and evidence.is_sufficient) else None,
                    web_ev,
                    query=cleaned,
                    knowledge_domain=web_decision.knowledge_domain,
                    allowed_story_access_level=story_access_level,
                )

        if evidence is None or not evidence.is_sufficient:
            fallback_text = (
                SPOILER_FALLBACK_TEXT
                if decision.rag_evidence_status == RagEvidenceStatus.ACCESS_RESTRICTED
                else SAFE_FALLBACK_TEXT
            )
            if conversation_id:
                self._memory.append_turn(
                    conversation_id,
                    turn_id="dev",
                    user_query=raw_query,
                    resolved_query=cleaned,
                    answer_text=fallback_text,
                    knowledge_domain=plan.knowledge_domain,
                    verification_status="FALLBACK",
                )
            return RagTurnOutput(text=fallback_text, verification_status="FALLBACK")

        outcome = await run_verified_answer_pipeline(
            cleaned,
            evidence,
            generator=self._generator,
            verifier=self._verifier,
            gate=self._gate,
            allowed_story_access_level=story_access_level,
        )
        # If internal RAG matched generic Vietnamese tokens on a non-lore question but did not
        # actually contain the answer, fall back to public Web Search and re-verify once.
        if not outcome.passed and can_try_public_web and not web_searched:
            assert self._web_search is not None
            web_decision = RoutingDecision(
                query=cleaned,
                knowledge_domain="real_world",
                route_mode=RouteMode.WEB_FALLBACK,
                allowed_sources=("internal_rag", "web_search"),
                rag_evidence_status=RagEvidenceStatus.INSUFFICIENT,
                web_search_allowed=True,
                fallback_reason="RAG_INSUFFICIENT_PUBLIC_WEB_ALLOWED",
                public_web_subquery=cleaned,
            )
            web_ev = await self._web_search.search_if_eligible(
                web_decision,
                allowed_story_access_level=story_access_level,
            )
            if web_ev.is_sufficient:
                outcome = await run_verified_answer_pipeline(
                    cleaned,
                    web_ev,
                    generator=self._generator,
                    verifier=self._verifier,
                    gate=self._gate,
                    allowed_story_access_level=story_access_level,
                )
        if conversation_id:
            self._memory.append_turn(
                conversation_id,
                turn_id="dev",
                user_query=raw_query,
                resolved_query=cleaned,
                answer_text=outcome.text,
                knowledge_domain=plan.knowledge_domain,
                verification_status=outcome.verification_status,
            )
        return RagTurnOutput(
            text=outcome.text,
            verification_status=outcome.verification_status,
            citations=tuple(c.to_public_dict() for c in outcome.citations),
        )


class VoiceLoopService:
    """Run STT -> text generation (or Verified RAG) -> TTS while keeping TTS failure non-fatal."""

    def __init__(self, stt: SttProvider, generator: GeneratorProvider, tts: TtsProvider) -> None:
        self._stt = stt
        self._generator = generator
        self._tts = tts

    def process(
        self,
        input_audio_path: Path,
        output_audio_path: Path,
        *,
        progress: ProgressCallback = _no_progress,
        wants_audio: bool = True,
        story_access_level: int = 0,
        conversation_id: str | None = None,
    ) -> VoiceLoopResult:
        progress("TRANSCRIBING", None, None)
        started = perf_counter()
        raw_transcript = self._stt.transcribe(input_audio_path, language_code="vi-VN")
        transcript = normalize_stt_transcript(raw_transcript) or raw_transcript
        stt_seconds = perf_counter() - started
        progress("TRANSCRIBED", transcript, stt_seconds)
        return self.process_text(
            transcript,
            output_audio_path,
            progress=progress,
            wants_audio=wants_audio,
            stt_seconds=stt_seconds,
            story_access_level=story_access_level,
            conversation_id=conversation_id,
        )

    def process_text(
        self,
        transcript: str,
        output_audio_path: Path,
        *,
        progress: ProgressCallback = _no_progress,
        wants_audio: bool = True,
        stt_seconds: float = 0.0,
        story_access_level: int = 0,
        conversation_id: str | None = None,
    ) -> VoiceLoopResult:
        progress("GENERATING", None, None)
        started = perf_counter()
        if hasattr(self._generator, "generate_with_rag"):
            try:
                rag_out: RagTurnOutput = self._generator.generate_with_rag(  # type: ignore[attr-defined]
                    transcript,
                    story_access_level=story_access_level,
                    conversation_id=conversation_id,
                )
            except TypeError:
                rag_out = self._generator.generate_with_rag(  # type: ignore[attr-defined]
                    transcript,
                    story_access_level=story_access_level,
                )
            answer = rag_out.text
            verification_status: VerificationState = rag_out.verification_status
            citations = rag_out.citations
        else:
            answer = self._generator.generate(transcript)
            verification_status = "NOT_RUN"
            citations = ()
        llm_seconds = perf_counter() - started
        progress("GENERATED", answer, llm_seconds)
        if not wants_audio:
            return VoiceLoopResult(
                transcript,
                answer,
                stt_seconds,
                llm_seconds,
                None,
                None,
                verification_status=verification_status,
                citations=citations,
            )

        progress("SYNTHESIZING", None, None)
        started = perf_counter()
        try:
            audio_path = self._tts.synthesize(answer, output_audio_path)
        except Exception:
            return VoiceLoopResult(
                transcript=transcript,
                answer=answer,
                stt_seconds=stt_seconds,
                llm_seconds=llm_seconds,
                tts_seconds=perf_counter() - started,
                audio_path=None,
                tts_error="TTS failed or timed out; the text response is still available.",
                verification_status=verification_status,
                citations=citations,
            )

        return VoiceLoopResult(
            transcript=transcript,
            answer=answer,
            stt_seconds=stt_seconds,
            llm_seconds=llm_seconds,
            tts_seconds=perf_counter() - started,
            audio_path=audio_path,
            verification_status=verification_status,
            citations=citations,
        )


def validate_voice_configuration(settings: Settings) -> None:
    """Report missing setting names without constructing clients or revealing values."""
    required = {
        "GEMINI_API_KEY": settings.gemini_api_key,
        "GEMINI_STT_MODEL": settings.gemini_stt_model,
        "GROQ_GENERATOR_API_KEY": settings.groq_generator_api_key,
        "GROQ_GENERATOR_MODEL": settings.groq_generator_model,
    }
    if settings.tts_provider == "gemini":
        required["GEMINI_TTS_MODEL"] = settings.gemini_tts_model
    elif settings.tts_provider == "elevenlabs":
        required["ELEVENLABS_API_KEY"] = settings.elevenlabs_api_key
        required["ELEVENLABS_TTS_MODEL"] = settings.elevenlabs_tts_model
        required["ELEVENLABS_TTS_VOICE_ID"] = settings.elevenlabs_tts_voice_id
    else:
        required["VIENEU_TTS_BASE_URL"] = settings.vieneu_tts_base_url
        required["VIENEU_TTS_MODEL"] = settings.vieneu_tts_model
        required["VIENEU_TTS_VOICE"] = settings.vieneu_tts_voice
    missing = [name for name, value in required.items() if not value or not value.strip()]
    if missing:
        raise VoiceLoopConfigurationError(
            "Missing voice-loop configuration: " + ", ".join(missing)
        )


def build_voice_loop_service(
    settings: Settings,
    *,
    tts_provider: Literal["gemini", "vieneu", "elevenlabs"] | None = None,
    vieneu_tts_voice: str | None = None,
) -> VoiceLoopService:
    """Construct voice service; automatically enables Verified RAG when Supabase is configured."""
    updates: dict[str, Any] = {}
    if tts_provider is not None:
        updates["tts_provider"] = tts_provider
    target_provider = tts_provider or settings.tts_provider
    if vieneu_tts_voice is not None:
        if target_provider == "elevenlabs":
            updates["elevenlabs_tts_voice_id"] = vieneu_tts_voice
        else:
            updates["vieneu_tts_voice"] = vieneu_tts_voice
    effective_settings = settings.model_copy(update=updates) if updates else settings

    validate_voice_configuration(effective_settings)
    if effective_settings.tts_provider == "gemini":
        tts: TtsProvider = GeminiTtsProvider(
            effective_settings.gemini_api_key or "",
            effective_settings.gemini_tts_model,
            effective_settings.gemini_request_timeout_seconds,
        )
    elif effective_settings.tts_provider == "elevenlabs":
        tts = ElevenLabsTtsProvider(
            effective_settings.elevenlabs_api_key or "",
            effective_settings.elevenlabs_tts_voice_id or "",
            effective_settings.elevenlabs_tts_model,
            effective_settings.elevenlabs_tts_sample_rate,
            effective_settings.elevenlabs_tts_timeout_seconds,
            base_url=effective_settings.elevenlabs_api_base_url,
        )
    else:
        tts = VieneuTtsProvider(
            effective_settings.vieneu_tts_base_url or "",
            effective_settings.vieneu_tts_model,
            effective_settings.vieneu_tts_voice,
            effective_settings.vieneu_tts_sample_rate,
            effective_settings.vieneu_tts_timeout_seconds,
            api_key=effective_settings.vieneu_tts_api_key,
        )

    if effective_settings.supabase_url and effective_settings.supabase_secret_key:
        embedder = GeminiEmbeddingProvider(
            api_key=effective_settings.gemini_api_key or "",
            model=effective_settings.gemini_embedding_model,
            dimensions=effective_settings.gemini_embedding_dimensions,
            timeout_seconds=effective_settings.gemini_request_timeout_seconds,
        )
        retriever = SupabaseKnowledgeRetriever(
            supabase_url=effective_settings.supabase_url,
            supabase_secret_key=effective_settings.supabase_secret_key,
            embedder=embedder,
        )
        grounded_gen = GroqGroundedGenerator(
            effective_settings.groq_generator_api_key or "",
            effective_settings.groq_generator_model or "",
            effective_settings.groq_request_timeout_seconds,
            max_retries=effective_settings.groq_max_retries,
        )
        verifier_key = (
            effective_settings.groq_verifier_api_key
            or effective_settings.groq_generator_api_key
            or ""
        )
        verifier_model = (
            effective_settings.groq_verifier_model
            or effective_settings.groq_generator_model
            or ""
        )
        claim_verifier = GroqClaimVerifier(
            verifier_key,
            verifier_model,
            effective_settings.groq_request_timeout_seconds,
            max_retries=effective_settings.groq_max_retries,
        )
        if (
            effective_settings.web_search_provider == "tavily"
            and effective_settings.tavily_api_key
        ):
            web_provider: WebSearchProvider = TavilyWebSearchProvider(
                effective_settings.tavily_api_key,
                timeout_seconds=effective_settings.web_search_timeout_seconds,
            )
        elif (
            effective_settings.web_search_provider == "brave"
            and effective_settings.brave_search_api_key
        ):
            web_provider = BraveWebSearchProvider(
                effective_settings.brave_search_api_key,
                timeout_seconds=effective_settings.web_search_timeout_seconds,
            )
        else:
            web_provider = WikipediaViWebSearchProvider(
                timeout_seconds=effective_settings.web_search_timeout_seconds,
            )
        web_search = WebSearchService(web_provider, cache=_SHARED_WEB_CACHE)
        generator: GeneratorProvider = RagVerifiedGeneratorProvider(
            retriever=retriever,
            generator=grounded_gen,
            verifier=claim_verifier,
            web_search=web_search,
            memory=_SHARED_CONVERSATION_MEMORY,
        )
    else:
        generator = GroqGeneratorProvider(
            effective_settings.groq_generator_api_key or "",
            effective_settings.groq_generator_model or "",
            effective_settings.groq_request_timeout_seconds,
            max_retries=effective_settings.groq_max_retries,
        )

    return VoiceLoopService(
        stt=GeminiSttProvider(
            effective_settings.gemini_api_key or "",
            effective_settings.gemini_stt_model,
            effective_settings.gemini_request_timeout_seconds,
        ),
        generator=generator,
        tts=tts,
    )
