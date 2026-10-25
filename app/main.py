from __future__ import annotations
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from app import auth, errors, health, request_id, shape_probe, versioning
from app.api import add as add_api
from app.api import search as search_api
from app.config import Settings
from app.logging import get_logger, log_event
from app.store import Store

def create_app(**settings_overrides) -> FastAPI:
    settings = Settings(**settings_overrides)
    settings.validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings)
        app.state.store = store
        revoked_claims = store.revoke_leftover_claims()
        log_event(get_logger(), 20, event='startup_revoke', result_count=revoked_claims)
        try:
            yield
        finally:
            store.close()
    app = FastAPI(title=settings.service_name, version=settings.version, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    logger = get_logger()
    errors.install(app, settings)
    auth.install(app, settings)
    request_id.install(app, settings)
    versioning.install(app, settings)
    add_api.install(app, settings)
    search_api.install(app, settings)

    @app.middleware('http')
    async def log_request(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        log_event(logger, 20, event='request', method=request.method, endpoint=request.url.path, status=response.status_code, duration_ms=round((time.perf_counter() - started) * 1000, 2), version=settings.version)
        return response
    health.install(app, settings)
    shape_probe.install(app, settings)
    return app
