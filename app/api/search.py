from __future__ import annotations
import logging
import math
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from app.concurrency import AdmissionGate
from app.embedding import Embedder, make_embedder
from app.errors import ApiError, InvalidPayloadError, ServiceFailureError
from app.logging import get_logger, log_event
from app.relative_dates import resolve_relative_dates
from app.retrieval.fusion import FusionRecall
from app.retrieval.lexical import LexicalRecall
from app.retrieval.vector import VectorRecall
from app.shape_probe import record_search as record_search_shape
from app.store import Store
SEARCH_PATH = '/v1/memories/search'
SEARCH_GATE_ATTR = 'search_admission_gate'
PLATFORM_CONTEXT_TOKEN_BUDGET = 117760
SINGLE_CANDIDATE_TOKEN_CAP = PLATFORM_CONTEXT_TOKEN_BUDGET // 8
DEFAULT_TOP_K = 100
_UNSET = object()
_gate_lock = threading.Lock()
_logger = get_logger()
_CJK_RE = re.compile('[\u3040-ヿ㐀-䶿一-鿿豈-\ufaff]')
_LATIN_RE = re.compile('[A-Za-z0-9]')

class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='ignore')
    query: str
    user_id: str
    top_k: int = DEFAULT_TOP_K
    options: list[str] | None = None

def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_CJK_RE.findall(text)) + math.ceil(len(_LATIN_RE.findall(text)) / 4)

def _compress_to_token_cap(text: str, cap: int) -> str:
    if estimate_tokens(text) <= cap:
        return text
    low, high = (0, len(text))
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= cap:
            low = mid
        else:
            high = mid - 1
    return text[:low]

def _store_for(app: FastAPI) -> Store:
    store = getattr(app.state, 'store', None)
    if store is None:
        raise ServiceFailureError('存储层未就绪：lifespan 未挂载 app.state.store')
    return store

def _embedder_for(app: FastAPI, settings: Any) -> Embedder | None:
    override = getattr(app.state, 'search_embedder', _UNSET)
    if override is not _UNSET:
        return override
    if settings.embedding_mode != 'api':
        return None
    cached = getattr(app.state, 'search_embedder_api', None)
    if cached is not None:
        return cached
    cached = make_embedder(mode='api', dimensions=settings.embedding_dimensions, base_url=settings.embedding_base_url, model=settings.embedding_model, api_key=settings.embedding_api_key, batch_size=settings.embedding_batch_size)
    app.state.search_embedder_api = cached
    return cached

def _pack(candidates, top_k: int) -> list[dict]:
    data: list[dict] = []
    used = 0
    for candidate in candidates:
        if len(data) >= top_k:
            break
        content = resolve_relative_dates(candidate.content, candidate.row.event_time)
        if not content:
            continue
        cost = estimate_tokens(content)
        if cost > SINGLE_CANDIDATE_TOKEN_CAP:
            content = _compress_to_token_cap(content, SINGLE_CANDIDATE_TOKEN_CAP)
            cost = estimate_tokens(content)
        if used + cost > PLATFORM_CONTEXT_TOKEN_BUDGET:
            break
        used += cost
        item: dict = {'id': str(candidate.id), 'content': content, 'score': round(candidate.score, 6)}
        created_at = _iso_created_at(candidate.row.event_time)
        if created_at:
            item['created_at'] = created_at
        data.append(item)
    return data
_EPOCH_TEXT = re.compile('-?\\d+')

def _iso_created_at(value: str | None) -> str | None:
    text = (value or '').strip()
    if not text:
        return None
    try:
        if _EPOCH_TEXT.fullmatch(text):
            moment = datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            moment = moment.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')

def _admission_gate(app: FastAPI, settings: Any) -> AdmissionGate:
    gate = getattr(app.state, SEARCH_GATE_ATTR, None)
    if gate is not None:
        return gate
    with _gate_lock:
        gate = getattr(app.state, SEARCH_GATE_ATTR, None)
        if gate is None:
            gate = AdmissionGate(endpoint=SEARCH_PATH, setting_name='search_concurrency', capacity=settings.search_concurrency, wait_seconds=settings.admission_wait_seconds)
            setattr(app.state, SEARCH_GATE_ATTR, gate)
    return gate

def _correlation_id(request: Request | None) -> str | None:
    if request is None:
        return None
    return getattr(getattr(request, 'state', None), 'request_id', None)

def _log_completion(request: Request | None, payload: SearchRequest, settings: Any, started: float, outcome: dict[str, Any], *, status: int, error_kind: str | None) -> None:
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    fields: dict[str, Any] = {'event': 'search', 'endpoint': SEARCH_PATH, 'method': getattr(request, 'method', 'POST'), 'status': status, 'duration_ms': round((time.perf_counter() - started) * 1000, 2), 'request_id': _correlation_id(request), 'user_id': payload.user_id, 'top_k': int(payload.top_k), 'result_count': outcome.get('result_count', 0), 'version': getattr(settings, 'version', None)}
    if error_kind:
        fields['error_kind'] = error_kind
    log_event(_logger, level, **fields)

def _handle_search(app: FastAPI, request: Request, payload: SearchRequest) -> JSONResponse:
    started = time.perf_counter()
    settings = getattr(app.state, 'settings', None)
    outcome: dict[str, Any] = {}
    record_search_shape(app, payload, request)
    try:
        response = _perform_search(app, payload, settings, outcome)
    except ApiError as exc:
        _log_completion(request, payload, settings, started, outcome, status=exc.status_code, error_kind=type(exc).__name__)
        raise
    except BaseException as exc:
        _log_completion(request, payload, settings, started, outcome, status=500, error_kind=type(exc).__name__)
        raise
    _log_completion(request, payload, settings, started, outcome, status=200, error_kind=None)
    return response

def _perform_search(app: FastAPI, payload: SearchRequest, settings: Any, outcome: dict[str, Any]) -> JSONResponse:
    store = _store_for(app)
    if settings is None:
        raise ServiceFailureError('配置未就绪：app.state.settings 缺失')
    if not payload.user_id:
        raise InvalidPayloadError('字段 user_id 必须是非空字符串', fields=['user_id'])
    top_k = int(payload.top_k)
    if top_k <= 0:
        outcome['result_count'] = 0
        return JSONResponse(status_code=200, content={'data': []})
    gate = _admission_gate(app, settings)
    with gate.admission():
        return _admitted_search(app, store, settings, payload, top_k, outcome)

def _admitted_search(app: FastAPI, store: Store, settings: Any, payload: SearchRequest, top_k: int, outcome: dict[str, Any]) -> JSONResponse:
    fusion = FusionRecall(store, LexicalRecall(store), VectorRecall(store, _embedder_for(app, settings)), settings)
    candidates = fusion.recall(payload.user_id, payload.query, top_k)
    data = _pack(candidates, top_k)
    outcome['result_count'] = len(data)
    return JSONResponse(status_code=200, content={'data': data})

def install(app: FastAPI, settings: Any=None) -> None:
    settings = settings if settings is not None else getattr(app.state, 'settings', None)
    if settings is None:
        raise RuntimeError('search.install 需要 settings（或 app.state.settings 已就绪）')
    _admission_gate(app, settings)
    if getattr(app.state, 'search_installed', False):
        return

    @app.post(SEARCH_PATH)
    def search_memories(payload: SearchRequest, request: Request) -> JSONResponse:
        return _handle_search(app, request, payload)
    app.state.search_installed = True
