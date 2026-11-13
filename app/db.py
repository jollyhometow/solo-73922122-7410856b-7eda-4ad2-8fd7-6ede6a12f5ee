"""PostgreSQL 访问层：连接池、建表、事务性提交与一致性查询。

并发模型（两台 web 进程共用同一个数据库）：
- 准备阶段（读历史、合并序列、校验）不持有任何锁，其他上传可以同时进行；
- 提交阶段按 meter_id 全局排序后，逐表获取事务级咨询锁
  (pg_advisory_xact_lock)，锁内重读权威序列并再次校验；
- 咨询锁按同一全局顺序申请，避免死锁；
- 读数 upsert + 受影响区间整体重算在同一事务内完成，原子提交；
- 任何一个水表倒退则整批回滚，“失败全不改”。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import logic
from .logic import Conflict, Interval, Reading

# 咨询锁命名空间（key 的高 32 位）。meter_id 哈希占低 32 位。
ADVISORY_LOCK_NAMESPACE = 0x57415445  # 'WATE'

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meter_reading (
    meter_id    TEXT        NOT NULL,
    ts_utc      TIMESTAMPTZ NOT NULL,
    cumulative  BIGINT      NOT NULL CHECK (cumulative >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT meter_reading_pkey PRIMARY KEY (meter_id, ts_utc)
);

CREATE TABLE IF NOT EXISTS meter_interval (
    meter_id          TEXT        NOT NULL,
    start_ts          TIMESTAMPTZ NOT NULL,
    end_ts            TIMESTAMPTZ NOT NULL,
    start_cumulative  BIGINT      NOT NULL,
    end_cumulative    BIGINT      NOT NULL,
    delta             BIGINT      NOT NULL CHECK (delta >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT meter_interval_pkey PRIMARY KEY (meter_id, start_ts, end_ts),
    CONSTRAINT meter_interval_time_chk CHECK (end_ts > start_ts),
    CONSTRAINT meter_interval_value_chk
        CHECK (end_cumulative >= start_cumulative AND delta = end_cumulative - start_cumulative)
);
CREATE INDEX IF NOT EXISTS meter_interval_meter_end_idx
    ON meter_interval (meter_id, end_ts);
"""

_pool: ConnectionPool | None = None


def init_pool(database_url: str, min_size: int, max_size: int) -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=database_url,
            min_size=min_size,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "autocommit": False},
            check=ConnectionPool.check_connection,
            open=True,
        )
    return _pool


def get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool not initialized")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_schema() -> None:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


def _row_to_reading(row: dict) -> Reading:
    ts = row["ts_utc"]
    if ts.tzinfo is None:  # psycopg 默认返回带时区的 datetime，防御一下。
        from datetime import timezone

        ts = ts.replace(tzinfo=timezone.utc)
    return Reading(
        meter_id=row["meter_id"],
        ts_utc=ts,
        cumulative=row["cumulative"],
    )


def fetch_readings(conn: psycopg.Connection, meter_id: str) -> list[Reading]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT meter_id, ts_utc, cumulative
              FROM meter_reading
             WHERE meter_id = %s
             ORDER BY ts_utc ASC
            """,
            (meter_id,),
        )
        rows = cur.fetchall()
    return [_row_to_reading(r) for r in rows]


def acquire_meter_lock(conn: psycopg.Connection, meter_id: str) -> None:
    """事务级咨询锁：同一水表的提交串行化；不同水表互不阻塞。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (ADVISORY_LOCK_NAMESPACE, hash32(meter_id)),
        )


def hash32(text: str) -> int:
    """稳定的 32 位非负哈希（Python hash 带随机种子，不能用于锁 key）。"""
    import zlib

    return zlib.crc32(text.encode("utf-8"))


@contextmanager
def repeatable_read_transaction() -> Iterator[psycopg.Connection]:
    """只读一致快照：一次查询里读数与区间要么都是旧状态、要么都是新状态。

    psycopg3 在连接上设置 isolation_level 后，下一条语句开启的事务即采用
    REPEATABLE READ；退出时 rollback 结束只读事务并复位隔离级别。
    """
    with get_pool().connection() as conn:
        previous = conn.isolation_level
        conn.isolation_level = "REPEATABLE READ"
        try:
            yield conn
        finally:
            conn.rollback()
            conn.isolation_level = previous


class RejectedBatch(Exception):
    """整批拒绝：携带每个水表的相邻冲突读数。"""

    def __init__(self, conflicts: list[Conflict]):
        super().__init__("cumulative regression detected; whole batch rejected")
        self.conflicts = conflicts


def commit_batch(readings: list[Reading]) -> dict:
    """原子提交一批读数并替换受影响区间。

    调用方需保证 readings 已通过 CSV 结构校验。
    步骤：
      1. 按 meter_id 排序，依次拿咨询锁（全局统一顺序，防死锁）；
      2. 锁内重读该表完整序列，与本批合并后按最终序列检测倒退；
      3. 任一水表冲突 -> 抛 RejectedBatch，事务随连接归还回滚；
      4. upsert 读数、删除并重建该表全部区间（区间由读数派生，重算即“替换旧区间”）。
    """
    grouped = logic.group_by_meter(readings)
    meter_ids = sorted(grouped.keys())
    conflicts: list[Conflict] = []
    affected: dict[str, dict] = {}

    pool = get_pool()
    # pool.connection() 退出时：正常 -> commit，异常 -> rollback。
    with pool.connection() as conn:
        for meter_id in meter_ids:
            acquire_meter_lock(conn, meter_id)

        # 锁全部到手后再读，保证读到的是“提交那一刻”的权威状态。
        final_sequences: dict[str, list[Reading]] = {}
        for meter_id in meter_ids:
            existing = fetch_readings(conn, meter_id)
            sequence = logic.merge_readings(existing, grouped[meter_id])
            final_sequences[meter_id] = sequence
            conflicts.extend(logic.find_conflicts(sequence, meter_id))

        if conflicts:
            # 显式回滚后抛错；连接归还时重复 rollback 也无害。
            conn.rollback()
            raise RejectedBatch(conflicts)

        for meter_id in meter_ids:
            incoming = grouped[meter_id]
            _upsert_readings(conn, meter_id, incoming)
            _rebuild_intervals(conn, meter_id)
            affected[meter_id] = {
                "readings_total": len(final_sequences[meter_id]),
                "readings_upserted": len(incoming),
                "intervals_total": max(len(final_sequences[meter_id]) - 1, 0),
            }

    return {
        "status": "committed",
        "meters": affected,
        "rows": len(readings),
    }


def _upsert_readings(
    conn: psycopg.Connection, meter_id: str, incoming: list[Reading]
) -> None:
    rows = [(r.meter_id, r.ts_utc, r.cumulative) for r in incoming]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO meter_reading (meter_id, ts_utc, cumulative)
            VALUES (%s, %s, %s)
            ON CONFLICT (meter_id, ts_utc) DO UPDATE
               SET cumulative = EXCLUDED.cumulative,
                   updated_at = now()
            """,
            rows,
        )


def _rebuild_intervals(conn: psycopg.Connection, meter_id: str) -> None:
    """删除该水表旧区间并按最终序列整体重算。

    只有受影响（本批涉及的）水表会进入这里；其他水表的区间原样保留。
    读数已在同一事务内 upsert 完成，故窗口函数读到的就是最终完整序列。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM meter_interval WHERE meter_id = %s", (meter_id,))
        # 子查询先算窗口，外层过滤末行 lead 为 NULL 的记录，避免违反 NOT NULL。
        cur.execute(
            """
            INSERT INTO meter_interval
                (meter_id, start_ts, end_ts, start_cumulative, end_cumulative, delta)
            SELECT meter_id, start_ts, end_ts,
                   start_cumulative, end_cumulative, delta
              FROM (
                    SELECT meter_id,
                           ts_utc                              AS start_ts,
                           lead(ts_utc) OVER w                 AS end_ts,
                           cumulative                          AS start_cumulative,
                           lead(cumulative) OVER w             AS end_cumulative,
                           lead(cumulative) OVER w - cumulative AS delta
                      FROM meter_reading
                     WHERE meter_id = %s
                    WINDOW w AS (PARTITION BY meter_id ORDER BY ts_utc ASC)
                   ) led
             WHERE end_ts IS NOT NULL
            """,
            (meter_id,),
        )


def query_meter(meter_id: str, limit: int | None = None) -> dict:
    """同一 REPEATABLE READ 快照里返回读数与区间，杜绝混用前后状态。"""
    with repeatable_read_transaction() as conn:
        readings = fetch_readings(conn, meter_id)
        with conn.cursor() as cur:
            sql = (
                """
                SELECT meter_id, start_ts, end_ts,
                       start_cumulative, end_cumulative, delta
                  FROM meter_interval
                 WHERE meter_id = %s
                 ORDER BY start_ts ASC
                """
            )
            params: tuple = (meter_id,)
            if limit is not None:
                sql += " LIMIT %s"
                params = (meter_id, limit)
            cur.execute(sql, params)
            interval_rows = cur.fetchall()

    intervals = [
        Interval(
            meter_id=row["meter_id"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            start_cumulative=row["start_cumulative"],
            end_cumulative=row["end_cumulative"],
            delta=row["delta"],
        )
        for row in interval_rows
    ]
    shown_readings = readings if limit is None else readings[-limit:]
    return {
        "meter_id": meter_id,
        "reading_count": len(readings),
        "interval_count": len(intervals),
        "readings": [logic.reading_dict(r) for r in shown_readings],
        "intervals": [logic.interval_dict(i) for i in intervals],
    }


def list_meters(limit: int = 200) -> list[dict]:
    with repeatable_read_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.meter_id,
                       count(*)                       AS reading_count,
                       min(r.ts_utc)                  AS first_ts,
                       max(r.ts_utc)                  AS last_ts,
                       coalesce(max(v.cnt), 0)        AS interval_count
                  FROM meter_reading r
                  LEFT JOIN (
                      SELECT meter_id, count(*) AS cnt
                        FROM meter_interval
                       GROUP BY meter_id
                  ) v ON v.meter_id = r.meter_id
                 GROUP BY r.meter_id
                 ORDER BY r.meter_id ASC
                 LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [
        {
            "meter_id": row["meter_id"],
            "reading_count": row["reading_count"],
            "interval_count": row["interval_count"],
            "first_ts_utc": _iso(row["first_ts"]),
            "last_ts_utc": _iso(row["last_ts"]),
        }
        for row in rows
    ]


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
