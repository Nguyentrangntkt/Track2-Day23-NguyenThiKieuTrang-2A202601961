# Runbook 1 trang — Region chính down

Runbook này được viết để on-call có thể chạy lúc 3h sáng mà không cần hiểu sâu codebase. Mỗi bước đều có lệnh copy-paste và điều kiện xác nhận hoàn tất.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | Region A fail health liên tiếp / `UNHEALTHY` | on-call |
| 2 | Mở incident + chạy failover orchestration | `python3 dr/runbook.py --primary a --target b --backend fs --auto` | `reports/runbook-run.jsonl` được tạo và workflow bắt đầu chạy | on-call |
| 3 | Xác nhận snapshot restore | `grep '"step": "2_restore_snapshot"' reports/failover-events.jsonl` | `ok:true`, có `rpo_seconds` và `docs_lost` | on-call |
| 4 | Xác nhận Region B ready | `grep '"step": "4_wait_ready"' reports/failover-events.jsonl` | `ok:true`, `ready:true` | on-call |
| 5 | Xác nhận DNS/LB cutover | `grep '"step": "5_dns_cutover"' reports/failover-events.jsonl` | `ok:true`, `from:"a"`, `to:"b"` | on-call |
| 6 | Verify golden signals | `curl -s http://127.0.0.1:8080/edge/state && curl -s http://127.0.0.1:8002/readyz` | edge ở B, Region B ready, request trả 200 | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `rto_verdict:"PASS"` | incident commander |

## Kết quả drill gần nhất

- RTO đo được: `22.5s`
- RTO mục tiêu: `300s`
- RPO đo được: `28.0s`
- Documents lost: `14`
- Region phục hồi: `b`
- Verdict: `PASS`

## Golden signals cần xác nhận

Trong lab này, tiêu chí tối thiểu để kết luận Region B đủ an toàn để phục vụ traffic là:

- `/readyz` của Region B trả ready.
- Edge đã chuyển `active_region=b`.
- Request qua port `8080` trả HTTP `200`.
- Không có cảnh báo invalid từ `tools/measure_rto.py`.
- Error rate phải giảm về 0 sau recovery trong cửa sổ quan sát.
- Latency của các request sau recovery phải quay về mức bình thường của drill; trong run này các request từ B chủ yếu ở mức vài chục ms.

Không hard-code một ngưỡng p95 production từ lab này vì đề bài/log hiện tại không cung cấp SLO production cụ thể.

## Rollback / failback về Region A

Không tự động chuyển traffic ngược về Region A ngay khi A vừa sống lại, vì điều đó có thể gây flapping giữa hai region.

Chỉ failback khi tất cả điều kiện sau cùng đúng:

1. Region A đã được restore và `/readyz` trả healthy ổn định qua nhiều probe liên tiếp.
2. Model weights, vector DB và pool state của A đều hợp lệ.
3. State replication từ B/A đã được kiểm tra, không có nguy cơ mất dữ liệu mới hơn.
4. Golden signals của A ổn định.
5. Không còn incident đang diễn tiến.
6. Incident commander hoặc on-call lead phê duyệt failback.

Người quyết định failback: **incident commander / on-call lead**, không để script tự động đảo traffic qua lại.

Sau khi được phê duyệt, mới thực hiện cutover có kiểm soát và tiếp tục theo dõi health/error/latency.