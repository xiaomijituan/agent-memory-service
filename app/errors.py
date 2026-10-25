from __future__ import annotations
from typing import Any, Iterable
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

class ApiError(Exception):
    status_code: int = 500
    code: str = 'internal_error'

    def __init__(self, message: str='', *, fields: Iterable[str]=(), retry_after: int | None=None) -> None:
        self.message = message or self.code
        self.fields = [f for f in fields if f]
        self.retry_after = retry_after
        super().__init__(message)

class InvalidPayloadError(ApiError):
    status_code = 400
    code = 'invalid_payload'

class ResourceNotFoundError(ApiError):
    status_code = 404
    code = 'not_found'

class ConflictError(ApiError):
    status_code = 409
    code = 'conflict'

class ScopeDeniedError(ApiError):
    status_code = 403
    code = 'scope_denied'

class RequestTimeoutError(ApiError):
    status_code = 408
    code = 'request_timeout'

class TooEarlyError(ApiError):
    status_code = 425
    code = 'too_early'

class RateLimitedError(ApiError):
    status_code = 429
    code = 'rate_limited'

    def __init__(self, message: str='', *, retry_after: int=1, fields: Iterable[str]=()) -> None:
        if retry_after < 1:
            retry_after = 1
        super().__init__(message, fields=fields, retry_after=retry_after)

class ServiceFailureError(ApiError):
    status_code = 503
    code = 'service_unavailable'

def _request_id(request: Request) -> str | None:
    return getattr(getattr(request, 'state', None), 'request_id', None)

def error_payload(request: Request, code: str, message: str, fields: list[str] | None=None) -> dict[str, Any]:
    payload: dict[str, Any] = {'error': code, 'message': message}
    if fields:
        payload['fields'] = fields
    rid = _request_id(request)
    if rid:
        payload['request_id'] = rid
    return payload

def _render(request: Request, exc: ApiError) -> JSONResponse:
    response = JSONResponse(status_code=exc.status_code, content=error_payload(request, exc.code, exc.message, exc.fields))
    if isinstance(exc, RateLimitedError) and exc.retry_after is not None:
        response.headers['Retry-After'] = str(exc.retry_after)
    return response

def _field_path(loc: Iterable[Any]) -> str:
    parts = [str(p) for p in loc if p not in ('body', 'query', 'path')]
    return '.'.join(parts) or 'payload'

async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return _render(request, exc)

async def _request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields: list[str] = []
    problems: list[str] = []
    for err in exc.errors():
        path = _field_path(err.get('loc', ()))
        fields.append(path)
        problems.append(f"{path}: {err.get('msg', 'invalid')}")
    message = '请求载荷非法，缺少必填字段或类型不符：' + '; '.join(problems)
    response = JSONResponse(status_code=422, content=error_payload(request, 'invalid_payload', message, sorted(set(fields))))
    return response

async def _pydantic_validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
    fields = [_field_path(err['loc']) for err in exc.errors()]
    message = '载荷字段校验失败：' + '; '.join((f'{f}' for f in fields))
    return JSONResponse(status_code=422, content=error_payload(request, 'invalid_payload', message, sorted(set(fields))))

async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: 'not_found', 405: 'method_not_allowed'}.get(exc.status_code, 'http_error')
    message = str(exc.detail) if exc.detail else f'HTTP {exc.status_code}'
    response = JSONResponse(status_code=exc.status_code, content=error_payload(request, code, message))
    headers = getattr(exc, 'headers', None)
    if headers:
        for key, value in headers.items():
            response.headers[key] = value
    return response

async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content=error_payload(request, 'internal_error', '服务内部故障，请凭 request_id 联系维护方'))

def install(app: FastAPI, settings: Any=None) -> None:
    state = getattr(app, 'state', None)
    if state is not None and getattr(state, 'errors_installed', False):
        return
    for error_class in (InvalidPayloadError, ResourceNotFoundError, ConflictError, ScopeDeniedError, RequestTimeoutError, TooEarlyError, RateLimitedError, ServiceFailureError):
        app.add_exception_handler(error_class, _api_error_handler)
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(ValidationError, _pydantic_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unexpected_error_handler)
    if state is not None:
        state.errors_installed = True
