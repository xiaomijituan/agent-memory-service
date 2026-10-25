from __future__ import annotations
import contextlib
import logging
import threading
import time
from typing import Iterator
from app.errors import RateLimitedError
from app.logging import get_logger, log_event
DEFAULT_ADMISSION_WAIT_SECONDS = 10.0
ADMISSION_RETRY_AFTER_SECONDS = 30
_LOGGER = get_logger()

class AdmissionGate:

    def __init__(self, *, endpoint: str, capacity: int, wait_seconds: float, setting_name: str='', retry_after: int=ADMISSION_RETRY_AFTER_SECONDS) -> None:
        if capacity < 1:
            raise ValueError(f"字段 {setting_name or 'concurrency'} 的闸门容量必须至少为 1")
        if wait_seconds < 0:
            raise ValueError(f"字段 {setting_name or 'admission_wait_seconds'} 的等待上界不得为负")
        self.endpoint = endpoint
        self.setting_name = setting_name
        self.capacity = int(capacity)
        self.wait_seconds = float(wait_seconds)
        self.retry_after = int(retry_after)
        self._slots = threading.Semaphore(self.capacity)
        self._lock = threading.Lock()
        self._active = 0
        self._waiting = 0
        self.peak_active = 0
        self.waiting_peak = 0
        self.admitted_total = 0
        self.rejected_total = 0

    @property
    def active_now(self) -> int:
        with self._lock:
            return self._active

    @property
    def waiting_now(self) -> int:
        with self._lock:
            return self._waiting

    @contextlib.contextmanager
    def admission(self) -> Iterator[None]:
        started = time.monotonic()
        with self._lock:
            self._waiting += 1
            if self._waiting > self.waiting_peak:
                self.waiting_peak = self._waiting
        try:
            if self.wait_seconds > 0:
                granted = self._slots.acquire(timeout=self.wait_seconds)
            else:
                granted = self._slots.acquire(blocking=False)
        finally:
            with self._lock:
                self._waiting -= 1
        if not granted:
            with self._lock:
                self.rejected_total += 1
                rejected_total = self.rejected_total
            log_event(_LOGGER, logging.WARNING, event='admission_gate', endpoint=self.endpoint, status='rejected', gate_rejected_total=rejected_total, gate_wait_ms=round((time.monotonic() - started) * 1000.0, 1))
            raise RateLimitedError(f"{self.endpoint} 的并发已达配置上限（{self.setting_name or 'concurrency'}={self.capacity}，等待 {self.wait_seconds:g}s 后仍无额度）——可重试", retry_after=self.retry_after)
        with self._lock:
            self._active += 1
            self.admitted_total += 1
            if self._active > self.peak_active:
                self.peak_active = self._active
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
            self._slots.release()
