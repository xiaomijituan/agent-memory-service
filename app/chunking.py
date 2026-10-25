from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any
_TOKEN_RE = re.compile("[A-Za-z0-9]+(?:['\\-][A-Za-z0-9]+)*|[\\u3040-\\u30ff\\u3400-\\u4dbf\\uf900-\\ufaff\\u4e00-\\u9fff]")

@dataclass(frozen=True)
class Chunk:
    text: str
    chunk_index: int
    start_char: int
    end_char: int

def _content_of(message: Any) -> str:
    if isinstance(message, dict):
        return message['content']
    return message.content

def _window_params(settings: Any) -> tuple[int, int, int]:
    window = int(settings.chunk_window_words)
    overlap = int(settings.chunk_overlap_words)
    stride = int(settings.chunk_stride_words)
    if window <= 0 or overlap < 0 or stride <= 0:
        raise ValueError('chunk 参数非法：chunk_window_words/chunk_stride_words 必须为正，chunk_overlap_words 不得为负')
    if window - overlap != stride:
        raise ValueError(f'chunk 参数不自洽：chunk_window_words({window}) - chunk_overlap_words({overlap}) != chunk_stride_words({stride})')
    return (window, overlap, stride)

def _token_spans(content: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _TOKEN_RE.finditer(content)]

def chunk_message(content: str, settings: Any) -> list[Chunk]:
    window, _overlap, stride = _window_params(settings)
    spans = _token_spans(content)
    if not spans:
        return [Chunk(text=content, chunk_index=0, start_char=0, end_char=len(content))]
    if len(spans) <= window:
        return [Chunk(text=content, chunk_index=0, start_char=0, end_char=len(content))]
    chunks: list[Chunk] = []
    start = 0
    previous_end = 0
    while True:
        end = min(start + window, len(spans))
        start_char = min(previous_end, spans[start][0])
        end_char = len(content) if end >= len(spans) else spans[end - 1][1]
        chunks.append(Chunk(text=content[start_char:end_char], chunk_index=len(chunks), start_char=start_char, end_char=end_char))
        previous_end = end_char
        if end >= len(spans):
            break
        start += stride
    return chunks

def chunk_messages(messages: Any, settings: Any) -> list[list[Chunk]]:
    return [chunk_message(_content_of(message), settings) for message in messages]
