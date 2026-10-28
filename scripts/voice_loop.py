# ruff: noqa: E402
"""Minimal microphone -> Gemini STT -> Groq -> Gemini TTS terminal demo.

Run from the repository root with ``python scripts/voice_loop.py``.
"""

from __future__ import annotations

import argparse
import ctypes
import shutil
import sys
import time
import uuid
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

# ``python scripts/voice_loop.py`` puts scripts/ on sys.path, not the repo root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.core.config import get_settings
from app.integrations.errors import ProviderRequestError
from app.integrations.stt_providers.gemini_stt import GeminiSttProvider
from app.modules.voice_loop.audio import inspect_wav
from app.modules.voice_loop.service import (
    VoiceLoopConfigurationError,
    VoiceLoopService,
    build_voice_loop_service,
)

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
TEMP_ROOT = REPOSITORY_ROOT / ".voice_loop_tmp"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal Saigon: Veilwake terminal voice loop.")
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=25,
        help="Maximum microphone recording time (1-30 seconds; default: 25).",
    )
    parser.add_argument(
        "--debug-audio",
        action="store_true",
        help="Keep temporary input/output WAV files in .voice_loop_tmp for debugging.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List audio input/output devices, then exit without contacting any provider.",
    )
    parser.add_argument(
        "--input-device",
        help="Microphone device name or index from --list-devices.",
    )
    parser.add_argument(
        "--output-device",
        help="Speaker device name or index from --list-devices.",
    )
    parser.add_argument(
        "--record-only",
        action="store_true",
        help="Record once and keep the WAV for offline debugging; no API calls.",
    )
    parser.add_argument(
        "--input-audio",
        type=Path,
        help="Process an existing mono PCM16 WAV once; never open the microphone.",
    )
    parser.add_argument(
        "--stt-only",
        action="store_true",
        help="Run only STT once (one provider request); do not call Groq or TTS.",
    )
    parser.add_argument(
        "--play-input",
        action="store_true",
        help="Listen to the recorded/input WAV before sending it to STT.",
    )
    return parser.parse_args()


def record_wav(output_path: Path, max_seconds: int, input_device: str | int | None) -> None:
    """Record mono 16 kHz PCM until Enter or the time limit is reached."""
    if not 1 <= max_seconds <= 30:
        raise ValueError("--max-seconds must be between 1 and 30.")

    frames: list[bytes] = []

    def callback(
        indata: np.ndarray,
        frame_count: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        del frame_count, time_info
        if status:
            # The microphone stream stays usable; report a generic status only.
            print("[Cảnh báo: microphone bị gián đoạn ngắn]", flush=True)
        frames.append(indata.copy().tobytes())

    _discard_pending_console_input()
    print(f"Đang ghi âm (tối đa {max_seconds} giây). Nhấn Enter để dừng sớm.")
    started = time.monotonic()
    with sd.InputStream(
        device=input_device,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
        callback=callback,
    ):
        started = time.monotonic()
        _wait_until_enter_or_timeout(max_seconds, started)
    _discard_pending_console_input()

    if not frames:
        raise RuntimeError("Không nhận được âm thanh từ microphone.")
    recorded = b"".join(frames)[: max_seconds * SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH]
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(recorded)


def _discard_pending_console_input() -> None:
    """Discard the Enter keystroke that started the turn before polling for stop."""
    if sys.platform != "win32":
        return
    import msvcrt

    while msvcrt.kbhit():
        msvcrt.getwch()


def _wait_until_enter_or_timeout(max_seconds: int, started: float) -> None:
    """Use console key polling on Windows; other terminals record to the time limit."""
    if sys.platform != "win32":
        time.sleep(max_seconds)
        return

    import msvcrt

    while time.monotonic() - started < max_seconds:
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in {"\r", "\n"}:
                return
        time.sleep(0.05)


def play_wav(audio_path: Path, output_device: str | int | None) -> None:
    """Play a PCM WAV response through the default output device."""
    with wave.open(str(audio_path), "rb") as wav_file:
        sample_width = wav_file.getsampwidth()
        if sample_width != SAMPLE_WIDTH:
            raise RuntimeError("TTS returned an unsupported WAV sample width.")
        samples = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16)
        channels = wav_file.getnchannels()
        if channels > 1:
            samples = samples.reshape(-1, channels)
        sd.play(samples, wav_file.getframerate(), device=output_device)
        sd.wait()


def run_turn(
    service: VoiceLoopService | GeminiSttProvider | None,
    max_seconds: int,
    debug_audio: bool,
    input_device: str | int | None,
    output_device: str | int | None,
    *,
    input_audio: Path | None = None,
    record_only: bool = False,
    play_input: bool = False,
) -> bool:
    turn_dir = TEMP_ROOT / uuid.uuid4().hex
    turn_dir.mkdir(parents=True, exist_ok=False)
    input_path = turn_dir / "microphone.wav"
    output_path = turn_dir / "response.wav"
    try:
        if input_audio is None:
            record_wav(input_path, max_seconds, input_device)
        else:
            # Copy only after validating size/format; never delete the user's source WAV.
            contents, _ = inspect_wav(input_audio)
            input_path.write_bytes(contents)
        _, stats = inspect_wav(input_path)
        print(stats.summary())
        if stats.rms_dbfs < -40:
            print("Âm lượng ghi khá nhỏ; hãy kiểm tra gain và nghe lại WAV.")
        if play_input:
            print("Đang phát lại audio đầu vào...")
            play_wav(input_path, output_device)
        if record_only:
            print("Đã ghi WAV để kiểm tra offline; chưa gửi audio đến provider.")
            return True
        if isinstance(service, GeminiSttProvider):
            started = time.monotonic()
            transcript = service.transcribe(input_path)
            print(f"Transcript: {transcript}")
            print(f"STT: {time.monotonic() - started:.2f}s | {service.last_diagnostics}")
            return True
        if service is None:
            raise RuntimeError("Voice service was not initialized.")
        result = service.process(input_path, output_path)
        print(f"\nTranscript: {result.transcript}")
        print(f"Trả lời: {result.answer}")
        print(
            f"Thời gian — STT: {result.stt_seconds:.2f}s | "
            f"LLM: {result.llm_seconds:.2f}s | "
            f"TTS: {result.tts_seconds:.2f}s"
        )
        if result.tts_error:
            print(f"TTS: {result.tts_error}")
        elif result.audio_path:
            print("Đang phát audio phản hồi...")
            try:
                play_wav(result.audio_path, output_device)
            except Exception:
                print("Không thể phát audio tự động; câu trả lời text vẫn hiển thị ở trên.")
        return True
    except (ProviderRequestError, ValueError) as exc:
        print(f"Lượt voice không hoàn tất: {exc}")
        return False
    except Exception:
        print("Lượt voice không hoàn tất. Kiểm tra thiết bị âm thanh và file WAV.")
        return False
    finally:
        if debug_audio or record_only:
            print(f"Audio debug được giữ tại: {turn_dir}")
        else:
            shutil.rmtree(turn_dir, ignore_errors=True)


def main() -> int:
    _configure_windows_utf8_console()
    args = parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return 0
    if not 1 <= args.max_seconds <= 30:
        print("Lỗi cấu hình: --max-seconds phải từ 1 đến 30.")
        return 2
    if args.record_only and (args.input_audio or args.stt_only):
        print("--record-only không dùng cùng --input-audio hoặc --stt-only.")
        return 2
    try:
        service: VoiceLoopService | GeminiSttProvider | None = None
        if not args.record_only:
            settings = get_settings()
            if args.stt_only:
                missing = []
                if not settings.gemini_api_key or not settings.gemini_api_key.strip():
                    missing.append("GEMINI_API_KEY")
                if not settings.gemini_stt_model.strip():
                    missing.append("GEMINI_STT_MODEL")
                if missing:
                    raise VoiceLoopConfigurationError(
                        "Missing configuration: " + ", ".join(missing)
                    )
                service = GeminiSttProvider(
                    settings.gemini_api_key or "",
                    settings.gemini_stt_model,
                    settings.gemini_request_timeout_seconds,
                )
            else:
                service = build_voice_loop_service(settings)
    except VoiceLoopConfigurationError as exc:
        print(f"Lỗi cấu hình voice loop: {exc}")
        return 2
    except Exception:
        print("Không thể khởi tạo voice loop. Hãy kiểm tra dependency và cấu hình .env.")
        return 2

    if args.record_only or args.input_audio or args.stt_only:
        success = run_turn(
            service,
            args.max_seconds,
            args.debug_audio,
            _device_argument(args.input_device),
            _device_argument(args.output_device),
            input_audio=args.input_audio,
            record_only=args.record_only,
            play_input=args.play_input,
        )
        return 0 if success else 1

    print("Terminal Voice Loop POC")
    print(
        "Nhấn Enter để bắt đầu ghi âm. Khi đang ghi, nhấn Enter để dừng. Gõ q rồi Enter để thoát."
    )
    while True:
        command = input("\n[Enter] ghi âm | [q] thoát: ").strip().lower()
        if command in {"q", "quit", "exit"}:
            print("Đã thoát voice loop.")
            return 0
        if command:
            print("Lệnh không hợp lệ. Nhấn Enter để ghi âm hoặc gõ q để thoát.")
            continue
        run_turn(
            service,
            args.max_seconds,
            args.debug_audio,
            _device_argument(args.input_device),
            _device_argument(args.output_device),
            play_input=args.play_input,
        )


def _configure_windows_utf8_console() -> None:
    """Avoid mojibake when the script prints Vietnamese in Windows PowerShell."""
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _device_argument(value: str | None) -> str | int | None:
    return int(value) if value and value.isdigit() else value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nĐã thoát voice loop.")
