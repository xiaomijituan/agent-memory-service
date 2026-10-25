from __future__ import annotations
import json
import logging
import time
from typing import Any
ALLOWED_FIELDS = frozenset({'ts', 'level', 'event', 'endpoint', 'method', 'status', 'duration_ms', 'request_id', 'user_id', 'session_id', 'message_count', 'result_count', 'top_k', 'version', 'error_kind', 'gate_rejected_total', 'gate_wait_ms'})

class MetadataOnlyFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {'ts': round(time.time(), 3), 'level': record.levelname, 'event': getattr(record, 'event', 'log')}
        payload.update({k: v for k, v in record.__dict__.items() if k in ALLOWED_FIELDS})
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

def get_logger() -> logging.Logger:
    logger = logging.getLogger('amc')
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(MetadataOnlyFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger

def log_event(logger: logging.Logger, level: int, **fields: Any) -> None:
    extra = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS}
    logger.log(level, '', extra=extra)
