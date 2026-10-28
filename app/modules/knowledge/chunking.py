import re
from pathlib import Path

from app.modules.knowledge.models import KnowledgeChunk

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


def chunk_markdown(
    content: str,
    *,
    max_chars: int = 1200,
    overlap_chars: int = 150,
    knowledge_domain: str = "game_lore",
    story_access_level: int = 0,
) -> list[KnowledgeChunk]:
    """Split Markdown on headings first, then paragraph boundaries with a bounded overlap."""

    if max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("Require max_chars > overlap_chars >= 0.")

    sections = _sections(content)
    chunks: list[KnowledgeChunk] = []
    for section_title, section_text in sections:
        for piece in _split_section(section_text, max_chars, overlap_chars):
            chunks.append(
                KnowledgeChunk(
                    ordinal=len(chunks),
                    content=piece,
                    section_title=section_title,
                    knowledge_domain=knowledge_domain,
                    story_access_level=story_access_level,
                )
            )
    return chunks


def load_markdown(path: Path, **kwargs: object) -> list[KnowledgeChunk]:
    if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise ValueError("Only UTF-8 Markdown/text sources are supported by the initial ingester.")
    return chunk_markdown(path.read_text(encoding="utf-8"), **kwargs)


def _sections(content: str) -> list[tuple[str | None, str]]:
    matches = list(HEADING.finditer(content))
    if not matches:
        return [(None, content.strip())] if content.strip() else []

    sections: list[tuple[str | None, str]] = []
    preamble = content[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end() : end].strip()
        if body:
            sections.append((match.group(2), body))
    return sections


def _split_section(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    paragraphs = [
        paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()
    ]
    output: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                output.append(current)
                current = ""
            output.extend(_split_long_text(paragraph, max_chars, overlap_chars))
        elif not current:
            current = paragraph
        elif len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}"
        else:
            output.append(current)
            overlap = current[-overlap_chars:].strip() if overlap_chars else ""
            current = f"{overlap}\n\n{paragraph}".strip()
    if current:
        output.append(current)
    return output


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    output: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(text.rfind(" ", start, end), text.rfind("\n", start, end))
            if boundary > start + max_chars // 2:
                end = boundary
        output.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return output
