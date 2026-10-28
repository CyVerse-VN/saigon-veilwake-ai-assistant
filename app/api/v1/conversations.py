from fastapi import APIRouter, Depends, HTTPException, status

from app.core.security import require_internal_service
from app.modules.orchestration.service import InMemoryTurnService
from app.schemas.conversation import CreateConversationResponse

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


def get_turn_service() -> InMemoryTurnService:
    from app.main import turn_service

    return turn_service


@router.post("", response_model=CreateConversationResponse, status_code=status.HTTP_201_CREATED)
def create_conversation(
    _: None = Depends(require_internal_service),
    service: InMemoryTurnService = Depends(get_turn_service),
) -> CreateConversationResponse:
    return CreateConversationResponse(conversation_id=service.create_conversation())


def require_existing_conversation(service: InMemoryTurnService, conversation_id: str) -> None:
    try:
        service.require_conversation(conversation_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail="Conversation not found in caller scope."
        ) from None
