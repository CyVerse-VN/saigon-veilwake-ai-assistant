"""Prepare and commit approved DOCX knowledge chunks to Gemini Embeddings and Supabase."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.integrations.embeddings.gemini import GeminiEmbeddingProvider
from app.modules.knowledge.docx_loader import (
    ManifestError,
    _load_manifest,
    _policy_for_block,
    _resolve_source_path,
    _select_rule,
    _sha256,
    build_dry_run_report,
    load_docx,
)

PARSER_VERSION = "docx_loader_v1"
CHUNKER_VERSION = "heading_paragraph_v1"


class DocumentEmbedder(Protocol):
    async def embed_document(self, text: str, title: str | None = None) -> list[float]: ...


@dataclass(frozen=True)
class PreparedChunk:
    source_span: str
    chunk_ordinal: int
    content: str
    content_hash: str
    section_path: tuple[str, ...]
    metadata: dict[str, Any]


def prepare_chunks(manifest_path: Path, project_root: Path | None = None) -> list[PreparedChunk]:
    """Create spoiler-filtered chunks from a manifest-controlled DOCX source."""

    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    source_path = _resolve_source_path(manifest_path, manifest, project_root)
    source_hash = _sha256(source_path)
    if source_hash != str(manifest["source"]["sha256"]).lower():
        raise ManifestError("Source SHA-256 does not match manifest; refusing to prepare chunks.")

    document = load_docx(source_path)
    rules = list(manifest.get("section_rules", [])) + list(manifest.get("child_rules", []))
    table_row_rules = list(manifest.get("table_row_rules", []))
    max_chars = _max_chars(manifest)
    chunks: list[PreparedChunk] = []
    pending: list[tuple[str, str]] = []
    pending_path: tuple[str, ...] | None = None
    pending_metadata: dict[str, Any] | None = None

    def flush_pending() -> None:
        nonlocal pending, pending_path, pending_metadata
        if not pending:
            return
        assert pending_path is not None
        assert pending_metadata is not None
        body_text = "\n\n".join(text for _, text in pending)
        chunks.append(
            _prepared_chunk(
                ordinal=len(chunks),
                source_spans=[span for span, _ in pending],
                content=_with_breadcrumb(pending_path, body_text),
                section_path=pending_path,
                metadata=pending_metadata,
            )
        )
        pending = []
        pending_path = None
        pending_metadata = None

    for block in document.blocks:
        rule = _select_rule(block, rules)
        policy = _policy_for_block(block, rule, manifest, table_row_rules)
        if not policy["included"]:
            continue

        metadata = policy["metadata"]
        section_path = tuple(policy.get("section_path", block.heading_path))
        if block.kind == "table_row":
            flush_pending()
            chunks.append(
                _prepared_chunk(
                    ordinal=len(chunks),
                    source_spans=[block.block_id],
                    content=_with_breadcrumb(section_path, block.text),
                    section_path=section_path,
                    metadata=metadata,
                )
            )
            continue

        same_group = pending_path == section_path and pending_metadata == metadata
        breadcrumb_len = len(" > ".join(section_path)) + 2 if section_path else 0
        next_size = (
            breadcrumb_len
            + sum(len(text) for _, text in pending)
            + len(block.text)
            + (2 * len(pending))
        )
        if pending and (not same_group or next_size > max_chars):
            flush_pending()
        pending.append((block.block_id, block.text))
        pending_path = section_path
        pending_metadata = metadata

    flush_pending()
    return chunks


def build_ingestion_plan(manifest_path: Path, project_root: Path | None = None) -> dict[str, Any]:
    """Return a serializable, no-write review plan for a future ingestion commit."""

    chunks = prepare_chunks(manifest_path, project_root)
    return {
        "report_type": "knowledge_ingestion_plan",
        "dry_run": True,
        "embedding_performed": False,
        "database_write_performed": False,
        "chunk_count": len(chunks),
        "chunks": [
            {
                **asdict(chunk),
                "section_path": list(chunk.section_path),
                "content_preview": _preview(chunk.content),
                "content": None,
            }
            for chunk in chunks
        ],
    }


def _prepared_chunk(
    *,
    ordinal: int,
    source_spans: list[str],
    content: str,
    section_path: tuple[str, ...],
    metadata: dict[str, Any],
) -> PreparedChunk:
    return PreparedChunk(
        source_span="..".join(source_spans),
        chunk_ordinal=ordinal,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        section_path=section_path,
        metadata=dict(metadata),
    )


def _max_chars(manifest: dict[str, Any]) -> int:
    chunking = manifest.get("ingestion_policy", {}).get("chunking", {})
    max_tokens = int(chunking.get("max_tokens", 650))
    return max(500, max_tokens * 4)


def _with_breadcrumb(heading_path: tuple[str, ...], text: str) -> str:
    if not heading_path:
        return text
    return f"{' > '.join(heading_path)}\n\n{text}"


def _preview(content: str, limit: int = 280) -> str:
    normalized = " ".join(content.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_pgvector(values: list[float]) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"


def _map_approval_status(raw_status: Any) -> str:
    normalized = str(raw_status or "").strip().lower()
    if normalized in {"approved", "approved_for_reviewed_ingestion", "design_approved"}:
        return "APPROVED"
    if normalized == "retired":
        return "RETIRED"
    if normalized == "rejected":
        return "REJECTED"
    return "PENDING"


def _map_visibility(raw_visibility: Any) -> str:
    normalized = str(raw_visibility or "player").strip().upper()
    if normalized not in {"PLAYER", "INTERNAL"}:
        raise ManifestError(f"Unsupported visibility value: {raw_visibility}")
    return normalized


def _validate_chunks_for_commit(
    manifest: dict[str, Any], dry_run_report: dict[str, Any], chunks: list[PreparedChunk]
) -> None:
    if not manifest.get("ingestion_policy", {}).get("embedding", {}).get("enabled", False):
        raise ManifestError(
            "Manifest embedding policy is disabled; review the plan and enable it before commit."
        )
    review = dry_run_report.get("review", {})
    if review.get("unmatched_rule_ids") or review.get("unmatched_table_row_rule_ids"):
        raise ManifestError("Manifest contains unmatched rules; refusing to commit.")
    if not chunks:
        raise ManifestError("No chunks prepared for commit.")

    for chunk in chunks:
        visibility = _map_visibility(chunk.metadata.get("visibility", "player"))
        if visibility != "PLAYER":
            raise ManifestError(
                f"Chunk {chunk.chunk_ordinal} ({chunk.source_span}) is not player-visible."
            )
        access_level = int(chunk.metadata.get("story_access_level", 0))
        if access_level < 5 and (
            "Người Giữ Im Lặng" in chunk.content
            or any("Người Giữ Im Lặng" in heading for heading in chunk.section_path)
        ):
            raise ManifestError(
                f"Spoiler boundary violation in chunk {chunk.chunk_ordinal} ({chunk.source_span}): "
                "level-5 antagonist leaked into a lower story_access_level chunk."
            )


class SupabaseKnowledgeRepository:
    """Persist approved knowledge sources, versions, chunks, and ingestion jobs via PostgREST."""

    def __init__(
        self,
        supabase_url: str,
        supabase_secret_key: str,
        *,
        timeout_seconds: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not supabase_url or not supabase_secret_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for commit.")
        self._base_url = f"{supabase_url.rstrip('/')}/rest/v1"
        self._headers = {
            "apikey": supabase_secret_key,
            "Authorization": f"Bearer {supabase_secret_key}",
            "Content-Type": "application/json",
        }
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout_seconds,
            transport=self._transport,
        )

    async def upsert_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                "/knowledge_sources",
                params={"on_conflict": "source_key"},
                headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                json=payload,
            )
            response.raise_for_status()
            rows = response.json()
            return rows[0]

    async def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                "/ingestion_jobs",
                headers={"Prefer": "return=representation"},
                json=payload,
            )
            response.raise_for_status()
            rows = response.json()
            return rows[0]

    async def update_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.patch(
                "/ingestion_jobs",
                params={"id": f"eq.{job_id}"},
                headers={"Prefer": "return=representation"},
                json=payload,
            )
            response.raise_for_status()
            rows = response.json()
            return rows[0]

    async def upsert_document_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                "/document_versions",
                params={"on_conflict": "source_id,document_version"},
                headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                json=payload,
            )
            response.raise_for_status()
            rows = response.json()
            return rows[0]

    async def upsert_chunks(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async with self._client() as client:
            response = await client.post(
                "/document_chunks",
                params={
                    "on_conflict": "document_version_id,source_span,chunk_ordinal,chunker_version"
                },
                headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                json=payloads,
            )
            response.raise_for_status()
            return list(response.json())

    async def delete_stale_chunks(
        self, document_version_id: str, chunker_version: str, min_ordinal: int
    ) -> None:
        async with self._client() as client:
            response = await client.delete(
                "/document_chunks",
                params={
                    "document_version_id": f"eq.{document_version_id}",
                    "chunker_version": f"eq.{chunker_version}",
                    "chunk_ordinal": f"gte.{min_ordinal}",
                },
            )
            response.raise_for_status()

    async def activate_document_version(
        self, source_id: str, document_version_id: str
    ) -> dict[str, Any]:
        async with self._client() as client:
            deactivate = await client.patch(
                "/document_versions",
                params={
                    "source_id": f"eq.{source_id}",
                    "is_active": "eq.true",
                    "id": f"neq.{document_version_id}",
                },
                json={"is_active": False},
            )
            deactivate.raise_for_status()

            activate = await client.patch(
                "/document_versions",
                params={"id": f"eq.{document_version_id}"},
                headers={"Prefer": "return=representation"},
                json={"approval_status": "APPROVED", "is_active": True},
            )
            activate.raise_for_status()
            rows = activate.json()
            return rows[0]


async def commit_ingestion(
    manifest_path: Path,
    project_root: Path | None = None,
    *,
    embedder: DocumentEmbedder | None = None,
    repository: SupabaseKnowledgeRepository | None = None,
    requested_by: str = "cli_commit",
) -> dict[str, Any]:
    """Embed approved chunks with Gemini and atomically activate the version in Supabase."""

    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    source_path = _resolve_source_path(manifest_path, manifest, project_root)
    source_hash = _sha256(source_path)
    if source_hash != str(manifest["source"]["sha256"]).lower():
        raise ManifestError("Source SHA-256 does not match manifest; refusing to commit.")

    dry_run_report = build_dry_run_report(manifest_path, project_root)
    chunks = prepare_chunks(manifest_path, project_root)
    _validate_chunks_for_commit(manifest, dry_run_report, chunks)

    settings = get_settings()
    embedding_model = getattr(embedder, "model_name", settings.gemini_embedding_model)
    embedding_dimensions = int(
        getattr(embedder, "dimensions", settings.gemini_embedding_dimensions)
    )
    embedding_version = f"dim-{embedding_dimensions}"

    if embedder is None:
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required for --commit ingestion.")
        embedder = GeminiEmbeddingProvider(
            api_key=settings.gemini_api_key,
            model=embedding_model,
            dimensions=embedding_dimensions,
            timeout_seconds=settings.gemini_request_timeout_seconds,
        )

    if repository is None:
        if not settings.supabase_url or not settings.supabase_secret_key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY are required for --commit ingestion."
            )
        repository = SupabaseKnowledgeRepository(
            supabase_url=settings.supabase_url,
            supabase_secret_key=settings.supabase_secret_key,
        )

    source_meta = manifest["source"]
    source_title = str(source_meta["title"])
    source_row = await repository.upsert_source(
        {
            "source_key": str(source_meta["source_id"]),
            "title": source_title,
            "canonical_url": source_meta.get("canonical_url"),
            "knowledge_domain": str(source_meta["knowledge_domain"]),
            "authority": str(source_meta["authority"]),
            "approval_status": _map_approval_status(source_meta.get("approval_status")),
            "visibility": _map_visibility(source_meta.get("visibility", "player")),
            "language": str(source_meta.get("language", "vi")),
            "default_story_access_level": int(
                manifest["default_chunk_metadata"].get("story_access_level", 0)
            ),
            "license": source_meta.get("license"),
            "metadata": {
                "manifest_id": manifest["manifest_id"],
                "notes": list(source_meta.get("notes", [])),
            },
        }
    )
    source_id = str(source_row["id"])

    started_at = _utc_now_iso()
    job_row = await repository.create_job(
        {
            "source_id": source_id,
            "run_mode": "COMMIT",
            "status": "RUNNING",
            "requested_by": requested_by,
            "parser_version": PARSER_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "embedding_model": embedding_model,
            "source_hash": source_hash,
            "chunks_discovered": len(chunks),
            "chunks_indexed": 0,
            "started_at": started_at,
        }
    )
    job_id = str(job_row["id"])

    try:
        if hasattr(embedder, "embed_documents"):
            vectors = await embedder.embed_documents(  # type: ignore[attr-defined]
                [chunk.content for chunk in chunks], title=source_title
            )
        else:
            vectors = [
                await embedder.embed_document(chunk.content, title=source_title)
                for chunk in chunks
            ]

        if len(vectors) != len(chunks):
            raise RuntimeError("Embedding count did not match prepared chunk count.")

        version_row = await repository.upsert_document_version(
            {
                "source_id": source_id,
                "document_version": str(source_meta["document_version"]),
                "content_hash": source_hash,
                "original_filename": source_path.name,
                "source_published_at": source_meta.get("published_at"),
                "approval_status": "PENDING",
                "is_active": False,
                "parser_version": PARSER_VERSION,
                "manifest_version": str(manifest.get("$schema_version", "1.0")),
                "metadata": {
                    "manifest_id": manifest["manifest_id"],
                    "chunk_count": len(chunks),
                },
            }
        )
        document_version_id = str(version_row["id"])

        embedded_at = _utc_now_iso()
        chunk_payloads: list[dict[str, Any]] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk_payloads.append(
                {
                    "document_version_id": document_version_id,
                    "source_span": chunk.source_span,
                    "chunk_ordinal": chunk.chunk_ordinal,
                    "section_path": list(chunk.section_path),
                    "content": chunk.content,
                    "content_hash": chunk.content_hash,
                    "language": str(chunk.metadata.get("language", "vi")),
                    "knowledge_domain": str(chunk.metadata.get("knowledge_domain", "game_lore")),
                    "visibility": _map_visibility(chunk.metadata.get("visibility", "player")),
                    "story_access_level": int(chunk.metadata.get("story_access_level", 0)),
                    "canonical_status": str(
                        chunk.metadata.get("canonical_status", "design_approved")
                    ),
                    "tags": list(chunk.metadata.get("tags", [])),
                    "metadata": dict(chunk.metadata),
                    "embedding": _format_pgvector(vector),
                    "embedding_model": embedding_model,
                    "embedding_version": embedding_version,
                    "embedded_at": embedded_at,
                    "chunker_version": CHUNKER_VERSION,
                    "token_count": max(1, len(chunk.content.split())),
                }
            )

        upserted_chunks = await repository.upsert_chunks(chunk_payloads)
        await repository.delete_stale_chunks(
            document_version_id, CHUNKER_VERSION, min_ordinal=len(chunks)
        )
        active_version = await repository.activate_document_version(source_id, document_version_id)
        completed_job = await repository.update_job(
            job_id,
            {
                "document_version_id": document_version_id,
                "status": "COMPLETED",
                "chunks_indexed": len(upserted_chunks),
                "completed_at": _utc_now_iso(),
            },
        )
    except Exception as exc:
        try:
            await repository.update_job(
                job_id,
                {
                    "status": "FAILED",
                    "error_code": type(exc).__name__,
                    "safe_error_detail": "Ingestion commit failed during embedding or DB write.",
                    "completed_at": _utc_now_iso(),
                },
            )
        except Exception:
            pass
        raise

    return {
        "report_type": "knowledge_ingestion_commit",
        "dry_run": False,
        "embedding_performed": True,
        "database_write_performed": True,
        "source_id": source_id,
        "source_key": str(source_meta["source_id"]),
        "document_version_id": document_version_id,
        "document_version": str(source_meta["document_version"]),
        "is_active": bool(active_version.get("is_active", True)),
        "job_id": job_id,
        "job_status": str(completed_job.get("status", "COMPLETED")),
        "embedding_model": embedding_model,
        "embedding_version": embedding_version,
        "chunks_discovered": len(chunks),
        "chunks_indexed": len(upserted_chunks),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare or commit RAG chunks for approved DOCX sources."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Embed chunks with Gemini and upsert into Supabase.",
    )
    args = parser.parse_args()

    if args.commit:
        report = asyncio.run(commit_ingestion(args.manifest, args.project_root))
    else:
        report = build_ingestion_plan(args.manifest, args.project_root)

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
