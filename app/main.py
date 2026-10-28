import asyncio
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import dev_voice
from app.api.v1 import conversations, health, turns, voice
from app.core.config import Settings, get_settings
from app.core.dev_voice_http import DevRequestGuard
from app.modules.orchestration.service import InMemoryTurnService, build_turn_service
from app.modules.voice_loop.dev_api import DevApiError, DevVoiceManager
from app.modules.voice_loop.service import (
    VoiceLoopService,
    build_voice_loop_service,
    validate_voice_configuration,
)

turn_service = InMemoryTurnService() if "pytest" in sys.modules else build_turn_service()


def create_app(
    settings: Settings | None = None,
    *,
    dev_service_factory: Callable[[], VoiceLoopService] | None = None,
    audio_root: Path | None = None,
) -> FastAPI:
    explicit_settings = settings
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        task = None
        store = None
        if settings.is_development and settings.dev_voice_enabled:
            if dev_service_factory is None:
                validate_voice_configuration(settings)
            # Bound API retry costs independently of the existing terminal workflow.
            # Reload `.env` per turn when using default settings so key updates apply live.
            factory = dev_service_factory or (
                lambda provider, voice: build_voice_loop_service(
                    (explicit_settings or Settings()).model_copy(update={"groq_max_retries": 0}),
                    tts_provider=provider,
                    vieneu_tts_voice=voice,
                )
            )
            root = audio_root or Path(__file__).resolve().parents[1] / ".voice_loop_tmp" / "dev_api"
            store = DevVoiceManager(settings, root, factory)
            application.state.dev_voice_manager = store

            async def housekeeping():
                while True:
                    await asyncio.to_thread(store.sweep)
                    await asyncio.sleep(1)

            task = asyncio.create_task(housekeeping())
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if store:
                store.close()

    application = FastAPI(
        title="Saigon: Veilwake AI Guide Service",
        version="0.1.0",
        description="Internal AI Guide; /dev endpoints are an unverified local test harness.",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.explicit_settings = explicit_settings is not None

    @application.exception_handler(DevApiError)
    async def dev_error(request: Request, exc: DevApiError):
        headers = {"Cache-Control": "no-store"}
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            {"error": exc.error.model_dump()}, status_code=exc.status, headers=headers
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/dev/"):
            # Default Pydantic errors may contain raw input; never echo dev request values.
            return JSONResponse(
                {
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "Invalid request fields or missing Idempotency-Key.",
                        "retryable": False,
                    }
                },
                status_code=422,
            )
        return await request_validation_exception_handler(request, exc)

    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()] or ["*"]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    application.add_middleware(DevRequestGuard, settings=settings)

    application.include_router(health.router)
    application.include_router(conversations.router)
    application.include_router(turns.router)
    application.include_router(voice.router)
    application.include_router(dev_voice.router)
    return application


app = create_app()
