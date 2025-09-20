"""Текстовые утилиты очистки новостного контента CryptoPanic."""
from __future__ import annotations

import re

PUNCTS = [
    '.',
    ',',
    '?',
    '!',
    '"',
    ':',
    "\u201D",
    "\u201C",
    "\u201D",
    "\u201C",
    "\u201C",
]
SYMBOLS_FOR_STRIP = [
    '!',
    ':',
    '?',
    ',',
    '.',
    '"',
    '”',
    '“',
    '”',
    '“',
    '“',
]
BLACK_LIST = [
    'our website',
    'read more:',
    'can send him leads at',
    '@theblockcrypto.com',
    'follow him on twitter',
    'read more',
    'about author',
    'read more:',
    'read more',
    'image:',
    'disclosure:',
    'redeem now!',
    'redeem now',
    'follow our',
    'our twitter',
    'thanks for reading',
    'please',
    'your inbox',
    'subscribe',
    'first name',
    'select your',
    'topics',
    'share article',
    'disclaimer',
    'image credit',
    'related posts',
    'sign up',
    'sign in',
    'email address',
    'your email',
    'exclusive offers',
    'newsletter',
    'you may also like',
    'banner',
    'related news',
    'privacy policy',
    'terms of services',
    'twitter.com/',
    'advertisement',
    'you agree',
    'subscribing',
    'newsletter',
    'related:',
    'in this article',
    'also like:',
    'by the author',
    '©',
    'about us',
    'advertise',
    'terms and conditions',
    'write for us',
    'pixabay',
    'informational purposes only',
    'your deposit',
    'this link',
    'to register',
    'code to receive',
    'sponsored',
    'special offer',
    'submit a press release',
    'shutterstock',
    'http',
    'featured image',
    'not investment advice',
    'join now',
    'newsletters',
    'subscribe to',
    '(@',
    'my website',
    'follow me',
    'related image',
    'news writer',
    'his articles',
    'journalist',
    'about the author',
    'article image',
    'the link',
    'contact:',
    'from author',
    'related articles',
    'from author',
    'gamza',
    'гамза',
    'университет',
    'закончил',
    'окончил',
]


def _last_meaningful_char(value: str) -> str:
    """Вернуть последний значащий символ строки, игнорируя пробелы и кавычки."""

    if not value:
        return ''
    i = len(value) - 1
    while i >= 0 and value[i] in (' ', '\t', '\r', '\n'):
        i -= 1
    skip = "\"'”’»)]}"
    while i >= 0 and value[i] in skip:
        i -= 1
    if i >= 0 and value[i] == ',':
        i -= 1
        while i >= 0 and value[i] in skip:
            i -= 1
    return value[i] if i >= 0 else ''


def _ends_with_punct(line: str) -> bool:
    """Проверить, заканчивается ли строка допустимым знаком препинания."""

    return _last_meaningful_char(line) in PUNCTS


def _normalize_space_line(value: str | None) -> str:
    """Схлопнуть пробелы и табы внутри строки."""

    if value is None:
        return ""
    value = value.replace('\t', ' ')
    value = re.sub(r'\s+', ' ', value)
    return value.strip()


def clean_text_pipeline(raw: str) -> str:
    """Очистить текст источника по шагам CryptoPanic."""

    if not raw:
        return ""

    txt = re.sub(r"\r\n?", "\n", raw)
    lines = [line for line in txt.split("\n")]
    lines = [line for line in lines if len(line)]

    def is_sentence_like(line: str, is_last: bool) -> bool:
        last = _last_meaningful_char(line)
        if last == ':':
            return not is_last
        return last in ('.', '!', '?') and len(line) >= 40 and (line.count(' ') >= 4)

    start_idx = -1
    for i in range(len(lines)):
        ok = True
        for offset in range(3):
            j = i + offset
            if j >= len(lines) or not is_sentence_like(lines[j], j == len(lines) - 1):
                ok = False
                break
        if ok:
            start_idx = i
            break
    if start_idx == -1:
        for i in range(len(lines)):
            if is_sentence_like(lines[i], i == len(lines) - 1):
                start_idx = i
                break
    if start_idx > 0:
        lines = lines[start_idx:]

    lower_blacklist = [word.lower() for word in BLACK_LIST]

    def strip_if_black(value: str) -> str:
        low = value.lower()
        if any(word in low for word in lower_blacklist):
            for symbol in SYMBOLS_FOR_STRIP:
                value = value.replace(symbol, '')
        return value

    lines = [strip_if_black(item) for item in lines]

    n_in_row = 6
    count = 0
    start_run = -1
    for idx, line in enumerate(lines):
        if _ends_with_punct(line):
            count = 0
        else:
            count += 1
            if count >= n_in_row:
                start_run = idx + 1 - n_in_row
                break
    if start_run >= 0:
        lines = lines[:start_run]

    if len(lines) > 25:
        lines = lines[:25]

    i = 0
    while i < len(lines):
        if lines[i] == "":
            i += 1
            continue
        last = _last_meaningful_char(lines[i])
        if last in PUNCTS:
            i += 1
        else:
            del lines[i]

    normalized: list[str] = []
    prev_empty = False
    for item in lines:
        if item == "":
            if not prev_empty:
                normalized.append(item)
            prev_empty = True
        else:
            normalized.append(item)
            prev_empty = False
    lines = normalized

    def not_black(line: str) -> bool:
        low = line.lower()
        return all(word not in low for word in lower_blacklist)

    lines = [line for line in lines if not_black(line)]

    if lines and _last_meaningful_char(lines[-1]) == ':':
        lines.pop()

    def is_short(value: str) -> bool:
        return len(value.strip()) < 60

    if lines:
        if lines[-1].strip() == "" or is_short(lines[-1]):
            lines.pop()
    if len(lines) >= 2:
        if lines[-2].strip() != "" and is_short(lines[-2]):
            del lines[-2]

    seen: set[str] = set()
    kept: list[str] = []
    for item in lines:
        if item.strip() == "":
            kept.append(item)
            continue
        key = item.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    lines = kept

    lines = [_normalize_space_line(item) for item in lines]
    return "\n".join(lines).strip()


__all__ = ["clean_text_pipeline"]
