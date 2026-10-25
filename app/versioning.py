from __future__ import annotations
from typing import Any
from fastapi import FastAPI
from starlette.responses import JSONResponse
HEADER = 'X-Memory-Version'
DEFAULT_CHUNK_WINDOW_WORDS = 320
DEFAULT_CHUNK_OVERLAP_WORDS = 40
DEFAULT_CHUNK_STRIDE_WORDS = 280
DEFAULT_FUSION_WEIGHT_LEXICAL = 1.0
DEFAULT_FUSION_WEIGHT_VECTOR = 1.0

def _fmt_number(value: Any) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f'{number:.6f}'.rstrip('0')

def _get(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, default)
    return default if value in (None, '') else value

def components(settings: Any) -> dict[str, str]:
    mode = str(_get(settings, 'embedding_mode', 'off'))
    if mode == 'api':
        provider = str(_get(settings, 'embedding_provider', 'api'))
        model = str(_get(settings, 'embedding_model', 'unknown'))
        dims = _fmt_number(_get(settings, 'embedding_dimensions', 1024))
    else:
        provider, model, dims = ('off', 'off', '0')
    return {'service': str(_get(settings, 'version', 'v0')), 'provider': provider, 'emb_model': model, 'emb_dims': dims, 'w_lex': _fmt_number(_get(settings, 'fusion_weight_lexical', DEFAULT_FUSION_WEIGHT_LEXICAL)), 'w_vec': _fmt_number(_get(settings, 'fusion_weight_vector', DEFAULT_FUSION_WEIGHT_VECTOR)), 'chunk_win': _fmt_number(_get(settings, 'chunk_window_words', DEFAULT_CHUNK_WINDOW_WORDS)), 'chunk_overlap': _fmt_number(_get(settings, 'chunk_overlap_words', DEFAULT_CHUNK_OVERLAP_WORDS)), 'chunk_stride': _fmt_number(_get(settings, 'chunk_stride_words', DEFAULT_CHUNK_STRIDE_WORDS))}

def version_string(settings: Any) -> str:
    parts = components(settings)
    return '|'.join((f'{key}={value}' for key, value in parts.items()))

def install(app: FastAPI, settings: Any=None) -> None:
    settings = settings if settings is not None else getattr(app.state, 'settings', None)
    if settings is None:
        raise RuntimeError('versioning.install 需要 settings（或 app.state.settings 已就绪）')
    if getattr(app.state, 'versioning_installed', False):
        return
    fingerprint = version_string(settings)
    app.state.version_string = fingerprint

    @app.get('/version')
    def read_version() -> JSONResponse:
        return JSONResponse({'version': fingerprint, 'components': components(settings)})

    @app.middleware('http')
    async def attach_version_header(request, call_next):
        response = await call_next(request)
        response.headers[HEADER] = fingerprint
        return response
    app.state.versioning_installed = True
