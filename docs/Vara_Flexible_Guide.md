# Vara: mở rộng câu hỏi có nguồn — 05/11/2026

Luồng `/v1` dùng chung cho câu hỏi gõ và giọng nói. Bản sửa giữ contract mobile
(`npc_id=kiri`, `location_context`, câu trả lời/citation/audio) và các provider hiện có.

## Những nguyên nhân đã sửa

- Router trước đây mặc định câu không khớp từ khóa thành `game_lore`. Nay câu hỏi
  thông thường truy xuất `real_world` trước, rồi tìm nguồn công khai nếu cần. Lore,
  tiến trình, spoiler và câu thiếu chủ thể vẫn đi qua các nhánh kiểm soát riêng.
- Context chỉ nhận sáu địa danh và một số mẫu câu. Nay nhận đúng 24 ID địa danh của
  mobile, câu ngắn như “Địa chỉ?”, “Ai thiết kế?”, “Bao nhiêu tuổi?”, tiếng Việt
  không dấu cho các mẫu này và các lượt hỏi tiếp. Tên địa danh khác được nêu rõ
  trong câu không bị thay bằng địa danh đang chọn. Context chỉ hỗ trợ hiểu câu hỏi;
  không trở thành bằng chứng hoặc xác nhận nhận diện ảnh.
- Sanitizer tìm web trước đây bỏ câu hỏi và chỉ giữ chủ thể trong ngoặc. Nay giữ
  cả ý hỏi lẫn chủ thể; cache cho địa chỉ và năm xây dựng không bị gộp chung.
- Wikipedia trước đây truyền `exchars=2800`, vượt giới hạn 1.200 của TextExtracts,
  và yêu cầu nhiều full extract trong cùng request. Nay tìm theo tên địa danh,
  đọc từng trang riêng, chọn đoạn liên quan từ tối đa 80.000 ký tự bài thay vì chỉ
  giữ phần đầu. Mỗi đoạn chứng cứ đưa vào model vẫn tối đa 2.800 ký tự. Giữ infobox,
  nhận thêm ngày xây dựng/hoàn thành, bỏ trang định hướng, thử lại lỗi tải bài.
- Khi RAG nội bộ lỗi, câu công khai vẫn được tìm web và kiểm chứng. Lore không dùng
  nhánh này. Generator/Verifier/VerificationGate và điều kiện PASS trước TTS giữ nguyên.
- Prompt trả lời theo ý hỏi và độ dài người dùng yêu cầu, cho phép trả lời phần có
  bằng chứng và nói rõ phần chưa xác minh. Không thêm kiến thức model làm dữ kiện.
- Text + public search có thể khởi tạo với cấu hình Groq mà không bắt buộc key
  STT/embedding hoặc Supabase. Những tính năng chưa cấu hình vẫn không hoạt động.

## Nguồn công khai và thông tin thay đổi theo thời gian

`WEB_SEARCH_PROVIDER=wikipedia` là mặc định, không cần thêm key; phù hợp lịch sử,
kiến trúc, vị trí và dữ kiện bách khoa khi bài có thông tin tương ứng.

Để tìm rộng hơn, chọn **một** provider trong cấu hình server:

```dotenv
WEB_SEARCH_PROVIDER=tavily
TAVILY_API_KEY=<secret trong Render>
# Hoặc WEB_SEARCH_PROVIDER=brave và BRAVE_SEARCH_API_KEY=<secret trong Render>
WEB_SEARCH_TIMEOUT_SECONDS=10
WEB_SEARCH_CACHE_TTL_SECONDS=900
```

Không đưa secret vào APK/Git. Các adapter này đã tồn tại; bản sửa không tự đăng ký
dịch vụ hoặc thay provider đang cấu hình. Nếu thiếu key provider đã chọn, giữ fallback
Wikipedia như hành vi trước đây.

Câu hỏi giá vé, giờ mở cửa, thời tiết hoặc “hôm nay” được chuyển yêu cầu freshness
`day` tới Tavily/Brave. Wikipedia không đáp ứng yêu cầu này; ngày sửa bài không chứng
minh giờ/giá hiện tại. Nội dung RAG không có hợp đồng thời hạn nên không dùng thay
nguồn mới cho nhóm câu này. Bộ lọc thời gian cũng không bảo đảm nội dung chính xác;
Verifier vẫn phải đối chiếu từng khẳng định với nguồn trả về. Không có nguồn phù hợp
thì trả FALLBACK, không phát audio chưa được duyệt.

## Kiểm và triển khai

```powershell
python -m pytest -q
python -m ruff check app tests
```

`tests/test_flexible_guide.py` dùng HTTP/provider/WAV tổng hợp có nhãn, kiểm đủ 24 ID,
câu ngắn và follow-up, routing, lỗi RAG, lỗi Wikipedia, đoạn cuối bài, freshness,
nguồn/citation, nhánh kiểm chứng lại và text/voice API. Không gọi Groq/STT/TTS thật.
Các test cũ tiếp tục kiểm việc chặn câu chưa xác minh, truy cập và audio.

Sau khi cập nhật main, Render cần deploy bản mới (tự động nếu đã bật auto-deploy,
hoặc Manual Deploy → Deploy latest commit). Kiểm `GET /health` có:

```json
{"status":"ok","guide_policy_version":"2026-11-05.flexible-v2"}
```

Marker chứng minh phiên bản policy, không chứng minh corpus/provider có đủ dữ liệu.
Chạy các câu “Địa chỉ?”, “Xây dựng năm nào?”, “Ai thiết kế?”, “Có gì thú vị?” ở một
địa danh đã chọn; kiểm citation, rồi hỏi tiếp và đổi địa danh. Giờ/giá cần provider
phù hợp như trên. Phiên hội thoại ở RAM nên cần tạo lại phiên sau restart/deploy.
Không thay đổi dữ liệu hoặc tiến trình game khi kiểm.

Tham chiếu API:
[MediaWiki TextExtracts](https://www.mediawiki.org/wiki/Extension:TextExtracts#API),
[Brave freshness](https://api-dashboard.search.brave.com/api-reference/web/search/get).

## Kết quả kiểm tại checkout

161 pytest đạt (104 bài kiểm trước thay đổi, thêm 57 trường hợp); Ruff và
`git diff --check` đạt. Có cảnh báo deprecation từ pytest-asyncio và
Starlette/httpx của môi trường hiện có. Kiểm HTTP công khai trực tiếp bằng adapter
mới đã lấy được bài Nhà thờ chính tòa Đức Bà với mốc 1880 và bài Bưu điện Sài Gòn
có dữ liệu địa chỉ/năm. Lỗi tên chuyển hướng Wikipedia được tái hiện và sửa, có
kiểm hồi quy riêng. Các nguồn mạng có thể từ chối/timeout theo từng request;
đây không phải cam kết mọi câu hỏi hoặc mọi địa danh đều có đủ dữ liệu.

Chưa xác minh Groq/Verifier/STT/TTS thật của bản mới trên Render trong lần kiểm
cục bộ; kiểm runtime sau deploy theo marker nêu trên. Không ingest/ghi DB,
không sửa Android hoặc dữ liệu người chơi.

## Vòng kiểm câu hỏi thực tế tiếp theo

Bổ sung xử lý “công trình hiện nay” theo địa điểm đang chọn, bỏ địa danh bị phủ định
trong mẫu “X, không phải Y”, tìm cả hai địa danh khi so sánh và yêu cầu nguồn mới
cho lịch biểu diễn/tối nay. Có kiểm hồi quy tương ứng. Các lỗi này được tìm trong
đợt kiểm API live 36 câu trên bản flexible-v1; không thay nhãn kết quả cũ thành
kết quả của bản mới. Nguồn Wikipedia vẫn có thể chứa dữ kiện lịch sử không chính
xác; PASS chỉ xác nhận đối chiếu bằng chứng hiện có, không thay thẩm định nguồn.
