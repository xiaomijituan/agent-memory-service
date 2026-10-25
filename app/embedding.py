from __future__ import annotations
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
import httpx
import numpy as np
from app.logging import get_logger, log_event
_WHITESPACE = re.compile('\\s+')
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
EMBEDDINGS_PATH = '/embeddings'
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

class EmbeddingError(RuntimeError):
    pass

def _normalize_text(text: str) -> str:
    return _WHITESPACE.sub(' ', text).strip().casefold()

class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        ...

def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise EmbeddingError('向量全零，无法归一化')
    return vector / norm

class HashingEmbedder:

    def __init__(self, dimensions: int=1024) -> None:
        if dimensions <= 0:
            raise EmbeddingError('dimensions 必须为正整数')
        self.dimensions = dimensions

    def _features(self, text: str) -> list[str]:
        normalized = _normalize_text(text)
        if not normalized:
            raise EmbeddingError('空白文本不可向量化')
        features = normalized.split(' ')
        features += [normalized[index:index + 2] for index in range(len(normalized) - 1)]
        return features

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        vectors = []
        for text in texts:
            accumulator = np.zeros(self.dimensions, dtype=np.float32)
            for feature in self._features(text):
                digest = hashlib.blake2b(feature.encode('utf-8'), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], 'big') % self.dimensions
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                accumulator[bucket] += sign
            vectors.append(_unit(accumulator))
        return vectors

@dataclass(frozen=True)
class ProviderRetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    factor: float = 2.0
    max_delay: float = 30.0
    total_sleep_budget_seconds: float = 60.0
    deadline_seconds: float = 600.0

    def scheduled_delay(self, attempt: int) -> float:
        return self.base_delay * self.factor ** max(attempt - 1, 0)

    def delay_for(self, attempt: int, retry_after: float | None=None) -> tuple[float, bool]:
        delay = self.scheduled_delay(attempt)
        if retry_after is not None:
            delay = max(delay, float(retry_after))
        capped = delay > self.max_delay
        return (self.max_delay if capped else delay, capped)

    def snapshot(self) -> dict[str, Any]:
        return {'max_attempts': self.max_attempts, 'base_delay': self.base_delay, 'factor': self.factor, 'max_delay': self.max_delay, 'total_sleep_budget_seconds': self.total_sleep_budget_seconds, 'deadline_seconds': self.deadline_seconds, 'jitter': False}
DEFAULT_RETRY_POLICY = ProviderRetryPolicy()

def make_probe_policy(deadline_seconds: float) -> ProviderRetryPolicy:
    return ProviderRetryPolicy(max_attempts=1, base_delay=0.0, factor=1.0, max_delay=0.0, total_sleep_budget_seconds=0.0, deadline_seconds=float(deadline_seconds))

def _monotonic() -> float:
    return time.monotonic()

class _RetryBudget:
    __slots__ = ('_sleep_remaining', '_deadline_at', '_attempt_seconds')

    def __init__(self, policy: ProviderRetryPolicy, attempt_seconds: float, started_at: float) -> None:
        self._sleep_remaining = float(policy.total_sleep_budget_seconds)
        self._deadline_at = started_at + float(policy.deadline_seconds)
        self._attempt_seconds = float(attempt_seconds)

    def reservable(self, delay: float, now: float) -> bool:
        if delay > self._sleep_remaining:
            return False
        return now + delay + self._attempt_seconds <= self._deadline_at

    def spend(self, delay: float) -> None:
        self._sleep_remaining -= delay

@dataclass
class EmbeddingStats:
    batches: int = 0
    ok_batches: int = 0
    attempts: int = 0
    retries: int = 0
    slept_seconds: float = 0.0
    timeouts: int = 0
    rate_limited: int = 0
    server_errors: int = 0
    not_retried: int = 0
    prompt_tokens: int = 0
    total_tokens: int = 0
    usage_missing: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record_batch(self) -> None:
        with self._lock:
            self.batches += 1

    def record_ok_batch(self) -> None:
        with self._lock:
            self.ok_batches += 1

    def record_attempt(self) -> None:
        with self._lock:
            self.attempts += 1

    def record_retry(self, slept: float) -> None:
        with self._lock:
            self.retries += 1
            self.slept_seconds = round(self.slept_seconds + slept, 3)

    def record_timeout(self) -> None:
        with self._lock:
            self.timeouts += 1

    def record_unavailable_status(self, status: int) -> None:
        with self._lock:
            if status == 429:
                self.rate_limited += 1
            else:
                self.server_errors += 1

    def record_not_retried(self) -> None:
        with self._lock:
            self.not_retried += 1

    def record_usage(self, counts: tuple[int, int] | None) -> None:
        with self._lock:
            if counts is None:
                self.usage_missing += 1
                return
            self.prompt_tokens += counts[0]
            self.total_tokens += counts[1]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {'batches': self.batches, 'ok_batches': self.ok_batches, 'attempts': self.attempts, 'retries': self.retries, 'slept_seconds': self.slept_seconds, 'timeouts': self.timeouts, 'rate_limited': self.rate_limited, 'server_errors': self.server_errors, 'not_retried': self.not_retried, 'prompt_tokens': self.prompt_tokens, 'total_tokens': self.total_tokens, 'usage_missing': self.usage_missing}

class _RetryableProviderFailure(Exception):

    def __init__(self, *, kind: str, detail: str, status: int | None=None, retry_after: float | None=None) -> None:
        self.kind = kind
        self.detail = detail
        self.status = status
        self.retry_after = retry_after
        super().__init__(detail)

def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get('Retry-After')
    if isinstance(raw, str) and raw.strip().isdigit():
        return float(int(raw.strip()))
    return None

def _usage_counts(payload: Mapping[str, Any]) -> tuple[int, int] | None:
    usage = payload.get('usage')
    if not isinstance(usage, Mapping):
        return None

    def as_count(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(int(value), 0)
    return (as_count(usage.get('prompt_tokens')), as_count(usage.get('total_tokens')))
_logger = get_logger()

class HttpEmbedder:

    def __init__(self, base_url: str, model: str, api_key: str, dimensions: int=1024, batch_size: int=10, timeout: float=DEFAULT_REQUEST_TIMEOUT_SECONDS, transport: httpx.BaseTransport | None=None, *, retry_policy: ProviderRetryPolicy | None=None) -> None:
        if not (base_url and model and api_key):
            raise EmbeddingError('api 模式需要 base_url、model 与 api_key 三项齐全')
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.dimensions = dimensions
        self.batch_size = max(1, batch_size)
        self._api_key = api_key
        self._request_timeout_seconds = timeout
        self._retry_policy = DEFAULT_RETRY_POLICY if retry_policy is None else retry_policy
        self.stats = EmbeddingStats()
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, headers={'Authorization': f'Bearer {api_key}'}, transport=transport)

    def close(self) -> None:
        self._client.close()

    def embed(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        budget = _RetryBudget(self._retry_policy, self._request_timeout_seconds, _monotonic())
        vectors: list[np.ndarray] = []
        for start in range(0, len(texts), self.batch_size):
            batch = [_normalize_text(t) for t in texts[start:start + self.batch_size]]
            if any((not t for t in batch)):
                raise EmbeddingError('批次中含空白文本')
            self.stats.record_batch()
            vectors.extend(self._request(batch, budget))
        return vectors

    def _request(self, batch: list[str], budget: _RetryBudget) -> list[np.ndarray]:
        policy = self._retry_policy
        started = time.perf_counter()
        attempt = 0
        while True:
            attempt += 1
            self.stats.record_attempt()
            try:
                vectors, status, usage = self._send_once(batch)
            except _RetryableProviderFailure as failure:
                if attempt >= policy.max_attempts:
                    raise EmbeddingError(f'embedding 提供方在 {attempt} 次尝试后仍不可用：{failure.detail}') from None
                delay, _capped = policy.delay_for(attempt, failure.retry_after)
                if not budget.reservable(delay, _monotonic()):
                    raise EmbeddingError(f'退避预算已用尽（睡眠 {policy.total_sleep_budget_seconds:g} 秒 / 整轮 {policy.deadline_seconds:g} 秒），第 {attempt} 次之后不再重发：{failure.detail}') from None
                budget.spend(delay)
                self.stats.record_retry(delay)
                log_event(_logger, logging.WARNING, event='embedding_retry', endpoint=EMBEDDINGS_PATH, status=failure.status, error_kind=failure.kind, duration_ms=round(delay * 1000.0, 2))
                time.sleep(delay)
                continue
            self.stats.record_ok_batch()
            self.stats.record_usage(usage)
            log_event(_logger, logging.INFO, event='embedding', endpoint=EMBEDDINGS_PATH, status=status, duration_ms=round((time.perf_counter() - started) * 1000.0, 2), result_count=None if usage is None else usage[1])
            return vectors

    def _send_once(self, batch: list[str]) -> tuple[list[np.ndarray], int, tuple[int, int] | None]:
        try:
            response = self._client.post(EMBEDDINGS_PATH, json={'model': self.model, 'input': batch, 'dimensions': self.dimensions})
        except httpx.TimeoutException as exc:
            self.stats.record_timeout()
            raise _RetryableProviderFailure(kind=type(exc).__name__, detail=f'超时 {type(exc).__name__}') from None
        except httpx.HTTPError as exc:
            self.stats.record_not_retried()
            raise EmbeddingError(f'embedding 提供方不可达：{type(exc).__name__}') from None
        status = response.status_code
        if status // 100 != 2:
            if status in RETRYABLE_STATUS:
                self.stats.record_unavailable_status(status)
                raise _RetryableProviderFailure(kind=f'http_{status}', detail=f'HTTP {status}', status=status, retry_after=_retry_after_seconds(response))
            self.stats.record_not_retried()
            raise EmbeddingError(f'embedding 提供方返回 {status}')
        try:
            payload = response.json()
        except ValueError:
            self.stats.record_not_retried()
            raise EmbeddingError('embedding 响应体不是 JSON') from None
        if not isinstance(payload, Mapping):
            self.stats.record_not_retried()
            raise EmbeddingError('embedding 响应体不是 JSON 对象')
        usage = _usage_counts(payload)
        data = payload.get('data')
        if not isinstance(data, list) or len(data) != len(batch):
            self.stats.record_not_retried()
            raise EmbeddingError(f'embedding 返回 {(0 if not isinstance(data, list) else len(data))} 条，应为 {len(batch)} 条')
        ordered = sorted(data, key=lambda item: item.get('index', 0) if isinstance(item, Mapping) else 0)
        vectors: list[np.ndarray] = []
        for position, item in enumerate(ordered):
            raw = item.get('embedding') if isinstance(item, Mapping) else None
            if not isinstance(raw, (list, tuple)):
                self.stats.record_not_retried()
                raise EmbeddingError(f'字段 data[{position}].embedding 缺失或不是列表')
            try:
                array = np.asarray(raw, dtype=np.float32)
            except (TypeError, ValueError):
                self.stats.record_not_retried()
                raise EmbeddingError(f'字段 data[{position}].embedding 不是数值列表') from None
            if array.ndim != 1 or array.size != self.dimensions:
                self.stats.record_not_retried()
                raise EmbeddingError(f'第 {position} 条向量长度 {array.size}，应为 {self.dimensions}')
            vectors.append(_unit(array))
        return (vectors, status, usage)

def make_embedder(mode: str, dimensions: int=1024, base_url: str='', model: str='', api_key: str='', batch_size: int=10, *, timeout: float=DEFAULT_REQUEST_TIMEOUT_SECONDS, retry_policy: ProviderRetryPolicy | None=None, transport: httpx.BaseTransport | None=None) -> Embedder:
    if mode == 'off':
        return HashingEmbedder(dimensions=dimensions)
    if mode == 'api':
        return HttpEmbedder(base_url=base_url, model=model, api_key=api_key, dimensions=dimensions, batch_size=batch_size, timeout=timeout, retry_policy=retry_policy, transport=transport)
    raise EmbeddingError(f"未知 embedding_mode：{mode!r}（只接受 'api' 或 'off'）")
