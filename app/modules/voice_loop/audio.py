"""Offline validation of the actual WAV bytes sent to speech recognition."""

from __future__ import annotations

import math
import sys
import wave
from array import array
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path


@dataclass(frozen=True)
class AudioStats:
    seconds: float
    sample_rate: int
    rms_dbfs: float
    peak_dbfs: float

    def summary(self) -> str:
        return (
            f"WAV: {self.seconds:.2f}s, {self.sample_rate} Hz, mono PCM16 | "
            f"RMS {self.rms_dbfs:.1f} dBFS | peak {self.peak_dbfs:.1f} dBFS"
        )


def inspect_wav(path: Path) -> tuple[bytes, AudioStats]:
    """Validate format/duration before any network request; RMS is not a speech detector."""
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("WAV exceeds the 4 MiB voice-demo limit.")
        data = path.read_bytes()
    except OSError as from_exc:
        raise ValueError("Cannot read a valid PCM WAV file.") from from_exc
    return inspect_wav_bytes(data)


def inspect_wav_bytes(data: bytes) -> tuple[bytes, AudioStats]:
    """Validate a bounded HTTP upload without creating a temporary file."""
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("WAV exceeds the 4 MiB voice-demo limit.")
    try:
        with wave.open(BytesIO(data), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("Voice input must be mono 16-bit PCM WAV.")
            sample_rate = wav.getframerate()
            if not 8000 <= sample_rate <= 48000 or wav.getcomptype() != "NONE":
                raise ValueError("Voice input must be PCM WAV at 8000-48000 Hz.")
            frame_count = wav.getnframes()
            pcm = wav.readframes(frame_count)
            if len(pcm) != frame_count * 2:
                raise ValueError("WAV is truncated; record again.")
    except (OSError, EOFError, wave.Error) as from_exc:
        raise ValueError("Cannot read a valid PCM WAV file.") from from_exc
    seconds = frame_count / sample_rate
    if not 1 <= seconds <= 30:
        raise ValueError("Record 1-30 seconds of speech before stopping.")
    samples = array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    stats = AudioStats(
        seconds,
        sample_rate,
        20 * math.log10(max(rms, 1e-10) / 32768),
        20 * math.log10(max(peak, 1e-10) / 32768),
    )
    if peak == 0 or stats.rms_dbfs < -65:
        raise ValueError("WAV is silent or nearly silent. Check the selected microphone and gain.")
    return data, stats
