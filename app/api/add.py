from __future__ import annotations
import hashlib
import json
import logging
import threading
import time
from typing import Any
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from app.annotate import annotate_chunk
from app.chunking import chunk_message
from app.concurrency import AdmissionGate
from app.embedding import Embedder, EmbeddingError, make_embedder
from app.errors import ApiError, InvalidPayloadError, RateLimitedError, ServiceFailureError
from app.logging import get_logger, log_event
from app.shape_probe import record_add as record_add_shape
from app.store import Claim, NewMemory, Store
ADD_PATH = '/v1/memories/add'
IN_PROGRESS_RETRY_AFTER_SECONDS = 5
ADD_GATE_ATTR = 'add_admission_gate'
_UNSET = object()
_embedder_lock = threading.Lock()
_gate_lock = threading.Lock()
_logger = get_logger()

class AddMessage(BaseModel):
    model_config = ConfigDict(extra='ignore')
    role: str
    content: str
    timestamp: int | str | None = None

class AddRequest(BaseModel):
    model_config = ConfigDict(extra='ignore')
    request_id: str
    messages: list[AddMessage]
    user_id: str
    session_id: str

def _canonical_bytes(payload: AddRequest) -> bytes:
    return json.dumps(payload.model_dump(mode='json'), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')

def _payload_sha256(payload: AddRequest) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()

def _success_bytes(request_id: str, user_id: str, session_id: str) -> bytes:
    payload = {'success': True, 'request_id': request_id, 'user_id': user_id, 'session_id': session_id}
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

def _json_response(content: bytes) -> Response:
    return Response(content=content, status_code=200, media_type='application/json')

def _validated_identifiers(payload: AddRequest) -> tuple[str, str, str]:
    for name in ('request_id', 'user_id', 'session_id'):
        if not getattr(payload, name):
            raise InvalidPayloadError(f'字段 {name} 必须是非空字符串', fields=[name])
    return (payload.request_id, payload.user_id, payload.session_id)

def _validated_batch(payload: AddRequest) -> None:
    if not payload.messages:
        raise InvalidPayloadError('字段 messages 不得为空批次', fields=['messages'])
    for index, message in enumerate(payload.messages):
        path = f'messages[{index}]'
        if not message.role.strip():
            raise InvalidPayloadError(f'字段 {path}.role 不得为空', fields=['role', f'{path}.role'])
        if not message.content.strip():
            raise InvalidPayloadError(f'字段 {path}.content 不得为空白', fields=['content', f'{path}.content'])

def _event_time_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)

def _annotation_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=False)

def _build_rows(payload: AddRequest, settings: Any, embedder: Embedder | None) -> list[NewMemory]:
    pending = []
    for message_index, message in enumerate(payload.messages):
        wire = message.model_dump()
        for chunk in chunk_message(message.content, settings):
            pending.append(annotate_chunk(chunk, {**wire, 'content': chunk.text}, message_index=message_index))
    vectors = None if embedder is None else _vectorize(embedder, [annotated.text for annotated in pending])
    rows: list[NewMemory] = []
    for index, annotated in enumerate(pending):
        rows.append(NewMemory(session_id=payload.session_id, msg_index=annotated.message_index, chunk_index=annotated.chunk_index, content=annotated.text, event_time=_event_time_text(annotated.event_time), speaker=annotated.speaker, entities=_annotation_text(list(annotated.entities)), attribute_slots=_annotation_text(dict(annotated.attribute_slots)), vector=None if vectors is None else vectors[index]))
    return rows

def _embedder_for(app: FastAPI, settings: Any) -> Embedder | None:
    override = getattr(app.state, 'add_embedder', _UNSET)
    if override is not _UNSET:
        return override
    if settings.embedding_mode != 'api':
        return None
    cached = getattr(app.state, 'add_embedder_api', None)
    if cached is not None:
        return cached
    with _embedder_lock:
        cached = getattr(app.state, 'add_embedder_api', None)
        if cached is None:
            cached = make_embedder(mode='api', dimensions=settings.embedding_dimensions, base_url=settings.embedding_base_url, model=settings.embedding_model, api_key=settings.embedding_api_key, batch_size=settings.embedding_batch_size)
            app.state.add_embedder_api = cached
    return cached

def _vectorize(embedder: Embedder | None, contents: list[str]) -> list[np.ndarray] | None:
    if embedder is None:
        return None
    try:
        return embedder.embed(contents)
    except EmbeddingError as exc:
        raise ServiceFailureError(f'内联向量化失败：{type(exc).__name__}') from None

def _store_for(app: FastAPI) -> Store:
    store = getattr(app.state, 'store', None)
    if store is None:
        raise ServiceFailureError('存储层未就绪：lifespan 未挂载 app.state.store')
    return store

def _replay_response(claim: Claim) -> Response:
    stored = claim.stored
    if stored is None or not isinstance(stored.response_bytes, (bytes, bytearray)):
        raise RuntimeError('协议不变量破坏：done 的留存记录里没有响应字节')
    return _json_response(bytes(stored.response_bytes))

def _release_claim(store: Store, request_id: str, user_id: str, payload_sha256: str, cause: BaseException) -> None:
    release_error: str | None = None
    released = False
    try:
        released = bool(store.release_request(request_id, user_id, payload_sha256))
    except Exception as exc:
        release_error = type(exc).__name__
    log_event(_logger, 30, event='claim_release', endpoint=ADD_PATH, request_id=request_id, user_id=user_id, status='released' if released else 'failed' if release_error else 'kept', error_kind='+'.join((name for name in (type(cause).__name__, release_error) if name)))

def _admission_gate(app: FastAPI, settings: Any) -> AdmissionGate:
    gate = getattr(app.state, ADD_GATE_ATTR, None)
    if gate is not None:
        return gate
    with _gate_lock:
        gate = getattr(app.state, ADD_GATE_ATTR, None)
        if gate is None:
            gate = AdmissionGate(endpoint=ADD_PATH, setting_name='add_concurrency', capacity=settings.add_concurrency, wait_seconds=settings.admission_wait_seconds)
            setattr(app.state, ADD_GATE_ATTR, gate)
    return gate

def _correlation_id(request: Request | None) -> str | None:
    if request is None:
        return None
    return getattr(getattr(request, 'state', None), 'request_id', None)

def _log_completion(request: Request | None, payload: AddRequest, settings: Any, started: float, outcome: dict[str, Any], *, status: int, error_kind: str | None) -> None:
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    fields: dict[str, Any] = {'event': 'add', 'endpoint': ADD_PATH, 'method': getattr(request, 'method', 'POST'), 'status': status, 'duration_ms': round((time.perf_counter() - started) * 1000, 2), 'request_id': _correlation_id(request), 'user_id': payload.user_id, 'session_id': payload.session_id, 'message_count': len(payload.messages), 'version': getattr(settings, 'version', None)}
    if outcome.get('chunk_count') is not None:
        fields['result_count'] = outcome['chunk_count']
    if error_kind:
        fields['error_kind'] = error_kind
    log_event(_logger, level, **fields)

def _handle_add(app: FastAPI, request: Request, payload: AddRequest) -> Response:
    started = time.perf_counter()
    settings = getattr(app.state, 'settings', None)
    outcome: dict[str, Any] = {}
    record_add_shape(app, payload, request)
    try:
        response = _perform_add(app, payload, settings, outcome)
    except ApiError as exc:
        _log_completion(request, payload, settings, started, outcome, status=exc.status_code, error_kind=type(exc).__name__)
        raise
    except BaseException as exc:
        _log_completion(request, payload, settings, started, outcome, status=500, error_kind=type(exc).__name__)
        raise
    _log_completion(request, payload, settings, started, outcome, status=200, error_kind=None)
    return response

def _perform_add(app: FastAPI, payload: AddRequest, settings: Any, outcome: dict[str, Any]) -> Response:
    store = _store_for(app)
    if settings is None:
        raise ServiceFailureError('配置未就绪：app.state.settings 缺失')
    request_id, user_id, session_id = _validated_identifiers(payload)
    _validated_batch(payload)
    gate = _admission_gate(app, settings)
    with gate.admission():
        return _admitted_add(app, store, payload, settings, request_id, user_id, session_id, outcome)

def _admitted_add(app: FastAPI, store: Store, payload: AddRequest, settings: Any, request_id: str, user_id: str, session_id: str, outcome: dict[str, Any]) -> Response:
    payload_sha256 = _payload_sha256(payload)
    claim = store.claim_request(request_id, user_id, payload_sha256)
    if claim.status == 'replay':
        return _replay_response(claim)
    if claim.status == 'in_progress':
        raise RateLimitedError('同一 request_id 正在处理中，请按 Retry-After 重试（并发撞车）', retry_after=IN_PROGRESS_RETRY_AFTER_SECONDS)
    try:
        rows = _build_rows(payload, settings, _embedder_for(app, settings))
        store.insert_memories(user_id, request_id, rows)
        outcome['chunk_count'] = len(rows)
        content = _success_bytes(request_id, user_id, session_id)
        store.finish_request(request_id, content)
    except BaseException as exc:
        _release_claim(store, request_id, user_id, payload_sha256, exc)
        raise
    return _json_response(content)

def install(app: FastAPI, settings: Any=None) -> None:
    settings = settings if settings is not None else getattr(app.state, 'settings', None)
    if settings is None:
        raise RuntimeError('add.install 需要 settings（或 app.state.settings 已就绪）')
    _admission_gate(app, settings)
    if getattr(app.state, 'add_installed', False):
        return

    @app.post(ADD_PATH)
    def add_memories(payload: AddRequest, request: Request) -> Response:
        return _handle_add(app, request, payload)
    app.state.add_installed = True
