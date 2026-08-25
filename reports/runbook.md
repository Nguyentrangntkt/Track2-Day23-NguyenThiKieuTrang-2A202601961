# Runbook 1 trang — Region chính down

Runbook này được viết để on-call có thể chạy lúc 3h sáng mà không cần hiểu sâu codebase. Mỗi bước đều có lệnh copy-paste và điều kiện xác nhận hoàn tất.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | Region A không còn ready / health probe fail liên tiếp; health checker ghi `region:a`, `to:UNHEALTHY` trong `reports/health-events.jsonl` | on-call |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs --auto` | `reports/runbook-run.jsonl` có bước `thong_bao_incident` và ghi `t_outage` | on-call |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | Snapshot restore thành công; `reports/failover-events.jsonl` có `step:2_restore_snapshot`, `ok:true`, kèm `rpo_seconds` và `docs_lost` | on-call / platform |
| 4 | Scale pool warm→full | `python3 dr/runbook.py --primary a --target b --backend fs --auto` | `reports/failover-events.jsonl` có `step:4_wait_ready`, `ok:true`; `/readyz` của Region B trả ready | platform |
| 5 | DNS/LB cutover | `python3 dr/runbook.py --primary a --target b --backend fs --auto` | `reports/failover-events.jsonl` có `step:5_dns_cutover`, `ok:true`, `from:a`, `to:b`; edge chuyển sang Region B | on-call / platform |
| 6 | Verify golden signals | `curl -s http://127.0.0.1:8080/edge/state && curl -s http://127.0.0.1:8002/readyz` | Edge active region là `b`, Region B ready, request qua edge trả 200; trong drill thực tế request phục hồi đầu tiên từ B tại `+22.5s` | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output có `valid:true`, `recovered_by_region:"b"`, `rto_verdict:"PASS"` và `rto_measured_s` khác null | on-call / incident commander |

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