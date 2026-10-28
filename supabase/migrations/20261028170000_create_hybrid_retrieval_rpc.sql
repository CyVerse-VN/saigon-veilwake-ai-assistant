-- Hybrid retrieval RPC for Saigon: Veilwake RAG.
-- Enforces spoiler (story_access_level), domain, visibility, and active approved version
-- filters BEFORE semantic (pgvector) or lexical (FTS) ranking.

begin;

create or replace function public.search_knowledge_chunks(
    p_query_text text,
    p_query_embedding extensions.vector(768),
    p_knowledge_domain text default 'game_lore',
    p_allowed_story_access_level integer default 0,
    p_language text default 'vi',
    p_candidate_limit integer default 20,
    p_match_count integer default 8,
    p_rrf_k integer default 60
)
returns table (
    chunk_id uuid,
    document_version_id uuid,
    source_id uuid,
    source_key text,
    source_title text,
    canonical_url text,
    document_version text,
    source_span text,
    chunk_ordinal integer,
    section_path text[],
    content text,
    knowledge_domain text,
    story_access_level integer,
    canonical_status text,
    tags text[],
    metadata jsonb,
    semantic_score double precision,
    lexical_score double precision,
    semantic_rank integer,
    lexical_rank integer,
    hybrid_score double precision
)
language sql
stable
security invoker
set search_path = ''
as $$
with authorized_chunks as (
    select
        dc.id as chunk_id,
        dc.document_version_id,
        ks.id as source_id,
        ks.source_key,
        ks.title as source_title,
        ks.canonical_url,
        dv.document_version,
        dc.source_span,
        dc.chunk_ordinal,
        dc.section_path,
        dc.content,
        dc.knowledge_domain,
        dc.story_access_level,
        dc.canonical_status,
        dc.tags,
        dc.metadata,
        dc.fts,
        dc.embedding
    from public.document_chunks as dc
    inner join public.document_versions as dv
        on dv.id = dc.document_version_id
    inner join public.knowledge_sources as ks
        on ks.id = dv.source_id
    where ks.approval_status = 'APPROVED'
      and ks.visibility = 'PLAYER'
      and dv.is_active is true
      and dv.approval_status = 'APPROVED'
      and dc.visibility = 'PLAYER'
      and dc.knowledge_domain = p_knowledge_domain
      and dc.language = p_language
      and dc.story_access_level <= p_allowed_story_access_level
      and dc.embedding is not null
),
semantic_candidates as (
    select
        ac.chunk_id,
        (1.0 - (ac.embedding operator(extensions.<=>) p_query_embedding))::double precision as semantic_score,
        row_number() over (
            order by ac.embedding operator(extensions.<=>) p_query_embedding asc, ac.chunk_ordinal asc
        )::integer as semantic_rank
    from authorized_chunks as ac
    order by ac.embedding operator(extensions.<=>) p_query_embedding asc, ac.chunk_ordinal asc
    limit greatest(1, p_candidate_limit)
),
lexical_query as (
    select
        case
            when length(btrim(coalesce(pg_catalog.plainto_tsquery('simple', p_query_text)::text, ''))) > 0
                then pg_catalog.replace(
                    pg_catalog.plainto_tsquery('simple', p_query_text)::text,
                    ' & ',
                    ' | '
                )::tsquery
            else null::tsquery
        end as tsq
),
lexical_candidates as (
    select
        ac.chunk_id,
        pg_catalog.ts_rank_cd(ac.fts, lq.tsq)::double precision as lexical_score,
        row_number() over (
            order by pg_catalog.ts_rank_cd(ac.fts, lq.tsq) desc, ac.chunk_ordinal asc
        )::integer as lexical_rank
    from authorized_chunks as ac
    cross join lexical_query as lq
    where lq.tsq is not null
      and ac.fts @@ lq.tsq
    order by pg_catalog.ts_rank_cd(ac.fts, lq.tsq) desc, ac.chunk_ordinal asc
    limit greatest(1, p_candidate_limit)
),
fused as (
    select
        coalesce(sc.chunk_id, lc.chunk_id) as chunk_id,
        coalesce(sc.semantic_score, 0.0)::double precision as semantic_score,
        coalesce(lc.lexical_score, 0.0)::double precision as lexical_score,
        sc.semantic_rank,
        lc.lexical_rank,
        (
            coalesce(1.0 / (p_rrf_k + sc.semantic_rank), 0.0)
            + coalesce(1.0 / (p_rrf_k + lc.lexical_rank), 0.0)
        )::double precision as hybrid_score
    from semantic_candidates as sc
    full outer join lexical_candidates as lc
        on lc.chunk_id = sc.chunk_id
)
select
    ac.chunk_id,
    ac.document_version_id,
    ac.source_id,
    ac.source_key,
    ac.source_title,
    ac.canonical_url,
    ac.document_version,
    ac.source_span,
    ac.chunk_ordinal,
    ac.section_path,
    ac.content,
    ac.knowledge_domain,
    ac.story_access_level,
    ac.canonical_status,
    ac.tags,
    ac.metadata,
    f.semantic_score,
    f.lexical_score,
    f.semantic_rank,
    f.lexical_rank,
    f.hybrid_score
from fused as f
inner join authorized_chunks as ac
    on ac.chunk_id = f.chunk_id
order by f.hybrid_score desc, coalesce(f.lexical_rank, 999999) asc, coalesce(f.semantic_rank, 999999) asc
limit greatest(1, p_match_count);
$$;

revoke all on function public.search_knowledge_chunks(
    text, extensions.vector(768), text, integer, text, integer, integer, integer
) from public, anon, authenticated;

grant execute on function public.search_knowledge_chunks(
    text, extensions.vector(768), text, integer, text, integer, integer, integer
) to service_role;

commit;

