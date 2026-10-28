# Saigon: Veilwake — AI Guide Service

Backend component for the game's AI Guide. The Game Backend remains the authority for
player identity, quests, rewards, and story-access level. This service accepts only a
trusted internal request, then handles the AI conversation, evidence pipeline and voice.

## Current implementation boundary

The real local voice demo is now available through `/dev/v1` and the built-in
`/dev/` web UI, separately from the existing mock game pipeline described below.
See [Dev Voice API + local UI setup and contracts](docs/Dev_Voice_API_Local_UI.md).

The first vertical slice is deliberately provider-free: it exposes the v1 conversation,
text-turn and voice-turn contracts, validates internal access and enforces the state
machine. `MockTurnProcessor` returns only an approved, fixed fallback; it never
fabricates a lore answer or audio. Real Groq/Gemini/Supabase integrations belong behind
the adapters described in the proposal.

Voice is a turn-based flow, not realtime streaming:

`audio upload -> STT adapter -> verified text pipeline -> approved answer -> TTS adapter -> audio`

TTS must receive an approved answer ID only. Until a Vietnamese TTS provider is enabled,
the API correctly completes with text only rather than returning an invented audio URL.

## Run locally

Requires Python 3.10+.

```powershell
cd D:\Saigon-Veilwake\saigon-veilwake-ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for the OpenAPI UI. Development mode permits requests
without an internal token. Staging/production refuses to boot without
`SVAI_INTERNAL_SERVICE_TOKEN`; mobile clients must never call this service directly.

## Test

```powershell
pytest
ruff check .
```

## Before connecting real providers

1. Agree the Game Backend service-token claims and the trusted story-access context.
2. Create the Supabase project, RLS policies and private audio bucket.
3. Add separate `GROQ_GENERATOR_API_KEY` and `GROQ_VERIFIER_API_KEY` values to the local
   `.env`; select model IDs only after the Vietnamese/structured-output benchmark.
4. Approve the voice-data policy (consent, retention TTL and whether provider free tiers
   may process voice/lore). Select and benchmark Vietnamese STT and TTS adapters.
5. Supply an approved lore seed corpus plus spoiler/access tags.

See [the proposal](docs/Saigon_Veilwake_AI_Guide_Service_Proposal_v1.3.md).

## Terminal voice-loop POC

Xem [tài liệu tiếng Việt giải thích cơ chế và diagram của Terminal Voice Loop](docs/Terminal_Voice_Loop_POC_Giai_Thich_Co_Che.md).

This is a deliberately separate local demo; it does not call the FastAPI routes,
Supabase, RAG, the game backend, or the verifier. It records at most 30 seconds of
microphone audio, sends it to Gemini STT, sends the transcript to Groq, then plays a
Gemini TTS response.

```powershell
cd D:\Saigon-Veilwake\saigon-veilwake-ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# Fill GEMINI_API_KEY, GROQ_GENERATOR_API_KEY and GROQ_GENERATOR_MODEL in .env
python scripts/voice_loop.py
```

Use `python scripts/voice_loop.py --debug-audio` only when temporary microphone and
response WAV files need to be retained under the ignored `.voice_loop_tmp/` directory.
Without that flag, each turn removes both files after playback.

The voice adapters use the documented Gemini Interactions REST API directly with
`httpx`. No SDK upgrade is required for transcription or synthesis. Input is a short
mono PCM16 WAV sent inline with `vi-VN`; requests use `store=false`, make no Files API
uploads, and never switch models automatically.

To diagnose microphone versus STT independently, record five seconds and listen locally
(this command keeps its debug WAV and makes no provider request):

```powershell
python scripts/voice_loop.py --record-only --play-input --max-seconds 5 --input-device 2 --output-device 4
```

Speak a complete sentence and let recording stop automatically. Use the printed path
to replay that exact file through STT once, without recording again or calling Groq/TTS:

```powershell
python scripts/voice_loop.py --input-audio ".voice_loop_tmp/<turn-id>/microphone.wav" --stt-only
```

The original WAV is preserved. Audio duration/rate/RMS/peak and safe response token counts
help distinguish an invalid/silent recording from an empty provider result. Token counts
show that audio was processed, not that intelligible speech was present. HTTP status,
timeout, and incomplete interactions are reported separately; raw provider bodies,
headers, keys, and audio data are never printed.

See [voice troubleshooting and API references](docs/voice_loop_troubleshooting.md).
The confirmed empty-transcript bug was reading only `Part.text` while the dedicated
STT model returned `Part.audioTranscription.text`. The parser now handles that structured
response as well as Interactions text output. Live STT verification with a saved WAV
succeeded; repeated SDK upgrades or changing the selected model are unnecessary for
this parsing defect.

## VieNeu self-hosted TTS

The terminal voice loop and local `/dev/` UI can use either Gemini TTS (the default)
or a self-hosted VieNeu speech service. Gemini remains the STT provider in both cases.
Set `TTS_PROVIDER=vieneu` only after the separate VieNeu service is healthy. See
[VieNeu CPU local setup](docs/VieNeu_CPU_Local.md) for the Docker command, `.env`
settings, validation steps, and CPU tuning guidance.

## Dev Voice API + local web UI

Keep the existing working provider keys/models in `.env`. Add `SVAI_APP_ENV=development`
and `DEV_VOICE_ENABLED=true`. No test token is needed for this local-only demo.

```powershell
python -m pip install -e ".[dev]"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open `http://127.0.0.1:8000/dev/`, click Connect, and press the mic to speak.
Press again to stop; the UI sends the WAV automatically, polls for transcript/text
and timings, and plays the reply. Text input and text-only replies are also available.
No frontend build, npm dependencies, or FFmpeg are required.

This demo is unverified (`NOT_RUN`), without RAG, web search, Supabase or game context.
It is disabled by default, accepts only loopback connections, and has no per-user auth.
Use a single server
worker; jobs are in memory and output audio expires after ten minutes by default.
The original terminal command continues to work independently.

See [the full Vietnamese guide](docs/Dev_Voice_API_Local_UI.md) for endpoints,
configuration, diagrams, error handling, data lifetime and the mobile handoff boundary.
