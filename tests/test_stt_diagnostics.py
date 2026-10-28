import json

from scripts.diagnose_stt import safe_structure


def test_structure_diagnostics_never_dump_keys_or_field_values() -> None:
    result = safe_structure(
        {
            "test-secret-key": "test-secret-value",
            "candidates": [{"content": {"parts": [{"audioTranscription": {"text": "Xin chào"}}]}}],
        }
    )
    serialized = json.dumps(result)
    assert "test-secret" not in serialized
    assert "Xin chào" not in serialized
    assert "audioTranscription" in serialized
    assert "string_characters" in serialized
