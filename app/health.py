from __future__ import annotations
import asyncio
import threading
import time
from typing import Any, Callable
from fastapi import FastAPI
from starlette.responses import JSONResponse
from app.config import Settings
from app.embedding import Embedder, EmbeddingError, ProviderRetryPolicy, make_embedder, make_probe_policy
from app.errors import ServiceFailureError
from app.logging import get_logger, log_event
OK_BODY: dict[str, str] = {'status': 'ok'}
UNAVAILABLE_MESSAGE = '服务依赖不可用，请凭 request_id 联系维护方'
STORAGE = 'storage'
EMBEDDING = 'embedding'
DEFAULT_PROBE_CACHE_SECONDS = 5.0
PROBE_DEADLINE_SECONDS = 1.5
PROBE_RETRY_POLICY: ProviderRetryPolicy = make_probe_policy(PROBE_DEADLINE_SECONDS)
_PROBE_TEXT = 'health'
_STORE_ATTR = 'store'

class HealthProbes:

    def __init__(self, settings: Settings, *, store_provider: Callable[[], Any] | None=None, embedder_provider: Callable[[], Embedder] | None=None, cache_seconds: float=DEFAULT_PROBE_CACHE_SECONDS, deadline_seconds: float=PROBE_DEADLINE_SECONDS) -> None:
        self._settings = settings
        self._store_provider = store_provider or _no_store
        self._embedder_provider = embedder_provider or self._embedder_from_settings
        self._cache_seconds = max(0.0, float(cache_seconds))
        self._deadline_seconds = float(deadline_seconds)
        self._embedder: Embedder | None = None
        self._cache: tuple[float, bool] | None = None
        self._lock = threading.Lock()

    @property
    def deadline_seconds(self) -> float:
        return self._deadline_seconds

    def storage_ok(self) -> bool:
        store = self._store_provider()
        if store is None:
            return False
        try:
            health = store.health()
        except Exception:
            return False
        return bool(getattr(health, 'ok', False))

    def embedding_ok(self) -> bool:
        with self._lock:
            if self._cache is not None and self._cache_seconds > 0.0:
                probed_at, result = self._cache
                if time.monotonic() - probed_at < self._cache_seconds:
                    return bool(result)
            result = self._probe_embedder()
            self._cache = (time.monotonic(), result)
            return result

    def _probe_embedder(self) -> bool:
        return _call_within(self._embed_once, self._deadline_seconds)

    def _embed_once(self) -> bool:
        try:
            embedder = self._embedder_provider()
            vectors = embedder.embed([_PROBE_TEXT])
        except EmbeddingError:
            return False
        except Exception:
            return False
        return bool(vectors)

    def _embedder_from_settings(self) -> Embedder:
        if self._embedder is None:
            self._embedder = make_probe_embedder(self._settings)
        return self._embedder

    def failures(self) -> list[str]:
        found: list[str] = []
        if not self.storage_ok():
            found.append(STORAGE)
        if not self.embedding_ok():
            found.append(EMBEDDING)
        return found

def _no_store() -> Any:
    return None

def _call_within(work: Callable[[], Any], deadline_seconds: float) -> bool:
    if deadline_seconds <= 0.0:
        return False
    outcome: dict[str, bool] = {}
    finished = threading.Event()

    def runner() -> None:
        try:
            outcome['ok'] = bool(work())
        except Exception:
            outcome['ok'] = False
        finally:
            finished.set()
    threading.Thread(target=runner, name='amc-health-probe', daemon=True).start()
    if not finished.wait(deadline_seconds):
        return False
    return bool(outcome.get('ok', False))

async def _run_within(work: Callable[[], bool], deadline_seconds: float) -> bool:
    if deadline_seconds <= 0.0:
        return False
    try:
        return bool(await asyncio.wait_for(asyncio.to_thread(work), timeout=deadline_seconds))
    except Exception:
        return False

async def _dependency_failures(probes: HealthProbes) -> list[str]:
    failed: list[str] = []
    if not await _run_within(probes.storage_ok, probes.deadline_seconds):
        failed.append(STORAGE)
    if not await _run_within(probes.embedding_ok, probes.deadline_seconds):
        failed.append(EMBEDDING)
    return failed

def make_probe_embedder(settings: Settings, *, transport: Any=None) -> Embedder:
    return make_embedder(settings.embedding_mode, dimensions=settings.embedding_dimensions, base_url=settings.embedding_base_url, model=settings.embedding_model, api_key=settings.embedding_api_key, batch_size=settings.embedding_batch_size, timeout=PROBE_DEADLINE_SECONDS, retry_policy=PROBE_RETRY_POLICY, transport=transport)

def build_probes(app: FastAPI, settings: Settings, *, embedder: Embedder | None=None, cache_seconds: float=DEFAULT_PROBE_CACHE_SECONDS, deadline_seconds: float=PROBE_DEADLINE_SECONDS) -> HealthProbes:
    return HealthProbes(settings, store_provider=lambda: getattr(app.state, _STORE_ATTR, None), embedder_provider=(lambda: embedder) if embedder is not None else None, cache_seconds=cache_seconds, deadline_seconds=deadline_seconds)

def install(app: FastAPI, settings: Any=None, *, embedder: Embedder | None=None, cache_seconds: float=DEFAULT_PROBE_CACHE_SECONDS, deadline_seconds: float=PROBE_DEADLINE_SECONDS) -> None:
    settings = settings if settings is not None else getattr(app.state, 'settings', None)
    if settings is None:
        raise RuntimeError('health.install 需要 settings（或 app.state.settings 已就绪）')
    if getattr(app.state, 'health_installed', False):
        return
    app.state.health_probes = build_probes(app, settings, embedder=embedder, cache_seconds=cache_seconds, deadline_seconds=deadline_seconds)
    app.state.health_installed = True
    logger = get_logger()

    @app.get('/healthz')
    async def healthz() -> JSONResponse:
        probes: HealthProbes = getattr(app.state, 'health_probes', None) or build_probes(app, settings)
        failed = await _dependency_failures(probes)
        if failed:
            log_event(logger, 30, event='health', endpoint='/healthz', status='unavailable', error_kind='+'.join(failed))
            raise ServiceFailureError(UNAVAILABLE_MESSAGE)
        return JSONResponse(dict(OK_BODY))
