"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
from contextlib import contextmanager
import json
import os
import pathlib
import sys
import time

import httpx

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = PROJECT_ROOT / "reports/failover-events.jsonl"


@contextmanager
def project_cwd():
    """Run legacy snapshot helpers against this checkout, not the caller's CWD.

    ``state.snapshot`` intentionally models its storage with relative paths.  A
    failover launched by an IDE, Task Scheduler, or another shell directory would
    otherwise restore a second ``state/region-*`` tree that the serving processes
    never read.
    """
    previous = pathlib.Path.cwd()
    os.chdir(PROJECT_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def emit(**kw):
    """Append one timestamped failover event and echo it to stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {"ts": now,
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)), **kw}
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str) -> dict:
    """Read the target's current state without treating liveness as readiness."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def _failure(target: str, failed_step: str, reason: str, **kw) -> dict:
    return {"ok": False, "target": target, "failed_step": failed_step,
            "reason": reason, **kw}


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, warm, verify, and then cut traffic over to ``target``."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if wait <= 0:
        raise ValueError("wait must be positive")

    primary = "b" if target == "a" else "a"
    target_dir = PROJECT_ROOT / "state" / f"region-{target}"
    pool_file = target_dir / "pool_state"
    active_file = PROJECT_ROOT / "edge" / "active_region"

    try:
        target_before = state_of(target)
        emit(step="1_verify_target", ok=True, target=target,
             pool_state=target_before.get("pool_state"),
             weights=target_before.get("weights"), count=target_before.get("count"))
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="1_verify_target", ok=False, target=target, reason=reason)
        return _failure(target, "1_verify_target", reason)

    try:
        # snapshot.get() uses relative filesystem paths by design. Anchor that call
        # to this checkout so the restored files are the same files serving.app reads
        # when started through scripts/up_bare.sh.
        with project_cwd():
            restore_meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(PROJECT_ROOT / "state" / f"region-{primary}" / "vectors.sqlite",
                           target_dir / "vectors.sqlite")
        restored_state = state_of(target)
        emit(step="2_restore_snapshot", ok=True, target=target, backend=backend,
             rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
             embed_model_version=restore_meta.get("embed_model_version"),
             snapshot_at=restore_meta.get("snapshot_at"),
             restored_at=restore_meta.get("restored_at"),
             weights=restored_state.get("weights"), count=restored_state.get("count"),
             state_dir=str(target_dir))
    except (Exception, SystemExit) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="2_restore_snapshot", ok=False, target=target, backend=backend,
             reason=reason)
        return _failure(target, "2_restore_snapshot", reason,
                        target_before=target_before)

    old_pool_state = (pool_file.read_text().strip() if pool_file.exists()
                      else target_before.get("pool_state", "cold"))
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full", encoding="utf-8")
        emit(step="3_scale_pool", ok=True, target=target,
             **{"from": old_pool_state, "to": "full"})
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="3_scale_pool", ok=False, target=target, reason=reason)
        return _failure(target, "3_scale_pool", reason,
                        target_before=target_before, restored_state=restored_state,
                        restore=restore_meta, rpo=rpo)

    started_wait = time.monotonic()
    deadline = started_wait + wait
    last_reason = "readiness timeout"
    ready_body = None
    while time.monotonic() < deadline:
        try:
            remaining = deadline - time.monotonic()
            response = httpx.get(f"{URL[target]}/readyz",
                                 timeout=min(2.0, max(0.05, remaining)))
            try:
                ready_body = response.json()
            except ValueError:
                ready_body = None
            if response.status_code == 200 and (
                    not isinstance(ready_body, dict) or ready_body.get("ready", True)):
                break
            if isinstance(ready_body, dict) and ready_body.get("reasons"):
                last_reason = ",".join(str(x) for x in ready_body["reasons"])
            else:
                last_reason = f"http_status={response.status_code}"
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    else:
        waited_s = round(time.monotonic() - started_wait, 2)
        # A failed warm-up must not leave the standby scaled differently just because
        # a drill/unit test timed out. State restore remains available for diagnosis.
        pool_file.write_text(old_pool_state, encoding="utf-8")
        emit(step="4_wait_ready", ok=False, target=target, waited_s=waited_s,
             reason=last_reason, cutover=False, pool_rolled_back_to=old_pool_state)
        return _failure(target, "4_wait_ready", last_reason,
                        target_before=target_before, restored_state=restored_state,
                        restore=restore_meta, rpo=rpo, cutover=False)

    waited_s = round(time.monotonic() - started_wait, 2)
    emit(step="4_wait_ready", ok=True, target=target, waited_s=waited_s,
         ready=ready_body)

    active_file.parent.mkdir(parents=True, exist_ok=True)
    active_before = active_file.read_text().strip() if active_file.exists() else None
    active_file.write_text(target, encoding="utf-8")
    cutover = {"ok": True, "from": active_before, "to": target}
    emit(step="5_dns_cutover", ok=True, target=target,
         **{"from": active_before, "to": target})
    return {"ok": True, "target": target, "backend": backend,
            "target_before": target_before, "restored_state": restored_state,
            "restore": restore_meta, "rpo": rpo, "ready": ready_body,
            "waited_s": waited_s, "cutover": cutover}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
