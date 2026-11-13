"""领域逻辑单元测试（不依赖数据库/第三方库）：python -m unittest app.tests.test_logic -v"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app import logic
from app.logic import CsvError, Reading


def ts(text: str) -> datetime:
    return logic.parse_utc(text)


def r(meter: str, t: str, v: int) -> Reading:
    return Reading(meter, ts(t), v)


class ParseCsvTests(unittest.TestCase):
    def test_basic_parse_and_bom(self):
        csv_data = (
            "meter_id,ts_utc,cumulative\n"
            "MW-1,2026-01-01T00:00:00Z,10\n"
            "MW-1,2026-01-01T01:00:00Z,12\n"
        )
        rows = logic.parse_csv("﻿" + csv_data)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].cumulative, 12)
        self.assertEqual(rows[0].ts_utc.tzinfo, timezone.utc)

    def test_duplicate_same_meter_same_ts_last_wins(self):
        csv_data = (
            "meter_id,ts_utc,cumulative\n"
            "MW-1,2026-01-01T00:00:00Z,10\n"
            "MW-1,2026-01-01T00:00:00Z,42\n"
        )
        rows = logic.parse_csv(csv_data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].cumulative, 42)

    def test_non_utc_offset_rejected(self):
        csv_data = (
            "meter_id,ts_utc,cumulative\n"
            "MW-1,2026-01-01T08:00:00+08:00,10\n"
        )
        with self.assertRaises(CsvError) as ctx:
            logic.parse_csv(csv_data)
        self.assertEqual(ctx.exception.row, 1)
        self.assertEqual(ctx.exception.column, "ts_utc")

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(CsvError):
            logic.parse_csv(
                "meter_id,ts_utc,cumulative\nMW-1,2026-01-01T00:00:00,10\n"
            )

    def test_negative_and_float_rejected(self):
        for bad in ("-1", "1.0", "1e3", "+5", "", "  "):
            with self.subTest(bad=bad):
                with self.assertRaises(CsvError):
                    logic.parse_csv(
                        f"meter_id,ts_utc,cumulative\nMW-1,2026-01-01T00:00:00Z,{bad}\n"
                    )

    def test_bad_header_and_empty_file(self):
        with self.assertRaises(CsvError):
            logic.parse_csv("a,b,c\n")
        with self.assertRaises(CsvError):
            logic.parse_csv("")

    def test_row_limit(self):
        header = "meter_id,ts_utc,cumulative\n"
        body = "".join(
            f"MW-1,2026-01-01T{i // 60:02d}:{i % 60:02d}:00Z,{i}\n"
            for i in range(1001)
        )
        with self.assertRaises(CsvError) as ctx:
            logic.parse_csv(header + body)
        self.assertIn("1000", str(ctx.exception))

    def test_wrong_column_count(self):
        with self.assertRaises(CsvError):
            logic.parse_csv(
                "meter_id,ts_utc,cumulative\nMW-1,2026-01-01T00:00:00Z\n"
            )


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.base = [
            r("MW-1", "2026-01-01T00:00:00Z", 100),
            r("MW-1", "2026-01-01T01:00:00Z", 112),
            r("MW-1", "2026-01-01T02:00:00Z", 125),
        ]

    def test_merge_backfill_and_correction(self):
        incoming = [
            r("MW-1", "2026-01-01T00:30:00Z", 105),  # 补录
            r("MW-1", "2026-01-01T02:00:00Z", 130),  # 同刻更正
        ]
        merged = logic.merge_readings(self.base, incoming)
        self.assertEqual([x.ts_utc.minute for x in merged], [0, 30, 0, 0])
        self.assertEqual(merged[-1].cumulative, 130)
        self.assertEqual(len(merged), 4)

    def test_find_conflicts_on_final_sequence(self):
        # 补录点单独合法，合并后出现相邻倒退。
        incoming = [
            r("MW-1", "2026-01-01T00:30:00Z", 110),
            r("MW-1", "2026-01-01T00:45:00Z", 105),
        ]
        merged = logic.merge_readings(self.base, incoming)
        conflicts = logic.find_conflicts(merged, "MW-1")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].earlier.cumulative, 110)
        self.assertEqual(conflicts[0].later.cumulative, 105)

    def test_equal_cumulative_is_not_regression(self):
        incoming = [r("MW-1", "2026-01-01T00:30:00Z", 100)]
        merged = logic.merge_readings(self.base, incoming)
        self.assertEqual(logic.find_conflicts(merged, "MW-1"), [])

    def test_build_intervals_first_reading_has_no_interval(self):
        intervals = logic.build_intervals(self.base, "MW-1")
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0].delta, 12)
        self.assertEqual(intervals[1].delta, 13)
        self.assertEqual(intervals[0].start_ts, self.base[0].ts_utc)

    def test_intervals_rebuilt_after_backfill(self):
        incoming = [r("MW-1", "2026-01-01T00:30:00Z", 105)]
        merged = logic.merge_readings(self.base, incoming)
        intervals = logic.build_intervals(merged, "MW-1")
        self.assertEqual([i.delta for i in intervals], [5, 7, 13])

    def test_single_reading_no_interval(self):
        seq = [r("MW-9", "2026-01-01T00:00:00Z", 7)]
        self.assertEqual(logic.build_intervals(seq, "MW-9"), [])
        self.assertEqual(logic.find_conflicts(seq, "MW-9"), [])

    def test_batch_regression_within_one_file(self):
        rows = logic.parse_csv(
            "meter_id,ts_utc,cumulative\n"
            "MW-3,2026-01-01T00:00:00Z,10\n"
            "MW-3,2026-01-01T01:00:00Z,9\n"
        )
        seq = logic.merge_readings([], rows)
        self.assertEqual(len(logic.find_conflicts(seq, "MW-3")), 1)


if __name__ == "__main__":
    unittest.main()
