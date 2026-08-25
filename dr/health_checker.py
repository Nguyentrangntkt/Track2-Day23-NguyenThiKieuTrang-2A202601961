"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Return whether a region can serve traffic and a diagnostic reason."""
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        try:
            body = response.json()
        except ValueError:
            body = {}

        if response.status_code == 200 and body.get("ready", True):
            return True, "ready"

        reasons = body.get("reasons")
        if isinstance(reasons, list) and reasons:
            reason = ",".join(str(item) for item in reasons)
        else:
            reason = f"http_status={response.status_code}"
        return False, reason
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll both regions and append only confirmed state transitions to JSONL."""
    if interval <= 0 or timeout <= 0 or threshold <= 0 or duration < 0:
        raise ValueError("interval/timeout/threshold must be positive and duration non-negative")

    out.parent.mkdir(parents=True, exist_ok=True)
    state = {region: "HEALTHY" for region in URL}
    consecutive_fails = {region: 0 for region in URL}
    deadline = time.monotonic() + duration

    with out.open("a", encoding="utf-8") as log:
        while time.monotonic() < deadline:
            cycle_started = time.monotonic()
            for region in URL:
                ready, reason = probe(region, timeout)
                if ready:
                    consecutive_fails[region] = 0
                    if state[region] == "UNHEALTHY":
                        state[region] = "HEALTHY"
                        record = {
                            "event": "state_change",
                            "ts": time.time(),
                            "region": region,
                            "to": "HEALTHY",
                            "reason": reason,
                            "interval_s": interval,
                            "threshold": threshold,
                            "consecutive_fails": 0,
                        }
                        log.write(json.dumps(record, ensure_ascii=False) + "\n")
                        log.flush()
                        print("HEALTH", json.dumps(record, ensure_ascii=False))
                    continue

                consecutive_fails[region] += 1
                if (state[region] == "HEALTHY"
                        and consecutive_fails[region] == threshold):
                    state[region] = "UNHEALTHY"
                    record = {
                        "event": "state_change",
                        "ts": time.time(),
                        "region": region,
                        "to": "UNHEALTHY",
                        "reason": reason,
                        "interval_s": interval,
                        "threshold": threshold,
                        "consecutive_fails": consecutive_fails[region],
                    }
                    log.write(json.dumps(record, ensure_ascii=False) + "\n")
                    log.flush()
                    print("HEALTH", json.dumps(record, ensure_ascii=False))

            remaining = deadline - time.monotonic()
            sleep_for = min(max(0.0, interval - (time.monotonic() - cycle_started)),
                            max(0.0, remaining))
            if sleep_for:
                time.sleep(sleep_for)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
