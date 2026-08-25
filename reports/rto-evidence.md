# RTO/RPO Evidence — Lab 23

Quy tắc: mọi số liệu dưới đây đều được lấy từ log thật của Drill 1 và Drill 2.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T09:29:17` | chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.1s` | request `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | từ request lỗi đầu tiên đến cuối loadgen không có request phục hồi | `reports/drill-1-nodr.jsonl:17` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` không tìm thấy request phục hồi | `reports/drill-1-nodr.jsonl:32` |

Drill 1 cho thấy khi Region A bị outage nhưng chưa có DR, traffic tiếp tục trỏ vào Region A. Từ request ở dòng 17 trở đi đều thất bại và không có recovery trong cửa sổ đo, vì vậy kết quả là `NO_RECOVERY`.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage (mốc 0) | `0.0s` | `action:kill` Region A | `chaos/chaos-events.jsonl:5` |
| User thấy lỗi đầu tiên | `0.3s` | request `ok:false` đầu tiên sau outage | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | `14.8s` | `to:UNHEALTHY`, `region:a`, sau 3 lần fail liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore xong | `12.2s` | `step:2_restore_snapshot`, `ok:true` | `reports/failover-events.jsonl:2` |
| Region phụ ready | `18.4s` | `step:4_wait_ready`, `ok:true` | `reports/failover-events.jsonl:4` |
| DNS cutover | `18.4s` | `step:5_dns_cutover`, `a -> b` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **`22.5s`** | request `ok:true` đầu tiên sau chuỗi lỗi, được phục vụ bởi Region B | `reports/drill-2-withdr.jsonl:37` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---:|---:|---|
| RTO — Inference API | `22.5s` | `300s` (5 phút) | **PASS** |
| RPO — Vector DB | `28.0s / 14 docs` | `300s` (5 phút) | **PASS** |

RTO được đo từ `t_outage = 1787653210.8968222` tới request thành công đầu tiên từ Region B tại `1787653233.4186535`, tương đương khoảng `22.5s`.

RPO tại thời điểm restore là `28.0s`, tương ứng `14 docs` chưa có trong snapshot được restore. Evidence nằm tại `reports/failover-events.jsonl:2`.

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---|---|
| Health-check detect floor | `15.0s` cấu hình; thực đo `14.8s` | `interval_s=5.0 × threshold=3`; Region A chuyển `UNHEALTHY` tại `reports/health-events.jsonl:2` | Có thể giảm `interval_s` hoặc threshold, nhưng threshold quá thấp làm tăng nguy cơ failover do lỗi probe tạm thời/flapping |
| Snapshot restore | khoảng `0.0s` giữa event restore hoàn tất và bước scale kế tiếp | `2_restore_snapshot` tại `reports/failover-events.jsonl:2`, bước `3_scale_pool` ngay sau đó tại `reports/failover-events.jsonl:3` | Snapshot nhỏ hơn, storage nhanh hơn hoặc replication gần Region B hơn |
| GPU pool warm-up | `6.21s` | trường `waited_s=6.21` trong `step:4_wait_ready` tại `reports/failover-events.jsonl:4` | Giữ Region B warm hơn, preload model weights hoặc duy trì capacity dự phòng |
| DNS/LB TTL cache | khoảng `4.1s` | DNS cutover tại `+18.4s` ở `reports/failover-events.jsonl:5`; request B đầu tiên tại `+22.5s` ở `reports/drill-2-withdr.jsonl:37` | Giảm TTL/cache hoặc dùng cơ chế traffic switching có propagation nhanh hơn |

### Lưu ý về critical path

Trong run này, các thành phần không hoàn toàn nối tiếp nhau.

`dr/runbook.py` bắt đầu xác nhận outage và thực hiện restore trong khi external health checker vẫn đang chạy threshold detection. Vì vậy snapshot restore hoàn tất tại khoảng `+12.2s`, còn health checker ghi nhận Region A `UNHEALTHY` tại `+14.8s`.

Do có phần thực thi song song này, không thể cộng trực tiếp:

`14.8 + restore + 6.21 + 4.1`

để suy ra RTO mà không tính phần thời gian overlap.

RTO end-to-end chuẩn vẫn được lấy trực tiếp từ timestamp của load generator:

`1787653233.4186535 - 1787653210.8968222 ≈ 22.5s`

Evidence cuối cùng: `chaos/chaos-events.jsonl:5` và `reports/drill-2-withdr.jsonl:37`.

### Kết luận

- Drill 1: `NO_RECOVERY`
- Drill 2: Region B phục hồi thành công
- RTO: `22.5s < 300s` → **PASS**
- RPO: `28.0s < 300s` → **PASS**
- Documents lost tại snapshot restore: `14 docs`