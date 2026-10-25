from __future__ import annotations
import re
from collections.abc import Sequence
__all__ = ['participant_names', 'participant_surface']
_TITLE_CASE_RE = re.compile('(?<![A-Za-z])(?P<name>[A-Z][a-z]{1,19})(?![A-Za-z])')
_MIN_OCCURRENCES = 2
_MIN_MESSAGES = 2
_MIN_MID_SENTENCE = 2
_MID_SENTENCE_BOUNDARIES = ',;:'
_MAX_PARTICIPANTS = 8
_PARTICIPANT_STOPWORDS: tuple[str, ...] = ('I', 'It', 'Its', 'He', 'She', 'They', 'We', 'You', 'Me', 'Us', 'Them', 'Him', 'My', 'Our', 'Your', 'Their', 'His', 'Her', 'The', 'A', 'An', 'This', 'That', 'These', 'Those', 'There', 'Here', 'What', 'When', 'Where', 'Who', 'Whom', 'Whose', 'Why', 'How', 'Which', 'Oh', 'Hey', 'Hi', 'Hello', 'Yeah', 'Yes', 'No', 'Okay', 'Ok', 'Please', 'Thanks', 'Thank', 'Wow', 'Omg', 'Lol', 'Haha', 'Hmm', 'Huh', 'Ah', 'Aw', 'Uh', 'Um', 'Bye', 'Goodbye', 'Congrats', 'Congratulations', 'Sorry', 'Cheers', 'And', 'But', 'Or', 'So', 'If', 'Then', 'Now', 'Well', 'Also', 'Anyway', 'Anytime', 'Today', 'Tomorrow', 'Yesterday', 'Tonight', 'Morning', 'Afternoon', 'Evening', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday', 'January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December')
_STOPWORDS = frozenset(_PARTICIPANT_STOPWORDS)

def _occurs_mid_sentence(text: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and text[index].isspace():
        index -= 1
    if index < 0:
        return False
    last = text[index]
    return last.islower() or last in _MID_SENTENCE_BOUNDARIES

def participant_names(texts: Sequence[str]) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    messages: dict[str, int] = {}
    mid_sentence: dict[str, int] = {}
    first_seen: dict[str, tuple[int, int]] = {}
    for message_index, text in enumerate(texts):
        if not text:
            continue
        seen_here: set[str] = set()
        for match in _TITLE_CASE_RE.finditer(text):
            name = match.group('name')
            if name in _STOPWORDS:
                continue
            counts[name] = counts.get(name, 0) + 1
            first_seen.setdefault(name, (message_index, match.start()))
            if name not in seen_here:
                seen_here.add(name)
                messages[name] = messages.get(name, 0) + 1
            if _occurs_mid_sentence(text, match.start()):
                mid_sentence[name] = mid_sentence.get(name, 0) + 1
    corroborated = [name for name, count in counts.items() if count >= _MIN_OCCURRENCES and messages.get(name, 0) >= _MIN_MESSAGES and (mid_sentence.get(name, 0) >= _MIN_MID_SENTENCE)]
    corroborated.sort(key=lambda name: (-counts[name], first_seen[name], name))
    return tuple(corroborated[:_MAX_PARTICIPANTS])

def participant_surface(names: Sequence[str]) -> str:
    return ' '.join((name for name in names if name))
