from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response, status

from app.api.v1.conversations import get_turn_service, require_existing_conversation
from app.core.security import require_internal_service
from app.modules.orchestration.service import InMemoryTurnService
from app.schemas.conversation import TextTurnRequest, TurnAcceptedResponse, TurnResponse

router = APIRouter(tags=["turns"])


@router.post(
    "/v1/conversations/{conversation_id}/turns",
    response_model=TurnAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_text_turn(
    conversation_id: str,
    body: TextTurnRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_internal_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    service: InMemoryTurnService = Depends(get_turn_service),
) -> TurnAcceptedResponse:
    # Persistence of this key is intentionally deferred to the durable repository phase.
    del idempotency_key
    require_existing_conversation(service, conversation_id)
    turn_id = service.accept_turn(
        conversation_id,
        "text",
        body.message,
        body.response_mode,
        story_access_level=body.story_access_level,
    )
    background_tasks.add_task(service.process_mock_turn, turn_id)
    return TurnAcceptedResponse(
        turn_id=turn_id, conversation_id=conversation_id, status_url=f"/v1/turns/{turn_id}"
    )


@router.get("/v1/turns/{turn_id}", response_model=TurnResponse)
def get_turn(
    turn_id: str,
    _: None = Depends(require_internal_service),
    service: InMemoryTurnService = Depends(get_turn_service),
) -> TurnResponse:
    try:
        return service.get_turn(turn_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Turn not found in caller scope.") from None


@router.get("/v1/turns/{turn_id}/audio")
def get_turn_audio(
    turn_id: str,
    _: None = Depends(require_internal_service),
    service: InMemoryTurnService = Depends(get_turn_service),
) -> Response:
    try:
        audio_bytes = service.get_turn_audio(turn_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail="Verified audio not found in caller scope."
        ) from None
    return Response(content=audio_bytes, media_type="audio/wav")
