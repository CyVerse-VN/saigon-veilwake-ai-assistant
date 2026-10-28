"""Read DOCX knowledge sources and produce a review-only ingestion report.

The loader deliberately has no embedding, database, or network dependency.  Its
only responsibility is to preserve document structure and apply the local
manifest's visibility/spoiler policy before a later ingestion step is allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NAMESPACES = {"w": WORD_NS}
HEADING_STYLE = re.compile(r"^Heading\s*(\d+)$", re.IGNORECASE)


class ManifestError(ValueError):
    """Raised when an ingestion manifest cannot be used safely."""


@dataclass(frozen=True)
class DocumentBlock:
    """One paragraph or one semantic table row in the original document."""

    block_id: str
    kind: str
    text: str
    heading_path: tuple[str, ...]
    paragraph_style: str | None = None
    table_headers: tuple[str, ...] = ()
    table_first_cell: str | None = None


@dataclass(frozen=True)
class ParsedDocument:
    blocks: tuple[DocumentBlock, ...]
    heading_count: int
    table_count: int


def load_docx(path: Path) -> ParsedDocument:
    """Extract paragraphs and table rows in body order from a DOCX file."""

    try:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
    except FileNotFoundError as error:
        raise ManifestError(f"Source DOCX does not exist: {path}") from error
    except KeyError as error:
        raise ManifestError(f"DOCX has no word/document.xml part: {path}") from error
    except zipfile.BadZipFile as error:
        raise ManifestError(f"Source is not a readable DOCX/ZIP file: {path}") from error

    root = ElementTree.fromstring(document_xml)
    body = root.find("w:body", NAMESPACES)
    if body is None:
        raise ManifestError(f"DOCX has no document body: {path}")

    blocks: list[DocumentBlock] = []
    headings: list[str] = []
    heading_count = 0
    table_count = 0
    paragraph_index = 0

    for element in body:
        element_type = _local_name(element.tag)
        if element_type == "p":
            paragraph_index += 1
            text = _paragraph_text(element)
            style = _paragraph_style(element)
            heading_level = _heading_level(style)
            if heading_level is not None and text:
                heading_count += 1
                headings = headings[: heading_level - 1]
                headings.append(text)
                continue
            if text:
                blocks.append(
                    DocumentBlock(
                        block_id=f"p-{paragraph_index:04d}",
                        kind="paragraph",
                        text=text,
                        heading_path=tuple(headings),
                        paragraph_style=style,
                    )
                )
        elif element_type == "tbl":
            table_count += 1
            blocks.extend(_table_blocks(element, table_count, tuple(headings)))

    return ParsedDocument(tuple(blocks), heading_count, table_count)


def build_dry_run_report(manifest_path: Path, project_root: Path | None = None) -> dict[str, Any]:
    """Apply a manifest to a DOCX and return a report without side effects."""

    manifest_path = manifest_path.resolve()
    manifest = _load_manifest(manifest_path)
    source_path = _resolve_source_path(manifest_path, manifest, project_root)
    source_hash = _sha256(source_path)
    expected_hash = str(manifest["source"].get("sha256", "")).lower()
    if source_hash != expected_hash:
        raise ManifestError(
            "Source SHA-256 does not match manifest; review the document and update "
            "the manifest before dry-running it."
        )

    document = load_docx(source_path)
    section_rules = list(manifest.get("section_rules", []))
    child_rules = list(manifest.get("child_rules", []))
    table_row_rules = list(manifest.get("table_row_rules", []))
    all_rules = section_rules + child_rules
    items: list[dict[str, Any]] = []
    matched_rule_ids: set[str] = set()
    matched_table_row_rule_ids: set[str] = set()

    for block in document.blocks:
        rule = _select_rule(block, all_rules)
        policy = _policy_for_block(block, rule, manifest, table_row_rules)
        matched_rule_id = policy.pop("matched_rule_id")
        matched_table_row_rule_id = policy.pop("matched_table_row_rule_id")
        if matched_rule_id:
            matched_rule_ids.add(matched_rule_id)
        if matched_table_row_rule_id:
            matched_table_row_rule_ids.add(matched_table_row_rule_id)

        item = {
            "source_span": block.block_id,
            "kind": block.kind,
            "heading_path": list(block.heading_path),
            "paragraph_style": block.paragraph_style,
            "table_headers": list(block.table_headers),
            "table_first_cell": block.table_first_cell,
            "text_preview": _preview(block.text),
            "policy": policy,
        }
        items.append(item)

    indexed_items = [item for item in items if item["policy"]["included"]]
    excluded_items = [item for item in items if not item["policy"]["included"]]
    structural_rule_ids = [
        rule["rule_id"]
        for rule in all_rules
        if rule["action"] == "index_only_matching_child_rules"
    ]
    unmatched_rules = [
        rule["rule_id"]
        for rule in all_rules
        if rule["rule_id"] not in matched_rule_ids and rule["rule_id"] not in structural_rule_ids
    ]
    unmatched_table_row_rules = [
        rule["rule_id"]
        for rule in table_row_rules
        if rule["rule_id"] not in matched_table_row_rule_ids
    ]

    return {
        "report_type": "knowledge_ingestion_dry_run",
        "dry_run": True,
        "manifest_id": manifest["manifest_id"],
        "source": {
            "source_id": manifest["source"]["source_id"],
            "path": str(source_path),
            "sha256": source_hash,
            "source_hash_verified": True,
            "document_version": manifest["source"]["document_version"],
        },
        "summary": {
            "heading_count": document.heading_count,
            "table_count": document.table_count,
            "content_block_count": len(items),
            "indexed_block_count": len(indexed_items),
            "excluded_block_count": len(excluded_items),
            "paragraph_block_count": sum(item["kind"] == "paragraph" for item in items),
            "table_row_block_count": sum(item["kind"] == "table_row" for item in items),
        },
        "review": {
            "matched_rule_ids": sorted(matched_rule_ids),
            "structural_rule_ids": structural_rule_ids,
            "unmatched_rule_ids": unmatched_rules,
            "matched_table_row_rule_ids": sorted(matched_table_row_rule_ids),
            "unmatched_table_row_rule_ids": unmatched_table_row_rules,
            "commit_blocked": True,
            "embedding_performed": False,
            "database_write_performed": False,
        },
        "items": items,
    }


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ManifestError(f"Manifest does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ManifestError(f"Manifest is not valid JSON: {path}: {error.msg}") from error

    for key in ("manifest_id", "source", "default_chunk_metadata"):
        if key not in manifest:
            raise ManifestError(f"Manifest is missing required field: {key}")
    for key in ("source_id", "path", "sha256", "document_version"):
        if key not in manifest["source"]:
            raise ManifestError(f"Manifest source is missing required field: {key}")
    return manifest


def _resolve_source_path(
    manifest_path: Path, manifest: dict[str, Any], project_root: Path | None
) -> Path:
    raw_source_path = Path(str(manifest["source"]["path"]))
    if raw_source_path.is_absolute():
        return raw_source_path

    root = project_root.resolve() if project_root else manifest_path.parent.parent.resolve()
    candidate = root / raw_source_path
    if candidate.exists():
        return candidate

    sibling_candidate = manifest_path.parent / raw_source_path
    if sibling_candidate.exists():
        return sibling_candidate
    return candidate


def _table_blocks(
    table: ElementTree.Element, table_index: int, heading_path: tuple[str, ...]
) -> list[DocumentBlock]:
    rows = table.findall("w:tr", NAMESPACES)
    if not rows:
        return []
    parsed_rows = [_table_row_text(row) for row in rows]
    parsed_rows = [row for row in parsed_rows if any(cell for cell in row)]
    if len(parsed_rows) < 2:
        return []

    headers = tuple(parsed_rows[0])
    blocks: list[DocumentBlock] = []
    for row_index, row in enumerate(parsed_rows[1:], start=2):
        labelled_cells = []
        for column_index, value in enumerate(row):
            fallback_header = f"Column {column_index + 1}"
            header = headers[column_index] if column_index < len(headers) else fallback_header
            if value:
                labelled_cells.append(f"{header}: {value}")
        if labelled_cells:
            blocks.append(
                DocumentBlock(
                    block_id=f"t-{table_index:02d}-r-{row_index:02d}",
                    kind="table_row",
                    text="\n".join(labelled_cells),
                    heading_path=heading_path,
                    table_headers=headers,
                    table_first_cell=row[0] if row else None,
                )
            )
    return blocks


def _table_row_text(row: ElementTree.Element) -> list[str]:
    cells: list[str] = []
    for cell in row.findall("w:tc", NAMESPACES):
        paragraphs = [_paragraph_text(paragraph) for paragraph in cell.findall("w:p", NAMESPACES)]
        cells.append("\n".join(text for text in paragraphs if text))
    return cells


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    values: list[str] = []
    for child in paragraph.iter():
        local_name = _local_name(child.tag)
        if local_name == "t" and child.text:
            values.append(child.text)
        elif local_name == "tab":
            values.append("\t")
        elif local_name in {"br", "cr"}:
            values.append("\n")
    return "".join(values).strip()


def _paragraph_style(paragraph: ElementTree.Element) -> str | None:
    style = paragraph.find("w:pPr/w:pStyle", NAMESPACES)
    return style.get(f"{{{WORD_NS}}}val") if style is not None else None


def _heading_level(style: str | None) -> int | None:
    if not style:
        return None
    match = HEADING_STYLE.match(style)
    return int(match.group(1)) if match else None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _select_rule(block: DocumentBlock, rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for rule in rules:
        selector = rule.get("selector", {})
        heading_path = selector.get("heading_path")
        if heading_path:
            selected_path = tuple(heading_path)
            if block.heading_path[: len(selected_path)] == selected_path:
                candidates.append((float(len(selected_path)), rule))
        elif selector.get("before_heading") and not block.heading_path:
            candidates.append((0.5, rule))
        elif selector.get("table_after_heading") and block.kind == "table_row":
            if block.heading_path and block.heading_path[-1] == selector["table_after_heading"]:
                candidates.append((len(block.heading_path) + 0.5, rule))

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _policy_for_block(
    block: DocumentBlock,
    rule: dict[str, Any] | None,
    manifest: dict[str, Any],
    table_row_rules: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata = dict(manifest["default_chunk_metadata"])
    action = rule["action"] if rule else "exclude_unmatched"
    matched_rule_id = rule["rule_id"] if rule else None
    table_row_rule = _select_table_row_rule(block, rule, table_row_rules)

    if rule:
        for key in ("story_access_level", "visibility", "canonical_status", "tags"):
            if key in rule:
                metadata[key] = rule[key]
    if table_row_rule:
        metadata["story_access_level"] = table_row_rule["story_access_level"]
        metadata["tags"] = table_row_rule.get("tags", metadata.get("tags", []))

    section_path = (
        tuple(rule["section_path_override"])
        if rule and "section_path_override" in rule
        else block.heading_path
    )

    excluded_actions = {
        "exclude_from_player_rag",
        "exclude_unmatched",
        "index_only_matching_child_rules",
    }
    included = action not in excluded_actions
    if action == "exclude_from_player_rag":
        metadata["visibility"] = "internal"
    return {
        "included": included,
        "action": action,
        "matched_rule_id": matched_rule_id,
        "matched_table_row_rule_id": table_row_rule["rule_id"] if table_row_rule else None,
        "section_path": list(section_path),
        "metadata": metadata,
    }


def _select_table_row_rule(
    block: DocumentBlock, rule: dict[str, Any] | None, rules: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if block.kind != "table_row" or not rule or not block.table_first_cell:
        return None
    for row_rule in rules:
        expected_rule = row_rule.get("applies_to")
        expected_cell = row_rule.get("row_selector", {}).get("first_cell")
        if expected_rule == rule["rule_id"] and expected_cell == block.table_first_cell:
            return row_rule
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _preview(text: str, limit: int = 280) -> str:
    normalized = " ".join(text.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review a DOCX knowledge source without ingesting it."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument(
        "--output", type=Path, help="Write the JSON report to this path instead of stdout."
    )
    args = parser.parse_args()

    report = build_dry_run_report(args.manifest, args.project_root)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{output}\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
