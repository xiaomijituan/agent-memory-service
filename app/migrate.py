from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from app.config import Settings
from app.store import LexicalDerivationState, Store, classify_lexical_derivation, has_any_memory, lexical_derivation_stamp, read_lexical_stamp, scan_lexical_rows
__all__ = ['EXIT_CONSISTENT', 'EXIT_MIGRATION_REQUIRED', 'EXIT_MIGRATED', 'EXIT_FAILURE', 'MigrationInputError', 'MigrationOutcome', 'lexical_derivation_states', 'main', 'migrate_lexical_indexes']
EXIT_CONSISTENT = 0
EXIT_MIGRATION_REQUIRED = 1
EXIT_MIGRATED = 2
EXIT_FAILURE = 3
NOT_A_STORE = 'not a memory store'
'失败类报告里那句可 grep 的话（票面：拒绝要「给出可行动原因」，日志里要能认出是哪一格）。'

class MigrationInputError(ValueError):
    pass

@dataclass(frozen=True)
class MigrationOutcome:
    dry_run: bool
    states_before: tuple[LexicalDerivationState, ...]
    states_after: tuple[LexicalDerivationState, ...]
    rebuilt_rows: dict[str, int]
    probe_hits_before: dict[str, list[int]]
    probe_hits_after: dict[str, list[int]]
    errors: tuple[tuple[str, str], ...] = ()
    pending_user_ids: tuple[str | None, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        if self.errors:
            return EXIT_FAILURE
        if self.dry_run:
            return EXIT_MIGRATION_REQUIRED if self.pending_user_ids else EXIT_CONSISTENT
        return EXIT_MIGRATED if self.rebuilt_rows else EXIT_CONSISTENT

def _users_root(db_dir: str) -> str:
    return os.path.join(db_dir, 'users')

def _iter_user_db_files(db_dir: str) -> list[str]:
    root = _users_root(db_dir)
    if not os.path.isdir(root):
        raise MigrationInputError(f'{NOT_A_STORE}: {root} 不存在或不是目录；--db-dir 必须是服务的 db_dir（布局：<db_dir>/users/<hh>/<H>.db 与 <db_dir>/global.db）')
    found: list[str] = []
    for bucket in sorted(os.listdir(root)):
        bucket_dir = os.path.join(root, bucket)
        if not os.path.isdir(bucket_dir):
            continue
        for name in sorted(os.listdir(bucket_dir)):
            if name.endswith('.db'):
                found.append(os.path.join(bucket_dir, name))
    return found

def _open_read_only(path: str) -> sqlite3.Connection:
    uri = 'file:' + os.path.abspath(path).replace(os.sep, '/') + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute('PRAGMA busy_timeout=10000')
    return conn

def _state_one(path: str, current: str, *, count_stale_rows: bool) -> LexicalDerivationState:
    conn = _open_read_only(path)
    try:
        stored = read_lexical_stamp(conn)
        row = conn.execute("SELECT value FROM db_meta WHERE key='user_id'").fetchone()
        user_id = None if row is None else str(row[0])
        if count_stale_rows:
            rows, stale = scan_lexical_rows(conn)
            has_rows = rows > 0
        else:
            rows, stale = (None, None)
            has_rows = has_any_memory(conn)
    finally:
        conn.close()
    return LexicalDerivationState(user_id=user_id, db_path=path, stored_stamp=stored, current_stamp=current, rows=rows, has_rows=has_rows, status=classify_lexical_derivation(stored, current, has_rows), stale_rows=stale)

def _scan(db_dir: str, *, count_stale_rows: bool, strict: bool) -> tuple[list[LexicalDerivationState], list[tuple[str, str]]]:
    files = _iter_user_db_files(db_dir)
    current = lexical_derivation_stamp()
    states: list[LexicalDerivationState] = []
    errors: list[tuple[str, str]] = []
    for path in files:
        try:
            states.append(_state_one(path, current, count_stale_rows=count_stale_rows))
        except (sqlite3.Error, OSError) as exc:
            if strict:
                raise MigrationInputError(f'{NOT_A_STORE}: {path}（{_reason(exc)}）') from None
            errors.append((path, f'{NOT_A_STORE}（{_reason(exc)}）'))
    return (states, errors)

def _reason(exc: BaseException) -> str:
    return f'{type(exc).__name__}: {exc}'

def lexical_derivation_states(db_dir: str, *, count_stale_rows: bool=True) -> list[LexicalDerivationState]:
    states, errors = _scan(db_dir, count_stale_rows=count_stale_rows, strict=True)
    if errors:
        raise MigrationInputError(errors[0][1])
    return states

def _migration_settings(db_dir: str) -> Settings:
    return Settings(db_dir=db_dir, embedding_mode='off', auth_scheme='none')

def _probe(store: Store, user_ids: Sequence[str | None], query: str | None) -> dict[str, list[int]]:
    if query is None:
        return {}
    hits: dict[str, list[int]] = {}
    for user_id in user_ids:
        if user_id is None:
            continue
        hits[user_id] = [hit.row.id for hit in store.lexical_search(user_id, query, 20)]
    return hits

def migrate_lexical_indexes(db_dir: str, *, dry_run: bool=False, probe_query: str | None=None, user: str | None=None, count_stale_rows: bool=True) -> MigrationOutcome:
    states, errors = _scan(db_dir, count_stale_rows=count_stale_rows, strict=False)
    if user is not None:
        states = [state for state in states if state.user_id == user]
    pending = [state for state in states if state.status != 'consistent']
    pending_ids = tuple((state.user_id for state in pending))
    if dry_run or not pending:
        return MigrationOutcome(dry_run=dry_run, states_before=tuple(states), states_after=(), rebuilt_rows={}, probe_hits_before={}, probe_hits_after={}, errors=tuple(errors), pending_user_ids=pending_ids)
    rebuilt: dict[str, int] = {}
    probe_before: dict[str, list[int]] = {}
    probe_after: dict[str, list[int]] = {}
    store = Store(_migration_settings(db_dir))
    try:
        probe_before = _probe(store, pending_ids, probe_query)
        for state in pending:
            if state.user_id is None:
                errors.append((state.db_path, 'db_meta 里没有 user_id，无法映射回用户库去重算'))
                continue
            try:
                rebuilt[state.user_id] = store.rebuild_lexical_index(state.user_id)
                store.stamp_lexical_derivation(state.user_id)
            except (sqlite3.Error, OSError, RuntimeError) as exc:
                errors.append((state.db_path, f'重算失败：{_reason(exc)}'))
        probe_after = _probe(store, tuple(rebuilt), probe_query)
    finally:
        store.close()
    after_states, after_errors = _scan(db_dir, count_stale_rows=count_stale_rows, strict=False)
    errors.extend(after_errors)
    return MigrationOutcome(dry_run=False, states_before=tuple(states), states_after=tuple(after_states), rebuilt_rows=rebuilt, probe_hits_before=probe_before, probe_hits_after=probe_after, errors=tuple(errors), pending_user_ids=pending_ids)

def format_report(outcome: MigrationOutcome) -> str:
    lines: list[str] = []
    mode = 'dry-run' if outcome.dry_run else 'migrate'
    lines.append(f'mode={mode} scanned={len(outcome.states_before)} pending={len(outcome.pending_user_ids)} rebuilt={len(outcome.rebuilt_rows)} current_stamp={lexical_derivation_stamp()}')
    after = {state.db_path: state for state in outcome.states_after}
    for state in outcome.states_before:
        line = f'DB path={state.db_path} user={state.user_id} rows={state.rows} status={state.status}'
        if state.stale_rows is not None:
            line += f' stale_rows={state.stale_rows}'
        line += f' stored_stamp={state.stored_stamp}'
        rebuilt = state.user_id is not None and state.user_id in outcome.rebuilt_rows
        if rebuilt:
            after_state = after.get(state.db_path)
            line += f' -> rebuilt_rows={outcome.rebuilt_rows[state.user_id]}'
            if after_state is not None:
                line += f' after_status={after_state.status}'
                if after_state.stale_rows is not None:
                    line += f' after_stale_rows={after_state.stale_rows}'
            if state.user_id in outcome.probe_hits_before:
                line += f' probe_before={outcome.probe_hits_before[state.user_id]} probe_after={outcome.probe_hits_after.get(state.user_id)}'
        lines.append(line)
    for path, reason in outcome.errors:
        lines.append(f'ERROR path={path} reason={reason}')
    lines.append(f'EXIT {outcome.exit_code}')
    return '\n'.join(lines)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m app.migrate', description='词法索引派生源迁移入口。只动 fts_tokens 与 db_meta 的戳。')
    parser.add_argument('--db-dir', required=True, help='服务的 db_dir（存储根目录）')
    parser.add_argument('--dry-run', action='store_true', help='只读比对，不落任何写；发现问题返回退出码 1')
    parser.add_argument('--user', default=None, help='只处理这一个 user_id（默认处理目录下全部库）')
    parser.add_argument('--probe-query', default=None, help='迁移前后各用这条查询走一次产品 lexical_search，报出前 20 的行号（可核对读数）')
    parser.add_argument('--no-count', action='store_true', help='不整表逐行数差多少行，只比戳（大库上快得多；启动自检用的就是这一档）')
    return parser

def main(argv: Sequence[str] | None=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_FAILURE
    try:
        outcome = migrate_lexical_indexes(args.db_dir, dry_run=args.dry_run, probe_query=args.probe_query, user=args.user, count_stale_rows=not args.no_count)
    except MigrationInputError as exc:
        print(f'FAILED {exc}', file=sys.stdout)
        return EXIT_FAILURE
    except (sqlite3.Error, OSError) as exc:
        print(f'FAILED {NOT_A_STORE}: {_reason(exc)}', file=sys.stdout)
        return EXIT_FAILURE
    print(format_report(outcome))
    return outcome.exit_code
if __name__ == '__main__':
    raise SystemExit(main())
