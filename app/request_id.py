from __future__ import annotations
import uuid
from typing import Any
from fastapi import FastAPI
HEADER = 'X-Request-Id'
_ALLOWED_CHARS = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.,:')

def _sanitize(incoming: str) -> str:
    cleaned = ''.join((c for c in incoming if c in _ALLOWED_CHARS))[:128]
    return cleaned

def install(app: FastAPI, settings: Any=None) -> None:
    if getattr(app.state, 'request_id_installed', False):
        return

    @app.middleware('http')
    async def attach_request_id(request, call_next):
        rid = _sanitize(request.headers.get(HEADER, '').strip()) or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[HEADER] = rid
        return response
    app.state.request_id_installed = True
