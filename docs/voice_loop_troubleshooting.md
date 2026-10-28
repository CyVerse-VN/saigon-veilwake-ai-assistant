# Voice loop: evidence and verification

## Confirmed local defects

- Root cause of the repeated empty-transcript error: a real `generateContent`
  response contains the transcript in
  `candidates[0].content.parts[0].audioTranscription.text`. The previous implementation
  read only `parts[].text`. The 47-character transcript was present but discarded.
  The official REST `Part`/`AudioTranscription` schema confirms this field.
- The installed `google-genai==1.75.0` has `AudioTranscriptionConfig.language_codes`,
  but its `GenerateContentConfig` does not expose `audio_transcription_config`.
  This explains the observed local Pydantic validation failure, before HTTP generation.
- The TTS adapter used `interaction.output_audio` although the installed SDK's
  Interaction model exposes `outputs`, not that convenience property. This would fail
  once a turn reached TTS.
- The previous empty-transcript error discarded response status and audio/output token
  counts. It cannot establish whether the microphone captured understandable speech,
  Gemini produced zero text, or a response was incomplete.
- Earlier mock tests constructed the desired provider result rather than validating
  the real HTTP contract. Their passing did not prove that a live STT request worked.

## Implementation

Voice adapters now use Interactions REST directly via the existing `httpx` dependency.
STT validates the exact mono PCM16 WAV (1-30 s, 8-48 kHz, <=4 MiB), sends its bytes as
inline base64 `audio/wav`, with `generation_config.transcription_config.language_codes`
set to `["vi-VN"]`. Both STT and TTS use `store=false`, `stream=false`, and one HTTP
request per call with no hidden retries. No Files API upload/poll/delete is necessary
for a short demo recording.

The parser reads only final `model_output` text/audio content in current `steps`.
It also handles the older `outputs` response shape present in the installed SDK.
Incomplete/failed interactions are rejected, and user input/thoughts are never
treated as transcripts. WAV audio is validated and saved as returned; raw PCM16 audio
is wrapped with mono, 16-bit little-endian WAV at the documented rate (24 kHz default).

## Live verification on 2026-10-28

The user explicitly authorized two STT requests, then one additional STT request.
All three used the same user-recorded WAV and `gemini-3.5-transcribe`. No microphone,
Groq, or TTS call was made by the agent during verification. No model was changed.

The source WAV is 4.99 seconds, 16 kHz mono PCM16, RMS -37.4 dBFS, peak -16.3 dBFS.

| Request | Result |
| --- | --- |
| Interactions REST, current adapter | completed; 47 transcript characters; 125 audio input tokens; 12 output tokens |
| generateContent REST, old text-only parser | 0 extracted characters despite 12 output tokens |
| generateContent REST, safe schema inspection | `audioTranscription.text` is a string of length 47; no direct `Part.text` |

This controlled reproduction identifies a response-parsing defect, not zero model
output. Neither a different microphone, another key, a quota upgrade, nor a different
model is required to fix this observed issue. The shared transcript parser now also
handles structured `audioTranscription.text`, with a regression test reproducing the
observed response shape using a synthetic transcript rather than personal data.

The STT adapter remains on the current Interactions endpoint, which succeeded live.
TTS/Groq are covered by offline tests here; end-to-end live voice playback has not
been exercised by the agent. No further provider requests are authorized by the
three-request allowance, which is now exhausted.

## Reproduction without conflating recording and API behavior

1. Run `--record-only --play-input --max-seconds 5 --input-device 2 --output-device 4`.
   Say a complete sentence and let the timer stop recording. No API call is made.
2. Confirm the saved WAV contains clear speech. RMS/peak detect signal level, not
   speech intelligibility. A quiet/noisy or wrong-source recording is a device/gain issue.
3. Run `--input-audio <saved-path> --stt-only`. This sends exactly one STT request using
   the same WAV and does not call Groq, TTS, or open the microphone.
4. If speech is clear and the response has `status=completed`, positive
   `audio_input_tokens`, and zero `total_output_tokens`, the provider accepted audio
   but produced no transcript for that request. Preserve only the safe counts and
   local WAV for a reproducible report; this still does not establish a global outage.
5. A provider-side zero-output fault cannot be fixed by a text parser. Changing the
   selected STT model or adding a fallback requires an explicit configuration decision.

Input/output copies are removed per turn unless `--debug-audio` or `--record-only`
explicitly retains them under ignored `.voice_loop_tmp/`. File replay never deletes
the original source file. No raw response, key, auth header, URI, or audio base64 is logged.

## Official contracts consulted

- [Gemini Transcribe](https://ai.google.dev/gemini-api/docs/transcribe): Interactions
  invocation, language codes, and transcript output.
- [Interactions OpenAPI schema](https://ai.google.dev/static/api/interactions.openapi.json):
  inline `AudioContent.data`, `steps/model_output/content`, and usage counters.
- [Interactions overview](https://ai.google.dev/gemini-api/docs/interactions-overview):
  `store=false` and SDK version requirements for newer convenience methods.
- [Gemini speech generation](https://ai.google.dev/gemini-api/docs/speech-generation):
  unary WAV and streaming/raw little-endian PCM16 formats.
- [Legacy transcription guide](https://ai.google.dev/gemini-api/docs/generate-content/transcribe):
  the old `generateContent` path used by the earlier implementation is separately documented;
  being legacy alone is not proof of why a given request returned no text.
- [REST AudioTranscription schema](https://ai.google.dev/api/generate-content#AudioTranscription):
  `Part.audioTranscription` contains the required transcript `text` string.
