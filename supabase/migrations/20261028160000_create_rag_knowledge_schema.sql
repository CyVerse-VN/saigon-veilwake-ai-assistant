-- RAG knowledge foundation for Saigon: Veilwake.
-- This migration stores approved, versioned knowledge only. It does not ingest a
-- document, call an embedding provider, or grant direct client access to lore.

begin;

create extension if not exists vector with schema extensions;

create table public.knowledge_sources (
    id uuid primary key default gen_random_uuid(),
    source_key text not null unique,
    title text not null,
    canonical_url text,
    knowledge_domain text not null,
    authority text not null,
    approval_status text not null default 'PENDING',
    visibility text not null default 'PLAYER',
    language text not null default 'vi',
    default_story_access_level integer not null default 0,
    license text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint knowledge_sources_domain_check
        check (knowledge_domain in ('game_lore', 'real_world', 'dynamic_web')),
    constraint knowledge_sources_authority_check
        check (authority in ('game_design_canonical', 'reviewed_public_source')),
    constraint knowledge_sources_approval_status_check
        check (approval_status in ('PENDING', 'APPROVED', 'RETIRED')),
    constraint knowledge_sources_visibility_check
        check (visibility in ('PLAYER', 'INTERNAL')),
    constraint knowledge_sources_story_access_check
        check (default_story_access_level >= 0)
);

create table public.document_versions (
    id uuid primary key default gen_random_uuid(),
    source_id uuid not null references public.knowledge_sources(id) on delete cascade,
    document_version text not null,
    content_hash char(64) not null,
    original_filename text not null,
    source_published_at timestamptz,
    approval_status text not null default 'PENDING',
    is_active boolean not null default false,
    parser_version text not null,
    manifest_version text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint document_versions_approval_status_check
        check (approval_status in ('PENDING', 'APPROVED', 'REJECTED', 'RETIRED')),
    constraint document_versions_content_hash_check
        check (content_hash ~ '^[0-9a-f]{64}$'),
    constraint document_versions_source_version_unique unique (source_id, document_version),
    constraint document_versions_source_hash_unique unique (source_id, content_hash)
);

create unique index document_versions_one_active_per_source_idx
    on public.document_versions (source_id)
    where is_active;

create table public.document_chunks (
    id uuid primary key default gen_random_uuid(),
    document_version_id uuid not null references public.document_versions(id) on delete cascade,
    source_span text not null,
    chunk_ordinal integer not null,
    section_path text[] not null default '{}'::text[],
    content text not null,
    content_hash char(64) not null,
    language text not null default 'vi',
    knowledge_domain text not null,
    visibility text not null default 'PLAYER',
    story_access_level integer not null default 0,
    canonical_status text not null,
    tags text[] not null default '{}'::text[],
    metadata jsonb not null default '{}'::jsonb,
    fts tsvector generated always as (to_tsvector('simple', content)) stored,
    embedding extensions.vector(768),
    embedding_model text,
    embedding_version text,
    embedded_at timestamptz,
    chunker_version text not null,
    token_count integer,
    created_at timestamptz not null default now(),
    constraint document_chunks_content_check check (length(btrim(content)) > 0),
    constraint document_chunks_content_hash_check check (content_hash ~ '^[0-9a-f]{64}$'),
    constraint document_chunks_ordinal_check check (chunk_ordinal >= 0),
    constraint document_chunks_story_access_check check (story_access_level >= 0),
    constraint document_chunks_visibility_check check (visibility in ('PLAYER', 'INTERNAL')),
    constraint document_chunks_embedding_metadata_check check (
        embedding is null or (embedding_model is not null and embedding_version is not null)
    ),
    constraint document_chunks_token_count_check check (token_count is null or token_count > 0),
    constraint document_chunks_version_span_unique
        unique (document_version_id, source_span, chunk_ordinal, chunker_version)
);

create index document_chunks_document_version_idx
    on public.document_chunks (document_version_id);
create index document_chunks_filter_idx
    on public.document_chunks (knowledge_domain, visibility, story_access_level, language);
create index document_chunks_fts_idx
    on public.document_chunks using gin (fts);

create table public.ingestion_jobs (
    id uuid primary key default gen_random_uuid(),
    source_id uuid not null references public.knowledge_sources(id) on delete restrict,
    document_version_id uuid references public.document_versions(id) on delete set null,
    run_mode text not null,
    status text not null default 'PENDING',
    requested_by text,
    parser_version text not null,
    chunker_version text not null,
    embedding_model text,
    source_hash char(64) not null,
    chunks_discovered integer not null default 0,
    chunks_indexed integer not null default 0,
    error_code text,
    safe_error_detail text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint ingestion_jobs_mode_check check (run_mode in ('DRY_RUN', 'COMMIT')),
    constraint ingestion_jobs_status_check
        check (status in ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    constraint ingestion_jobs_source_hash_check check (source_hash ~ '^[0-9a-f]{64}$'),
    constraint ingestion_jobs_chunk_counts_check
        check (
            chunks_discovered >= 0
            and chunks_indexed >= 0
            and chunks_indexed <= chunks_discovered
        )
);

create index ingestion_jobs_source_created_idx
    on public.ingestion_jobs (source_id, created_at desc);

create or replace function public.set_knowledge_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = pg_catalog.now();
    return new;
end;
$$;

create trigger knowledge_sources_set_updated_at
before update on public.knowledge_sources
for each row execute function public.set_knowledge_updated_at();

create trigger document_versions_set_updated_at
before update on public.document_versions
for each row execute function public.set_knowledge_updated_at();

alter table public.knowledge_sources enable row level security;
alter table public.document_versions enable row level security;
alter table public.document_chunks enable row level security;
alter table public.ingestion_jobs enable row level security;

revoke all on table public.knowledge_sources from anon, authenticated;
revoke all on table public.document_versions from anon, authenticated;
revoke all on table public.document_chunks from anon, authenticated;
revoke all on table public.ingestion_jobs from anon, authenticated;

grant select, insert, update, delete on table public.knowledge_sources to service_role;
grant select, insert, update, delete on table public.document_versions to service_role;
grant select, insert, update, delete on table public.document_chunks to service_role;
grant select, insert, update, delete on table public.ingestion_jobs to service_role;

commit;
