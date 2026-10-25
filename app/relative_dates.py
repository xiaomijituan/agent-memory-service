from __future__ import annotations
import re
from datetime import date, datetime, timedelta, timezone
_DAY_PHRASES = re.compile('\\b(yesterday|today|tonight|tomorrow|last night|this morning|this afternoon|this evening)\\b', re.IGNORECASE)
_OFFSET_DAYS = {'yesterday': -1, 'today': 0, 'tonight': 0, 'tomorrow': 1, 'last night': -1, 'this morning': 0, 'this afternoon': 0, 'this evening': 0}
_EPOCH_TEXT = re.compile('-?\\d+')
_WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
_RANGE_PHRASES = re.compile('\\b(last weekend|this weekend|next weekend|last week|this week|next week|last month|this month|next month|last year|this year|next year|last (?:' + '|'.join(_WEEKDAYS) + ')|next (?:' + '|'.join(_WEEKDAYS) + ')|(?:a|one|two|three|four) (?:weeks?|months?|years?) ago)\\b', re.IGNORECASE)
_AGO = re.compile('(a|one|two|three|four) (week|month|year)s? ago', re.IGNORECASE)
_AGO_COUNT = {'a': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4}

def _month_shift(day: date, delta: int) -> str:
    year, month = (day.year, day.month + delta)
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return f'{year:04d}-{month:02d}'

def _resolve_range(phrase: str, day: date) -> str | None:
    lowered = phrase.lower()
    if lowered.endswith('weekend'):
        monday = day - timedelta(days=day.weekday())
        shift = {'last': -7, 'this': 0, 'next': 7}[lowered.split()[0]]
        saturday = monday + timedelta(days=5 + shift)
        return f'{saturday.isoformat()} to {(saturday + timedelta(days=1)).isoformat()}'
    if lowered.endswith('week'):
        monday = day - timedelta(days=day.weekday())
        shift = {'last': -7, 'this': 0, 'next': 7}[lowered.split()[0]]
        start = monday + timedelta(days=shift)
        return f'{start.isoformat()} to {(start + timedelta(days=6)).isoformat()}'
    if lowered.endswith('month'):
        shift = {'last': -1, 'this': 0, 'next': 1}[lowered.split()[0]]
        return _month_shift(day, shift)
    if lowered.endswith('year'):
        shift = {'last': -1, 'this': 0, 'next': 1}[lowered.split()[0]]
        return str(day.year + shift)
    weekday = lowered.split()[1]
    target = _WEEKDAYS.index(weekday)
    if lowered.startswith('last'):
        offset = (day.weekday() - target) % 7 or 7
        return (day - timedelta(days=offset)).isoformat()
    offset = (target - day.weekday()) % 7 or 7
    return (day + timedelta(days=offset)).isoformat()

def _resolve_ago(phrase: str, day: date) -> str | None:
    match = _AGO.fullmatch(phrase)
    if match is None:
        return None
    count = _AGO_COUNT[match.group(1).lower()]
    unit = match.group(2).lower()
    if unit == 'week':
        return (day - timedelta(days=7 * count)).isoformat()
    if unit == 'month':
        return _month_shift(day, -count)
    return str(day.year - count)

def _event_date(value: str | None) -> date | None:
    text = (value or '').strip()
    if not text:
        return None
    try:
        if _EPOCH_TEXT.fullmatch(text):
            return datetime.fromtimestamp(int(text) / 1000.0, tz=timezone.utc).date()
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).date()
    except (ValueError, OverflowError, OSError):
        return None

def resolve_relative_dates(text: str, event_time: str | None) -> str:
    day = _event_date(event_time)
    if day is None or not text:
        return text

    def annotate(match: re.Match[str]) -> str:
        offset = _OFFSET_DAYS[match.group(0).lower()]
        return f'{match.group(0)} ({(day + timedelta(days=offset)).isoformat()})'

    def annotate_range(match: re.Match[str]) -> str:
        phrase = match.group(0)
        resolved = _resolve_ago(phrase, day) if ' ago' in phrase.lower() else _resolve_range(phrase, day)
        return phrase if resolved is None else f'{phrase} ({resolved})'
    resolved_days = _DAY_PHRASES.sub(annotate, text)
    return _RANGE_PHRASES.sub(annotate_range, resolved_days)
