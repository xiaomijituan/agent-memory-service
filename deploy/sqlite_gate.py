#!/usr/bin/env python3
from __future__ import annotations
import sqlite3
import sys
import tempfile
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
BASELINE_SQLITE_VERSION = '3.45.1'
USER_ID = 'gate-user'
SESSION_ID = 'gate-session'
CORPUS = [(1, '我最近失眠很严重，整夜睡不着'), (2, '白天的嗜睡让我无法工作'), (3, 'Her chronic insomnia became very severe'), (4, '他忘记带钥匙'), (5, '记忆力下降明显'), (6, '失眠时喝热牛奶并没有帮助'), (7, '整夜咳嗽影响睡眠'), (8, 'insomnia 需要专业评估')]
QUERIES: list[tuple[str, set[int]]] = [('失眠', {1, 6}), ('整夜', {1, 7}), ('很严重', {1}), ('嗜睡', {2}), ('记忆力', {5}), ('insomnia', {3, 8}), ('不存在词', set()), ('睡眠', {7})]
_failures: list[str] = []

def check(label: str, ok: bool, detail: str='') -> None:
    print(f"[{('PASS' if ok else 'FAIL')}] {label}{(f' :: {detail}' if detail else '')}", flush=True)
    if not ok:
        _failures.append(label)

def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple((int(part) for part in text.split('.') if part.isdigit()))

def main() -> int:
    version = sqlite3.sqlite_version
    print(f'sqlite_version={version} python={sys.version.split()[0]}', flush=True)
    print(f"baseline={BASELINE_SQLITE_VERSION} version_check={('MATCH' if version == BASELINE_SQLITE_VERSION else 'DRIFT')}", flush=True)
    try:
        con = sqlite3.connect(':memory:')
        con.execute("CREATE VIRTUAL TABLE probe USING fts5(t, tokenize='unicode61')")
        con.execute("INSERT INTO probe(t) VALUES ('x')")
        check('fts5_unicode61_available', True)
    except sqlite3.Error as exc:
        check('fts5_unicode61_available', False, f'{type(exc).__name__}: {exc}')
        print(f'behavior=FAIL failures={len(_failures)}', flush=True)
        return 1
    finally:
        con.close()
    from app.config import Settings
    from app.store import NewMemory, Store, split_for_fts
    db_dir = Path(tempfile.mkdtemp(prefix='amc-sqlite-gate-'))
    settings = Settings(db_dir=str(db_dir), embedding_mode='off', auth_scheme='none')
    settings.validate()
    store = Store(settings)
    try:
        store.insert_memories(USER_ID, 'gate-request-1', [NewMemory(session_id=SESSION_ID, msg_index=msg_index, chunk_index=0, content=content) for msg_index, content in CORPUS])
        for query, expected in QUERIES:
            hits = store.lexical_search(USER_ID, query, limit=50)
            got = {row.msg_index for row in (hit.row for hit in hits)}
            check(f'lexical_exact_hits[{query}]', got == expected, f'got={sorted(got)} want={sorted(expected)}')
        hits = store.lexical_search(USER_ID, '失眠', limit=50)
        ranks = [hit.rank for hit in hits]
        check('bm25_rank_negative_on_hits', bool(ranks) and all((r < 0 for r in ranks)), f'ranks={[round(r, 4) for r in ranks]}')
        check('bm25_rank_ascending_is_relevance', ranks == sorted(ranks), f'ranks={[round(r, 4) for r in ranks]}')
        first_id = hits[0].row.id if hits else None
        updated_speaker = 0
    finally:
        store.close()
    user_dbs = [p for p in sorted(db_dir.rglob('*.db')) if p.name != 'global.db']
    check('user_db_created', bool(user_dbs), f'db_dir={db_dir} files={[str(p.relative_to(db_dir)) for p in user_dbs]}')
    if user_dbs:
        raw = sqlite3.connect(user_dbs[0])
        try:
            mode = raw.execute('PRAGMA journal_mode').fetchone()[0]
            check('wal_journal_mode', str(mode).lower() == 'wal', f'journal_mode={mode}')
            before = {r[0] for r in raw.execute('SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?', (split_for_fts('失眠'),))}
            raw.execute('UPDATE memories SET speaker=? WHERE speaker IS NULL', ('gate-speaker',))
            raw.commit()
            after = {r[0] for r in raw.execute('SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?', (split_for_fts('失眠'),))}
            check('au_trigger_keeps_fts_after_annotation_update', before == after and bool(before), f'before={sorted(before)} after={sorted(after)}')
            updated_speaker = raw.execute('SELECT count(*) FROM memories WHERE speaker IS NOT NULL').fetchone()[0]
            if first_id is not None:
                raw.execute('DELETE FROM memories WHERE id=?', (first_id,))
                raw.commit()
                still = {r[0] for r in raw.execute('SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?', (split_for_fts('失眠'),))}
                check('ad_trigger_removes_deleted_row_from_fts', first_id not in still, f'deleted={first_id} remaining={sorted(still)}')
        finally:
            raw.close()
    check('annotation_update_touched_rows', updated_speaker > 0, f'rows={updated_speaker}')
    print(f"behavior={('PASS' if not _failures else 'FAIL')} failures={len(_failures)}{(f' failed={_failures}' if _failures else '')}", flush=True)
    if _failures:
        return 1
    return 0
if __name__ == '__main__':
    sys.exit(main())
