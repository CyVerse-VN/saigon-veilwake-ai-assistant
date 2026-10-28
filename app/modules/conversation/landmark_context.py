"""Location is a disambiguation hint, never evidence or authorization."""

from __future__ import annotations

import re

from app.modules.conversation.memory import TurnMemoryEntry, resolve_followup_query

LANDMARK_NAMES = {
    "post_office": "Bưu điện Trung tâm Sài Gòn",
    "cathedral": "Nhà thờ Đức Bà Sài Gòn",
    "independence_palace": "Dinh Độc Lập",
    "ben_thanh": "Chợ Bến Thành",
    "war_remnants": "Bảo tàng Chứng tích Chiến tranh",
    "city_hall": "Ủy ban nhân dân Thành phố Hồ Chí Minh",
}
_EXPLICIT_NAMES = tuple(name.casefold() for name in LANDMARK_NAMES.values()) + (
    "nhà thờ đức bà",
    "bưu điện trung tâm",
    "nhà thờ tân định",
    "thảo cầm viên",
    "bảo tàng mỹ thuật",
    "nhà hát thành phố",
    "bến nhà rồng",
    "dinh độc lập",
    "chợ bến thành",
    "bảo tàng chứng tích",
    "ủy ban nhân dân",
    "tòa thị chính",
)
_INTRODUCTION = re.compile(
    r"^(?:hãy |kiri[,:]? )?(?:giới thiệu|kể một chút|đây là đâu|đây là địa điểm gì|công trình gì)",
    re.IGNORECASE,
)


def resolve_landmark_query(query: str, location_id: str | None) -> str:
    name = LANDMARK_NAMES.get(location_id or "")
    if name is None or any(topic in query.casefold() for topic in _EXPLICIT_NAMES):
        return query
    hint = TurnMemoryEntry(
        turn_id="location_context",
        user_query="",
        resolved_query="",
        answer_text="",
        knowledge_domain="real_world",
        topic_hint=name,
        verification_status="NOT_EVIDENCE",
    )
    resolved = resolve_followup_query(query, (hint,))
    if resolved == query and _INTRODUCTION.match(query.strip()):
        return f"{query} ({name})"
    return resolved
