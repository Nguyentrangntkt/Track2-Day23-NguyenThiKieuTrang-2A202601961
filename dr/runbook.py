"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import math
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook step and echo it to stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {"ts": now,
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
              "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(record, ensure_ascii=False))
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Return immediately in CI, otherwise require an explicit ``y``."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def _latest_outage(primary: str) -> dict | None:
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            latest = event
    return latest


def _golden_signals(target: str) -> dict:
    results = []
    with httpx.Client(timeout=3.0) as client:
        for request_no in range(10):
            started = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer",
                                      params={"q": f"golden signal {request_no}"})
                body = response.json()
                results.append({
                    "request": request_no,
                    "ok": response.status_code == 200 and body.get("region") == target,
                    "status": response.status_code,
                    "served_by": body.get("region"),
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                })
            except Exception as exc:
                results.append({
                    "request": request_no, "ok": False, "status": None,
                    "served_by": None,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                    "error": type(exc).__name__,
                })

    latencies = sorted(item["latency_ms"] for item in results)
    p95_index = max(0, math.ceil(0.95 * len(latencies)) - 1)
    failures = sum(not item["ok"] for item in results)
    return {"requests": len(results), "failures": failures,
            "error_rate": round(failures / len(results), 4),
            "p95_latency_ms": latencies[p95_index], "results": results}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the seven-step, semi-automatic primary-region outage runbook."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different known regions")

    run_started = time.time()
    attempts = []
    consecutive_primary_fails = 0
    # Match the health checker's graded 5s/3-probe anti-flap policy. Probes are
    # start-to-start so their HTTP timeout does not silently add another interval.
    for attempt in range(1, 4):
        attempt_started = time.monotonic()
        primary_ready, primary_reason = hc.probe(primary, timeout=2.0)
        target_ready, target_reason = hc.probe(target, timeout=2.0)
        consecutive_primary_fails = (0 if primary_ready
                                     else consecutive_primary_fails + 1)
        attempts.append({
            "attempt": attempt,
            "primary_ready": primary_ready, "primary_reason": primary_reason,
            "target_ready": target_ready, "target_reason": target_reason,
            "consecutive_primary_fails": consecutive_primary_fails,
        })
        if attempt < 3:
            time.sleep(max(0.0, 5.0 - (time.monotonic() - attempt_started)))

    outage_confirmed = consecutive_primary_fails >= 3
    step(1, "xac_nhan_outage", ok=outage_confirmed, primary=primary,
         target=target, attempts=attempts,
         consecutive_primary_fails=consecutive_primary_fails)
    if not outage_confirmed:
        return {"ok": False, "aborted": True,
                "reason": f"region-{primary} outage not confirmed",
                "primary": primary, "target": target}

    outage = _latest_outage(primary)
    incident = step(
        2, "thong_bao_incident", ok=True, primary=primary, target=target,
        t_outage=None if outage is None else outage.get("ts"),
        t_outage_iso=None if outage is None else outage.get("iso"),
        notification_delay_s=(None if outage is None else
                              round(time.time() - outage["ts"], 2)),
    )
    if not confirm(auto, f"Fail over region-{primary} to region-{target}?"):
        step(3, "scale_gpu_pool", ok=False, skipped=True,
             reason="operator declined failover")
        return {"ok": False, "aborted": True,
                "reason": "operator declined failover",
                "primary": primary, "target": target}

    # This is deliberately the only call to fo.failover in the runbook.
    failover_result = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=bool(failover_result.get("ok")),
         failover_ok=bool(failover_result.get("ok")),
         failed_step=failover_result.get("failed_step"),
         reason=failover_result.get("reason"))

    restored_state = failover_result.get("restored_state") or {}
    state_ok = bool(restored_state.get("weights") and restored_state.get("count", 0) > 0)
    step(4, "verify_state_replica", ok=state_ok,
         weights=restored_state.get("weights"),
         vector_count=restored_state.get("count"),
         rpo_seconds=(failover_result.get("rpo") or {}).get("rpo_seconds"),
         docs_lost=(failover_result.get("rpo") or {}).get("docs_lost"))

    cutover = failover_result.get("cutover") or {"ok": False}
    step(5, "dns_cutover", ok=bool(cutover.get("ok")),
         target=target, cutover=cutover,
         reason=failover_result.get("reason"))

    if failover_result.get("ok") and cutover.get("ok"):
        golden = _golden_signals(target)
        step(6, "verify_golden_signals", ok=golden["failures"] == 0, **golden)
    else:
        golden = None
        step(6, "verify_golden_signals", ok=False, skipped=True,
             reason="failover did not complete; refusing misleading verification")

    elapsed_s = round(time.time() - incident["ts"], 2)
    measurement_command = (
        "python3 tools/measure_rto.py --loadgen "
        "reports/drill-2-withdr.jsonl --target-rto 300"
    )
    overall_ok = bool(failover_result.get("ok") and state_ok and golden
                      and golden["failures"] == 0)
    step(7, "post_incident", ok=overall_ok, elapsed_s=elapsed_s,
         measurement_command=measurement_command,
         run_elapsed_s=round(time.time() - run_started, 2))
    return {"ok": overall_ok, "primary": primary, "target": target,
            "incident_ts": incident["ts"], "elapsed_s": elapsed_s,
            "failover": failover_result, "golden_signals": golden,
            "measurement_command": measurement_command}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
