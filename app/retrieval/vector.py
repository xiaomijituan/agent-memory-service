from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Sequence
import numpy as np
from app.embedding import Embedder, EmbeddingError
from app.logging import get_logger, log_event
from app.store import MemoryRow, Store, VectorRow
__all__ = ['VectorCandidate', 'VectorRecall']
_LOGGER = get_logger()

@dataclass(frozen=True)
class VectorCandidate:
    row: MemoryRow
    similarity: float
    position: int

    @property
    def id(self) -> int:
        return self.row.id

    @property
    def content(self) -> str:
        return self.row.content

class VectorRecall:

    def __init__(self, store: Store, embedder: Embedder | None=None) -> None:
        self._store = store
        self._embedder = embedder

    def recall(self, user_id: str, query: str, top_k: int) -> list[VectorCandidate]:
        if top_k <= 0:
            return []
        rows = self._store.load_vectors(user_id=user_id)
        if not rows:
            return []
        query_vector = self._embed_query(query)
        if query_vector is None:
            return []
        dimensions = int(rows[0].vector.size)
        if query_vector.ndim != 1 or query_vector.size != dimensions:
            raise RuntimeError(f'字段 vector 的维度与库内向量不符：查询 {query_vector.size} 维，库内 {dimensions} 维')
        similarities = _cosines(rows, query_vector)
        scored = sorted(((similarities[index], row) for index, row in enumerate(rows)), key=lambda item: (-item[0], item[1].id))[:top_k]
        return self._build_candidates(user_id, scored)

    def _embed_query(self, query: str) -> np.ndarray | None:
        if self._embedder is None:
            _note_degradation('no_embedding_provider')
            return None
        try:
            encoded = self._embedder.embed([query])
        except EmbeddingError as exc:
            _note_degradation(type(exc).__name__)
            return None
        if len(encoded) != 1:
            _note_degradation('provider_returned_wrong_count')
            return None
        vector = np.asarray(encoded[0], dtype='<f4')
        if float(np.linalg.norm(vector)) == 0.0:
            _note_degradation('zero_norm_query_vector')
            return None
        return vector

    def _build_candidates(self, user_id: str, scored: Sequence[tuple[float, VectorRow]]) -> list[VectorCandidate]:
        fetched = {row.id: row for row in self._store.fetch_by_ids(user_id=user_id, ids=[row.id for _, row in scored])}
        candidates: list[VectorCandidate] = []
        for similarity, row in scored:
            memory_row = fetched.get(row.id)
            if memory_row is None:
                continue
            candidates.append(VectorCandidate(row=memory_row, similarity=float(similarity), position=len(candidates) + 1))
        return candidates

def _cosines(rows: Sequence[VectorRow], query_vector: np.ndarray) -> np.ndarray:
    matrix = np.vstack([row.vector for row in rows])
    scores = matrix @ query_vector
    denominators = np.linalg.norm(matrix, axis=1) * np.float32(np.linalg.norm(query_vector))
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(denominators > 0, scores / denominators, 0.0).astype(np.float64)

def _note_degradation(error_kind: str) -> None:
    log_event(_LOGGER, logging.WARNING, event='vector_recall_degraded', error_kind=error_kind)
