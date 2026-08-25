# Postmortem — DR Drill Lab 23

Postmortem này theo nguyên tắc blameless: tập trung vào điều kiện hệ thống và quy trình đã cho phép sự cố xảy ra, không quy lỗi cho cá nhân.

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| `2026-08-25T10:20:10` | Outage bắt đầu: Region A bị `netblock` | `chaos/chaos-events.jsonl:5` |
| `2026-08-25T10:20:11` | User đầu tiên bị ảnh hưởng, request đầu tiên trả `503 ReadTimeout` | `reports/drill-2-withdr.jsonl:26` |
| `2026-08-25T10:20:25` | Health checker xác nhận Region A `UNHEALTHY` sau 3 lần fail liên tiếp | `reports/health-events.jsonl:2` |
| `2026-08-25T10:20:29` | Failover workflow xác nhận Region B ready và thực hiện DNS cutover từ A sang B | `reports/failover-events.jsonl:5` |
| `2026-08-25T10:20:33` | Resolved: request thành công đầu tiên được phục vụ bởi Region B | `reports/drill-2-withdr.jsonl:37` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: `300s` · đo được: `22.5s` · headroom so với mục tiêu: `277.5s`
- RPO mục tiêu: `300s` · đo được: `28.0s` (`14 docs` bị mất) · headroom so với mục tiêu: `272.0s`
- **Bước tốn nhiều giây nhất:** Health-check detection floor — cấu hình `interval=5s × threshold=3` tạo detection floor `15s`, chiếm phần lớn RTO.

RTO thực tế gồm các mốc chính:

- Health checker phát hiện Region A unhealthy: `14.8s`
- Snapshot restore hoàn tất: khoảng `12.2s` sau outage
- GPU/pool warm-up: `6.21s`
- DNS cutover: khoảng `18.4s`
- Request thành công đầu tiên sau recovery: `22.5s`

Một số bước chạy chồng lấn nhau, vì vậy không thể cộng tất cả thời gian trên theo cách tuyến tính để ra RTO. Giá trị RTO chuẩn được lấy trực tiếp từ timestamp outage tới request thành công đầu tiên từ Region B.

Evidence:
- `chaos/chaos-events.jsonl:5`
- `reports/health-events.jsonl:2`
- `reports/failover-events.jsonl:2`
- `reports/failover-events.jsonl:4`
- `reports/failover-events.jsonl:5`
- `reports/drill-2-withdr.jsonl:37`

## 3. Root cause (5 whys)

### Vấn đề quan sát được

Trong một lần chạy trước, snapshot restore báo thành công nhưng Region B vẫn không ready và failover dừng tại `4_wait_ready`.

### Why 1 — Vì sao Region B không ready?

Region B vẫn thấy state không hợp lệ, ví dụ pool còn `warm`, weights hoặc vector state không đúng với state vừa restore.

### Why 2 — Vì sao state được restore nhưng serving process không nhìn thấy?

Failover tooling và Region B serving process không nhất thiết đọc cùng một cây `state/region-b`.

### Why 3 — Vì sao chúng có thể đọc hai cây state khác nhau?

`dr/failover.py` trước đó sử dụng một số đường dẫn tương đối phụ thuộc vào current working directory của process.

### Why 4 — Vì sao khác current working directory lại gây lỗi?

DR process và serving process có thể được khởi động từ hai checkout hoặc working directory khác nhau. Snapshot restore thành công về mặt filesystem nhưng lại ghi vào một cây state khác với cây mà Region B thực tế đang sử dụng.

### Why 5 — Vì sao hệ thống không phát hiện cấu hình không nhất quán này sớm hơn?

Chưa có validation bắt buộc để bảo đảm DR tooling và serving process dùng cùng absolute project/state root, đồng thời chưa có startup check phát hiện service trên port 8001/8002/8080 đang chạy từ một checkout khác.

### Root cause

Root cause không phải việc chạy chaos script. Root cause là **state path của DR workflow chưa được neo tuyệt đối và chưa có validation đảm bảo failover process với serving process sử dụng cùng state root**.

Sau khi `dr/failover.py` được sửa để sử dụng absolute project paths và các serving process được khởi động lại từ đúng checkout, failover hoàn tất thành công:

- Restore snapshot: `ok=true` — `reports/failover-events.jsonl:2`
- Region B ready: `ok=true` — `reports/failover-events.jsonl:4`
- DNS cutover: `ok=true` — `reports/failover-events.jsonl:5`

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giảm health-check interval từ `5s` xuống `2s` nhưng giữ threshold `3`, sau đó chaos-test để kiểm tra false positive | SRE / Platform owner | T+7 ngày | Detection floor từ `15s` xuống `6s`, tiềm năng giảm RTO tối đa khoảng `9s` |
| 2 | Giảm replication interval từ `30s` xuống `10s` và đo lại RPO qua nhiều drill | Data / Platform owner | T+14 ngày | Worst-case replication lag giảm khoảng `20s`; trực tiếp cải thiện RPO |
| 3 | Chuẩn hóa `STATE_DIR`/project root thành absolute path và thêm startup validation để phát hiện process chạy từ checkout khác | DR / Platform owner | T+3 ngày | Không trực tiếp giảm số giây trong run thành công, nhưng loại bỏ failure mode có thể khiến RTO trở thành `NO_RECOVERY` |

## 5. Ba câu hỏi bắt buộc trả lời

### 1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?

Cấu hình hiện tại:

`5s × 3 = 15s`

Detection floor là `15s`.

So với RTO đo được `22.5s`:

`15 / 22.5 × 100 ≈ 66.7%`

Như vậy detection floor tương đương khoảng **66.7% RTO**.

Nếu dùng thời gian phát hiện thực đo `14.8s` thì:

`14.8 / 22.5 × 100 ≈ 65.8%`

Evidence: `reports/health-events.jsonl:2`.

### 2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và trả giá gì?

Nếu vẫn giữ threshold `3`:

- Detection floor hiện tại: `5 × 3 = 15s`
- Detection floor mới: `1 × 3 = 3s`
- Giảm lý thuyết: `12s`

Do các bước failover trong run hiện tại có overlap, RTO thực tế không chắc giảm đúng trọn `12s`, nhưng detection component có thể giảm tối đa khoảng `12s`.

Chi phí/rủi ro:

- Health endpoint bị probe thường xuyên hơn.
- Tăng network/request overhead.
- Các lỗi transient ngắn dễ đạt threshold nhanh hơn.
- Nguy cơ false-positive failover và flapping tăng.
- Cần thêm hysteresis/cooldown hoặc threshold phù hợp để tránh chuyển vùng qua lại không cần thiết.

Vì vậy giảm interval phải được chaos-test và load-test trước khi áp dụng production.

### 3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` có nghĩa gì với khách hàng?

Trong drill này:

`docs_lost = 14`

Evidence: `reports/failover-events.jsonl:2`.

Điều đó có nghĩa snapshot được restore sang Region B chưa chứa 14 document mới nhất đã tồn tại ở Region A trước outage.

Nếu Region A mất dữ liệu vĩnh viễn thì 14 document đó không chỉ là độ trễ tạm thời; chúng có thể trở thành mất dữ liệu thực tế đối với khách hàng.

Tác động có thể gồm:

- dữ liệu mới chưa xuất hiện trong vector search,
- câu trả lời AI thiếu các document vừa ingest,
- retrieval trả về context cũ,
- khách hàng phải ingest lại dữ liệu nếu còn bản gốc,
- nếu không còn nguồn khác thì dữ liệu có thể mất vĩnh viễn.

Vì vậy RPO không chỉ là một con số kỹ thuật. `28.0s / 14 docs` mô tả trực tiếp lượng dữ liệu khách hàng có nguy cơ mất nếu primary region không thể phục hồi.

## Kết luận

Drill 2 đạt cả hai mục tiêu:

- **RTO: `22.5s < 300s` → PASS**
- **RPO: `28.0s < 300s` → PASS**

Điểm cần tối ưu lớn nhất về RTO là health-check detection floor. Điểm cần cải thiện về RPO là replication interval. Ngoài hiệu năng, drill cũng phát hiện một failure mode quan trọng liên quan đến path consistency giữa DR tooling và serving process.