from fastapi import Header, HTTPException, status

from app.core.config import Settings, get_settings


def require_internal_service(
    authorization: str | None = Header(default=None),
) -> None:
    """Temporary service-to-service boundary; replace with signed Game Backend claims."""

    active_settings: Settings = get_settings()
    expected = active_settings.internal_service_token

    if active_settings.is_development and not expected:
        return
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal authentication is not configured.",
        )
    if authorization != f"Bearer {expected}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal service token.",
        )
