"""纯领域逻辑：CSV 解析、读数合并、倒退冲突检测、耗水区间生成。

本模块不接触数据库与 Web 框架，方便单元测试。
所有时间均为带时区的 UTC 时间（datetime, tzinfo=timezone.utc）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

MAX_ROWS_PER_BATCH = 1000
# PostgreSQL bigint 上限；题目要求非负整数升累计量。
MAX_VALUE = 9_223_372_036_854_775_807


class CsvError(ValueError):
    """CSV 结构 / 字段级错误，带行号（数据行号，从 1 开始；表头问题行号为 0）。"""

    def __init__(self, message: str, row: int | None = None, column: str | None = None):
        super().__init__(message)
        self.row = row
        self.column = column

    def to_detail(self) -> dict:
        d = {"message": str(self)}
        if self.row is not None:
            d["row"] = self.row
        if self.column is not None:
            d["column"] = self.column
        return d


@dataclass(frozen=True)
class Reading:
    meter_id: str
    ts_utc: datetime
    cumulative: int


@dataclass(frozen=True)
class Conflict:
    """相邻两条造成累计量倒退的读数（按时间排序后的相邻关系）。"""

    meter_id: str
    earlier: Reading
    later: Reading

    def to_dict(self) -> dict:
        return {
            "meter_id": self.meter_id,
            "earlier": _reading_dict(self.earlier),
            "later": _reading_dict(self.later),
            "expected": f"later cumulative >= earlier cumulative",
        }


@dataclass(frozen=True)
class Interval:
    meter_id: str
    start_ts: datetime
    end_ts: datetime
    start_cumulative: int
    end_cumulative: int
    delta: int


def _reading_dict(r: Reading) -> dict:
    return {
        "meter_id": r.meter_id,
        "ts_utc": r.ts_utc.isoformat().replace("+00:00", "Z"),
        "cumulative": r.cumulative,
    }


def reading_dict(r: Reading) -> dict:
    return _reading_dict(r)


def interval_dict(i: Interval) -> dict:
    return {
        "meter_id": i.meter_id,
        "start_ts_utc": i.start_ts.isoformat().replace("+00:00", "Z"),
        "end_ts_utc": i.end_ts.isoformat().replace("+00:00", "Z"),
        "start_cumulative": i.start_cumulative,
        "end_cumulative": i.end_cumulative,
        "delta": i.delta,
    }


def parse_utc(text: str) -> datetime:
    """解析 ISO-8601 UTC 时间；非 UTC（带其他偏移）拒绝，避免隐式换算歧义。"""
    text = text.strip()
    if not text:
        raise ValueError("empty timestamp")
    candidate = text
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {text!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware UTC: {text!r}")
    offset = dt.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"timestamp must be UTC (offset +00:00 / Z): {text!r}")
    return dt.astimezone(timezone.utc)


def parse_cumulative(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("empty cumulative")
    # 拒绝 1.0 / 1e3 / +5 / 00 之外的奇怪写法；只接受十进制非负整数字面量。
    if not text.isdigit():
        raise ValueError(f"cumulative must be a non-negative integer: {text!r}")
    value = int(text)
    if value > MAX_VALUE:
        raise ValueError(f"cumulative exceeds int64 max ({MAX_VALUE}): {value}")
    return value


def parse_csv(content: bytes | str) -> list[Reading]:
    """解析上传 CSV。

    表头固定三列：meter_id, ts_utc, cumulative（顺序固定，兼容 BOM、表头空白）。
    同表同刻在同一文件内出现多次时，以后出现的一行为准（同一读数的更正）。
    行数（数据行）不得超过 MAX_ROWS_PER_BATCH，空文件拒绝。
    """
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CsvError(f"file is not valid UTF-8: {exc}") from exc
    else:
        text = content.lstrip("﻿")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CsvError("empty file: header row required", row=0) from exc

    header = [h.strip().lower() for h in header]
    expected = ["meter_id", "ts_utc", "cumulative"]
    if header != expected:
        raise CsvError(
            f"header must be exactly {expected}, got {header}", row=0, column="header"
        )

    readings: list[Reading] = []
    seen: dict[tuple[str, datetime], int] = {}
    row_no = 0
    for raw in reader:
        row_no += 1
        if row_no > MAX_ROWS_PER_BATCH:
            raise CsvError(
                f"batch exceeds {MAX_ROWS_PER_BATCH} data rows",
                row=row_no,
            )
        if not raw or all(not c.strip() for c in raw):
            # 跳过完全空白的行，但计入行数上限更安全——这里选择直接报错，数据文件不该含空行。
            raise CsvError("blank row", row=row_no)
        if len(raw) != 3:
            raise CsvError(
                f"expected 3 columns, got {len(raw)}", row=row_no, column="row"
            )
        meter_id = raw[0].strip()
        if not meter_id:
            raise CsvError("meter_id must not be empty", row=row_no, column="meter_id")
        if len(meter_id) > 128:
            raise CsvError("meter_id longer than 128 chars", row=row_no, column="meter_id")
        try:
            ts = parse_utc(raw[1])
        except ValueError as exc:
            raise CsvError(str(exc), row=row_no, column="ts_utc") from exc
        try:
            cumulative = parse_cumulative(raw[2])
        except ValueError as exc:
            raise CsvError(str(exc), row=row_no, column="cumulative") from exc

        reading = Reading(meter_id=meter_id, ts_utc=ts, cumulative=cumulative)
        key = (meter_id, ts)
        if key in seen:
            # 同表同刻：同一读数，后写覆盖先写（更正语义）。
            readings[seen[key]] = reading
        else:
            seen[key] = len(readings)
            readings.append(reading)

    if row_no == 0:
        raise CsvError("no data rows", row=0)
    return readings


def group_by_meter(readings: list[Reading]) -> dict[str, list[Reading]]:
    grouped: dict[str, list[Reading]] = {}
    for r in readings:
        grouped.setdefault(r.meter_id, []).append(r)
    return grouped


def merge_readings(
    existing: list[Reading], incoming: list[Reading]
) -> list[Reading]:
    """把本批读数合并进某水表的完整序列。

    同表同刻以 incoming 为准（补录/更正），结果按时间升序排列。
    existing 假定来自数据库、已按 ts_utc 升序；incoming 顺序任意。
    """
    merged: dict[datetime, Reading] = {r.ts_utc: r for r in existing}
    for r in incoming:
        merged[r.ts_utc] = r
    return [merged[k] for k in sorted(merged.keys())]


def find_conflicts(sequence: list[Reading], meter_id: str) -> list[Conflict]:
    """在最终完整序列上找累计量随时间倒退的相邻读数对。"""
    conflicts: list[Conflict] = []
    for prev, cur in zip(sequence, sequence[1:]):
        if cur.cumulative < prev.cumulative:
            conflicts.append(Conflict(meter_id=meter_id, earlier=prev, later=cur))
    return conflicts


def build_intervals(sequence: list[Reading], meter_id: str) -> list[Interval]:
    """由完整有序读数序列生成相邻耗水区间；首条读数无区间。"""
    intervals: list[Interval] = []
    for prev, cur in zip(sequence, sequence[1:]):
        intervals.append(
            Interval(
                meter_id=meter_id,
                start_ts=prev.ts_utc,
                end_ts=cur.ts_utc,
                start_cumulative=prev.cumulative,
                end_cumulative=cur.cumulative,
                delta=cur.cumulative - prev.cumulative,
            )
        )
    return intervals


def summarize_batch(readings: list[Reading]) -> dict:
    meters = sorted({r.meter_id for r in readings})
    return {
        "rows": len(readings),
        "meters": meters,
        "meter_count": len(meters),
    }


# 仅供测试/调试：Decimal 不参与主流程，保留给未来精度扩展。
def _decimal_sanity(v: int) -> Decimal:  # pragma: no cover
    return Decimal(v)
