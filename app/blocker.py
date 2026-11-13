"""进程内“提交前暂停”开关——仅用于演示交错补录，不参与生产正确性。

两台 web 进程不共享内存：暂停只作用于恰好收到该请求的那台进程，
这恰好能把一次上传卡在“准备完成、尚未拿锁提交”的窗口里，
让另一台进程的并发上传先提交；恢复后本请求在锁内做权威复核，
从而暴露“分别合法、合起来倒退”的场景。

语义（一次性门闩）：
  1. 先调管理端点 arm(meter_id) 预置门闩；
  2. 带 X-Pause-Before-Commit: <meter_id> 的上传到达时，
     若门闩存在则等待 release，等待结束后门闩自动拆除；
  3. 未预置门闩时上传不等待，正常提交。
"""
from __future__ import annotations

import threading
import time

from .config import SETTINGS


class PauseBlocker:
    def __init__(self, enabled: bool, timeout_seconds: int):
        self._enabled = enabled
        self._timeout = timeout_seconds
        self._gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        # 记录正在等待的请求，便于页面观察。
        self._waiting: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def arm(self, key: str) -> None:
        if not self._enabled:
            return
        with self._lock:
            # 已存在（可能正有请求在等）时不覆盖，避免旧请求失联。
            self._gates.setdefault(key, threading.Event())

    def release(self, key: str) -> bool:
        with self._lock:
            event = self._gates.pop(key, None)
        if event is None:
            return False
        event.set()
        return True

    def release_all(self) -> int:
        with self._lock:
            events = list(self._gates.values())
            self._gates.clear()
        for event in events:
            event.set()
        return len(events)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "armed_keys": sorted(self._gates.keys()),
                "waiting_keys": sorted(self._waiting.keys()),
            }

    def pause_if_armed(self, key: str) -> bool:
        """若 key 已预置门闩则阻塞至放行（或超时），返回是否发生过等待。

        等待期间门闩保留在 _gates 中，管理端点凭它找到并释放本请求；
        等待超过 timeout 自动放行，避免演示失败时永久挂死连接。
        """
        if not self._enabled:
            return False
        with self._lock:
            event = self._gates.get(key)
        if event is None:
            return False
        with self._lock:
            self._waiting[key] = time.time()
        try:
            event.wait(timeout=self._timeout)
        finally:
            with self._lock:
                self._waiting.pop(key, None)
                # 正常由 release 摘除；超时路径下自行清理。
                self._gates.pop(key, None)
        return True


BLOCKER = PauseBlocker(
    enabled=SETTINGS.blocker_enabled,
    timeout_seconds=SETTINGS.blocker_timeout_seconds,
)
