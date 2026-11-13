#!/usr/bin/env python3
"""交错补录演示（可暂停计算）。

场景（水表 MW-001，基线序列 00:00=100, 01:00=112, 02:00=125, 03:00=140）：
  补录 A：00:30 = 110   —— 单独看合法（100 ≤ 110 ≤ 112）
  补录 B：00:45 = 105   —— 单独看也合法（100 ≤ 105 ≤ 112）
  但两者同时存在时序列变为 …110(00:30) → 105(00:45)…，累计量倒退。

流程：
  1. 基线批次正常提交；
  2. 在 web1 上 Arm 暂停开关，随后发起补录 A（带 X-Pause-Before-Commit 头），
     请求在“准备完成、拿锁提交之前”被挂起——此时不持有任何数据库锁；
  3. 与此同时在 web2 上提交补录 B，立即成功（不同进程、同一水表仍可提交）；
  4. 放行 A：A 在咨询锁内重读权威完整序列并复核，检测到倒退，整批拒绝、全不改；
  5. 查询最终读数与区间，确认只有 B 生效、区间与读数状态一致。

仅依赖标准库。用法：
  python scripts/interleave_demo.py
  python scripts/interleave_demo.py --web1 http://localhost:8001 --web2 http://localhost:8002
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
METER = "MW-001"


def _request(method: str, url: str, data: bytes | None = None,
             headers: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def post_csv(base: str, csv_name: str, pause: bool = False) -> tuple[int, dict]:
    body = (SAMPLES / csv_name).read_bytes()
    headers = {"Content-Type": "text/csv"}
    if pause:
        headers["X-Pause-Before-Commit"] = METER
    return _request("POST", f"{base}/api/readings/batch", body, headers, timeout=120)


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def show(label: str, status: int, payload: dict) -> None:
    mark = "OK " if 200 <= status < 300 else ("REFUSE" if status == 409 else "ERR")
    print(f"[{mark}] {label} (HTTP {status})")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="交错补录并发演示")
    parser.add_argument("--web1", default="http://localhost:8001")
    parser.add_argument("--web2", default="http://localhost:8002")
    args = parser.parse_args()
    web1, web2 = args.web1.rstrip("/"), args.web2.rstrip("/")

    # 0. 健康检查 & 清理上一次演示遗留的暂停开关。
    for name, base in (("web1", web1), ("web2", web2)):
        try:
            status, info = _request("GET", f"{base}/healthz")
            print(f"health {name}: {info}")
        except Exception as exc:  # noqa: BLE001
            print(f"无法连接 {name} ({base}): {exc}", file=sys.stderr)
            return 2
    _request("POST", f"{web1}/api/admin/blocker/release")

    # 1. 基线批次。
    banner("步骤 1/5  提交基线批次（web1）")
    show("baseline", *post_csv(web1, "01_baseline.csv"))

    # 2. Arm 暂停 + 后台发起补录 A，令其停在提交前。
    banner("步骤 2/5  web1 暂停补录 A（00:30=110）于提交前，不持锁")
    status, payload = _request(
        "POST", f"{web1}/api/admin/blocker/arm?key={METER}", b"",
        {"Content-Type": "application/json"})
    print(f"arm blocker: {payload}")

    result_a: dict = {}
    def run_a() -> None:
        result_a["status"], result_a["body"] = post_csv(
            web1, "02_backfill_a_legal_alone.csv", pause=True)

    t_a = threading.Thread(target=run_a, name="backfill-A")
    t_a.start()
    time.sleep(1.5)  # 等 A 进入暂停点。
    status, payload = _request("GET", f"{web1}/api/admin/blocker")
    print(f"blocker 状态（应看到 waiting_keys 含 {METER}）: {payload}")

    # 3. 并发：web2 提交补录 B，立即成功。
    banner("步骤 3/5  web2 并发提交补录 B（00:45=105），应立即成功")
    show("backfill B (web2)", *post_csv(web2, "03_backfill_b_legal_alone.csv"))

    # 4. 放行 A，等待其在锁内复核后被拒绝。
    banner("步骤 4/5  放行 A：锁内权威复核发现 110 → 105 倒退，整批拒绝")
    status, payload = _request(
        "POST", f"{web1}/api/admin/blocker/release?key={METER}", b"",
        {"Content-Type": "application/json"})
    print(f"release blocker: {payload}")
    t_a.join(timeout=30)
    if t_a.is_alive():
        print("补录 A 未在预期时间内返回，演示失败", file=sys.stderr)
        return 1
    show("backfill A (web1, resumed)", result_a["status"], result_a["body"])
    assert result_a["status"] == 409, "A 应当因倒退被整批拒绝"
    conflicts = result_a["body"].get("conflicts", [])
    assert conflicts and conflicts[0]["meter_id"] == METER

    # 5. 最终状态：只有 B 生效；读数与区间在同一快照查询。
    banner("步骤 5/5  查询最终读数与区间（web2，同一事务快照）")
    status, final_state = _request("GET", f"{web2}/api/meters/{METER}")
    show(f"final state of {METER}", status, final_state)

    readings = {r["ts_utc"]: r["cumulative"] for r in final_state["readings"]}
    assert readings.get("2026-01-01T00:30:00Z") is None, "A 必须未生效"
    assert readings.get("2026-01-01T00:45:00Z") == 105, "B 应当生效"
    assert final_state["interval_count"] == final_state["reading_count"] - 1
    print("\n演示结论：两个“分别合法”的并发补录没有合成为倒退序列；")
    print("失败的 A 全不改，成功的 B 与区间重算原子生效，查询结果前后状态一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
