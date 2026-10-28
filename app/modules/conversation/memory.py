"""Short-term conversation memory and coreference query rewriting (Module B03).

Follows Proposal v1.3 Section 9.1:
- Memory helps resolve pronouns and conversational continuity ("địa điểm đó", "chỗ đó", "nó").
- Memory NEVER replaces factual evidence and NEVER bypasses story_access_level spoiler checks.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from app.modules.knowledge.retrieval import DEFAULT_SEARCH_ALIASES

_KNOWN_TOPICS: tuple[str, ...] = (
    # Real-world Saigon & regional landmarks
    "Nhà thờ chính tòa Đức Bà Sài Gòn",
    "Nhà thờ Đức Bà Sài Gòn",
    "Nhà thờ Đức Bà",
    "Nhà thờ Tân Định",
    "Bưu điện Trung tâm Sài Gòn",
    "Bưu điện Trung tâm",
    "Dinh Độc Lập",
    "Hội trường Thống Nhất",
    "Chợ Bến Thành",
    "Bến Nhà Rồng",
    "Nhà hát Thành phố",
    "Bảo tàng Thành phố Hồ Chí Minh",
    "Bảo tàng Chứng tích Chiến tranh",
    "Bảo tàng Mỹ thuật",
    "Địa đạo Củ Chi",
    "Chùa Ngọc Hoàng",
    "Thảo Cầm Viên Sài Gòn",
    "Thảo Cầm Viên",
    "Phố đi bộ Nguyễn Huệ",
    "Hồ Con Rùa",
    "Chợ Lớn",
    "Bình Tây",
    "Vũng Tàu",
    "Bình Dương",
    # Canonical game lore entities
    *tuple(DEFAULT_SEARCH_ALIASES.keys()),
    "Saigon: Veilwake",
)

# Deictic / anaphoric phrases ordered from most specific to general.
_DEICTIC_REPLACEMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(địa\s+điểm|địa\s+danh|nhà\s+thờ|tòa\s+nhà|công\s+trình|khu\s+vực|thành\s+phố|"
        r"nhân\s+vật|sinh\s+vật|thực\s+thể|vật\s+phẩm|hiện\s+tượng|chương|cơ\s+chế)\s+"
        r"(đó|này|ấy|vừa\s+rồi|trên)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(chỗ|nơi|cái|điều|việc|chuyện)\s+(đó|này|ấy|vừa\s+rồi)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(ở|tại|trong|về)\s+(đó|đấy|nơi\s+đó|chỗ\s+đó)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(ông\s+ấy|bà\s+ấy|cô\s+ấy|anh\s+ấy|người\s+đó|nó|họ)\b",
        re.IGNORECASE,
    ),
)

_ELLIPTICAL_FOLLOWUP_PATTERN = re.compile(
    r"^(giới\s+thiệu\s+thêm|kể\s+thêm(?!\s+về\s+\S)|nói\s+rõ\s+hơn(?!\s+về\s+\S)|"
    r"nói\s+thêm(?!\s+về\s+\S)|chi\s+tiết\s+hơn|"
    r"xây\s+dựng\s+năm\s+nào|được\s+xây\s+dựng|ai\s+thiết\s+kế|có\s+kiến\s+trúc\s+gì|"
    r"kiến\s+trúc\s+như\s+thế\s+nào|lịch\s+sử\s+như\s+thế\s+nào|nằm\s+ở\s+đâu|"
    r"ở\s+đâu\s+vậy|mở\s+cửa\s+mấy\s+giờ|giá\s+vé\s+bao\s+nhiêu|"
    r"có\s+vai\s+trò\s+gì|dùng\s+để\s+làm\s+gì|hoạt\s+động\s+thế\s+nào)\b",
    re.IGNORECASE,
)

_QUESTION_PREFIX_STRIP = re.compile(
    r"^(xin\s+hỏi|cho\s+tôi\s+hỏi|cho\s+mình\s+hỏi|hãy\s+giới\s+thiệu\s+về|"
    r"giới\s+thiệu\s+về|kể\s+cho\s+tôi\s+nghe\s+về|kể\s+về|nói\s+về|tìm\s+hiểu\s+về|"
    r"thông\s+tin\s+về|cho\s+biết\s+về|hỏi\s+về)\s+",
    re.IGNORECASE,
)
_QUESTION_SUFFIX_STRIP = re.compile(
    r"\s+(là\s+gì|là\s+ai|ở\s+đâu|như\s+thế\s+nào|ra\s+sao|có\s+gì\s+đặc\s+biệt|"
    r"có\s+gì\s+hay|xây\s+dựng\s+năm\s+nào|có\s+ý\s+nghĩa\s+gì|đi|nhé|vậy|không)[?.!\s]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TurnMemoryEntry:
    """Single completed turn stored in the session's short-term conversation window."""

    turn_id: str
    user_query: str
    resolved_query: str
    answer_text: str
    knowledge_domain: str
    topic_hint: str | None = None
    verification_status: str = "PASS"


_STT_PROPER_NOUN_CORRECTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bnhà\s+hàng\s+đức\s+bà\b", re.IGNORECASE), "Nhà thờ Đức Bà"),
    (re.compile(r"\bnhà\s+thờ\s+đức\s+ba\b", re.IGNORECASE), "Nhà thờ Đức Bà"),
    (re.compile(r"\b(kí\s+giới|ký\s+dới|kí\s+dới)\b", re.IGNORECASE), "Ký Giới"),
    (re.compile(r"\b(điệp\s+ước|thiệp\s+ức|đẹp\s+ức)\b", re.IGNORECASE), "Điệp Ức"),
    (re.compile(r"\b(dư\s+ước|như\s+ức)\b", re.IGNORECASE), "Dư Ức"),
    (re.compile(r"\b(kí\s+vực|ký\s+vựt)\b", re.IGNORECASE), "Ký Vực"),
    (re.compile(r"\bấn\s+kí\b", re.IGNORECASE), "Ấn Ký"),
    (re.compile(r"\bmảnh\s+kí\s+ức\b", re.IGNORECASE), "Mảnh Ký Ức"),
)


def normalize_stt_transcript(text: str) -> str:
    """Correct common Vietnamese STT phonetic misrecognitions for proper nouns (Proposal 10.2)."""

    cleaned = unicodedata.normalize("NFC", text.strip())
    if not cleaned:
        return ""
    for pattern, replacement in _STT_PROPER_NOUN_CORRECTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _normalize(text: str) -> str:
    return normalize_stt_transcript(text)


def extract_topic_hint(query: str) -> str | None:
    """Extract the primary entity or subject phrase from a user question."""

    cleaned = _normalize(query)
    if not cleaned:
        return None
    lowered = cleaned.lower()

    # 1. Check ASCII aliases for canonical lore terms first.
    for canonical, variants in DEFAULT_SEARCH_ALIASES.items():
        if canonical.lower() in lowered or any(v.lower() in lowered for v in variants):
            return canonical

    # 2. Check known real-world landmarks and lore topics (longest match wins).
    matched_known = [topic for topic in _KNOWN_TOPICS if topic.lower() in lowered]
    if matched_known:
        matched_known.sort(key=len, reverse=True)
        return matched_known[0]

    # 3. If the query itself contains deictic references, do not extract the deictic as a topic.
    if any(pattern.search(cleaned) for pattern in _DEICTIC_REPLACEMENT_PATTERNS):
        return None

    # 4. Extract concise noun phrase after stripping question prefixes/suffixes.
    candidate = _QUESTION_PREFIX_STRIP.sub("", cleaned)
    candidate = _QUESTION_SUFFIX_STRIP.sub("", candidate).strip(" ,;:-?.!")
    words = candidate.split()
    if 1 <= len(words) <= 7 and candidate.lower() not in {
        "xin chào",
        "chào bạn",
        "hello",
        "hi",
        "cảm ơn",
    }:
        return candidate
    return None


def resolve_followup_query(
    query: str,
    history: Sequence[TurnMemoryEntry],
) -> str:
    """Rewrite anaphoric/follow-up questions using the most recent topic in session history."""

    cleaned = _normalize(query)
    if not cleaned or not history:
        return cleaned

    # Find the most recent turn with a non-empty topic_hint.
    last_topic: str | None = None
    for entry in reversed(history):
        if entry.topic_hint:
            last_topic = entry.topic_hint
            break
    if not last_topic:
        return cleaned

    # 1. Explicit deictic phrases ("địa điểm đó", "chỗ đó", "nhân vật đó", "ở đó", "nó"...)
    for idx, pattern in enumerate(_DEICTIC_REPLACEMENT_PATTERNS):
        if pattern.search(cleaned):
            if idx == 2:
                # Pattern 2 matches "ở đó" / "tại đó" / "về đó": preserve the preposition!
                rewritten = pattern.sub(
                    lambda m: f"{m.group(1)} {last_topic}",
                    cleaned,
                    count=1,
                )
            else:
                rewritten = pattern.sub(last_topic, cleaned, count=1)
            return re.sub(r"\s+", " ", rewritten).strip()

    # 2. Elliptical follow-up ("Giới thiệu thêm", "Xây dựng năm nào?", "Kiến trúc thế nào?")
    # Only rewrite if the new query does not already mention a known topic.
    lowered = cleaned.lower()
    already_has_known_topic = any(t.lower() in lowered for t in _KNOWN_TOPICS)
    if not already_has_known_topic and _ELLIPTICAL_FOLLOWUP_PATTERN.search(cleaned):
        return f"{cleaned} ({last_topic})"

    return cleaned


class ConversationMemoryStore:
    """Thread-safe bounded sliding-window memory store keyed by conversation_id."""

    def __init__(self, *, max_turns_per_conversation: int = 6) -> None:
        self._max_turns = max_turns_per_conversation
        self._lock = threading.RLock()
        self._conversations: dict[str, list[TurnMemoryEntry]] = {}

    def get_history(self, conversation_id: str) -> tuple[TurnMemoryEntry, ...]:
        with self._lock:
            return tuple(self._conversations.get(conversation_id, ()))

    def rewrite_query(self, conversation_id: str, query: str) -> str:
        history = self.get_history(conversation_id)
        return resolve_followup_query(query, history)

    def append_turn(
        self,
        conversation_id: str,
        *,
        turn_id: str,
        user_query: str,
        resolved_query: str,
        answer_text: str,
        knowledge_domain: str,
        verification_status: str,
    ) -> TurnMemoryEntry:
        topic = extract_topic_hint(resolved_query) or extract_topic_hint(user_query)
        with self._lock:
            entries = self._conversations.setdefault(conversation_id, [])
            if topic is None and entries:
                # Carry forward the previous topic when a follow-up turn was about the same topic.
                topic = entries[-1].topic_hint
            entry = TurnMemoryEntry(
                turn_id=turn_id,
                user_query=user_query,
                resolved_query=resolved_query,
                answer_text=answer_text,
                knowledge_domain=knowledge_domain,
                topic_hint=topic,
                verification_status=verification_status,
            )
            entries.append(entry)
            if len(entries) > self._max_turns:
                del entries[: len(entries) - self._max_turns]
            return entry

    def clear_conversation(self, conversation_id: str) -> None:
        with self._lock:
            self._conversations.pop(conversation_id, None)

