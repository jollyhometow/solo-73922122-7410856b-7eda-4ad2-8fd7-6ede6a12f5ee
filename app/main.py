"""FastAPI 入口：CSV 上传、读数/区间查询、演示用暂停管理。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse

from . import db, logic
from .blocker import BLOCKER
from .config import SETTINGS

app = FastAPI(
    title="取水井耗水区间服务",
    description=(
        "上传水表累计读数 CSV，按同表时间相邻读数持久化耗水区间；"
        "累计量倒退整批拒绝；支持并发补录的锁内权威复核。"
    ),
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    db.init_pool(
        SETTINGS.database_url,
        SETTINGS.min_pool_size,
        SETTINGS.max_pool_size,
    )
    # 数据库容器可能晚于 web 启动，做有限次重试。
    last_error: Exception | None = None
    for attempt in range(30):
        try:
            db.init_schema()
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(1)
    else:  # pragma: no cover
        raise RuntimeError(f"database unavailable after retries: {last_error}")


@app.on_event("shutdown")
def _shutdown() -> None:
    db.close_pool()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "pid": os.getpid(), "host": os.environ.get("HOSTNAME", "")}


@app.post("/api/readings/batch")
async def upload_batch(
    request: Request,
    file: UploadFile | None = File(default=None),
    x_pause_before_commit: str | None = Header(default=None),
) -> JSONResponse:
    """上传一批 CSV（最多 1000 数据行）。

    成功：读数与区间在同一事务内原子生效。
    倒退：整批拒绝，返回相邻冲突读数对。
    可选请求头 X-Pause-Before-Commit: <meter_id>
        ——演示用，提交拿锁前暂停，直到 /api/admin/blocker/release 放行。
    """
    raw = await _read_payload(request, file)
    # CSV 解析是 CPU 阻塞操作，放线程池，避免卡住事件循环。
    try:
        readings = await run_in_threadpool(logic.parse_csv, raw)
    except logic.CsvError as exc:
        raise HTTPException(status_code=400, detail=exc.to_detail()) from exc

    summary = logic.summarize_batch(readings)

    # ---- 提交前暂停点（仅演示）：此时尚未持有任何数据库锁 ----
    pause_key = (x_pause_before_commit or "").strip() or None
    if pause_key is not None and not BLOCKER.enabled:
        raise HTTPException(
            status_code=400,
            detail={"message": "pause blocker is disabled on this process"},
        )

    def _blocking_commit() -> JSONResponse:
        # 等待与数据库提交均为阻塞调用，整体在线程池内执行；
        # 等待期间不占数据库连接、不持任何锁，其他上传照常提交。
        if pause_key is not None:
            BLOCKER.pause_if_armed(pause_key)
        try:
            result = db.commit_batch(readings)
        except db.RejectedBatch as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "rejected",
                    "message": (
                        "cumulative regression detected on the final per-meter sequence; "
                        "the whole batch was rejected, nothing was written"
                    ),
                    "batch": summary,
                    "conflicts": [c.to_dict() for c in exc.conflicts],
                },
            )
        result["batch"] = summary
        return JSONResponse(status_code=200, content=result)

    return await run_in_threadpool(_blocking_commit)


async def _read_payload(request: Request, file: UploadFile | None) -> bytes:
    if file is not None:
        raw = await file.read()
    else:
        raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail={"message": "empty request body"})
    if len(raw) > SETTINGS.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "message": f"payload exceeds {SETTINGS.max_upload_bytes} bytes",
                "size": len(raw),
            },
        )
    return raw


@app.get("/api/meters")
def get_meters(limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    return {"meters": db.list_meters(limit=limit)}


@app.get("/api/meters/{meter_id}")
def get_meter(
    meter_id: str,
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> dict:
    result = db.query_meter(meter_id, limit=limit)
    if result["reading_count"] == 0:
        raise HTTPException(
            status_code=404, detail={"message": f"no readings for meter {meter_id!r}"}
        )
    return result


# ---------------- 演示用暂停管理端点 ----------------


@app.post("/api/admin/blocker/arm")
def blocker_arm(key: str = Query(..., min_length=1, max_length=128)) -> dict:
    if not BLOCKER.enabled:
        raise HTTPException(status_code=400, detail={"message": "blocker disabled"})
    BLOCKER.arm(key)
    return BLOCKER.status()


@app.post("/api/admin/blocker/release")
def blocker_release(key: str | None = Query(default=None)) -> dict:
    if not BLOCKER.enabled:
        raise HTTPException(status_code=400, detail={"message": "blocker disabled"})
    if key is None:
        released = BLOCKER.release_all()
        return {"released": released, **BLOCKER.status()}
    ok = BLOCKER.release(key)
    if not ok:
        raise HTTPException(status_code=404, detail={"message": f"no armed key {key!r}"})
    return BLOCKER.status()


@app.get("/api/admin/blocker")
def blocker_status() -> dict:
    return BLOCKER.status()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "web" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
