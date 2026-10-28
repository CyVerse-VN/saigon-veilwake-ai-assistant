from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_is_public() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_text_turn_is_fail_closed_and_never_creates_audio() -> None:
    conversation = client.post("/v1/conversations").json()["conversation_id"]
    accepted = client.post(
        f"/v1/conversations/{conversation}/turns",
        json={"message": "Kể tôi bí mật cuối game", "response_mode": "text_and_voice"},
    )
    assert accepted.status_code == 202

    turn = client.get(accepted.json()["status_url"])
    assert turn.status_code == 200
    body = turn.json()
    assert body["status"] == "COMPLETED_TEXT_ONLY"
    assert body["audio"]["status"] == "UNAVAILABLE"
    assert "chưa có đủ thông tin" in body["answer"]["text"]


def test_voice_turn_rejects_non_audio_upload() -> None:
    conversation = client.post("/v1/conversations").json()["conversation_id"]
    response = client.post(
        f"/v1/conversations/{conversation}/voice-turns",
        files={"audio": ("not-audio.txt", b"not audio", "text/plain")},
    )
    assert response.status_code == 415
