"""Local demo API; all provider work is delegated to bounded jobs."""

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Request, Response, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.integrations.errors import ProviderRequestError
from app.integrations.tts_providers.elevenlabs_tts import ElevenLabsTtsProvider
from app.integrations.tts_providers.vieneu_tts import VieneuTtsProvider
from app.modules.voice_loop.audio import inspect_wav_bytes
from app.modules.voice_loop.dev_api import DevApiError, DevVoiceManager
from app.schemas.conversation import ResponseMode
from app.schemas.dev_voice import (
    DevAccepted,
    DevConversation,
    DevTextRequest,
    DevTtsOptions,
    DevTtsProviderOption,
    DevTurn,
    DevVoiceOption,
)

router = APIRouter(tags=["dev-voice"])
WEB_ROOT = Path(__file__).resolve().parents[1] / "web" / "dev_voice"
MAX_AUDIO_BYTES = 4 * 1024 * 1024


def require_enabled(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.is_development or not settings.dev_voice_enabled:
        raise DevApiError(404, "NOT_FOUND", "Not found.")


def require_owner(
    _: Annotated[None, Depends(require_enabled)],
) -> str:
    # The local demo has one shared caller; IDs do not provide per-user authorization.
    return "local-demo"


def manager(request: Request) -> DevVoiceManager:
    instance = getattr(request.app.state, "dev_voice_manager", None)
    if instance is None:
        raise DevApiError(503, "DEV_NOT_READY", "Development voice API is not initialized.")
    return instance


Owner = Annotated[str, Depends(require_owner)]
Manager = Annotated[DevVoiceManager, Depends(manager)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)]


@router.post("/dev/v1/conversations", response_model=DevConversation, status_code=201)
def create_conversation(owner: Owner, store: Manager) -> DevConversation:
    return store.create_conversation(owner)


@router.post(
    "/dev/v1/conversations/{conversation_id}/turns", response_model=DevAccepted, status_code=202
)
def text_turn(
    conversation_id: str,
    body: DevTextRequest,
    owner: Owner,
    store: Manager,
    idempotency_key: IdempotencyKey,
) -> DevAccepted:
    if not body.message.strip():
        raise DevApiError(422, "EMPTY_MESSAGE", "Enter a non-empty message.")
    return store.accept(
        owner,
        conversation_id,
        idempotency_key,
        message=body.message,
        response_mode=body.response_mode,
        tts_provider=body.tts_provider,
        vieneu_voice=body.vieneu_voice,
        story_access_level=body.story_access_level,
    )


@router.post(
    "/dev/v1/conversations/{conversation_id}/voice-turns",
    response_model=DevAccepted,
    status_code=202,
)
async def voice_turn(
    conversation_id: str,
    owner: Owner,
    store: Manager,
    idempotency_key: IdempotencyKey,
    audio: Annotated[UploadFile, File()],
    response_mode: Annotated[ResponseMode, Form()] = ResponseMode.TEXT_AND_VOICE,
    locale: Annotated[Literal["vi-VN"], Form()] = "vi-VN",
    tts_provider: Annotated[Literal["gemini", "vieneu", "elevenlabs"] | None, Form()] = None,
    vieneu_voice: Annotated[str | None, Form(min_length=1, max_length=80)] = None,
    story_access_level: Annotated[int, Form(ge=0, le=5)] = 0,
) -> DevAccepted:
    del locale  # Validated Vietnamese contract; current providers use vi-VN.
    try:
        if audio.content_type not in {"audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"}:
            raise DevApiError(415, "WAV_REQUIRED", "Send a mono PCM16 WAV recording.")
        data = await audio.read(MAX_AUDIO_BYTES + 1)
        if len(data) > MAX_AUDIO_BYTES:
            raise DevApiError(413, "AUDIO_TOO_LARGE", "Audio exceeds 4 MiB.")
        try:
            await run_in_threadpool(inspect_wav_bytes, data)
        except ValueError:
            raise DevApiError(
                422, "INVALID_AUDIO", "Use a non-silent mono PCM16 WAV, 1-30 seconds, 8-48 kHz."
            ) from None
        return store.accept(
            owner,
            conversation_id,
            idempotency_key,
            audio=data,
            response_mode=response_mode,
            tts_provider=tts_provider,
            vieneu_voice=vieneu_voice,
            story_access_level=story_access_level,
        )
    finally:
        await audio.close()


@router.get("/dev/v1/tts/options", response_model=DevTtsOptions)
def tts_options(_: Owner, request: Request) -> DevTtsOptions:
    """List safe local test choices; clients never receive provider URLs or keys."""
    settings = (
        request.app.state.settings
        if getattr(request.app.state, "explicit_settings", True)
        else Settings()
    )
    voices: list[DevVoiceOption] = []
    vieneu_available = False
    if settings.vieneu_tts_base_url:
        try:
            provider = VieneuTtsProvider(
                settings.vieneu_tts_base_url,
                settings.vieneu_tts_model,
                settings.vieneu_tts_voice,
                settings.vieneu_tts_sample_rate,
                settings.vieneu_tts_timeout_seconds,
                api_key=settings.vieneu_tts_api_key,
            )
            voices = [DevVoiceOption(**voice) for voice in provider.list_voices()]
            vieneu_available = True
        except (ProviderRequestError, ValueError):
            pass

    el_voices: list[DevVoiceOption] = []
    elevenlabs_available = False
    if settings.elevenlabs_api_key and settings.elevenlabs_tts_voice_id:
        try:
            el_provider = ElevenLabsTtsProvider(
                settings.elevenlabs_api_key,
                settings.elevenlabs_tts_voice_id,
                settings.elevenlabs_tts_model,
                settings.elevenlabs_tts_sample_rate,
                settings.elevenlabs_tts_timeout_seconds,
                base_url=settings.elevenlabs_api_base_url,
            )
            el_voices = [DevVoiceOption(**voice) for voice in el_provider.list_voices()]
            elevenlabs_available = bool(el_voices)
        except (ProviderRequestError, ValueError):
            pass

    gemini_available = bool(settings.gemini_api_key and settings.gemini_tts_model.strip())
    return DevTtsOptions(
        default_provider=settings.tts_provider,
        vieneu_default_voice=settings.vieneu_tts_voice,
        elevenlabs_default_voice=settings.elevenlabs_tts_voice_id,
        providers=[
            DevTtsProviderOption(
                id="gemini", label="Gemini", available=gemini_available
            ),
            DevTtsProviderOption(
                id="elevenlabs",
                label="ElevenLabs (Voice Lab)",
                available=elevenlabs_available,
                voices=el_voices,
            ),
            DevTtsProviderOption(
                id="vieneu", label="VieNeu (local CPU)", available=vieneu_available, voices=voices
            ),
        ],
    )


@router.get("/dev/v1/turns/{turn_id}", response_model=DevTurn)
def get_turn(turn_id: str, owner: Owner, store: Manager, response: Response) -> DevTurn:
    response.headers["Cache-Control"] = "no-store"
    return store.get_turn(owner, turn_id)


@router.get("/dev/v1/turns/{turn_id}/audio")
def get_audio(turn_id: str, owner: Owner, store: Manager) -> Response:
    return Response(
        content=store.read_audio(owner, turn_id),
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="response.wav"',
        },
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/dev/", include_in_schema=False, dependencies=[Depends(require_enabled)])
def web_ui() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/dev/assets/{name}", include_in_schema=False, dependencies=[Depends(require_enabled)])
def web_asset(name: str) -> FileResponse:
    if name not in {"app.js", "audio.js", "pcm-worklet.js", "style.css", "tts.css"}:
        raise DevApiError(404, "NOT_FOUND", "Not found.")
    mime = "text/css" if name.endswith(".css") else "text/javascript"
    return FileResponse(WEB_ROOT / name, media_type=mime, headers={"Cache-Control": "no-store"})


@router.get("/ui/", include_in_schema=False)
def cloud_web_ui() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-store"})


@router.get("/ui/assets/{name}", include_in_schema=False)
def cloud_web_asset(name: str) -> FileResponse:
    if name not in {"app.js", "audio.js", "pcm-worklet.js", "style.css", "tts.css"}:
        raise DevApiError(404, "NOT_FOUND", "Not found.")
    mime = "text/css" if name.endswith(".css") else "text/javascript"
    return FileResponse(WEB_ROOT / name, media_type=mime, headers={"Cache-Control": "no-store"})
