import math
import struct
import wave
from pathlib import Path

import pytest


@pytest.fixture
def wav_path(tmp_path: Path) -> Path:
    """Synthetic local signal, not a provider call or microphone recording."""
    path = tmp_path / "input.wav"
    pcm = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(32000)
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)
    return path
