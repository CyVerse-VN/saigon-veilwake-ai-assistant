from fastapi import APIRouter

router = APIRouter(tags=["health"])
GUIDE_POLICY_VERSION = "2026-11-05.flexible-v1"


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "guide_policy_version": GUIDE_POLICY_VERSION}
