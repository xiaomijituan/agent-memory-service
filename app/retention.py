from __future__ import annotations
import argparse
import dataclasses
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any
from app.config import Settings
from app.logging import get_logger, log_event
from app.store import Store
DEFAULT_RETENTION_DAYS = 30
_ZULU = '%Y-%m-%dT%H:%M:%SZ'
_TASK_ID_PREFIX = 'retention'
STATUS_OK = 'ok'
STATUS_PARTIAL = 'partial'
STATUS_FAILED = 'failed'
EVENT = 'retention'

def utc_zulu(moment: datetime) -> str:
    if not isinstance(moment, datetime):
        raise ValueError(f'now 必须是 datetime，实得 {type(moment).__name__}')
    if moment.tzinfo is None:
        raise ValueError('now 必须带时区（tzinfo）：无时区就无法确定留存边界')
    return moment.astimezone(timezone.utc).strftime(_ZULU)

def retention_cutoff(now: datetime, retention_days: int) -> str:
    if isinstance(now, str) or not isinstance(now, datetime):
        raise ValueError('now 必须是带时区的 datetime，不是字符串：字符串 cutoff 走的是字典序比较，短串会静默少删')
    if isinstance(retention_days, bool) or not isinstance(retention_days, int):
        raise ValueError(f'retention_days 必须是整数，实得 {type(retention_days).__name__}')
    if retention_days < 0:
        raise ValueError('retention_days 不得为负：负数等于去删还没到的数据')
    return utc_zulu(now - timedelta(days=retention_days))

@dataclasses.dataclass(frozen=True)
class RetentionRecord:
    task_id: str
    started_at: str
    finished_at: str
    cutoff: str
    retention_days: int
    dbs_scanned: int
    memory_rows_deleted: int
    response_rows_deleted: int
    dbs_failed: int
    status: str
    error_kind: str | None
    duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

class RetentionLog:

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)

    @property
    def path(self) -> str:
        return self._path

    def append(self, record: RetentionRecord) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> list[dict[str, Any]]:
        try:
            with open(self._path, encoding='utf-8') as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

class RetentionRunner:

    def __init__(self, store: Any, log: RetentionLog, *, retention_days: int=DEFAULT_RETENTION_DAYS, now: Callable[[], datetime] | None=None, task_id: str | None=None, logger=None) -> None:
        self._store = store
        self._log = log
        self._retention_days = retention_days
        self._now = now if now is not None else _utc_now
        self._task_id = task_id
        self._logger = logger if logger is not None else get_logger()

    def run(self) -> RetentionRecord:
        started_moment = self._now()
        started_at = utc_zulu(started_moment)
        cutoff = retention_cutoff(started_moment, self._retention_days)
        task_id = self._task_id or new_task_id()
        clock_started = time.monotonic()
        dbs_scanned = memory_rows_deleted = response_rows_deleted = dbs_failed = 0
        status = STATUS_OK
        error_kind: str | None = None
        try:
            report = self._store.purge_memories_before(cutoff)
            dbs_scanned = int(report.dbs_scanned)
            memory_rows_deleted = int(report.rows_deleted)
            dbs_failed = int(report.dbs_failed)
            response_rows_deleted = int(self._store.purge_responses_before(cutoff))
            if dbs_failed:
                status = STATUS_PARTIAL
        except Exception as exc:
            status = STATUS_FAILED
            error_kind = type(exc).__name__
        record = RetentionRecord(task_id=task_id, started_at=started_at, finished_at=utc_zulu(self._now()), cutoff=cutoff, retention_days=self._retention_days, dbs_scanned=dbs_scanned, memory_rows_deleted=memory_rows_deleted, response_rows_deleted=response_rows_deleted, dbs_failed=dbs_failed, status=status, error_kind=error_kind, duration_ms=round((time.monotonic() - clock_started) * 1000, 2))
        self._log.append(record)
        log_event(self._logger, 40 if status == STATUS_FAILED else 30 if status == STATUS_PARTIAL else 20, event=EVENT, request_id=task_id, status=status, result_count=memory_rows_deleted + response_rows_deleted, duration_ms=record.duration_ms, error_kind=error_kind)
        return record

def new_task_id() -> str:
    return f'{_TASK_ID_PREFIX}-{uuid.uuid4()}'

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m app.retention', description='数据留存任务：删除过期记忆行与幂等留存行，并追加一条删除记录。')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run', help='执行一轮删除（不可逆，必须 --yes 显式确认）')
    run.add_argument('--db-dir', required=True, help='存储根目录（必填）。')
    run.add_argument('--log', required=True, help='删除记录文件（JSONL，追加）')
    run.add_argument('--retention-days', type=int, default=DEFAULT_RETENTION_DAYS, help='留存窗口，默认 30 天')
    run.add_argument('--now', default=None, help=f'覆盖判定时刻（定宽 UTC，{_ZULU}）。演练/复核用；默认取当前时间')
    run.add_argument('--yes', action='store_true', help='确认执行不可逆删除')
    return parser

def _parse_now(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError('--now 需要一个非空的定宽 UTC 串')
    parsed = datetime.strptime(value, _ZULU)
    return parsed.replace(tzinfo=timezone.utc)

def main(argv: Iterable[str] | None=None) -> int:
    try:
        args = _build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code or 2)
    if not getattr(args, 'yes', False):
        print('拒绝执行：删除不可逆，必须显式加 --yes 确认。', file=sys.stderr)
        return 2
    try:
        now = _parse_now(args.now) if args.now else _utc_now()
    except ValueError as exc:
        print(f'--now 无效：{exc}', file=sys.stderr)
        return 2
    clock: Callable[[], datetime] = (lambda: now) if args.now else _utc_now
    settings = Settings(db_dir=args.db_dir, embedding_mode='off', auth_scheme='none')
    try:
        store = Store(settings)
    except Exception as exc:
        print(f'存储未能打开：{type(exc).__name__}（详情见日志字段，不含路径）', file=sys.stderr)
        return 1
    try:
        record = RetentionRunner(store, RetentionLog(args.log), retention_days=args.retention_days, now=clock).run()
    finally:
        store.close()
    print(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if record.status in (STATUS_OK, STATUS_PARTIAL) else 1
if __name__ == '__main__':
    raise SystemExit(main())
