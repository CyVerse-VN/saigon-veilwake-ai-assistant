"""Conditional Web Search component (B10) following Proposal v1.3 Section 6.3."""

from __future__ import annotations

import ipaddress
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

import httpx

from app.modules.knowledge.models import Citation, EvidenceItem, EvidencePackage
from app.modules.query_router.router import (
    DEFAULT_SEARCH_ALIASES,
    RagEvidenceStatus,
    RoutingDecision,
)

_PRIVATE_LORE_STRIP_TERMS: tuple[str, ...] = (
    "saigon: veilwake",
    "veilwake",
    "ký giới",
    "dư ức",
    "ký vực",
    "nhịp cộng hưởng",
    "người lắng nghe",
    "người kể tiếp",
    "người giữ im lặng",
    "mảnh ký ức",
    "ấn ký",
    "điệp ức",
    "cảnh ký ức",
    "hồ sơ bí ẩn",
    "hộ chiếu ký ức",
    "huy hiệu địa danh",
    "đại huy hiệu",
    "hành trình truy ức",
    "đêm ký giới",
    "đêm cộng hưởng",
    "đại thức tỉnh",
    "nhật ký dẫn lối",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_REF_TAG_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^/>]*/\s*>", re.IGNORECASE | re.DOTALL)
_WIKI_LINK_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")
_WIKI_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
_WHITESPACE_RE = re.compile(r"\s+")
_INFOBOX_KEYS: dict[str, str] = {
    "location": "Địa chỉ/Vị trí",
    "địa chỉ": "Địa chỉ",
    "vị trí": "Vị trí",
    "address": "Địa chỉ",
    "architect": "Kiến trúc sư",
    "kiến trúc sư": "Kiến trúc sư",
    "architecture_style": "Phong cách kiến trúc",
    "phong cách": "Phong cách kiến trúc",
    "style": "Phong cách kiến trúc",
    "year started": "Năm khởi công",
    "year_started": "Năm khởi công",
    "khởi công": "Khởi công",
    "year_completed": "Năm hoàn thành",
    "completed": "Năm hoàn thành",
    "hoàn thành": "Hoàn thành",
    "khánh thành": "Khánh thành",
}
_PAREN_TOPIC_RE = re.compile(r"\(([^()]+)\)\s*$")
_SPOKEN_INTERJECTION_RE = re.compile(
    r"^(ồ|à|ừm|ừ|ờ|ủa|ơ|này|ê|bạn\s+ơi|ad\s+ơi|ai\s+ơi|cho\s+hỏi)[,.\s]+",
    re.IGNORECASE,
)
_QUERY_FILLER_PREFIX_RE = re.compile(
    r"^(làm\s+sao\s+(tôi|mình)\s+biết|làm\s+sao\s+biết|tôi\s+muốn\s+biết|"
    r"xin\s+hỏi|cho\s+tôi\s+hỏi|cho\s+mình\s+hỏi|hãy\s+giới\s+thiệu\s+về|"
    r"giới\s+thiệu\s+chi\s+tiết\s+về|giới\s+thiệu\s+về|giới\s+thiệu|"
    r"kể\s+cho\s+tôi\s+nghe\s+về|kể\s+thêm\s+về|kể\s+về|nói\s+rõ\s+hơn\s+về|nói\s+về|"
    r"tìm\s+hiểu\s+về|thông\s+tin\s+về|cho\s+biết\s+về|hỏi\s+về)\s+",
    re.IGNORECASE,
)
_QUERY_FILLER_SUFFIX_RE = re.compile(
    r"\s+(nằm\s+ở\s+đâu|ở\s+đâu\s+vậy|ở\s+đâu|là\s+gì\s+vậy|là\s+gì|là\s+ai|"
    r"như\s+thế\s+nào|ra\s+sao|có\s+gì\s+đặc\s+biệt|có\s+gì\s+hay|"
    r"có\s+kiến\s+trúc\s+gì|xây\s+dựng\s+năm\s+nào|vậy|nhé|đi|không)[?.!\s]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WebSearchResult:
    """Normalized search result returned by a WebSearchProvider adapter."""

    title: str
    url: str
    snippet: str
    published_at: str | None = None
    publisher: str | None = None
    relevance_score: float = 0.75


class WebSearchProvider(Protocol):
    async def search(
        self,
        query: str,
        *,
        freshness: str | None = None,
        locale: str = "vi-VN",
        max_results: int = 5,
    ) -> list[WebSearchResult]: ...


def _is_safe_public_https_url(url: str) -> bool:
    """Reject non-HTTPS, credentials-bearing, loopback, or private-network URLs (SSRF guard)."""

    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            return False
    except ValueError:
        # Hostname is a domain name, not an IP literal.
        pass
    return True


def _clean_snippet(raw: str, *, max_chars: int = 2800) -> str:
    without_tags = _HTML_TAG_RE.sub(" ", raw)
    collapsed = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFC", without_tags)).strip()
    return collapsed[:max_chars]


def _extract_infobox_facts(wikitext: str) -> str:
    """Extract key landmark metadata (location, architect, dates, style) from Wikipedia infobox."""

    if not wikitext:
        return ""
    cleaned = _REF_TAG_RE.sub("", wikitext)
    facts: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "=" not in line:
            continue
        key_part, val_part = line[1:].split("=", 1)
        key = key_part.strip().lower()
        label = _INFOBOX_KEYS.get(key)
        if not label:
            continue
        val = _WIKI_LINK_RE.sub(r"\1", val_part)
        val = _WIKI_TEMPLATE_RE.sub(" ", val)
        val = _HTML_TAG_RE.sub(" ", val)
        val = _WHITESPACE_RE.sub(" ", val).strip(" ,;|-")
        if val and len(val) <= 180:
            facts.append(f"{label}: {val}")
    return ". ".join(facts)


def sanitize_public_search_query(subquery: str) -> str:
    """Strip exclusive game-lore terms so private canon is never leaked to external search."""

    cleaned = unicodedata.normalize("NFC", subquery.strip())
    if not cleaned:
        return ""

    sanitized = cleaned
    for term in _PRIVATE_LORE_STRIP_TERMS:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        sanitized = pattern.sub(" ", sanitized)

    for canonical, variants in DEFAULT_SEARCH_ALIASES.items():
        for variant in (canonical, *variants):
            pattern = re.compile(re.escape(variant), re.IGNORECASE)
            sanitized = pattern.sub(" ", sanitized)

    sanitized = _WHITESPACE_RE.sub(" ", sanitized).strip(" ,;:-?.!")
    paren_match = _PAREN_TOPIC_RE.search(sanitized)
    if paren_match:
        return paren_match.group(1).strip()

    stripped = _SPOKEN_INTERJECTION_RE.sub("", sanitized).strip(" ,;:-?.!")
    stripped = _QUERY_FILLER_PREFIX_RE.sub("", stripped)
    stripped = _QUERY_FILLER_SUFFIX_RE.sub("", stripped).strip(" ,;:-?.!()")
    return stripped or sanitized


@dataclass
class _CacheEntry:
    expires_at: float
    package: EvidencePackage


class WebSearchCache:
    """Bounded TTL cache for authorized public web subqueries (consulted only AFTER Gate)."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 900,
        max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._store: dict[tuple[str, str, str], _CacheEntry] = {}

    def get(
        self, query: str, *, knowledge_domain: str, freshness: str | None = None
    ) -> EvidencePackage | None:
        now = self._clock()
        key = (query.lower().strip(), knowledge_domain, freshness or "")
        entry = self._store.get(key)
        if entry is None:
            return None
        if now >= entry.expires_at:
            self._store.pop(key, None)
            return None
        return entry.package

    def set(
        self,
        query: str,
        package: EvidencePackage,
        *,
        knowledge_domain: str,
        freshness: str | None = None,
    ) -> None:
        now = self._clock()
        if len(self._store) >= self._max_entries:
            expired = [k for k, v in self._store.items() if now >= v.expires_at]
            for k in expired:
                self._store.pop(k, None)
            if len(self._store) >= self._max_entries:
                oldest_key = min(self._store, key=lambda k: self._store[k].expires_at)
                self._store.pop(oldest_key, None)
        key = (query.lower().strip(), knowledge_domain, freshness or "")
        self._store[key] = _CacheEntry(expires_at=now + self._ttl_seconds, package=package)


class WikipediaViWebSearchProvider:
    """Public Vietnamese Wikipedia search adapter for real-world landmarks & history."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def search(
        self,
        query: str,
        *,
        freshness: str | None = None,
        locale: str = "vi-VN",
        max_results: int = 4,
    ) -> list[WebSearchResult]:
        del freshness, locale
        cleaned = query.strip()
        if not cleaned:
            return []

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; SaigonVeilwakeAIGuide/1.0; +http://localhost)",
            "Accept": "application/json",
        }
        limit = max(1, min(max_results, 6))
        search_hits: list[dict[str, Any]] = []
        pages_dict: dict[str, Any] = {}
        titles: list[str] = []
        last_exc: Exception | None = None
        for _attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                ) as client:
                    search_resp = await client.get(
                        "https://vi.wikipedia.org/w/api.php",
                        params={
                            "action": "query",
                            "list": "search",
                            "srsearch": cleaned,
                            "srlimit": limit,
                            "format": "json",
                        },
                        headers=headers,
                    )
                    search_resp.raise_for_status()
                    search_hits = search_resp.json().get("query", {}).get("search", [])
                    titles = [
                        str(hit.get("title") or "").strip()
                        for hit in search_hits
                        if hit.get("title")
                    ]
                    if not titles:
                        return []

                    detail_resp = await client.get(
                        "https://vi.wikipedia.org/w/api.php",
                        params={
                            "action": "query",
                            "titles": "|".join(titles[:limit]),
                            "prop": "extracts|info|revisions",
                            "inprop": "url",
                            "explaintext": 1,
                            "exchars": 2800,
                            "rvprop": "content",
                            "rvsection": 0,
                            "format": "json",
                        },
                        headers=headers,
                    )
                    detail_resp.raise_for_status()
                    pages_dict = detail_resp.json().get("query", {}).get("pages", {})
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc

        if not pages_dict:
            return []

        snippet_by_title = {
            str(hit.get("title") or "").strip(): str(hit.get("snippet") or "")
            for hit in search_hits
        }
        page_by_title = {
            str(page.get("title") or "").strip(): page for page in pages_dict.values()
        }
        results: list[WebSearchResult] = []
        for idx, title in enumerate(titles[:limit]):
            page = page_by_title.get(title)
            if not page:
                continue
            url = str(page.get("fullurl") or "").strip()
            revisions = page.get("revisions") or []
            rev_first = revisions[0] if revisions else {}
            wikitext = str(
                rev_first.get("*") or rev_first.get("slots", {}).get("main", {}).get("*") or ""
            )
            infobox_facts = _extract_infobox_facts(wikitext)
            raw_extract = str(page.get("extract") or "") or snippet_by_title.get(title, "")
            combined_text = f"{infobox_facts}. {raw_extract}" if infobox_facts else raw_extract
            extract = _clean_snippet(combined_text, max_chars=2800)
            if not title or not url or not extract:
                continue
            score = max(0.60, 0.90 - idx * 0.06)
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    snippet=extract,
                    published_at=page.get("touched"),
                    publisher="vi.wikipedia.org",
                    relevance_score=score,
                )
            )
        return results


class TavilyWebSearchProvider:
    """Tavily Search API adapter following the unified WebSearchProvider interface."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("TAVILY_API_KEY is required for TavilyWebSearchProvider.")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def search(
        self,
        query: str,
        *,
        freshness: str | None = None,
        locale: str = "vi-VN",
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        del locale
        body: dict[str, Any] = {
            "api_key": self._api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": max(1, min(max_results, 8)),
        }
        if freshness in {"day", "week", "month", "year"}:
            body["time_range"] = freshness

        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.post("https://api.tavily.com/search", json=body)
            response.raise_for_status()
            payload = response.json()

        results: list[WebSearchResult] = []
        for raw_item in payload.get("results", [])[:max_results]:
            title = str(raw_item.get("title") or "").strip()
            url = str(raw_item.get("url") or "").strip()
            snippet = _clean_snippet(str(raw_item.get("content") or ""))
            if not title or not url or not snippet:
                continue
            parsed_host = urlparse(url).hostname or "web"
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    published_at=raw_item.get("published_date"),
                    publisher=parsed_host,
                    relevance_score=float(raw_item.get("score") or 0.75),
                )
            )
        return results


class BraveWebSearchProvider:
    """Brave Search API adapter following the unified WebSearchProvider interface."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: int = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("BRAVE_SEARCH_API_KEY is required for BraveWebSearchProvider.")
        self._api_key = api_key.strip()
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def search(
        self,
        query: str,
        *,
        freshness: str | None = None,
        locale: str = "vi-VN",
        max_results: int = 5,
    ) -> list[WebSearchResult]:
        del locale
        params: dict[str, Any] = {
            "q": query,
            "count": max(1, min(max_results, 8)),
            "country": "VN",
            "search_lang": "vi",
        }
        if freshness:
            params["freshness"] = freshness

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()

        results: list[WebSearchResult] = []
        for idx, raw_item in enumerate(
            payload.get("web", {}).get("results", [])[:max_results]
        ):
            title = str(raw_item.get("title") or "").strip()
            url = str(raw_item.get("url") or "").strip()
            snippet = _clean_snippet(str(raw_item.get("description") or ""))
            if not title or not url or not snippet:
                continue
            parsed_host = urlparse(url).hostname or "web"
            results.append(
                WebSearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    published_at=raw_item.get("age"),
                    publisher=parsed_host,
                    relevance_score=max(0.60, 0.88 - idx * 0.05),
                )
            )
        return results


class WebSearchService:
    """Enforces Web Eligibility Gate, query sanitization, SSRF/source validation, and caching."""

    def __init__(
        self,
        provider: WebSearchProvider,
        *,
        cache: WebSearchCache | None = None,
        max_results: int = 4,
    ) -> None:
        self._provider = provider
        self._cache = cache or WebSearchCache()
        self._max_results = max_results
        self.call_count = 0
        self.last_queries: list[str] = []

    async def search_if_eligible(
        self,
        decision: RoutingDecision,
        *,
        allowed_story_access_level: int = 0,
        freshness: str | None = None,
    ) -> EvidencePackage:
        """Execute external web search ONLY if RoutingDecision explicitly permits it."""

        # 1. Hard Web Eligibility Gate check (Proposal v1.3 Section 6.3).
        if (
            not decision.web_search_allowed
            or decision.knowledge_domain in {"game_lore", "game_state", "unknown", "direct"}
            or decision.rag_evidence_status
            in {
                RagEvidenceStatus.SUFFICIENT,
                RagEvidenceStatus.ACCESS_RESTRICTED,
                RagEvidenceStatus.RETRIEVAL_ERROR,
                RagEvidenceStatus.UNKNOWN_DOMAIN,
            }
        ):
            return EvidencePackage(
                query=decision.query,
                normalized_query=decision.query,
                knowledge_domain=decision.knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="WEB_SEARCH_NOT_ELIGIBLE",
            )

        raw_subquery = decision.public_web_subquery or decision.query
        sanitized_query = sanitize_public_search_query(raw_subquery)
        if not sanitized_query:
            return EvidencePackage(
                query=decision.query,
                normalized_query="",
                knowledge_domain=decision.knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="EMPTY_PUBLIC_SUBQUERY",
            )

        # 2. Cache check happens strictly AFTER eligibility gate and sanitization.
        cached = self._cache.get(
            sanitized_query,
            knowledge_domain=decision.knowledge_domain,
            freshness=freshness,
        )
        if cached is not None:
            return cached

        # 3. Call provider and handle timeout/rate-limit fail-closed.
        try:
            self.call_count += 1
            self.last_queries.append(sanitized_query)
            raw_results = await self._provider.search(
                sanitized_query,
                freshness=freshness,
                locale="vi-VN",
                max_results=self._max_results,
            )
        except Exception:
            return EvidencePackage(
                query=decision.query,
                normalized_query=sanitized_query,
                knowledge_domain=decision.knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="WEB_PROVIDER_ERROR",
            )

        package = self._normalize_results(
            original_query=decision.query,
            sanitized_query=sanitized_query,
            knowledge_domain=decision.knowledge_domain,
            allowed_story_access_level=allowed_story_access_level,
            results=raw_results,
        )
        if package.is_sufficient:
            self._cache.set(
                sanitized_query,
                package,
                knowledge_domain=decision.knowledge_domain,
                freshness=freshness,
            )
        return package

    @staticmethod
    def _normalize_results(
        *,
        original_query: str,
        sanitized_query: str,
        knowledge_domain: str,
        allowed_story_access_level: int,
        results: Sequence[WebSearchResult],
    ) -> EvidencePackage:
        retrieved_at = datetime.now(timezone.utc).isoformat()
        items: list[EvidenceItem] = []
        citations: list[Citation] = []
        seen_urls: set[str] = set()

        for result in results:
            url = result.url.strip()
            if not _is_safe_public_https_url(url) or url in seen_urls:
                continue
            snippet = _clean_snippet(result.snippet)
            title = result.title.strip()
            if not snippet or not title:
                continue

            seen_urls.add(url)
            idx = len(items) + 1
            evidence_id = f"ev_{idx:02d}"
            span = f"web-{idx:02d}"
            publisher = result.publisher or (urlparse(url).hostname or "web")
            citation = Citation(
                evidence_id=evidence_id,
                source_id=f"web:{publisher}",
                source_type="web_search",
                title=title,
                document_version=result.published_at or "live",
                section_path=(publisher, title),
                source_span=span,
                url=url,
            )
            items.append(
                EvidenceItem(
                    evidence_id=evidence_id,
                    chunk_id=uuid5(NAMESPACE_URL, url),
                    source_kind="web_search",
                    source_id=f"web:{publisher}",
                    knowledge_domain=knowledge_domain,
                    title=title,
                    url=url,
                    document_version=result.published_at or "live",
                    section_path=(publisher, title),
                    source_span=span,
                    text=snippet,
                    story_access_level=0,
                    canonical_status="external_unverified",
                    trust_tier="external_web",
                    visibility_scope="public",
                    semantic_score=result.relevance_score,
                    lexical_score=result.relevance_score,
                    relevance_score=result.relevance_score,
                    retrieved_at=retrieved_at,
                    citation=citation,
                )
            )
            citations.append(citation)

        if not items:
            return EvidencePackage(
                query=original_query,
                normalized_query=sanitized_query,
                knowledge_domain=knowledge_domain,
                allowed_story_access_level=allowed_story_access_level,
                items=(),
                citations=(),
                is_sufficient=False,
                insufficiency_reason="NO_VALID_WEB_EVIDENCE",
            )

        return EvidencePackage(
            query=original_query,
            normalized_query=sanitized_query,
            knowledge_domain=knowledge_domain,
            allowed_story_access_level=allowed_story_access_level,
            items=tuple(items),
            citations=tuple(citations),
            is_sufficient=True,
            insufficiency_reason=None,
        )


def merge_evidence_packages(
    rag_evidence: EvidencePackage | None,
    web_evidence: EvidencePackage | None,
    *,
    query: str,
    knowledge_domain: str,
    allowed_story_access_level: int = 0,
    max_items: int = 8,
) -> EvidencePackage:
    """Combine valid internal RAG evidence and validated Web Search evidence with stable IDs."""

    combined_raw: list[EvidenceItem] = []
    if rag_evidence and rag_evidence.items:
        combined_raw.extend(
            item
            for item in rag_evidence.items
            if item.story_access_level <= allowed_story_access_level
        )
    if web_evidence and web_evidence.items:
        combined_raw.extend(web_evidence.items)

    if not combined_raw:
        return EvidencePackage(
            query=query,
            normalized_query=query,
            knowledge_domain=knowledge_domain,
            allowed_story_access_level=allowed_story_access_level,
            items=(),
            citations=(),
            is_sufficient=False,
            insufficiency_reason="NO_COMBINED_EVIDENCE",
        )

    renumbered_items: list[EvidenceItem] = []
    renumbered_citations: list[Citation] = []
    for idx, item in enumerate(combined_raw[:max_items], start=1):
        ev_id = f"ev_{idx:02d}"
        citation = Citation(
            evidence_id=ev_id,
            source_id=item.citation.source_id,
            source_type=item.citation.source_type,
            title=item.citation.title,
            document_version=item.citation.document_version,
            section_path=item.citation.section_path,
            source_span=item.citation.source_span,
            url=item.citation.url,
        )
        renumbered_items.append(
            EvidenceItem(
                evidence_id=ev_id,
                chunk_id=item.chunk_id,
                source_kind=item.source_kind,
                source_id=item.source_id,
                knowledge_domain=item.knowledge_domain,
                title=item.title,
                url=item.url,
                document_version=item.document_version,
                section_path=item.section_path,
                source_span=item.source_span,
                text=item.text,
                story_access_level=item.story_access_level,
                canonical_status=item.canonical_status,
                trust_tier=item.trust_tier,
                visibility_scope=item.visibility_scope,
                semantic_score=item.semantic_score,
                lexical_score=item.lexical_score,
                relevance_score=item.relevance_score,
                retrieved_at=item.retrieved_at,
                citation=citation,
            )
        )
        renumbered_citations.append(citation)

    return EvidencePackage(
        query=query,
        normalized_query=query,
        knowledge_domain=knowledge_domain,
        allowed_story_access_level=allowed_story_access_level,
        items=tuple(renumbered_items),
        citations=tuple(renumbered_citations),
        is_sufficient=True,
        insufficiency_reason=None,
    )
