from pathlib import Path

from app.modules.knowledge.docx_loader import build_dry_run_report

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "base_knowlegde" / "game_world_v1.0.manifest.json"


def test_game_world_manifest_dry_run_preserves_policy_boundaries() -> None:
    report = build_dry_run_report(MANIFEST_PATH, PROJECT_ROOT)

    assert report["dry_run"] is True
    assert report["source"]["source_hash_verified"] is True
    assert report["review"]["commit_blocked"] is True
    assert report["review"]["embedding_performed"] is False
    assert report["review"]["database_write_performed"] is False
    assert report["summary"]["heading_count"] == 26
    assert report["summary"]["table_count"] == 5
    assert report["summary"]["table_row_block_count"] > 0

    antagonist_items = [
        item
        for item in report["items"]
        if "Người Giữ Im Lặng" in item["text_preview"]
        and item["policy"]["metadata"].get("story_access_level") == 5
    ]
    assert antagonist_items

    chapter_one_rows = [
        item
        for item in report["items"]
        if item["table_first_cell"] == "1. Những Cánh Bướm Không Trở Về"
    ]
    assert len(chapter_one_rows) == 1
    assert chapter_one_rows[0]["policy"]["metadata"]["story_access_level"] == 1

    public_final_title_items = [
        item
        for item in report["items"]
        if item["heading_path"] == ["III. Người chơi là ai?"]
        or item["table_first_cell"] == "Người Kể Tiếp"
    ]
    assert public_final_title_items
    assert all(
        item["policy"]["metadata"]["story_access_level"] == 0
        for item in public_final_title_items
    )

    internal_items = [
        item
        for item in report["items"]
        if item["policy"]["action"] == "exclude_from_player_rag"
    ]
    assert internal_items
    assert all(item["policy"]["included"] is False for item in internal_items)
