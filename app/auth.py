from __future__ import annotations
import secrets
from typing import Any
from fastapi import FastAPI
from starlette.responses import JSONResponse
PROTECTED_PREFIX = '/v1/memories/'
_SCHEME_PREFIXES = {'token': 'Token', 'bearer': 'Bearer'}

def _extract_credential(request, scheme: str) -> str | None:
    if scheme == 'api_key':
        value = request.headers.get('x-api-key', '')
    else:
        raw = request.headers.get('authorization', '')
        prefix = _SCHEME_PREFIXES[scheme]
        parts = raw.split(' ', 1)
        if len(parts) != 2 or parts[0].lower() != prefix.lower():
            return None
        value = parts[1].strip()
    return value or None

def credential_matches(request, settings: Any) -> bool:
    provided = _extract_credential(request, settings.auth_scheme)
    if provided is None or not settings.memory_system_key:
        return False
    return secrets.compare_digest(provided, settings.memory_system_key)

def _unauthorized(request) -> JSONResponse:
    body: dict[str, Any] = {'error': 'unauthorized', 'message': '凭据缺失或不匹配：请按配置的鉴权方案携带服务密钥'}
    rid = getattr(getattr(request, 'state', None), 'request_id', None)
    if rid:
        body['request_id'] = rid
    return JSONResponse(status_code=401, content=body)

def install(app: FastAPI, settings: Any=None) -> None:
    settings = settings if settings is not None else getattr(app.state, 'settings', None)
    if settings is None:
        raise RuntimeError('auth.install 需要 settings（或 app.state.settings 已就绪）')
    if getattr(app.state, 'auth_installed', False):
        return

    @app.middleware('http')
    async def authenticate(request, call_next):
        if settings.auth_scheme == 'none' or not request.url.path.startswith(PROTECTED_PREFIX):
            return await call_next(request)
        if not credential_matches(request, settings):
            return _unauthorized(request)
        return await call_next(request)
    app.state.auth_installed = True
