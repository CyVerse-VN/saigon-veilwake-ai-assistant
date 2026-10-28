from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.modules.knowledge.ingestion import (
    SupabaseKnowledgeRepository,
    build_ingestion_plan,
    commit_ingestion,
    prepare_chunks,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "base_knowlegde" / "game_world_v1.0.manifest.json"


class _FakeEmbedder:
    model_name = "gemini-embedding-001"
    dimensions = 768

    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[tuple[list[str], str | None]] = []

    async def embed_documents(
        self, texts: list[str], *, title: str | None = None
    ) -> list[list[float]]:
        self.calls.append((list(texts), title))
        if self.should_fail:
            raise RuntimeError("Simulated embedding provider failure.")
        return [[1.0] + [0.0] * 767 for _ in texts]

    async def embed_document(self, text: str, title: str | None = None) -> list[float]:
        return (await self.embed_documents([text], title=title))[0]


def test_ingestion_plan_excludes_internal_design_and_preserves_spoiler_levels() -> None:
    chunks = prepare_chunks(MANIFEST_PATH, PROJECT_ROOT)
    plan = build_ingestion_plan(MANIFEST_PATH, PROJECT_ROOT)

    assert len(chunks) == 66
    assert plan["dry_run"] is True
    assert plan["embedding_performed"] is False
    assert plan["database_write_performed"] is False
    assert all("XI. Trạng thái quyết định" not in chunk.content for chunk in chunks)
    assert any(chunk.metadata["story_access_level"] == 5 for chunk in chunks)
    assert any(
        chunk.metadata["story_access_level"] == 0 and "Người Kể Tiếp" in chunk.content
        for chunk in chunks
    )
    assert all(chunk.content_hash for chunk in chunks)

    # Spoiler invariant: antagonist name must never appear in chunks below level 5.
    lower_access_chunks = [
        chunk for chunk in chunks if int(chunk.metadata["story_access_level"]) < 5
    ]
    assert lower_access_chunks
    assert all("Người Giữ Im Lặng" not in chunk.content for chunk in lower_access_chunks)
    assert all(
        all("Người Giữ Im Lặng" not in heading for heading in chunk.section_path)
        for chunk in lower_access_chunks
    )

    # Multi-paragraph chunks should prepend their heading breadcrumb once, not per paragraph.
    opening_chunk = next(chunk for chunk in chunks if chunk.chunk_ordinal == 1)
    assert opening_chunk.content.count("I. Lời mở đầu dành cho người chơi") == 1


@pytest.mark.asyncio
async def test_commit_ingestion_embeds_and_persists_to_supabase() -> None:
    recorded_requests: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        recorded_requests.append((request.method, request.url.path, body))
        path = request.url.path

        if request.method == "POST" and path.endswith("/knowledge_sources"):
            assert body["visibility"] == "PLAYER"
            assert body["approval_status"] == "APPROVED"
            return httpx.Response(201, json=[{"id": "00000000-0000-0000-0000-000000000001"}])
        if request.method == "POST" and path.endswith("/ingestion_jobs"):
            assert body["run_mode"] == "COMMIT"
            assert body["status"] == "RUNNING"
            assert body["chunks_discovered"] == 66
            return httpx.Response(201, json=[{"id": "00000000-0000-0000-0000-000000000002"}])
        if request.method == "POST" and path.endswith("/document_versions"):
            assert body["approval_status"] == "PENDING"
            assert body["is_active"] is False
            return httpx.Response(201, json=[{"id": "00000000-0000-0000-0000-000000000003"}])
        if request.method == "POST" and path.endswith("/document_chunks"):
            assert len(body) == 66
            assert all(row["visibility"] == "PLAYER" for row in body)
            assert all(row["embedding_model"] == "gemini-embedding-001" for row in body)
            return httpx.Response(201, json=body)
        if request.method == "DELETE" and path.endswith("/document_chunks"):
            return httpx.Response(204)
        if request.method == "PATCH" and path.endswith("/document_versions"):
            if "is_active=eq.true" in str(request.url.query):
                return httpx.Response(200, json=[])
            assert body == {"approval_status": "APPROVED", "is_active": True}
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "00000000-0000-0000-0000-000000000003",
                        "approval_status": "APPROVED",
                        "is_active": True,
                    }
                ],
            )
        if request.method == "PATCH" and path.endswith("/ingestion_jobs"):
            return httpx.Response(
                200,
                json=[{"id": "00000000-0000-0000-0000-000000000002", **body}],
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    embedder = _FakeEmbedder()
    repository = SupabaseKnowledgeRepository(
        "https://example.supabase.co",
        "secret-key",
        transport=httpx.MockTransport(handler),
    )

    result = await commit_ingestion(
        MANIFEST_PATH,
        PROJECT_ROOT,
        embedder=embedder,
        repository=repository,
    )

    assert result["dry_run"] is False
    assert result["embedding_performed"] is True
    assert result["database_write_performed"] is True
    assert result["chunks_discovered"] == 66
    assert result["chunks_indexed"] == 66
    assert result["is_active"] is True
    assert result["job_status"] == "COMPLETED"
    assert len(embedder.calls) == 1
    assert len(embedder.calls[0][0]) == 66


@pytest.mark.asyncio
async def test_commit_ingestion_marks_job_failed_on_embedding_error() -> None:
    job_patches: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        path = request.url.path
        if request.method == "POST" and path.endswith("/knowledge_sources"):
            return httpx.Response(201, json=[{"id": "00000000-0000-0000-0000-000000000001"}])
        if request.method == "POST" and path.endswith("/ingestion_jobs"):
            return httpx.Response(201, json=[{"id": "00000000-0000-0000-0000-000000000002"}])
        if request.method == "PATCH" and path.endswith("/ingestion_jobs"):
            job_patches.append(body)
            return httpx.Response(
                200,
                json=[{"id": "00000000-0000-0000-0000-000000000002", **body}],
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    repository = SupabaseKnowledgeRepository(
        "https://example.supabase.co",
        "secret-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="Simulated embedding provider failure"):
        await commit_ingestion(
            MANIFEST_PATH,
            PROJECT_ROOT,
            embedder=_FakeEmbedder(should_fail=True),
            repository=repository,
        )

    assert len(job_patches) == 1
    assert job_patches[0]["status"] == "FAILED"
    assert job_patches[0]["error_code"] == "RuntimeError"
