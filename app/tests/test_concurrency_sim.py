"""并发提交算法的内存模拟测试。

没有 PostgreSQL 环境时，用同一套算法（每表一把锁 -> 锁内重读权威序列 ->
合并 -> 最终序列倒退检测 -> 原子替换）模拟 db.commit_batch 的并发行为，
验证“分别合法的并发补录不能合成为倒退、失败全不改”。
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime

from app import logic
from app.blocker import PauseBlocker
from app.logic import Reading


def ts(text: str) -> datetime:
    return logic.parse_utc(text)


class InMemoryMeterStore:
    """复刻数据库提交语义：按 meter_id 排序取锁、锁内复核、整批原子。"""

    def __init__(self):
        self._data: dict[str, dict[datetime, int]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    def _lock_for(self, meter_id: str) -> threading.Lock:
        with self._meta_lock:
            if meter_id not in self._locks:
                self._locks[meter_id] = threading.Lock()
            return self._locks[meter_id]

    def snapshot(self, meter_id: str) -> list[Reading]:
        table = self._data.get(meter_id, {})
        return [
            Reading(meter_id, t, v) for t, v in sorted(table.items())
        ]

    def commit(self, readings: list[Reading]) -> dict:
        grouped = logic.group_by_meter(readings)
        locks = [self._lock_for(m) for m in sorted(grouped)]
        for lock in locks:
            lock.acquire()
        try:
            # 锁内复核：基于提交那一刻的权威状态构造最终序列。
            finals: dict[str, list[Reading]] = {}
            for meter_id, incoming in grouped.items():
                sequence = logic.merge_readings(self.snapshot(meter_id), incoming)
                if logic.find_conflicts(sequence, meter_id):
                    return {"status": "rejected", "meter": meter_id}
                finals[meter_id] = sequence
            # 全部水表通过后才一次性落库（模拟单事务原子提交）。
            for meter_id, incoming in grouped.items():
                self._data.setdefault(meter_id, {})
                for r in incoming:
                    self._data[meter_id][r.ts_utc] = r.cumulative
            return {"status": "committed", "finals": finals}
        finally:
            for lock in reversed(locks):
                lock.release()


class InterleaveSimulationTests(unittest.TestCase):
    def test_separately_legal_backfits_cannot_compose_regression(self):
        store = InMemoryMeterStore()
        blocker = PauseBlocker(enabled=True, timeout_seconds=10)
        meter = "MW-001"

        baseline = [
            Reading(meter, ts("2026-01-01T00:00:00Z"), 100),
            Reading(meter, ts("2026-01-01T01:00:00Z"), 112),
            Reading(meter, ts("2026-01-01T02:00:00Z"), 125),
        ]
        self.assertEqual(store.commit(baseline)["status"], "committed")

        backfill_a = [Reading(meter, ts("2026-01-01T00:30:00Z"), 110)]
        backfill_b = [Reading(meter, ts("2026-01-01T00:45:00Z"), 105)]

        # 两个补录单独提交都合法。
        for one_off in (backfill_a, backfill_b):
            tmp = InMemoryMeterStore()
            tmp.commit(baseline)
            self.assertEqual(tmp.commit(one_off)["status"], "committed")

        results: dict[str, dict] = {}
        blocker.arm(meter)

        def submit_a():
            blocker.pause_if_armed(meter)  # 模拟提交前暂停窗口
            results["A"] = store.commit(backfill_a)

        t_a = threading.Thread(target=submit_a)
        t_a.start()
        # 等 A 进入暂停；此时 B 可以先提交。
        self.assertTrue(_wait_until(lambda: meter in blocker.status()["waiting_keys"]))
        results["B"] = store.commit(backfill_b)
        self.assertEqual(results["B"]["status"], "committed")

        blocker.release(meter)
        t_a.join(timeout=10)
        self.assertFalse(t_a.is_alive())

        # A 在锁内复核时必须看到 B，发现 110 -> 105 倒退而整批拒绝。
        self.assertEqual(results["A"]["status"], "rejected")
        final = store.snapshot(meter)
        values = {r.ts_utc: r.cumulative for r in final}
        self.assertNotIn(ts("2026-01-01T00:30:00Z"), values)  # A 全不改
        self.assertEqual(values[ts("2026-01-01T00:45:00Z")], 105)  # B 生效
        # 最终序列单调不降，区间差值全部非负。
        intervals = logic.build_intervals(final, meter)
        self.assertTrue(all(i.delta >= 0 for i in intervals))
        self.assertEqual(len(intervals), len(final) - 1)

    def test_concurrent_corrections_last_commit_wins_but_never_regresses(self):
        store = InMemoryMeterStore()
        meter = "MW-002"
        store.commit([
            Reading(meter, ts("2026-01-01T00:00:00Z"), 10),
            Reading(meter, ts("2026-01-01T01:00:00Z"), 20),
            Reading(meter, ts("2026-01-01T02:00:00Z"), 30),
        ])
        # 同刻更正 01:00：改为 25（合法）并发改为 8（小于前一条 10，倒退）。
        ok_result, bad_result = [], []
        t1 = threading.Thread(
            target=lambda: ok_result.append(
                store.commit([Reading(meter, ts("2026-01-01T01:00:00Z"), 25)])))
        t2 = threading.Thread(
            target=lambda: bad_result.append(
                store.commit([Reading(meter, ts("2026-01-01T01:00:00Z"), 8)])))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(ok_result[0]["status"], "committed")
        self.assertEqual(bad_result[0]["status"], "rejected")
        self.assertEqual(
            store.snapshot(meter)[1].cumulative, 25
        )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        _time.sleep(0.02)
    return False


if __name__ == "__main__":
    unittest.main()
