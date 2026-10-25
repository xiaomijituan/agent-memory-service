from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Literal, Protocol, Sequence
from app.config import Settings
from app.retrieval.lexical import LexicalCandidate
from app.retrieval.vector import VectorCandidate
from app.store import MemoryRow
__all__ = ['FusedCandidate', 'FusionRecall', 'RRF_CONSTANT', 'SEED_LIMIT', 'fuse_candidates']
RRF_CONSTANT = 60.0
SEED_LIMIT = 20
ADJACENCY_RADIUS = 1

class _LexicalLane(Protocol):

    def recall(self, *, user_id: str, query: str, top_k: int) -> Sequence[LexicalCandidate]:
        ...

class _VectorLane(Protocol):

    def recall(self, *, user_id: str, query: str, top_k: int) -> Sequence[VectorCandidate]:
        ...

class _NeighbourSource(Protocol):

    def adjacent_rows(self, *, user_id: str, ids: Sequence[int]) -> Sequence[MemoryRow]:
        ...

@dataclass(frozen=True)
class FusedCandidate:
    row: MemoryRow
    score: float
    position: int
    lexical_position: int | None
    vector_position: int | None
    origin: Literal['fused', 'adjacent']
    seed_ids: tuple[int, ...]

    @property
    def id(self) -> int:
        return self.row.id

    @property
    def content(self) -> str:
        return self.row.content

@dataclass
class _Merged:
    row: MemoryRow
    lexical_position: int | None = None
    vector_position: int | None = None

def fuse_candidates(lexical: Sequence[LexicalCandidate], vector: Sequence[VectorCandidate], *, weight_lexical: float, weight_vector: float, constant: float=RRF_CONSTANT, top_k: int | None=None) -> list[FusedCandidate]:
    _check_constant(constant)
    merged: dict[int, _Merged] = {}
    for candidate in lexical:
        _merge(merged, candidate.id, candidate.row, lexical_position=candidate.position)
    for candidate in vector:
        _merge(merged, candidate.id, candidate.row, vector_position=candidate.position)
    scored: list[FusedCandidate] = []
    for entry in merged.values():
        supported = weight_lexical != 0.0 and entry.lexical_position is not None or (weight_vector != 0.0 and entry.vector_position is not None)
        if not supported:
            continue
        score = 0.0
        if entry.lexical_position is not None:
            score += weight_lexical / (constant + entry.lexical_position)
        if entry.vector_position is not None:
            score += weight_vector / (constant + entry.vector_position)
        scored.append(FusedCandidate(row=entry.row, score=score, position=0, lexical_position=entry.lexical_position, vector_position=entry.vector_position, origin='fused', seed_ids=()))
    return _number(scored, top_k=top_k, sort=True)

class FusionRecall:

    def __init__(self, store: _NeighbourSource, lexical: _LexicalLane, vector: _VectorLane, settings: Settings, *, rrf_constant: float=RRF_CONSTANT, seed_limit: int=SEED_LIMIT) -> None:
        _check_constant(rrf_constant)
        if seed_limit <= 0:
            raise ValueError('字段 seed_limit 必须为正整数')
        self._store = store
        self._lexical = lexical
        self._vector = vector
        self._settings = settings
        self._constant = float(rrf_constant)
        self._seed_limit = int(seed_limit)

    def recall(self, user_id: str, query: str, top_k: int, *, expand: bool=True) -> list[FusedCandidate]:
        if top_k <= 0:
            return []
        pool = self.fuse(user_id, query, lane_limit=max(top_k, self._seed_limit))
        if not pool:
            return []
        if not expand:
            return list(pool[:top_k])
        return self.expand(user_id, pool, top_k)

    def fuse(self, user_id: str, query: str, *, lane_limit: int | None=None) -> list[FusedCandidate]:
        limit = self._seed_limit if lane_limit is None else int(lane_limit)
        if limit <= 0:
            return []
        lexical = self._lexical.recall(user_id=user_id, query=query, top_k=limit)
        vector = self._vector.recall(user_id=user_id, query=query, top_k=limit)
        for candidate in lexical:
            _check_scope(user_id, candidate.row, '词法候选')
        for candidate in vector:
            _check_scope(user_id, candidate.row, '向量候选')
        return fuse_candidates(lexical, vector, weight_lexical=self._settings.fusion_weight_lexical, weight_vector=self._settings.fusion_weight_vector, constant=self._constant)

    def expand(self, user_id: str, fused: Sequence[FusedCandidate], top_k: int) -> list[FusedCandidate]:
        if top_k <= 0:
            return []
        pool = list(fused)
        if not pool:
            return []
        seeds = pool[:self._seed_limit]
        seed_rows = [seed.row for seed in seeds]
        neighbour_rows: list[MemoryRow] = []
        if seeds:
            neighbour_rows = list(self._store.adjacent_rows(user_id=user_id, ids=[seed.id for seed in seeds]))
        slot_of: dict[int, int] = {}
        origins: dict[int, tuple[int, ...]] = {}
        for row in neighbour_rows:
            _check_scope(user_id, row, '邻接扩展')
            adjacent = [index for index, seed_row in enumerate(seed_rows) if _is_adjacent(seed_row, row)]
            if not adjacent:
                raise RuntimeError('库内不变量破坏：邻接扩展收到了与种子不相邻的行')
            slot_of[row.id] = adjacent[0]
            origins[row.id] = tuple(sorted({seed_rows[index].id for index in adjacent}))
        pool_by_id = {candidate.id: candidate for candidate in pool}
        emitted: list[FusedCandidate] = []
        seen: set[int] = {seed.id for seed in seeds}
        for index, seed in enumerate(seeds):
            emitted.append(seed)
            for row in neighbour_rows:
                if row.id in seen or slot_of[row.id] != index:
                    continue
                seen.add(row.id)
                emitted.append(_as_adjacent(row, seed=seed, existing=pool_by_id.get(row.id), seed_ids=origins[row.id]))
        for candidate in pool[self._seed_limit:]:
            if len(emitted) >= top_k:
                break
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            emitted.append(candidate)
        return _number(emitted[:top_k], sort=False)

def _merge(merged: dict[int, _Merged], row_id: int, row: MemoryRow, **position: int) -> None:
    entry = merged.get(row_id)
    if entry is None:
        merged[row_id] = _Merged(row=row, **position)
        return
    for name, value in position.items():
        if name == 'lexical_position':
            entry.lexical_position = value
        elif name == 'vector_position':
            entry.vector_position = value

def _is_adjacent(seed_row: MemoryRow, row: MemoryRow) -> bool:
    return row.session_id == seed_row.session_id and abs(row.msg_index - seed_row.msg_index) <= ADJACENCY_RADIUS

def _as_adjacent(row: MemoryRow, *, seed: FusedCandidate, existing: FusedCandidate | None, seed_ids: tuple[int, ...]) -> FusedCandidate:
    return FusedCandidate(row=row, score=seed.score, position=0, lexical_position=existing.lexical_position if existing is not None else None, vector_position=existing.vector_position if existing is not None else None, origin='adjacent', seed_ids=seed_ids)

def _number(candidates: Sequence[FusedCandidate], *, top_k: int | None=None, sort: bool=False) -> list[FusedCandidate]:
    ordered = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.row.id)) if sort else list(candidates)
    numbered = [replace(candidate, position=position) for position, candidate in enumerate(ordered, start=1)]
    if top_k is None:
        return numbered
    return numbered[:max(top_k, 0)]

def _check_constant(constant: float) -> None:
    if constant < 0:
        raise ValueError('字段 rrf_constant 必须为非负数')

def _check_scope(user_id: str, row: MemoryRow, stage: str) -> None:
    if row.user_id != user_id:
        raise RuntimeError(f'库内不变量破坏：{stage}收到了非本作用域的行')
