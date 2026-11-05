"""Location is a disambiguation hint, never evidence or authorization."""

from __future__ import annotations

import re

from app.modules.conversation.landmarks import (
    LANDMARK_NAMES,
    fold_vietnamese,
    is_subject_free_place_question,
    known_landmark_topic,
)
from app.modules.conversation.memory import TurnMemoryEntry, resolve_followup_query

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
    r"^(?:hay |(?:vara|kiri)[,:]? )?(?:gioi thieu(?: ngan gon)?|ke mot chut|"
    r"day la dau|day la dia diem gi|cong trinh gi)(?: cho (?:toi|minh))?[?.!\s]*$",
    re.IGNORECASE,
)



def resolve_landmark_query(query: str, location_id: str | None) -> str:
    name = LANDMARK_NAMES.get(location_id or "")
    if name is None or known_landmark_topic(query) or any(
        fold_vietnamese(topic) in fold_vietnamese(query) for topic in _EXPLICIT_NAMES
    ):
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
    if resolved == query and (
        _INTRODUCTION.fullmatch(fold_vietnamese(query.strip()))
        or is_subject_free_place_question(query)
    ):
        return f"{query} ({name})"
    return resolved
