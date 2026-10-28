from collections.abc import Iterable

from app.modules.knowledge.models import KnowledgeChunk


def visible_chunks(
    chunks: Iterable[KnowledgeChunk], allowed_story_access_level: int
) -> list[KnowledgeChunk]:
    """Enforce spoiler access before embedding candidates reach any model."""

    return [chunk for chunk in chunks if chunk.story_access_level <= allowed_story_access_level]
