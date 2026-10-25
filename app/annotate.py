from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any
_PROPER_NOUN_RE = re.compile("\\b[A-Z][A-Za-z0-9'\\-]*(?:\\s+[A-Z][A-Za-z0-9'\\-]*)*\\b")
_STOP_WORDS = {'I', 'The', 'A', 'An', 'This', 'That', 'These', 'Those', 'It', 'Its', 'He', 'She', 'They', 'We', 'You', 'My', 'Our', 'Your', 'Their', 'Her', 'His', 'There', 'Here', 'What', 'When', 'Where', 'How', 'Why', 'Which', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'}
_BOOK_TITLE_RE = re.compile('《([^》\\s]{1,30})》')
_COLON_SLOT_RE = re.compile('([一-鿿A-Za-z0-9_]{1,12})\\s*[:：=]\\s*([^\\s，。；,;!?！？]{1,40})')
_PREDICATE_SLOT_RE = re.compile('(住在|名叫|职业是|任职于|负责|喜欢|偏好|过敏|计划|预定)\\s*([^\\s，。；,;!?！？、]{1,20})')

@dataclass(frozen=True)
class Annotation:
    event_time: Any
    speaker: str
    entities: tuple[str, ...] = ()
    attribute_slots: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class AnnotatedChunk:
    text: str
    event_time: Any
    speaker: str
    entities: tuple[str, ...] = ()
    attribute_slots: dict[str, str] = field(default_factory=dict)
    chunk_index: int = 0
    start_char: int = 0
    end_char: int = 0
    message_index: int = 0

def _field(message: Any, name: str, default: Any=None) -> Any:
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)

def _extract_entities(text: str) -> tuple[str, ...]:
    entities: list[str] = []
    for match in _PROPER_NOUN_RE.finditer(text):
        candidate = match.group()
        if candidate in _STOP_WORDS:
            continue
        entities.append(candidate)
    for match in _BOOK_TITLE_RE.finditer(text):
        entities.append(match.group(1))
    seen: set[str] = set()
    unique: list[str] = []
    for entity in entities:
        if entity not in seen:
            seen.add(entity)
            unique.append(entity)
    return tuple(unique)

def _extract_slots(text: str) -> dict[str, str]:
    slots: dict[str, str] = {}
    for key, value in _COLON_SLOT_RE.findall(text):
        slots.setdefault(key, value)
    for key, value in _PREDICATE_SLOT_RE.findall(text):
        slots.setdefault(key, value)
    return slots

def annotate_message(message: Any, arrival_time: Any=None) -> Annotation:
    timestamp = _field(message, 'timestamp')
    content = _field(message, 'content', '') or ''
    speaker = _field(message, 'role', '') or ''
    return Annotation(event_time=timestamp, speaker=speaker, entities=_extract_entities(content), attribute_slots=_extract_slots(content))

def annotate_chunk(chunk: Any, message: Any, message_index: int=0, arrival_time: Any=None) -> AnnotatedChunk:
    annotation = annotate_message(message, arrival_time=arrival_time)
    text = chunk.text
    return AnnotatedChunk(text=text, event_time=annotation.event_time, speaker=annotation.speaker, entities=_extract_entities(text), attribute_slots=_extract_slots(text), chunk_index=chunk.chunk_index, start_char=chunk.start_char, end_char=chunk.end_char, message_index=message_index)
