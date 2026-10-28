from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    PROJECT_ROOT / "supabase" / "migrations" / "20261028160000_create_rag_knowledge_schema.sql"
)


def test_rag_schema_migration_enforces_knowledge_boundaries() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8").lower()

    for table in (
        "knowledge_sources",
        "document_versions",
        "document_chunks",
        "ingestion_jobs",
    ):
        assert f"create table public.{table}" in sql
        assert f"alter table public.{table} enable row level security" in sql
        assert f"revoke all on table public.{table} from anon, authenticated" in sql

    assert "extensions.vector(768)" in sql
    assert "to_tsvector('simple', content)" in sql
    assert "story_access_level integer not null default 0" in sql
    assert "document_versions_one_active_per_source_idx" in sql
    assert (
        "embedding is null or (embedding_model is not null and embedding_version is not null)"
        in sql
    )
