# Test VieNeu TTS on a local CPU

VieNeu runs separately from the AI Guide. Keep it bound to loopback and let the
AI Guide call its OpenAI-compatible API; do not load the TTS model inside the
FastAPI process.

## 1. Start the local TTS service

From a separate directory, clone the upstream project and start its CPU API.
The AI Guide itself uses port 8000, so the TTS service uses host port 8001.

```powershell
git clone https://github.com/pnnbao97/VieNeu-TTS.git
cd VieNeu-TTS

$env:API_PORT = "8001"
$env:VIENEU_BACKEND = "onnx"
$env:VIENEU_PRECISION = "fp32"
$env:VIENEU_MAX_STREAMS = "1"
$env:VIENEU_QUEUE = "4"
$env:VIENEU_QUEUE_TIMEOUT = "10"
# Leave unset while the service is reachable only at 127.0.0.1.
$env:VIENEU_API_KEY = ""

docker compose -f docker/docker-compose.yml --profile api-cpu up --build -d
```

The checked-in local CPU profile caps the container at 2 vCPU and 3 GiB RAM,
keeps one active stream, and maps only `127.0.0.1:8001`. These limits are
intentional: the loaded fp32 model uses about 1.7 GiB while idle, so 2 GiB is
too tight for reliable synthesis.

Wait for the initial model download and warm-up, then check it from another
PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
docker compose -f docker/docker-compose.yml logs -f api-cpu
```

For a remote host or a container network shared with other workloads, set a
strong `VIENEU_API_KEY`, bind the service to a private network, and set the
same value as `VIENEU_TTS_API_KEY` in the AI Guide. The key is optional only
for loopback-only local development.

## 2. Configure the AI Guide

In the existing untracked `.env`, retain the Gemini settings used by STT and
set the TTS settings below:

```dotenv
TTS_PROVIDER=vieneu
VIENEU_TTS_BASE_URL=http://127.0.0.1:8001/v1
VIENEU_TTS_API_KEY=
VIENEU_TTS_MODEL=vieneu-v3-turbo
VIENEU_TTS_VOICE=Mai Anh
VIENEU_TTS_SAMPLE_RATE=24000
VIENEU_TTS_TIMEOUT_SECONDS=45
```

The adapter requests PCM and writes a completed mono PCM16 WAV itself. It uses
24 kHz by default so a long response is less likely to exceed the Dev Voice
API's 8 MiB temporary-audio limit. Raise it to `48000` only after confirming
your maximum response length and storage limits.

## 3. Run the AI Guide test UI

```powershell
cd D:\Saigon-Veilwake\saigon-veilwake-ai
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .

# DEV_VOICE_ENABLED=true is required in .env for the local UI.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open `http://127.0.0.1:8000/dev/`. Start with a text turn to test
Groq-to-VieNeu without the microphone, then test a full microphone turn.
If VieNeu is unavailable, the existing flow keeps the generated text and
returns `COMPLETED_TEXT_ONLY` rather than failing the entire turn.

After connecting a session, the **Giọng phản hồi** panel loads the configured
Gemini option and the preset voices reported by the local VieNeu service.
Choose the provider and, for VieNeu, a voice before sending each turn. The
choice applies only to that turn and does not expose provider URLs or keys to
the browser. Gemini uses its configured test voice; its voice selection is not
exposed in this local UI.

## 4. CPU tuning after the first successful run

Begin with `fp32` and one stream. Try `VIENEU_PRECISION=int8` only on a CPU
with VNNI support, then benchmark before raising stream limits. The initial
local target is correctness and pronunciation; capacity tuning comes after
that.
