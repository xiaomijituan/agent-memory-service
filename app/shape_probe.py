from __future__ import annotations
import json
import logging
import re
import threading
import time
from typing import Any
from fastapi import FastAPI
from starlette.responses import JSONResponse
ADD_ENDPOINT = '/v1/memories/add'
SEARCH_ENDPOINT = '/v1/memories/search'
SHAPEZ_PATH = '/shapez'
PROBE_LOGGER_NAME = 'amc.shape'
APP_STATE_ATTR = 'shape_probe'
SCHEMA_VERSION = 1
MAX_KEY_UNION = 64
MAX_ROLES = 16
MAX_NAME_CHARS = 64
MAX_ROLE_CHARS = 32
_ROLE_RE = re.compile('[A-Za-z0-9_.:\\-]{1,32}')
_ENABLE_VALUES = frozenset({'on', '1', 'true'})
_UNSET = object()

def enabled_for(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in _ENABLE_VALUES

def get_shape_logger() -> logging.Logger:
    logger = logging.getLogger(PROBE_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
    return logger

class _BoundedNames:
    __slots__ = ('_cap', '_names', '_max_chars', '_pattern', 'overflow')

    def __init__(self, cap: int, *, max_chars: int=MAX_NAME_CHARS, pattern: re.Pattern[str] | None=None) -> None:
        self._cap = cap
        self._names: set[str] = set()
        self._max_chars = max_chars
        self._pattern = pattern
        self.overflow = False

    def add(self, value: Any) -> None:
        if not isinstance(value, str) or not value or len(value) > self._max_chars or (self._pattern is not None and (not self._pattern.fullmatch(value))):
            self.overflow = True
            return
        if value in self._names:
            return
        if len(self._names) >= self._cap:
            self.overflow = True
            return
        self._names.add(value)

    def snapshot(self) -> list[str]:
        return sorted(self._names)

    @property
    def overflowed(self) -> bool:
        return bool(self.overflow)

def _key_names(keys: Any) -> list[str]:
    if not isinstance(keys, (list, tuple, set, frozenset)) and (not hasattr(keys, '__iter__')):
        return []
    return sorted({k for k in keys if isinstance(k, str) and 0 < len(k) <= MAX_NAME_CHARS})

def _raw_json_dict(request: Any) -> dict | None:
    body = getattr(request, '_body', None)
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode('utf-8')
        except UnicodeDecodeError:
            return None
    if not isinstance(body, str):
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None

def _role_of(container: dict) -> tuple[str | None, bool]:
    if 'role' not in container:
        return (None, False)
    value = container['role']
    if isinstance(value, str) and len(value) <= MAX_ROLE_CHARS and _ROLE_RE.fullmatch(value):
        return (value, False)
    return (None, True)

def extract_add_event(payload: Any, request: Any) -> dict[str, Any]:
    event: dict[str, Any] = {'event': 'shape_add', 'endpoint': ADD_ENDPOINT, 'ts': time.time(), 'message_count': 0, 'has_timestamp': False, 'top_level_keys': [], 'message_keys': [], 'roles': [], 'roles_overflow': False}
    raw = _raw_json_dict(request)
    if raw is not None:
        event['top_level_keys'] = _key_names(raw.keys())
        messages = raw.get('messages')
        if isinstance(messages, list):
            event['message_count'] = len(messages)
            _collect_message_shape(event, messages, from_raw=True)
        return event
    if payload is not None:
        event['top_level_keys'] = _key_names(getattr(payload, 'model_fields_set', set()))
        messages = list(getattr(payload, 'messages', []) or [])
        event['message_count'] = len(messages)
        _collect_message_shape(event, messages, from_raw=False)
    return event

def _collect_message_shape(event: dict[str, Any], messages: list[Any], *, from_raw: bool) -> None:
    message_keys: set[str] = set()
    roles: set[str] = set()
    roles_overflow = False
    has_timestamp = False
    for message in messages:
        if from_raw:
            if not isinstance(message, dict):
                continue
            message_keys.update((k for k in message.keys() if isinstance(k, str) and 0 < len(k) <= MAX_NAME_CHARS))
            if 'timestamp' in message and message['timestamp'] is not None:
                has_timestamp = True
            role, overflow = _role_of(message)
        else:
            fields = set(getattr(message, 'model_fields_set', set()) or set())
            message_keys.update((k for k in fields if isinstance(k, str) and 0 < len(k) <= MAX_NAME_CHARS))
            if getattr(message, 'timestamp', None) is not None:
                has_timestamp = True
            role, overflow = _role_of({'role': getattr(message, 'role', None)} if hasattr(message, 'role') else {})
        if overflow:
            roles_overflow = True
        if role is not None:
            roles.add(role)
    event['message_keys'] = sorted(message_keys)
    event['roles'] = sorted(roles)
    event['roles_overflow'] = roles_overflow
    event['has_timestamp'] = has_timestamp

def extract_search_event(payload: Any, request: Any) -> dict[str, Any]:
    event: dict[str, Any] = {'event': 'shape_search', 'endpoint': SEARCH_ENDPOINT, 'ts': time.time(), 'options_present': False, 'top_level_keys': []}
    raw = _raw_json_dict(request)
    if raw is not None:
        event['top_level_keys'] = _key_names(raw.keys())
        event['options_present'] = 'options' in raw
        return event
    if payload is not None:
        event['top_level_keys'] = _key_names(getattr(payload, 'model_fields_set', set()))
        event['options_present'] = getattr(payload, 'options', None) is not None
    return event

class ShapeProbe:

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._endpoints: dict[str, dict[str, Any]] = {}
        self._requests_total = 0

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get('event')
        endpoint = event.get('endpoint')
        if kind not in ('shape_add', 'shape_search') or not isinstance(endpoint, str):
            return
        with self._lock:
            agg = self._endpoint(endpoint, kind)
            self._apply(agg, event, kind)
            self._requests_total += 1
        get_shape_logger().info(json.dumps(event, ensure_ascii=False, sort_keys=True))

    def _endpoint(self, path: str, kind: str) -> dict[str, Any]:
        agg = self._endpoints.get(path)
        if agg is None:
            agg = self._new_agg(kind)
            self._endpoints[path] = agg
        return agg

    @staticmethod
    def _new_agg(kind: str) -> dict[str, Any]:
        agg: dict[str, Any] = {'kind': kind, 'requests': 0, 'top_level_keys': _BoundedNames(MAX_KEY_UNION), 'last_seen_ts': 0.0}
        if kind == 'shape_add':
            agg['requests_with_timestamp'] = 0
            agg['requests_without_timestamp'] = 0
            agg['single_message_with_timestamp'] = 0
            agg['single_message_without_timestamp'] = 0
            agg['multi_message_with_timestamp'] = 0
            agg['multi_message_without_timestamp'] = 0
            agg['message_count_total'] = 0
            agg['message_count_min'] = None
            agg['message_count_max'] = None
            agg['message_keys'] = _BoundedNames(MAX_KEY_UNION)
            agg['roles'] = _BoundedNames(MAX_ROLES, max_chars=MAX_ROLE_CHARS, pattern=_ROLE_RE)
        else:
            agg['requests_with_options'] = 0
            agg['requests_without_options'] = 0
        return agg

    @staticmethod
    def _apply(agg: dict[str, Any], event: dict[str, Any], kind: str) -> None:
        agg['requests'] += 1
        agg['last_seen_ts'] = max(agg['last_seen_ts'], float(event.get('ts') or 0.0))
        for name in event.get('top_level_keys', []):
            agg['top_level_keys'].add(name)
        if kind == 'shape_add':
            count = int(event.get('message_count') or 0)
            has_ts = bool(event.get('has_timestamp'))
            if has_ts:
                agg['requests_with_timestamp'] += 1
            else:
                agg['requests_without_timestamp'] += 1
            bucket = ('single_message_' if count == 1 else 'multi_message_') + ('with_timestamp' if has_ts else 'without_timestamp')
            agg[bucket] += 1
            agg['message_count_total'] += count
            if agg['message_count_min'] is None or count < agg['message_count_min']:
                agg['message_count_min'] = count
            if agg['message_count_max'] is None or count > agg['message_count_max']:
                agg['message_count_max'] = count
            for name in event.get('message_keys', []):
                agg['message_keys'].add(name)
            for role in event.get('roles', []):
                agg['roles'].add(role)
            if event.get('roles_overflow'):
                agg['roles'].overflow = True
        elif event.get('options_present'):
            agg['requests_with_options'] += 1
        else:
            agg['requests_without_options'] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            endpoints: dict[str, Any] = {}
            for path, agg in self._endpoints.items():
                rendered: dict[str, Any] = {'requests': agg['requests']}
                if agg['kind'] == 'shape_add':
                    rendered.update({'requests_with_timestamp': agg['requests_with_timestamp'], 'requests_without_timestamp': agg['requests_without_timestamp'], 'single_message_with_timestamp': agg['single_message_with_timestamp'], 'single_message_without_timestamp': agg['single_message_without_timestamp'], 'multi_message_with_timestamp': agg['multi_message_with_timestamp'], 'multi_message_without_timestamp': agg['multi_message_without_timestamp'], 'message_count_total': agg['message_count_total'], 'message_count_min': agg['message_count_min'], 'message_count_max': agg['message_count_max']})
                else:
                    rendered.update({'requests_with_options': agg['requests_with_options'], 'requests_without_options': agg['requests_without_options']})
                rendered['top_level_keys'] = agg['top_level_keys'].snapshot()
                rendered['top_level_keys_overflow'] = agg['top_level_keys'].overflowed
                if agg['kind'] == 'shape_add':
                    rendered['message_keys'] = agg['message_keys'].snapshot()
                    rendered['message_keys_overflow'] = agg['message_keys'].overflowed
                    rendered['roles'] = agg['roles'].snapshot()
                    rendered['roles_overflow'] = agg['roles'].overflowed
                rendered['last_seen_ts'] = agg['last_seen_ts']
                endpoints[path] = rendered
            return {'enabled': True, 'schema': SCHEMA_VERSION, 'requests_total': self._requests_total, 'endpoints': endpoints}

def record_add(app: Any, payload: Any, request: Any) -> None:
    _record(app, payload, request, extract_add_event)

def record_search(app: Any, payload: Any, request: Any) -> None:
    _record(app, payload, request, extract_search_event)

def _record(app: Any, payload: Any, request: Any, extractor: Any) -> None:
    probe = getattr(getattr(app, 'state', None), APP_STATE_ATTR, None)
    if probe is None:
        return
    if payload is None and _raw_json_dict(request) is None:
        return
    try:
        probe.observe(extractor(payload, request))
    except BaseException as exc:
        try:
            get_shape_logger().warning('shape_probe_error %s', type(exc).__name__)
        except BaseException:
            pass

def build(settings: Any) -> ShapeProbe | None:
    if enabled_for(getattr(settings, 'shape_probe', _UNSET)):
        return ShapeProbe()
    return None

def install(app: FastAPI, settings: Any=None) -> None:
    settings = settings if settings is not None else getattr(app.state, 'settings', None)
    if settings is None:
        raise RuntimeError('shape_probe.install 需要 settings（或 app.state.settings 已就绪）')
    if getattr(app.state, 'shape_probe_installed', False):
        return
    probe = build(settings)
    if probe is None:
        return
    app.state.shape_probe = probe

    @app.get(SHAPEZ_PATH)
    async def shapez() -> JSONResponse:
        return JSONResponse(probe.snapshot())
    app.state.shape_probe_installed = True
