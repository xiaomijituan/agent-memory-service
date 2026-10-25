from __future__ import annotations
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from pydantic_settings import BaseSettings, SettingsConfigDict
EMBEDDING_MODES = ('api', 'off')
AUTH_SCHEMES = ('token', 'bearer', 'api_key', 'none')
ENV_FILE_OVERRIDE = 'AMC_ENV_FILE'
REPO_ROOT = Path(__file__).resolve().parents[1]

class ConfigError(RuntimeError):
    pass

def env_file_path(environ: Mapping[str, str] | None=None) -> Path:
    env = os.environ if environ is None else environ
    override = (env.get(ENV_FILE_OVERRIDE) or '').strip()
    return Path(override) if override else REPO_ROOT / '.env'

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='AMC_', env_file=str(REPO_ROOT / '.env'), extra='ignore')
    service_name: str = 'amc-memory'
    version: str = 'v1.0'
    memory_system_key: str = ''
    auth_scheme: str = 'token'
    db_dir: str = 'data'
    max_open_dbs: int = 32
    add_concurrency: int = 8
    search_concurrency: int = 2
    admission_wait_seconds: float = 10.0
    request_timeout_seconds: int = 1800
    shape_probe: str = 'off'
    embedding_mode: str = ''
    embedding_provider: str = 'api'
    embedding_base_url: str = ''
    embedding_model: str = ''
    embedding_api_key: str = ''
    embedding_dimensions: int = 1024
    embedding_batch_size: int = 10
    fusion_weight_lexical: float = 1.0
    fusion_weight_vector: float = 1.0
    chunk_window_words: int = 320
    chunk_overlap_words: int = 40
    chunk_stride_words: int = 280

    def __init__(self, **values: Any) -> None:
        values.setdefault('_env_file', env_file_path())
        super().__init__(**values)

    def validate(self) -> None:
        problems: list[str] = []
        if self.embedding_mode not in EMBEDDING_MODES:
            problems.append("embedding_mode 必须显式设为 'api' 或 'off'" + (f'（当前 {self.embedding_mode!r}）' if self.embedding_mode else '（当前未设置）'))
        elif self.embedding_mode == 'api':
            missing = [name for name in ('embedding_base_url', 'embedding_model', 'embedding_api_key') if not getattr(self, name)]
            if missing:
                problems.append("embedding_mode='api' 需要这些字段：" + ', '.join(missing))
        if self.auth_scheme not in AUTH_SCHEMES:
            problems.append(f"auth_scheme 必须是 {'/'.join(AUTH_SCHEMES)} 之一（当前 {self.auth_scheme!r}）")
        elif self.auth_scheme != 'none' and (not self.memory_system_key):
            problems.append(f"auth_scheme={self.auth_scheme!r} 需要 memory_system_key；无鉴权只允许 auth_scheme='none'")
        if self.embedding_mode == 'api' and self.embedding_dimensions <= 0:
            problems.append('embedding_dimensions 必须为正整数')
        if self.add_concurrency < 1 or self.search_concurrency < 1:
            problems.append('add_concurrency 与 search_concurrency 必须至少为 1')
        if self.admission_wait_seconds < 0:
            problems.append('admission_wait_seconds 不得为负（0 表示超额立刻 429）')
        if problems:
            raise ConfigError('; '.join(problems))
