# Mobile Kiri landmark context

Android camera guide sends `location_id` as a stable place ID on both text JSON and
voice multipart turns. This patch carries that value past STT into deterministic
query disambiguation before the existing RAG/Verifier/TTS pipeline. The original
transcript remains unchanged in the response; only the retrieval query is resolved.

Supported IDs: post_office, cathedral, independence_palace, ben_thanh, war_remnants,
city_hall. Unknown IDs do not become prompt text. An explicit different landmark in
the user's question is honored. Location is a client selection hint, not proof of
presence, image recognition, a factual citation, or authority to unlock rewards/lore.
The Android client requests public story access level 0; game access rules do not change.

`TurnResponse.location_context` is an additive nullable object containing location_id,
display_name, source=client_selection, visual_verified=false. The Android UI can
identify old deployments that ignore location_id instead of claiming successful
context recognition. STT input bytes are released after transcription or STT failure.

Use the existing /v1 endpoints and existing server-side provider configuration. This
patch does not replace internal-service authentication or turn the local /dev voice
harness into a mobile API. The observed Render deployment accepts /v1 without a token
in development; authenticated production clients need a mobile/game-backend gateway,
never the internal service token or provider credentials bundled in an APK.

Tests use synthetic WAV and fake providers: pytest tests/test_mobile_landmark_context.py.
A passing local test is not evidence that this branch has deployed on Render. Merge
and deploy this patch deliberately, then verify the additive location_context field
on the live service. The existing general voice route can still work before deployment,
but pronouns such as “nơi này” cannot be relied on until the context patch is live.
