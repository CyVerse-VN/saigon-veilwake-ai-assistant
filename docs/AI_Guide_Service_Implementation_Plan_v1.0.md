# Saigon: Veilwake — AI Guide Service Implementation Plan

> **Phiên bản:** 1.0 — 27/10/2026  
> **Cập nhật tiến độ:** 28/10/2026 — dựa trên code hiện có, kết quả kiểm thử offline gần nhất và xác nhận test local của người dùng.  
> **Phạm vi:** Kế hoạch triển khai AI Guide Service, một thành phần AI chuyên biệt tích hợp qua Game Backend/API Gateway; không phải toàn bộ backend game.  
> **Mục tiêu MVP:** Người chơi gửi text hoặc giọng nói tiếng Việt, nhận câu trả lời có bằng chứng, không spoiler và có audio trả lời khi yêu cầu voice.  
> **Nguyên tắc:** Game Backend là authority của identity, game state, quest/reward và quyền truy cập; AI Guide chỉ đọc context được cấp quyền.

---

## 0. Bảng theo dõi tiến độ hiện tại

Quy ước: **Đã có** = code hoặc kết quả test đã có trong phạm vi ghi rõ; **Một phần** =
có scaffold/POC nhưng chưa đạt Definition of Done của phase; **Chưa bắt đầu** = chưa
có triển khai hoàn chỉnh. Không dùng trạng thái của demo để đánh dấu MVP verified đã xong.

| Hạng mục | Trạng thái | Bằng chứng / phần còn lại |
|---|---|---|
| Phase 0 — API skeleton `/v1` | **Một phần** | FastAPI, contract và mock/fallback đã có; pipeline verified, persistence bền vững và contract production chưa hoàn tất. |
| Terminal Voice Loop POC | **Đã có, đã test cơ bản** | Người dùng xác nhận nói và nghe được phản hồi: Gemini STT → Groq Generator → Gemini TTS. |
| Phase 0.5 — Dev Voice API + Web UI local | **Đã có, đã test cơ bản** | Người dùng xác nhận test phần web local ổn; API `/dev/v1` và UI `/dev/` thực hiện voice theo lượt. Chưa có bằng chứng nghiệm thu từng ca lỗi/thiết bị hoặc tích hợp mobile. |
| Phase 1 — Supabase/schema/RLS | **Chưa hoàn thành** | Tài liệu cũ ghi đã kiểm tra Data API HTTP 200; chưa xác minh lại trong lần cập nhật này. Repository chưa có thư mục migration/schema RAG. |
| Phase 2 — Ingestion/embeddings | **Một phần** | Có Markdown chunker và adapter Gemini embedding; chưa có CLI ingestion, corpus được ingest hoặc xác nhận embedding thật. |
| Phase 3 — Retrieval/evidence | **Một phần** | Có utility RRF và access filter; chưa có repository/RPC, retrieval dữ liệu thật hoặc evidence package hoàn chỉnh. |
| Phase 3.5 — Source Router + Web Search có điều kiện | **Chưa bắt đầu** | Đã chốt policy RAG-first và giới hạn nguồn; chưa có routing/eligibility gate, provider search/weather hay kết quả có freshness. |
| Phase 4 — Generator/Verifier/Gate | **Một phần** | Groq Generator đã dùng trong demo tổng quát; Verifier, evidence-grounded generation và deterministic gate chưa có. |
| Phase 5 — Voice cho pipeline verified | **Một phần** | Gemini STT/TTS đã dùng trong demo; chưa nối approved answer/Verification Gate và chưa benchmark giọng/tên riêng. |
| Phase 6 — Mobile/Game Backend | **Chưa bắt đầu** | Chưa có mobile integration, trusted context, service auth hoặc quyền truy cập theo người dùng. |
| Phase 7 — Evaluation/pilot | **Chưa bắt đầu** | Chưa có golden dataset, benchmark, security review và tiêu chí pilot được xác nhận. |

**Kết quả kiểm tra gần nhất đã ghi nhận:** sau thay đổi bỏ token demo, `pytest` **66
passed, 1 warning** deprecation của Starlette/httpx; test JavaScript **7 passed**;
`ruff check .` **passed**. Đây là kết quả từ lần kiểm tra trước, không phải test được
chạy lại khi cập nhật tài liệu này. Test tự động dùng mock provider; xác nhận test web
cơ bản là do người dùng báo cáo, không thay thế benchmark hoặc nghiệm thu mọi ca lỗi.

**Giới hạn hiện tại:** demo không có RAG, Web Search, thời tiết realtime, Supabase,
Verifier, Game Backend hoặc memory giữa các lượt. Vì không được cấp dữ liệu hiện tại,
demo không thể xác nhận thời tiết hôm nay. API demo local dùng `voice_demo` /
`verification_status=NOT_RUN`; chỉ chạy development, nhận kết nối loopback, không cần
token và không phân quyền giữa các client trên cùng máy.

Tài liệu thực hành: [Dev Voice API + Web UI local](Dev_Voice_API_Local_UI.md),
[giải thích Terminal Voice Loop](Terminal_Voice_Loop_POC_Giai_Thich_Co_Che.md) và
[proposal kiến trúc](Saigon_Veilwake_AI_Guide_Service_Proposal_v1.3.md).

---

## 1. Quyết định và trạng thái nền tảng

| Hạng mục | Quyết định hiện tại | Trạng thái |
|---|---|---|
| Service | FastAPI modular monolith | Khung đã tạo |
| LLM Generator/Verifier | Groq, hai key tách riêng | Generator đã tích hợp và test trong voice demo; Verifier chưa dùng/chưa tích hợp. |
| STT | Gemini `gemini-3.5-transcribe`, `vi-VN` | Adapter đã dùng trong demo terminal/web; glossary game và benchmark chưa có. |
| TTS | Gemini `gemini-3.8-flash-lite-tts` | Adapter đã dùng trong demo terminal/web; chưa nối verified answer. |
| Embedding | Gemini `gemini-embedding-001`, 768 dimensions | Có adapter scaffold; chưa xác nhận embedding thật, benchmark hoặc ingest. |
| Database/vector store | Supabase PostgreSQL + pgvector | Trước đây đã báo Data API HTTP 200; chưa xác minh lại, chưa có migration/schema trong repo. |
| Voice | Turn-based; app ghi và tự gửi audio | Demo STT → Generator → TTS đã chạy; luồng verified answer → TTS chưa hoàn thành. |
| Web Search / thời tiết | Chỉ dùng khi nguồn công khai đủ điều kiện sau RAG | Policy đã chốt; chưa tích hợp provider hay dữ liệu realtime. |
| Security boundary | Mobile → Game Backend/API Gateway → AI Guide | Đã xác định; contract Game Backend chưa có |

### Những điều không được làm

- Không để mobile/web client gọi Groq, Gemini hoặc Supabase Secret key.
- Không gửi JWT, raw game state, PII không cần thiết hoặc lore vượt quyền sang provider.
- Với **MVP verified/game**, không để Generator trả lời kiến thức trực tiếp khi thiếu evidence.
- Với **MVP verified/game**, không tạo TTS từ draft; TTS chỉ nhận `approved_answer_id` sau Verification Gate `PASS`.
- **Ngoại lệ demo local đã chốt:** `/dev/v1` được phát câu trả lời tổng quát `NOT_RUN` để test voice; không coi đó là lore/canon hay kết quả đã kiểm chứng.
- Không lọc spoiler chỉ bằng prompt; filter phải xảy ra trước retrieval.

---

## 2. Tổng quan phase và phụ thuộc

```text
P0 Foundation/Contracts
  ├─ P0.5 Terminal POC + Dev Voice API + web local (demo đã test cơ bản)
  ├─ P1 Supabase schema + security
  │    └─ P2 Lore ingestion + embeddings
  │         └─ P3 Hybrid retrieval + spoiler filter
  │              └─ P3.5 Source Router + RAG-first + web fallback có điều kiện
  │                   └─ P4 Groq generation + verification gate
  │                        └─ P5 Gemini voice cho verified answer
  ├─ P6 Game Backend integration (có thể chuẩn bị song song từ P1)
  └─ P7 Evaluation, hardening, pilot (sau P4–P6)
```

P1–P5 tạo được MVP nội bộ với game-context mock. P6 mới cho phép phát hành qua Mobile Game. P7 là điều kiện trước pilot người chơi thật.

---

## Phase 0 — Foundation, Contract & Skeleton

**Trạng thái:** Hoàn thành phần đầu; cần hoàn thiện khi các module thật được nối.

### Mục tiêu

Tạo nền API có contract ổn định, xử lý asynchronous turn và fail-closed khi provider/RAG chưa sẵn sàng.

### Đã có

- FastAPI app, `/health`, tạo conversation, text turn, voice turn và polling turn.
- Kiểm tra MIME/kích thước audio ở mức API.
- In-memory turn store/mock processor; fallback đã duyệt thay vì câu trả lời bịa đặt.
- Cấu hình `.env.example`, dependency và test API cơ bản.

### Việc còn lại

- Chuẩn hóa error envelope, `request_id`, idempotency và API versioning trong OpenAPI.
- Thay in-memory store/background task bằng repository + durable job/checkpoint ở Phase 1.
- Xác định interface dependency injection cho repository, embeddings, LLM, STT, TTS.
- Bổ sung contract test cho `202 + polling`, cancel và retry.

### Definition of Done

- API contract được version hóa và test không bị thay đổi vô ý.
- `/v1` verified không phát draft chưa được xác minh; `/dev/v1` tách riêng và đánh dấu `NOT_RUN`.
- API key/provider key không xuất hiện trong response, logs hoặc OpenAPI.

---

## Phase 0.5 — Terminal Voice POC, Dev Voice API & Web UI local

**Trạng thái:** Đã implement và người dùng xác nhận test cơ bản ổn trên terminal và
web local. Đây là milestone demo; chưa xác nhận toàn bộ acceptance trên nhiều thiết bị,
chưa tích hợp mobile, và không thay thế các phase evidence/verification.

### Đã thực hiện

- [x] `scripts/voice_loop.py` dùng adapter Gemini STT, Groq Generator và Gemini TTS;
  orchestration nằm trong `app/modules/voice_loop/service.py`.
- [x] `/dev/v1` có tạo conversation, text/voice turn, `202 + polling` và audio endpoint;
  output demo ghi `mode=voice_demo`, `verification_status=NOT_RUN`.
- [x] Web `/dev/` nhấn mic ghi/dừng, tự gửi WAV PCM16 mono 16 kHz, polling, hiển thị
  transcript/text/timing và tự phát audio; có chế độ text-only.
- [x] Giới hạn audio, số lượt đồng thời, timeout, idempotency; lỗi TTS vẫn giữ text;
  input/output tạm và output TTL. Các nhánh này đã có test mock offline.
- [x] Chỉ development/loopback, không cần dev token; một caller dùng chung, không có
  auth hoặc object authorization cho nhiều người dùng. Không dùng API này cho mobile qua mạng.
- [x] Người dùng báo test luồng voice trên web local ổn; chưa ghi nhận bộ kết quả chi tiết.

### Còn lại trước khi mở rộng demo

- [ ] Ghi lại các ca thử trên thiết bị/trình duyệt cần hỗ trợ: quyền mic, ghi ngắn,
  tự dừng, lỗi mạng/TTS, autoplay và audio hết TTL; chưa coi các ca này đã được người dùng nghiệm thu.
- [ ] Nếu đưa sang mobile: chốt audio codec, network/TLS, auth và phân quyền theo user,
  thời hạn dữ liệu và contract với Game Backend/API Gateway; test trên thiết bị thật.
- [ ] Tách riêng nghiệm thu demo với nghiệm thu pipeline RAG/Verifier/voice verified.

---

## Phase 1 — Supabase Foundation & Security

**Trạng thái:** Chưa hoàn thành. Tài liệu trước ghi đã kiểm tra Data API HTTP 200;
chưa kiểm tra lại trong lần cập nhật này. Chưa có migration/schema trong repository.

### Mục tiêu

Tạo persistence bền vững cho knowledge/RAG, turn lifecycle và audio metadata, với quyền truy cập an toàn.

### Công việc

1. Tạo migration có version trong `migrations/`.
2. Bật extension `vector` và `pgcrypto` qua migration/SQL Editor.
3. Tạo các bảng knowledge:
   - `knowledge_sources`: nguồn lore/web được phê duyệt, domain, canonical URL, owner, status.
   - `document_versions`: version, content hash, active flag, approval status.
   - `document_chunks`: content, section, metadata, language, `story_access_level`, embedding 768D, FTS vector.
   - `ingestion_jobs`: source, trạng thái, metrics, error an toàn.
4. Tạo bảng vận hành tối thiểu:
   - `conversations`, `conversation_turns`, `turn_events`, `turn_evidence`.
   - `verification_records`, `approved_answers`, `audio_assets`.
5. Bật RLS trên toàn bộ bảng application; không tạo public policy cho lore/private data.
6. Service dùng `SUPABASE_SECRET_KEY` server-side; public/publishable key không được dùng bởi AI Guide backend.
7. Tạo private Storage bucket `ai-guide-audio` nhưng chưa lưu raw microphone lâu dài.
8. Viết smoke test migration và kiểm tra role không được phép đọc lore bằng publishable key.

### Đầu vào cần có

- Supabase project đã tạo và pgvector đã enable.
- Quyết định retention: audio input, transcript, generated audio, trace log.

### Definition of Done

- Migration chạy được trên project trống và có thể chạy lại an toàn.
- `document_chunks.embedding` đúng 768 dimensions; mọi thay model/dimension yêu cầu migration/re-embed plan.
- Secret key đọc/ghi được từ backend; publishable key không đọc được lore/spoiler/private table.
- Không có audio bucket public.

---

## Phase 2 — Lore Ingestion & Embedding Pipeline

**Trạng thái:** Scaffold một phần đã có: Markdown chunker, spoiler access filter và Gemini embedding adapter; chưa có bằng chứng ingest dữ liệu hoặc xác nhận embedding provider thật.

### Mục tiêu

Biến lore đã được phê duyệt thành document chunks có provenance, access tag và embedding truy vấn được.

### Công việc

1. Chốt source manifest: tài liệu nào được ingest, owner, version, license, domain và mức spoiler.
2. Viết parser Markdown/text ban đầu; PDF/DOCX chỉ bổ sung khi có OCR/extraction policy rõ ràng.
3. Chunk theo heading/đoạn, có overlap; lưu `section_title`, ordinal, content hash và metadata.
4. Bắt buộc gắn `story_access_level` trước khi tạo embedding.
5. Gọi Gemini embedding với cùng `GEMINI_API_KEY`:
   - document: `RETRIEVAL_DOCUMENT`;
   - query: `RETRIEVAL_QUERY`;
   - dimension 768 và normalize vector.
6. Upsert source/version/chunk theo content hash; không duplicate khi chạy lại.
7. Ghi ingestion job, số chunk, token/latency, lỗi và model version.
8. Tạo CLI `ingest_lore` có `--dry-run`, `--source-id`, `--version`, `--story-access-level`.

### Đầu vào cần có

- Một corpus lore seed đã phê duyệt; tối thiểu 5–10 trang nội dung thật.
- Mapping spoiler/chapter rõ ràng cho từng source/section.
- Quyền sử dụng tài liệu khi gửi nội dung sang Gemini embedding.

### Definition of Done

- Một source seed được ingest nhiều lần không tạo duplicate.
- Mỗi chunk có source, version, section và access level truy vết được.
- Không có chunk spoiler xuất hiện trong candidate set của user chưa mở chapter.
- Có báo cáo ingest: source/version/hash/chunk count/error.

---

## Phase 3 — Hybrid Retrieval & Evidence Package

**Trạng thái:** Scaffold một phần đã có RRF/access-filter utility; chưa có Supabase RPC, repository hoặc retrieval chạy với dữ liệu thật. Phụ thuộc P1 và P2.

### Mục tiêu

Truy xuất lore bằng semantic + keyword search, lọc quyền trước retrieval và xuất evidence package cho LLM.

### Công việc

1. Tạo Postgres RPC `match_document_chunks`:
   - filter `is_active`, `knowledge_domain`, language và `story_access_level` trước ranking;
   - vector cosine search với pgvector;
   - full-text search dùng cấu hình `simple` ban đầu;
   - trả source/version/citation metadata, không chỉ raw content.
2. Dùng Reciprocal Rank Fusion (RRF) để gộp semantic rank và FTS rank.
3. Tạo `KnowledgeRepository`/`HybridRetriever` adapter, giới hạn Top-K và maximum context bytes.
4. Thêm reranker interface nhưng chưa cài provider cho đến khi benchmark chứng minh cần thiết.
5. Tạo `EvidencePackage` immutable: evidence ID, chunk ID, text đã lọc, source citation, freshness/version.
6. Tạo debug endpoint admin-only hiển thị candidate/rank/filter decision; không public cho client.
7. Tạo golden retrieval dataset gồm lore normal, tên riêng, paraphrase, câu spoiler và câu không có evidence.

### Definition of Done

- Candidate bị spoiler bị loại bỏ trước khi gọi Gemini/Groq.
- Mọi citation trả về Generator đều map được tới `document_chunk`/source version.
- Có Recall@K, MRR và spoiler leakage test baseline.
- Câu không có evidence cho kết quả rỗng/fallback, không ép retrieval một tài liệu không liên quan.

---

## Phase 3.5 — Source Router, RAG-first & Web Search có điều kiện

**Trạng thái:** Policy đã chốt trong proposal; chưa có implementation routing, web
eligibility, search/weather provider hoặc dữ liệu realtime trong demo hiện tại.

### Mục tiêu

Xác định nguồn theo domain và quyền, ưu tiên evidence nội bộ cho câu hỏi kiến thức;
chỉ bổ sung nguồn công khai khi RAG thiếu/không đủ mới **và** policy cho phép. Evidence
từ nguồn ngoài đi qua cùng pipeline kiểm chứng trước khi trở thành câu trả lời verified.

### Công việc

1. Phân loại câu hỏi/domain: lore/canon độc quyền, game state, kiến thức công khai,
   câu hỏi cần dữ liệu mới như thời tiết, hoặc câu trực tiếp không cần tra cứu.
2. Thực hiện retrieval nội bộ theo quyền/spoiler với câu hỏi kiến thức; đánh giá độ
   liên quan, bao phủ, độ tin cậy và freshness. Có kết quả retrieval chưa tự chứng minh
   đã đủ bằng chứng; lỗi retrieval hoặc nội dung bị chặn không tự cho phép web fallback.
3. Tạo eligibility gate trong backend. Chỉ domain công khai được phép tìm web/API khi
   bằng chứng nội bộ thiếu, không đủ hoặc quá cũ; chỉ truy vấn phần cần bổ sung.
4. Thiết kế adapter Web Search/nguồn dữ liệu chuyên biệt, ví dụ weather API cho thời
   tiết, theo interface thống nhất; provider, quota, timeout và tiêu chí nguồn sẽ chốt
   khi triển khai. Lưu provenance, thời điểm lấy dữ liệu, freshness và kết quả rỗng/lỗi.
5. Gộp evidence hợp lệ và chuyển cho Generator → Verifier/Gate ở Phase 4. Chưa release
   câu trả lời kiến thức như verified nếu chưa có evidence và gate phù hợp.
6. Với lore/canon độc quyền thiếu bằng chứng: báo chưa có thông tin được xác minh;
   **không** tìm web để lấp chỗ trống. Game state lấy từ Game Backend được cấp quyền,
   không dùng web/RAG thay thế. Nếu dữ liệu thời tiết không có, lỗi hoặc không đủ mới,
   nói rõ chưa kiểm tra được thay vì đoán.

### Definition of Done

- Không gọi web khi RAG đã đủ evidence còn mới hoặc khi domain/quyền không cho phép.
- Test lore độc quyền, câu hỏi bị chặn spoiler, game state, RAG miss, retrieval error,
  nội dung công khai thiếu evidence và thông tin cần freshness; có thể kiểm tra số lần gọi web bằng mock.
- Evidence ngoài có nguồn/thời điểm truy xuất; timeout/quota/lỗi không tạo câu trả lời
  giả đã xác minh. Kết quả được kiểm chứng qua Phase 4 trước khi cấp cho pipeline verified.
- Chốt cách xác định địa điểm và đơn vị khi thử thời tiết; người dùng chỉ nói “hôm nay”
  mà thiếu địa điểm thì hỏi lại hoặc dùng context địa điểm đã được phép cung cấp.

---

## Phase 4 — Groq Generator, Verifier & Deterministic Gate

**Trạng thái:** Một phần. Generator tổng quát đã tích hợp trong voice demo; pipeline
Generator dựa trên evidence, Verifier và Gate chưa triển khai. Verifier key chưa được dùng.

### Mục tiêu

Sinh câu trả lời có kiểm chứng độc lập; chỉ release answer khi code gate xác nhận pass.

### Công việc

1. Mở rộng `GroqGeneratorProvider` hiện có dùng `GROQ_GENERATOR_API_KEY` và `GROQ_GENERATOR_MODEL` để nhận evidence/schema cho pipeline verified; không xem prompt demo là pipeline verified.
2. Tạo `GroqVerifierProvider` dùng `GROQ_VERIFIER_API_KEY` và `GROQ_VERIFIER_MODEL`.
3. Benchmark model Groq trước khi đặt model ID: tiếng Việt, JSON schema, latency, quota, hallucination, false accept/reject.
4. Generator schema:
   - `answer_text`, `claims[]`, `citation_ids[]`, `content_mode`.
5. Verifier schema:
   - đánh giá từng claim: `SUPPORTED`, `UNSUPPORTED`, `NEEDS_REVISION`;
   - evidence IDs đã dùng, reason code, safe rewrite instruction.
6. Deterministic gate kiểm tra schema, evidence reference, spoiler scope, decision, revision count.
7. Một revision tối đa; nếu lỗi Groq, timeout, schema invalid hoặc `429` thì fallback đã duyệt.
8. Lưu trace an toàn: model/config version, timing, decision; không lưu secret/raw JWT.

### Definition of Done

- Unsupported claim không thể thành `APPROVED`.
- Citation ID không tồn tại khiến turn fail-closed.
- `429`, timeout và malformed JSON có retry giới hạn rồi fallback.
- Approved answer có `approved_answer_id`; draft không thể gọi TTS.

---

## Phase 5 — Gemini Voice Vertical Slice

**Trạng thái:** Một phần. Gemini STT/TTS đã tích hợp và được người dùng test cơ bản
trong demo; luồng voice dựa trên approved answer và benchmark chưa hoàn thành.

### Mục tiêu

Hoàn thành luồng voice-in/voice-out turn-based, dùng text verified của Phase 4 làm nguồn duy nhất cho TTS.

### Công việc

1. Mở rộng `GeminiSttProvider` hiện có dùng `gemini-3.5-transcribe`:
   - upload audio theo MIME/size/duration policy;
   - gửi language `vi-VN`;
   - inject domain glossary/tên riêng game;
   - trả transcript, confidence/metadata nếu provider có.
2. Tích hợp `GeminiTtsProvider` hiện có (`gemini-3.8-flash-lite-tts`) vào pipeline verified:
   - input chỉ là `approved_answer_id`;
   - lấy approved text server-side;
   - trả audio bytes + MIME/manifest, không để client gửi arbitrary text.
3. Lưu output audio vào private bucket, signed URL TTL ngắn; không public bucket.
4. Lưu raw microphone/audio input theo retention + consent policy; mặc định TTL ngắn hoặc không giữ.
5. Cập nhật turn state: `TRANSCRIBING`, `GENERATING`, `VERIFYING`, `SYNTHESIZING`, `COMPLETED`, `COMPLETED_TEXT_ONLY`.
6. Khi STT ambiguous tên riêng: hỏi lại thay vì suy đoán.
7. Khi TTS fail/quota: trả approved text và `audio.status=UNAVAILABLE`.

### Đầu vào cần có

- `GEMINI_API_KEY` backend-only.
- Chấp thuận policy dữ liệu Gemini Free Tier cho audio/transcript/lore được gửi.
- Bộ audio test tiếng Việt: giọng Bắc/Trung/Nam, môi trường ồn, tên riêng và địa danh game.

### Definition of Done

- Audio tiếng Việt → transcript → evidence → verified answer → audio tiếng Việt.
- Không có audio từ draft/rejected answer.
- TTS failure không làm mất approved text.
- Đo WER/proper-name accuracy, TTS pronunciation/naturalness và latency end-to-end.

---

## Phase 6 — Game Backend/API Gateway Integration

**Trạng thái:** Chưa bắt đầu; có thể chuẩn bị contract song song từ Phase 1.

### Mục tiêu

Tích hợp AI Guide vào game mà không biến service thành authority gameplay.

### Công việc

1. Chốt service-to-service authentication: issuer, audience, signature, expiry, token exchange, rotation.
2. Chốt trusted context tối thiểu từ Game Backend:
   - player/session ID pseudonymous;
   - `story_access_level`, chapter/quest flags;
   - NPC/location scope và locale.
3. Game Context adapter chỉ đọc; không có endpoint grant reward, update quest hoặc mutate inventory.
4. Object authorization cho conversation, turn, transcript, audio signed URL.
5. Mobile gọi Game Backend/API Gateway; tuyệt đối không gọi AI Guide, Groq/Gemini/Supabase trực tiếp.
6. Contract tests Game Backend ↔ AI Guide cho identity spoofing, stale story state, cancellation và reconnect/polling.

### Definition of Done

- Request tự khai player ID từ client bị từ chối/ghi đè bằng trusted context.
- Không thể lấy audio/turn của player khác.
- AI Guide không có quyền ghi state/reward game.

---

## Phase 7 — Evaluation, Security Hardening & Pilot

**Trạng thái:** Chưa bắt đầu; phụ thuộc P4–P6.

### Mục tiêu

Đo chất lượng, xử lý quota/failure và chuẩn bị pilot có quan sát được.

### Công việc

1. Golden test set:
   - retrieval, lore correctness, citations, spoiler boundaries;
   - Groq JSON/verification failure;
   - STT/TTS Vietnamese proper nouns;
   - quota exhaustion/timeout/provider outage.
2. Metrics:
   - Recall@K/MRR, citation coverage, unsupported-claim pass rate, spoiler leakage rate;
   - STT WER/CER, proper-name accuracy, TTS quality;
   - p50/p95 latency per stage, 429/error/fallback rate.
3. Security review: RLS tests, secret scan, dependency scan, prompt-injection fixtures, upload validation, audit trail.
4. Rate limits/budgets: per player/device/session, audio duration, Groq/Gemini quota guards, retry/backoff and circuit breaker.
5. Runbook: rotate Groq/Gemini/Supabase keys, provider outage, rollback migration, re-ingest/re-embed, delete user data/audio.
6. Deploy staging, then pilot cohort nhỏ; production rollout chỉ sau exit criteria.

### Exit criteria pilot

- Không có known spoiler leak trong golden/security suite.
- 100% verifier/provider failure không phát draft.
- Có alert cho quota, `401/429/5xx`, queue failure và storage failure.
- Có cơ chế xoá conversation/audio theo retention policy.
- Có phương án fallback text-only được UX/game team duyệt.

---

## 3. Checklist tài nguyên theo phase

| Resource | P1 | P2 | P3 | P4 | P5 | P6 |
|---|---:|---:|---:|---:|---:|---:|
| Supabase URL + Secret key | ✓ | ✓ | ✓ |  | ✓ |  |
| pgvector enabled | ✓ | ✓ | ✓ |  |  |  |
| Lore corpus + spoiler tags |  | ✓ | ✓ | ✓ |  | ✓ |
| Gemini API key |  | ✓ | ✓ |  | ✓ |  |
| Groq Generator/Verifier keys |  |  |  | ✓ |  |  |
| Vietnamese audio test set |  |  |  |  | ✓ |  |
| Game Backend service contract |  |  |  |  |  | ✓ |

---

## 4. Thứ tự thực thi kế tiếp

1. **Ghi nhận Phase 0.5:** demo terminal/web local đã được người dùng test cơ bản;
   bổ sung kết quả các ca lỗi/thiết bị khi thử tiếp. Mobile/auth/network vẫn chưa có.
2. **Phase 1:** rà soát project Supabase, thiết kế và tạo migration schema/RLS; chốt
   retention và private audio storage khi cần. Không xem kết nối HTTP cũ là đã có schema.
3. **Phase 2:** chuẩn bị corpus lore seed được phê duyệt và spoiler tags, implement CLI
   ingestion, embeddings và kiểm tra ingest lặp.
4. **Phase 3:** triển khai hybrid retrieval, evidence/provenance và test spoiler filter.
5. **Phase 3.5:** triển khai Source Router, RAG sufficiency/freshness và web eligibility;
   chọn tích hợp Web Search hoặc nguồn thời tiết phù hợp cho câu hỏi công khai cần cập nhật.
   Thực hiện test routing với mock trước khi gọi provider thật.
6. **Phase 4:** nối Generator/Verifier/Gate với evidence fixture rồi evidence thật;
   chỉ khi gate đạt yêu cầu mới công bố câu trả lời knowledge là verified.
7. **Phase 5–7:** nối voice vào approved answer, tích hợp Game Backend/mobile có auth,
   benchmark và hardening trước pilot.

Các phần contract/test fixture có thể chuẩn bị song song, nhưng không coi phase sau là
hoàn tất chỉ vì có API key hoặc demo hoạt động. Đặc biệt, access filter và Verification
Gate là điều kiện của MVP verified. Nếu cần thử weather sớm hơn bằng một demo riêng,
phải xác định rõ phạm vi/nguồn và tiếp tục đánh dấu kết quả chưa verified; không xem
đó là hoàn thành RAG-first hoặc policy cho lore.
