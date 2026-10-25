from __future__ import annotations
from dataclasses import dataclass
from app.store import MemoryRow, Store
__all__ = ['LexicalCandidate', 'LexicalRecall']

@dataclass(frozen=True)
class LexicalCandidate:
    row: MemoryRow
    rank: float
    position: int

    @property
    def id(self) -> int:
        return self.row.id

    @property
    def content(self) -> str:
        return self.row.content

class LexicalRecall:

    def __init__(self, store: Store) -> None:
        self._store = store

    def recall(self, user_id: str, query: str, top_k: int) -> list[LexicalCandidate]:
        hits = self._store.lexical_search(user_id=user_id, query=query, limit=top_k)
        return [LexicalCandidate(row=hit.row, rank=hit.rank, position=position) for position, hit in enumerate(hits, start=1)]
