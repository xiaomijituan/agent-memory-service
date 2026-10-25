from __future__ import annotations
import hashlib
import inspect
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Iterator, Literal, Sequence
import numpy as np
from app.config import Settings
from app.errors import ConflictError, InvalidPayloadError, ServiceFailureError
from app.participants import participant_names, participant_surface
__all__ = ['Claim', 'LEXICAL_STAMP_KEY', 'LexicalDerivationState', 'LexicalHit', 'MemoryRow', 'NewMemory', 'PurgeReport', 'Store', 'StoredResponse', 'StoreHealth', 'VectorRow', 'classify_lexical_derivation', 'derivation_source_surface', 'has_any_memory', 'lexical_derivation_stamp', 'read_lexical_stamp', 'scan_lexical_rows', 'split_for_fts']
_TOKEN_RE = re.compile('(?P<LATIN>[A-Za-z0-9]+)|(?P<CJK>[\\u3400-\\u4DBF\\u4E00-\\u9FFF\\uF900-\\uFAFF\\u3040-\\u30FF]+)')
_CJK_GROUP = 'CJK'
_LATIN_GROUP = 'LATIN'

def split_for_fts(text: str) -> str:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        piece = match.group()
        if match.lastgroup == _LATIN_GROUP:
            tokens.append(piece.lower())
        elif len(piece) == 1:
            tokens.append(piece)
        else:
            tokens.extend((piece[index:index + 2] for index in range(len(piece) - 1)))
    return ' '.join(tokens)

def _index_text(content: str, speaker: str | None, participants: str | None=None) -> str:
    head = f'{speaker}: ' if speaker else ''
    extra = f'{participants}: ' if participants else ''
    return f'{head}{extra}{content}' if head or extra else content

def _participant_surface(contents: Sequence[str]) -> str:
    return participant_surface(participant_names(contents))
_LEXICAL_STAMP_CALLABLES: tuple[str, ...] = ('split_for_fts', '_index_text')
_LEXICAL_STAMP_DATA: tuple[str, ...] = ('_TOKEN_RE', '_CJK_GROUP', '_LATIN_GROUP')
LEXICAL_STAMP_KEY = 'lexical_derivation_stamp'
'`db_meta.key`：这份库的 `fts_tokens` 是哪一条派生规则产出来的。与 `schema_version`/`user_id` 同表同形状。'

def _normalize_source_text(text: str) -> str:
    return '\n'.join((line.strip() for line in text.strip().splitlines() if line.strip()))

def derivation_source_surface() -> tuple[tuple[str, str], ...]:
    namespace = globals()
    surface: list[tuple[str, str]] = [(name, inspect.getsource(namespace[name])) for name in _LEXICAL_STAMP_CALLABLES]
    surface.extend(((name, repr(namespace[name])) for name in _LEXICAL_STAMP_DATA))
    return tuple(surface)

def lexical_derivation_stamp(surface: Sequence[tuple[str, str]] | None=None) -> str:
    if surface is not None:
        return _digest_lexical_surface(tuple(surface))
    global _LEXICAL_STAMP_CACHE
    if _LEXICAL_STAMP_CACHE is None:
        _LEXICAL_STAMP_CACHE = _digest_lexical_surface(derivation_source_surface())
    return _LEXICAL_STAMP_CACHE
_LEXICAL_STAMP_CACHE: str | None = None

def _digest_lexical_surface(pairs: Sequence[tuple[str, str]]) -> str:
    joined = '\x00'.join((f'{name}\x01{_normalize_source_text(text)}' for name, text in pairs))
    return 'sha256:' + hashlib.sha256(joined.encode('utf-8')).hexdigest()
LexicalDerivationStatus = Literal['consistent', 'stale', 'unstamped']

def classify_lexical_derivation(stored_stamp: str | None, current_stamp: str, has_rows: bool) -> LexicalDerivationStatus:
    if not has_rows:
        return 'consistent'
    if stored_stamp is None:
        return 'unstamped'
    return 'consistent' if stored_stamp == current_stamp else 'stale'

def read_lexical_stamp(conn: sqlite3.Connection) -> str | None:
    row = conn.execute('SELECT value FROM db_meta WHERE key = ?', (LEXICAL_STAMP_KEY,)).fetchone()
    return None if row is None else str(row[0])

def has_any_memory(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute('SELECT EXISTS(SELECT 1 FROM memories)').fetchone()[0])

def scan_lexical_rows(conn: sqlite3.Connection) -> tuple[int, int]:
    stored = conn.execute('SELECT content, speaker, fts_tokens FROM memories').fetchall()
    participants = _participant_surface([row[0] for row in stored])
    rows = 0
    stale = 0
    for content, speaker, tokens in stored:
        rows += 1
        if split_for_fts(_index_text(content, speaker, participants)) != tokens:
            stale += 1
    return (rows, stale)

@dataclass(frozen=True)
class LexicalDerivationState:
    user_id: str | None
    db_path: str
    stored_stamp: str | None
    current_stamp: str
    rows: int | None
    has_rows: bool
    status: LexicalDerivationStatus
    stale_rows: int | None
_QUERY_FUNCTION_CHARS: frozenset[str] = frozenset('的了了吗呢吧啊呀哦嘛么哈是也就还又并且而但因为或把被让给对从到向于之以使说候样些我你他她它们咱您这那其此该谁什么哪怎样啥不没有要')
_QUERY_FUNCTION_WORDS: frozenset[str] = frozenset('what when where who whom whose which how why is are was were be been being do does did done have has had having will would can could should shall may might must my me mine your you yours his her hers it its our we us their they them this that these those there here of to for in on at by with from and or but as if than the a an some any no not very'.split())

def _is_function_query_token(token: str) -> bool:
    if token.isascii():
        return token in _QUERY_FUNCTION_WORDS
    return any((char in _QUERY_FUNCTION_CHARS for char in token))

def _fts_match_string(query: str) -> str:
    tokens = split_for_fts(query).split()
    content = [token for token in tokens if not _is_function_query_token(token)]
    return ' '.join(content if content else tokens)
_BRIDGE_RESCUE_MIN_REMAINING = 2
_BRIDGE_RESCUE_MAX_TOKENS = 30

def _fts_bridge_retry_strings(query: str) -> list[str]:
    tokens = split_for_fts(query).split()
    if not tokens or len(tokens) > _BRIDGE_RESCUE_MAX_TOKENS:
        return []
    content = [token for token in tokens if not _is_function_query_token(token)]
    if not content or len(content) == len(tokens):
        return []
    if len(content) < _BRIDGE_RESCUE_MIN_REMAINING + 1:
        return []
    return [' '.join(content[:index] + content[index + 1:]) for index in range(len(content))]
_RELAX_MIN_GROUPS = 2
_RELAX_COVERAGE_NUMERATOR = 1
_RELAX_COVERAGE_DENOMINATOR = 2
_RELAX_SCAN_FACTOR = 8
_INFLECTION_MAX_VARIANTS = 16
_INFLECTION_STRIP = ('ies', 'ing', 'ies', 'es', 'ed', 'ly', 's', 'd')
_INFLECTION_ADD = ('s', 'es', 'ed', 'ing')
_VOWELS = 'aeiou'

def _stem_bases(token: str) -> set[str]:
    bases = {token}
    for suffix in _INFLECTION_STRIP:
        if not token.endswith(suffix):
            continue
        base = token[:-len(suffix)]
        if len(base) < 2:
            continue
        bases.add(base)
        if not base.endswith('i'):
            bases.add(base + 'e')
        if suffix == 'ies':
            bases.add(base + 'y')
    return bases

def _inflection_variants(token: str) -> tuple[str, ...]:
    if not token.isascii() or not token.isalpha() or len(token) < 3:
        return (token,)
    forms: set[str] = set()
    for base in _stem_bases(token):
        forms.add(base)
        for suffix in _INFLECTION_ADD:
            forms.add(base + suffix)
        if base.endswith('y') and len(base) > 2 and (base[-2] not in _VOWELS):
            forms.update({base[:-1] + 'ies', base[:-1] + 'ied'})
        if base.endswith('e'):
            forms.update({base[:-1] + 'ing', base + 'd'})
        if len(base) >= 3 and base[-1] not in _VOWELS and (base[-2] in _VOWELS) and (base[-3] not in _VOWELS):
            forms.update({base + base[-1] + 'ing', base + base[-1] + 'ed'})
    ordered = sorted((form for form in forms if len(form) >= 2), key=lambda form: (len(form), form))
    return tuple(ordered[:_INFLECTION_MAX_VARIANTS])

def _fts_relaxation_groups(query: str) -> list[tuple[str, ...]]:
    tokens = split_for_fts(query).split()
    if not tokens or len(tokens) > _BRIDGE_RESCUE_MAX_TOKENS:
        return []
    content = [token for token in tokens if not _is_function_query_token(token)]
    if not content or len(content) == len(tokens):
        return []
    if not all((token.isascii() for token in content)):
        return []
    distinct: list[str] = []
    seen: set[str] = set()
    for token in content:
        if token not in seen:
            seen.add(token)
            distinct.append(token)
    if len(distinct) < _RELAX_MIN_GROUPS + 1:
        return []
    return [_inflection_variants(token) for token in distinct]

def _coverage_sql(groups: int) -> str:
    one = 'SELECT rowid AS rid, 1 AS hit FROM memories_fts WHERE memories_fts MATCH ?'
    return 'SELECT rid, SUM(hit) FROM (' + ' UNION ALL '.join((one for _ in range(groups))) + ') GROUP BY rid HAVING SUM(hit) >= ?'

def _lexical_english_relaxation(conn: sqlite3.Connection, query: str, limit: int, user_id: str) -> list:
    groups = _fts_relaxation_groups(query)
    if not groups:
        return []
    matches = [' OR '.join(variants) if len(variants) > 1 else variants[0] for variants in groups]
    if any(("'" in item for item in matches)):
        return []
    required = max(_RELAX_MIN_GROUPS, (len(matches) * _RELAX_COVERAGE_NUMERATOR + _RELAX_COVERAGE_DENOMINATOR - 1) // _RELAX_COVERAGE_DENOMINATOR)
    if required > len(matches):
        return []
    fetched = conn.execute(_LEXICAL_SQL, (' OR '.join(matches), limit * _RELAX_SCAN_FACTOR, user_id)).fetchall()
    if not fetched:
        return []
    eligible = {int(row[0]) for row in conn.execute(_coverage_sql(len(matches)), tuple(matches) + (required,)).fetchall()}
    return [row for row in fetched if int(row[0]) in eligible][:limit]
_USER_DDL: tuple[str, ...] = ('\n    CREATE TABLE IF NOT EXISTS db_meta(\n      key   TEXT PRIMARY KEY,\n      value TEXT NOT NULL\n    )\n    ', '\n    CREATE TABLE IF NOT EXISTS memories(\n      id              INTEGER PRIMARY KEY AUTOINCREMENT,\n      user_id         TEXT    NOT NULL,\n      session_id      TEXT    NOT NULL,\n      request_id      TEXT    NOT NULL,\n      msg_index       INTEGER NOT NULL,\n      chunk_index     INTEGER NOT NULL,\n      content         TEXT    NOT NULL,\n      fts_tokens      TEXT    NOT NULL,\n      vector          BLOB,\n      event_time      TEXT,\n      speaker         TEXT,\n      entities        TEXT,\n      attribute_slots TEXT,\n      ingested_at     TEXT    NOT NULL\n    )\n    ', 'CREATE INDEX IF NOT EXISTS idx_mem_session  ON memories(session_id, msg_index, chunk_index)', 'CREATE INDEX IF NOT EXISTS idx_mem_request  ON memories(request_id)', 'CREATE INDEX IF NOT EXISTS idx_mem_ingested ON memories(ingested_at)', "\n    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(\n      fts_tokens,\n      content='memories',\n      content_rowid='id',\n      tokenize='unicode61'\n    )\n    ", '\n    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN\n      INSERT INTO memories_fts(rowid, fts_tokens) VALUES (new.id, new.fts_tokens);\n    END\n    ', "\n    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN\n      INSERT INTO memories_fts(memories_fts, rowid, fts_tokens) VALUES('delete', old.id, old.fts_tokens);\n    END\n    ", "\n    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN\n      INSERT INTO memories_fts(memories_fts, rowid, fts_tokens) VALUES('delete', old.id, old.fts_tokens);\n      INSERT INTO memories_fts(rowid, fts_tokens) VALUES (new.id, new.fts_tokens);\n    END\n    ")
_GLOBAL_DDL: tuple[str, ...] = ("\n    CREATE TABLE IF NOT EXISTS request_responses(\n      request_id     TEXT PRIMARY KEY,\n      user_id        TEXT NOT NULL,\n      payload_sha256 TEXT NOT NULL,\n      response_bytes BLOB,\n      status         TEXT NOT NULL CHECK (status IN ('in_progress','done')),\n      claimed_at     TEXT NOT NULL,\n      completed_at   TEXT\n    )\n    ",)

@dataclass(frozen=True)
class NewMemory:
    session_id: str
    msg_index: int
    chunk_index: int
    content: str
    event_time: str | None = None
    speaker: str | None = None
    entities: str | None = None
    attribute_slots: str | None = None
    vector: np.ndarray | None = None

@dataclass(frozen=True)
class MemoryRow:
    id: int
    user_id: str
    session_id: str
    request_id: str
    msg_index: int
    chunk_index: int
    content: str
    event_time: str | None
    speaker: str | None
    entities: str | None
    attribute_slots: str | None
    ingested_at: str
    has_vector: bool

@dataclass(frozen=True)
class LexicalHit:
    row: MemoryRow
    rank: float

@dataclass(frozen=True)
class VectorRow:
    id: int
    session_id: str
    msg_index: int
    chunk_index: int
    vector: np.ndarray

@dataclass(frozen=True)
class StoredResponse:
    request_id: str
    user_id: str
    payload_sha256: str
    response_bytes: bytes | None
    status: Literal['in_progress', 'done']

@dataclass(frozen=True)
class Claim:
    status: Literal['fresh', 'replay', 'in_progress']
    stored: StoredResponse | None

@dataclass(frozen=True)
class StoreHealth:
    ok: bool
    global_db_ok: bool
    data_dir_writable: bool
    open_db_count: int
    failed_db_count: int

@dataclass(frozen=True)
class PurgeReport:
    dbs_scanned: int
    rows_deleted: int
    dbs_failed: int
_INGEST_INSERT_COLUMNS = 'user_id, session_id, request_id, msg_index, chunk_index, content, fts_tokens, vector, event_time, speaker, entities, attribute_slots, ingested_at'

@dataclass(frozen=True)
class _PreparedRow:
    user_id: str
    session_id: str
    request_id: str
    msg_index: int
    chunk_index: int
    content: str
    vector: bytes | None
    event_time: str | None
    speaker: str | None
    entities: str | None
    attribute_slots: str | None
    ingested_at: str

    def values(self, fts_tokens: str) -> tuple[object, ...]:
        return (self.user_id, self.session_id, self.request_id, self.msg_index, self.chunk_index, self.content, fts_tokens, self.vector, self.event_time, self.speaker, self.entities, self.attribute_slots, self.ingested_at)
_MEMORY_ROW_FIELDS = ('id', 'user_id', 'session_id', 'request_id', 'msg_index', 'chunk_index', 'content', 'event_time', 'speaker', 'entities', 'attribute_slots', 'ingested_at')
_ID_CHUNK = 400
_WINDOW_CHUNK = 100
_HEALTH_PROBE_NAME = 'health_probe.tmp'

def _memory_columns(alias: str='') -> str:
    prefix = f'{alias}.' if alias else ''
    columns = ', '.join((f'{prefix}{name}' for name in _MEMORY_ROW_FIELDS))
    return f'{columns}, ({prefix}vector IS NOT NULL)'

def _memory_row(values: Sequence[object]) -> MemoryRow:
    return MemoryRow(id=int(values[0]), user_id=values[1], session_id=values[2], request_id=values[3], msg_index=int(values[4]), chunk_index=int(values[5]), content=values[6], event_time=values[7], speaker=values[8], entities=values[9], attribute_slots=values[10], ingested_at=values[11], has_vector=bool(values[12]))
_LEXICAL_SQL = 'SELECT ' + _memory_columns('m') + ', matched.rank FROM (SELECT rowid AS rid, rank FROM memories_fts WHERE memories_fts MATCH ?       ORDER BY rank, rowid LIMIT ?) AS matched JOIN memories AS m ON m.id = matched.rid WHERE m.user_id = ? ORDER BY matched.rank, matched.rid'
_LOAD_VECTORS_SQL = 'SELECT id, session_id, msg_index, chunk_index, vector FROM memories WHERE vector IS NOT NULL AND user_id=? ORDER BY id'

def _lexical_bridge_retry(conn: sqlite3.Connection, query: str, limit: int, user_id: str) -> list:
    for candidate in _fts_bridge_retry_strings(query):
        rows = conn.execute(_LEXICAL_SQL, (candidate, limit, user_id)).fetchall()
        if rows:
            return rows
    return []

def _batches(items: Sequence[object], size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield list(items[start:start + size])

def _merged_msg_windows(seeds: Sequence[MemoryRow]) -> list[tuple[str, int, int]]:
    per_session: dict[str, list[int]] = {}
    for row in seeds:
        per_session.setdefault(row.session_id, []).append(row.msg_index)
    windows: list[tuple[str, int, int]] = []
    for session_id in sorted(per_session):
        indices = sorted(set(per_session[session_id]))
        low = high = indices[0]
        for index in indices[1:]:
            if index <= high + 2:
                high = index
                continue
            windows.append((session_id, low - 1, high + 1))
            low = high = index
        windows.append((session_id, low - 1, high + 1))
    return windows

def _utc_zulu(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _parse_zulu(value: str) -> datetime:
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise RuntimeError(f'库内时间戳格式不合法：{type(exc).__name__}') from None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidPayloadError(f'字段 {field_name} 必须是非空字符串', fields=[field_name])
    return value

class _UserDb:
    __slots__ = ('conn', 'conn_lock', 'refs')

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn_lock = threading.Lock()
        self.refs = 0

class Store:

    def __init__(self, settings: Settings, *, clock: Callable[[], datetime] | None=None) -> None:
        self._db_dir = os.path.abspath(str(settings.db_dir))
        self._users_dir = os.path.join(self._db_dir, 'users')
        self._max_open_dbs = max(1, int(settings.max_open_dbs))
        self._dimensions = int(settings.embedding_dimensions)
        self._open_wait_seconds = max(1, int(settings.request_timeout_seconds))
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now
        self._global_lock = threading.Lock()
        self._global_conn: sqlite3.Connection | None = None
        self._manage = threading.Condition(threading.Lock())
        self._handles: OrderedDict[str, _UserDb | None] = OrderedDict()
        self._closed = False
        self._failed_db_count = 0
        try:
            os.makedirs(self._users_dir, exist_ok=True)
            self._global_conn = self._new_connection(os.path.join(self._db_dir, 'global.db'))
            self._create_tables(self._global_conn, _GLOBAL_DDL)
        except (OSError, sqlite3.Error) as exc:
            self._failed_db_count += 1
            raise ServiceFailureError(_failure_message(exc)) from None

    def close(self) -> None:
        with self._manage:
            if self._closed:
                return
            self._closed = True
            handles = [handle for handle in self._handles.values() if handle is not None]
            self._handles.clear()
            self._manage.notify_all()
        for handle in handles:
            _quiet_close(handle.conn)
        with self._global_lock:
            if self._global_conn is not None:
                _quiet_close(self._global_conn)
                self._global_conn = None

    def claim_request(self, request_id: str, user_id: str, payload_sha256: str) -> Claim:
        request_id = _require_text(request_id, 'request_id')
        user_id = self._validated_user_id(user_id)
        payload_sha256 = _require_text(payload_sha256, 'payload_sha256')
        conn = self._global_connection()
        with _retryable_io(), self._global_lock:
            _begin_immediate(conn)
            try:
                row = conn.execute('SELECT user_id, payload_sha256, response_bytes, status, claimed_at FROM request_responses WHERE request_id=?', (request_id,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO request_responses (request_id, user_id, payload_sha256, response_bytes, status, claimed_at) VALUES (?, ?, ?, NULL, 'in_progress', ?)", (request_id, user_id, payload_sha256, self._stamp()))
                    conn.execute('COMMIT')
                    return Claim('fresh', None)
                stored_user_id, stored_sha, stored_bytes, stored_status, claimed_at = row
                if stored_user_id != user_id or stored_sha != payload_sha256:
                    raise ConflictError('同一 request_id 已按不同载荷声明过')
                if stored_status == 'done':
                    conn.execute('COMMIT')
                    stored = StoredResponse(request_id=request_id, user_id=stored_user_id, payload_sha256=stored_sha, response_bytes=stored_bytes, status='done')
                    return Claim('replay', stored)
                age = (self._moment() - _parse_zulu(claimed_at)).total_seconds()
                if age <= self._open_wait_seconds:
                    conn.execute('COMMIT')
                    return Claim('in_progress', None)
                conn.execute('UPDATE request_responses SET claimed_at=? WHERE request_id=?', (self._stamp(), request_id))
                conn.execute('COMMIT')
                return Claim('fresh', None)
            except BaseException:
                _quiet_rollback(conn)
                raise

    def finish_request(self, request_id: str, response_bytes: bytes) -> None:
        request_id = _require_text(request_id, 'request_id')
        if not isinstance(response_bytes, (bytes, bytearray)):
            raise InvalidPayloadError('字段 response_bytes 必须是字节串', fields=['response_bytes'])
        conn = self._global_connection()
        payload = bytes(response_bytes)
        with _retryable_io(), self._global_lock:
            _begin_immediate(conn)
            try:
                row = conn.execute('SELECT status FROM request_responses WHERE request_id=?', (request_id,)).fetchone()
                if row is None:
                    raise RuntimeError('协议不变量破坏：未 claim 的 request_id 不能 finish')
                if row[0] != 'in_progress':
                    raise RuntimeError('协议不变量破坏：request_id 已 done，不许二次 finish')
                conn.execute("UPDATE request_responses SET response_bytes=?, status='done', completed_at=? WHERE request_id=?", (payload, self._stamp(), request_id))
                conn.execute('COMMIT')
            except BaseException:
                _quiet_rollback(conn)
                raise

    def release_request(self, request_id: str, user_id: str | None=None, payload_sha256: str | None=None) -> bool:
        request_id = _require_text(request_id, 'request_id')
        if user_id is not None:
            user_id = self._validated_user_id(user_id)
        if payload_sha256 is not None:
            payload_sha256 = _require_text(payload_sha256, 'payload_sha256')
        conn = self._global_connection()
        with _retryable_io(), self._global_lock:
            _begin_immediate(conn)
            try:
                row = conn.execute('SELECT user_id, payload_sha256, status FROM request_responses WHERE request_id=?', (request_id,)).fetchone()
                if row is None or row[2] != 'in_progress':
                    conn.execute('COMMIT')
                    return False
                if user_id is not None and row[0] != user_id:
                    conn.execute('COMMIT')
                    return False
                if payload_sha256 is not None and row[1] != payload_sha256:
                    conn.execute('COMMIT')
                    return False
                conn.execute('DELETE FROM request_responses WHERE request_id=?', (request_id,))
                conn.execute('COMMIT')
                return True
            except BaseException:
                _quiet_rollback(conn)
                raise

    def revoke_leftover_claims(self) -> int:
        conn = self._global_connection()
        with _retryable_io(), self._global_lock:
            _begin_immediate(conn)
            try:
                cursor = conn.execute("DELETE FROM request_responses WHERE status='in_progress'")
                revoked = max(0, int(cursor.rowcount))
                conn.execute('COMMIT')
                return revoked
            except BaseException:
                _quiet_rollback(conn)
                raise

    def insert_memories(self, user_id: str, request_id: str, rows: Sequence[NewMemory]) -> list[int]:
        user_id = self._validated_user_id(user_id)
        request_id = _require_text(request_id, 'request_id')
        prepared = self._prepare_batch(rows, user_id, request_id)
        if not prepared:
            return []
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                _begin_immediate(handle.conn)
                try:
                    handle.conn.execute('DELETE FROM memories WHERE request_id=?', (request_id,))
                    next_index = {session_id: int(max_index) + 1 for session_id, max_index in handle.conn.execute('SELECT session_id, MAX(msg_index) FROM memories WHERE user_id=? GROUP BY session_id', (user_id,))}
                    if next_index:
                        prepared = sorted((replace(entry, msg_index=entry.msg_index + next_index.get(entry.session_id, 0)) for entry in prepared), key=lambda entry: (entry.msg_index, entry.chunk_index))
                    existing = handle.conn.execute('SELECT id, content, speaker, fts_tokens FROM memories WHERE user_id=? ORDER BY id', (user_id,)).fetchall()
                    participants = _participant_surface([row[1] for row in existing] + [entry.content for entry in prepared])
                    new_ids: list[int] = []
                    for entry in prepared:
                        cursor = handle.conn.execute(f'INSERT INTO memories ({_INGEST_INSERT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', entry.values(split_for_fts(_index_text(entry.content, entry.speaker, participants))))
                        new_ids.append(int(cursor.lastrowid))
                    for row_id, content, speaker, stored in existing:
                        tokens = split_for_fts(_index_text(content, speaker, participants))
                        if tokens != stored:
                            handle.conn.execute('UPDATE memories SET fts_tokens=? WHERE id=?', (tokens, int(row_id)))
                    handle.conn.execute('COMMIT')
                    return new_ids
                except BaseException:
                    _quiet_rollback(handle.conn)
                    raise
        finally:
            self._release(handle)

    def rebuild_lexical_index(self, user_id: str) -> int:
        user_id = self._validated_user_id(user_id)
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                _begin_immediate(handle.conn)
                try:
                    rows = handle.conn.execute('SELECT id, content, speaker FROM memories WHERE user_id=? ORDER BY id', (user_id,)).fetchall()
                    participants = _participant_surface([row[1] for row in rows])
                    for row_id, content, speaker in rows:
                        handle.conn.execute('UPDATE memories SET fts_tokens=? WHERE id=?', (split_for_fts(_index_text(content, speaker, participants)), int(row_id)))
                    handle.conn.execute('COMMIT')
                    return len(rows)
                except BaseException:
                    _quiet_rollback(handle.conn)
                    raise
        finally:
            self._release(handle)

    def lexical_derivation_state(self, user_id: str, *, count_stale_rows: bool=False) -> LexicalDerivationState:
        user_id = self._validated_user_id(user_id)
        key, bucket = self._db_name(user_id)
        path = os.path.join(self._users_dir, bucket, f'{key}.db')
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                stored = read_lexical_stamp(handle.conn)
                if count_stale_rows:
                    rows, stale = scan_lexical_rows(handle.conn)
                    has_rows = rows > 0
                else:
                    rows, stale = (None, None)
                    has_rows = has_any_memory(handle.conn)
        finally:
            self._release(handle)
        current = lexical_derivation_stamp()
        return LexicalDerivationState(user_id=user_id, db_path=path, stored_stamp=stored, current_stamp=current, rows=rows, has_rows=has_rows, status=classify_lexical_derivation(stored, current, has_rows), stale_rows=stale)

    def stamp_lexical_derivation(self, user_id: str) -> str:
        user_id = self._validated_user_id(user_id)
        stamp = lexical_derivation_stamp()
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                _begin_immediate(handle.conn)
                try:
                    handle.conn.execute('INSERT OR REPLACE INTO db_meta(key, value) VALUES (?, ?)', (LEXICAL_STAMP_KEY, stamp))
                    handle.conn.execute('COMMIT')
                except BaseException:
                    _quiet_rollback(handle.conn)
                    raise
        finally:
            self._release(handle)
        return stamp

    def lexical_search(self, user_id: str, query: str, limit: int) -> list[LexicalHit]:
        user_id = self._validated_user_id(user_id)
        if not isinstance(query, str):
            raise InvalidPayloadError('字段 query 必须是字符串', fields=['query'])
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise InvalidPayloadError('字段 limit 必须是整数', fields=['limit'])
        tokens = _fts_match_string(query)
        if not tokens or limit <= 0:
            return []
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                rows = handle.conn.execute(_LEXICAL_SQL, (tokens, limit, user_id)).fetchall()
                if not rows:
                    rows = _lexical_bridge_retry(handle.conn, query, limit, user_id)
                if not rows:
                    rows = _lexical_english_relaxation(handle.conn, query, limit, user_id)
        finally:
            self._release(handle)
        return [LexicalHit(row=_memory_row(values), rank=float(values[-1])) for values in rows]

    def load_vectors(self, user_id: str) -> list[VectorRow]:
        user_id = self._validated_user_id(user_id)
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                rows = handle.conn.execute(_LOAD_VECTORS_SQL, (user_id,)).fetchall()
        finally:
            self._release(handle)
        expected = 4 * self._dimensions
        loaded: list[VectorRow] = []
        for row_id, session_id, msg_index, chunk_index, blob in rows:
            if not isinstance(blob, (bytes, bytearray)) or len(blob) != expected:
                raise RuntimeError('库内不变量破坏：向量 BLOB 长度与 embedding_dimensions 不符')
            try:
                vector = np.frombuffer(blob, dtype='<f4')
            except (TypeError, ValueError):
                raise RuntimeError('库内不变量破坏：向量 BLOB 无法按 float32 小端解码') from None
            loaded.append(VectorRow(id=int(row_id), session_id=session_id, msg_index=int(msg_index), chunk_index=int(chunk_index), vector=vector))
        return loaded

    def fetch_by_ids(self, user_id: str, ids: Sequence[int]) -> list[MemoryRow]:
        user_id = self._validated_user_id(user_id)
        wanted = self._validated_ids(ids)
        if not wanted:
            return []
        found = self._rows_by_id(user_id, wanted)
        return [found[row_id] for row_id in wanted if row_id in found]

    def adjacent_rows(self, user_id: str, ids: Sequence[int]) -> list[MemoryRow]:
        user_id = self._validated_user_id(user_id)
        wanted = self._validated_ids(ids)
        if not wanted:
            return []
        seeds = self._rows_by_id(user_id, wanted)
        if not seeds:
            return []
        windows = _merged_msg_windows(list(seeds.values()))
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                rows: list[tuple] = []
                for group in _batches(windows, _WINDOW_CHUNK):
                    clauses = ' OR '.join(('(session_id=? AND msg_index BETWEEN ? AND ?)' for _ in group))
                    params: list[object] = [user_id]
                    for session_id, low, high in group:
                        params.extend((session_id, low, high))
                    rows.extend(handle.conn.execute(f'SELECT {_memory_columns()} FROM memories WHERE user_id=? AND ({clauses})', params).fetchall())
        finally:
            self._release(handle)
        excluded = set(wanted)
        neighbours = [_memory_row(values) for values in rows if int(values[0]) not in excluded]
        neighbours.sort(key=lambda row: (row.session_id, row.msg_index, row.chunk_index))
        return neighbours

    def health(self) -> StoreHealth:
        data_dir_writable = self._probe_data_dir_writable()
        global_db_ok = self._probe_global_db()
        with self._manage:
            open_db_count = sum((1 for handle in self._handles.values() if handle is not None))
            failed_db_count = self._failed_db_count
        return StoreHealth(ok=bool(global_db_ok and data_dir_writable), global_db_ok=bool(global_db_ok), data_dir_writable=bool(data_dir_writable), open_db_count=int(open_db_count), failed_db_count=int(failed_db_count))

    def purge_memories_before(self, cutoff_ingested_at: str) -> PurgeReport:
        cutoff = _require_text(cutoff_ingested_at, 'cutoff_ingested_at')
        dbs_scanned = 0
        rows_deleted = 0
        dbs_failed = 0
        for path in self._user_db_paths():
            dbs_scanned += 1
            try:
                rows_deleted += self._purge_one_user_db(path, cutoff)
            except (OSError, sqlite3.Error):
                dbs_failed += 1
                self._count_failed_open()
        return PurgeReport(dbs_scanned=dbs_scanned, rows_deleted=rows_deleted, dbs_failed=dbs_failed)

    def purge_responses_before(self, cutoff_claimed_at: str) -> int:
        cutoff = _require_text(cutoff_claimed_at, 'cutoff_claimed_at')
        conn = self._global_connection()
        with _retryable_io(), self._global_lock:
            _begin_immediate(conn)
            try:
                cursor = conn.execute('DELETE FROM request_responses WHERE claimed_at<?', (cutoff,))
                deleted = max(0, int(cursor.rowcount))
                conn.execute('COMMIT')
                return deleted
            except BaseException:
                _quiet_rollback(conn)
                raise

    def _moment(self) -> datetime:
        moment = self._clock()
        if not isinstance(moment, datetime):
            raise TypeError('注入的 clock 必须返回 datetime')
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _stamp(self) -> str:
        return _utc_zulu(self._moment())

    def _probe_data_dir_writable(self) -> bool:
        path = os.path.join(self._db_dir, _HEALTH_PROBE_NAME)
        try:
            with open(path, 'wb') as probe:
                probe.write(b'')
        except OSError:
            return False
        try:
            os.remove(path)
        except OSError:
            pass
        return True

    def _probe_global_db(self) -> bool:
        with self._global_lock:
            conn = self._global_conn
            if conn is None:
                return False
            try:
                conn.execute('SELECT COUNT(*) FROM request_responses').fetchone()
            except (sqlite3.Error, OSError, RuntimeError, ValueError):
                return False
            return True

    def _user_db_paths(self) -> list[str]:
        try:
            buckets = sorted(os.listdir(self._users_dir))
        except OSError:
            return []
        found: list[str] = []
        for bucket in buckets:
            bucket_dir = os.path.join(self._users_dir, bucket)
            try:
                names = sorted(os.listdir(bucket_dir))
            except OSError:
                continue
            found.extend((os.path.join(bucket_dir, name) for name in names if name.endswith('.db')))
        return found

    def _purge_one_user_db(self, path: str, cutoff: str) -> int:
        conn = self._new_connection(path)
        try:
            _begin_immediate(conn)
            try:
                cursor = conn.execute('DELETE FROM memories WHERE ingested_at<?', (cutoff,))
                deleted = max(0, int(cursor.rowcount))
                conn.execute('COMMIT')
                return deleted
            except BaseException:
                _quiet_rollback(conn)
                raise
        finally:
            _quiet_close(conn)

    def _validated_ids(self, ids: Sequence[int]) -> list[int]:
        if ids is None or isinstance(ids, (str, bytes, bytearray)):
            raise InvalidPayloadError('字段 ids 必须是整数序列', fields=['ids'])
        wanted: list[int] = []
        for value in ids:
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidPayloadError('字段 ids 必须是整数序列', fields=['ids'])
            wanted.append(int(value))
        return wanted

    def _rows_by_id(self, user_id: str, wanted: Sequence[int]) -> dict[int, MemoryRow]:
        handle = self._acquire(user_id)
        try:
            with _retryable_io(), handle.conn_lock:
                rows: list[tuple] = []
                for chunk in _batches(wanted, _ID_CHUNK):
                    placeholders = ', '.join(('?' for _ in chunk))
                    rows.extend(handle.conn.execute(f'SELECT {_memory_columns()} FROM memories WHERE user_id=? AND id IN ({placeholders})', (user_id, *chunk)).fetchall())
        finally:
            self._release(handle)
        return {int(values[0]): _memory_row(values) for values in rows}

    def _validated_user_id(self, user_id: object) -> str:
        if not isinstance(user_id, str) or not user_id:
            raise InvalidPayloadError('字段 user_id 必须是非空字符串', fields=['user_id'])
        return user_id

    def _db_name(self, user_id: str) -> tuple[str, str]:
        digest = hashlib.blake2b(user_id.encode('utf-8'), digest_size=16).hexdigest()
        return (digest, digest[:2])

    def _encode_vector(self, vector: object) -> bytes | None:
        if vector is None:
            return None
        try:
            array = np.asarray(vector, dtype='<f4')
        except (TypeError, ValueError):
            raise InvalidPayloadError('字段 vector 无法编码为 float32', fields=['vector']) from None
        expected = 4 * self._dimensions
        if array.ndim != 1 or array.size != self._dimensions:
            raise InvalidPayloadError('字段 vector 的维度与 embedding_dimensions 不符', fields=['vector'])
        blob = array.tobytes(order='C')
        if len(blob) != expected:
            raise InvalidPayloadError('字段 vector 打包长度不符', fields=['vector'])
        return blob

    def _prepare_batch(self, rows: Sequence[NewMemory], user_id: str, request_id: str) -> list['_PreparedRow']:
        prepared: list[_PreparedRow] = []
        seen_keys: set[tuple[int, int]] = set()
        ingested_at = self._stamp()
        for row in rows:
            if not isinstance(row, NewMemory):
                raise InvalidPayloadError('批次元素必须是 NewMemory', fields=['rows'])
            session_id = _require_text(row.session_id, 'session_id')
            for name, value in (('msg_index', row.msg_index), ('chunk_index', row.chunk_index)):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise InvalidPayloadError(f'字段 {name} 必须是非负整数', fields=[name])
            if not isinstance(row.content, str):
                raise InvalidPayloadError('字段 content 必须是字符串', fields=['content'])
            key = (row.msg_index, row.chunk_index)
            if key in seen_keys:
                raise InvalidPayloadError('字段 msg_index/chunk_index 在批次内重复', fields=['msg_index', 'chunk_index'])
            seen_keys.add(key)
            for name in ('event_time', 'speaker', 'entities', 'attribute_slots'):
                value = getattr(row, name)
                if value is not None and (not isinstance(value, str)):
                    raise InvalidPayloadError(f'字段 {name} 必须是字符串或 None', fields=[name])
            prepared.append(_PreparedRow(user_id=user_id, session_id=session_id, request_id=request_id, msg_index=row.msg_index, chunk_index=row.chunk_index, content=row.content, vector=self._encode_vector(row.vector), event_time=row.event_time, speaker=row.speaker, entities=row.entities, attribute_slots=row.attribute_slots, ingested_at=ingested_at))
        prepared.sort(key=lambda entry: (entry.msg_index, entry.chunk_index))
        return prepared

    def _global_connection(self) -> sqlite3.Connection:
        with self._manage:
            closed = self._closed
        if closed or self._global_conn is None:
            raise RuntimeError('存储不可用：Store 已关闭')
        return self._global_conn

    def _new_connection(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA synchronous=NORMAL')
        return conn

    def _create_tables(self, conn: sqlite3.Connection, statements: Sequence[str]) -> None:
        for statement in statements:
            conn.execute(statement)

    def _open_user_db(self, user_id: str, path: str) -> _UserDb:
        conn: sqlite3.Connection | None = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = self._new_connection(path)
        except (OSError, sqlite3.Error) as exc:
            if conn is not None:
                _quiet_close(conn)
            self._count_failed_open()
            raise ServiceFailureError(_failure_message(exc)) from None
        try:
            _begin_immediate(conn)
            try:
                self._create_tables(conn, _USER_DDL)
                conn.execute("INSERT OR IGNORE INTO db_meta(key, value) VALUES ('schema_version', '1')")
                row = conn.execute("SELECT value FROM db_meta WHERE key='user_id'").fetchone()
                if row is None:
                    conn.execute("INSERT INTO db_meta(key, value) VALUES ('user_id', ?)", (user_id,))
                elif row[0] != user_id:
                    raise RuntimeError('库内不变量破坏：db_meta.user_id 与入参 user_id 不一致')
                conn.execute('COMMIT')
            except BaseException:
                _quiet_rollback(conn)
                raise
        except RuntimeError:
            _quiet_close(conn)
            raise
        except (OSError, sqlite3.Error) as exc:
            _quiet_close(conn)
            self._count_failed_open()
            raise ServiceFailureError(_failure_message(exc)) from None
        return _UserDb(conn)

    def _count_failed_open(self) -> None:
        with self._manage:
            self._failed_db_count += 1

    def _acquire(self, user_id: str) -> _UserDb:
        key, bucket = self._db_name(user_id)
        path = os.path.join(self._users_dir, bucket, f'{key}.db')
        deadline = time.monotonic() + self._open_wait_seconds
        with self._manage:
            while True:
                if self._closed:
                    raise RuntimeError('存储不可用：Store 已关闭')
                if key not in self._handles and len(self._handles) >= self._max_open_dbs:
                    victim = next((candidate for candidate, handle in self._handles.items() if handle is not None and handle.refs == 0), None)
                    if victim is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ServiceFailureError('存储不可用：连接额度等待超时')
                        self._manage.wait(remaining)
                        continue
                    evicted = self._handles.pop(victim)
                    if evicted is not None:
                        _quiet_close(evicted.conn)
                if key in self._handles:
                    handle = self._handles[key]
                    if handle is not None:
                        handle.refs += 1
                        self._handles.move_to_end(key)
                        return handle
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ServiceFailureError('存储不可用：连接额度等待超时')
                    self._manage.wait(remaining)
                    continue
                self._handles[key] = None
                break
        try:
            handle = self._open_user_db(user_id, path)
        except BaseException:
            with self._manage:
                self._handles.pop(key, None)
                self._manage.notify_all()
            raise
        with self._manage:
            self._handles[key] = handle
            handle.refs = 1
            self._handles.move_to_end(key)
            self._manage.notify_all()
        return handle

    def _release(self, handle: _UserDb) -> None:
        with self._manage:
            handle.refs -= 1
            self._manage.notify_all()

@contextmanager
def _retryable_io() -> Iterator[None]:
    try:
        yield
    except sqlite3.OperationalError as exc:
        raise ServiceFailureError(_failure_message(exc)) from None

def _failure_message(exc: BaseException) -> str:
    return f'存储不可用：{type(exc).__name__}'

def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute('BEGIN IMMEDIATE')

def _quiet_rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute('ROLLBACK')
    except sqlite3.Error:
        pass

def _quiet_close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:
        pass
