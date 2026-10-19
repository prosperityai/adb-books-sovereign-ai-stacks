"""PII redaction and prompt-injection signals, applied before anything is stored.

Book: Sovereign AI Stacks, Chapter 10 (threat T9)

Redaction is deliberately greedy. A false positive costs an auditor a few
characters of context; a false negative is a personal-data breach inside the
very log that exists to prove compliance. Patterns run in a fixed order, from
most to least specific, and each replaces its match with a token that contains
no digits, so a later pattern can never re-match part of an earlier one.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

PLACEHOLDER = "[REDACTED:{}]"


def luhn_ok(digits: str) -> bool:
    """True if the digit string passes the Luhn checksum used by payment cards."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _card(match: str) -> bool:
    d = _digits(match)
    return 13 <= len(d) <= 19 and luhn_ok(d)


def _iban(match: str) -> bool:
    # IBAN-like: country code, check digits, then a mostly numeric account.
    # Ten digits or more keeps ordinary words like "AB12CDEFGHIJKLM" out.
    return len(_digits(match)) >= 10


def _intl_phone(match: str) -> bool:
    return 8 <= len(_digits(match)) <= 15


@dataclass(frozen=True)
class Rule:
    label: str
    pattern: re.Pattern[str]
    accept: Callable[[str], bool] | None = None


RULES: tuple[Rule, ...] = (
    Rule("EMAIL", re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    # Tanzanian NIDA number: 20 digits, usually written YYYYMMDD-NNNNN-NNNNN-NN.
    Rule("NIDA", re.compile(r"(?<!\d)(?:\d{8}([- ])\d{5}\1\d{5}\1\d{2}|\d{20})(?!\d)")),
    Rule("CARD", re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"), _card),
    Rule("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b"), _iban),
    # Tanzania mobile: +255 / 255 / 0, then 6xx or 7xx and six more digits.
    Rule("PHONE", re.compile(r"(?<![\w+])(?:\+?255|0)[ -]?[67]\d{2}[ -]?\d{3}[ -]?\d{3}(?!\d)")),
    # UAE: +971 / 00971 / 0, then a mobile (5x) or landline area code.
    Rule("PHONE", re.compile(r"(?<![\w+])(?:(?:\+|00)971[ -]?0?|0)(?:5\d|[234679])[ -]?\d{3}[ -]?\d{4}(?!\d)")),
    # Anything else written in international format.
    Rule("PHONE", re.compile(r"(?<![\w+])(?:\+|00)[1-9][\d ()-]{6,18}\d(?!\d)"), _intl_phone),
)


class Redactor:
    """Redacts strings and nested JSON-like structures, counting what it removed."""

    def __init__(self, rules: tuple[Rule, ...] = RULES) -> None:
        self.rules = rules

    def text(self, s: str, counts: Counter[str]) -> str:
        for rule in self.rules:

            def repl(m: re.Match[str], rule: Rule = rule) -> str:
                if rule.accept is not None and not rule.accept(m.group(0)):
                    return m.group(0)
                counts[rule.label] += 1
                return PLACEHOLDER.format(rule.label)

            s = rule.pattern.sub(repl, s)
        return s

    def value(self, obj: object, counts: Counter[str]) -> object:
        """Redact every string leaf. Keys are left alone: they are schema, not data."""
        if isinstance(obj, str):
            return self.text(obj, counts)
        if isinstance(obj, dict):
            return {k: self.value(v, counts) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.value(v, counts) for v in obj]
        return obj


# ---------------------------------------------------------------------------
# Prompt-injection signals. These do not block anything: blocking belongs at
# the gateway and in tool permissions (Chapter 10.4). They mark records for
# review and give Chapter 11's red-team suite something to assert against.
# ---------------------------------------------------------------------------
# Each pattern carries the literal words it cannot match without. A substring
# test is far cheaper than a regex with gaps, so most texts never reach one.
INJECTION_PATTERNS: tuple[tuple[tuple[str, ...], re.Pattern[str]], ...] = (
    # English
    (
        ("instruction", "prompt", "rules"),
        re.compile(
            r"\b(?:ignore|disregard|forget)\b.{0,20}\b(?:previous|prior|above|earlier|all)\b.{0,20}"
            r"\b(?:instructions?|prompts?|rules)\b",
            re.I | re.S,
        ),
    ),
    (("system prompt",), re.compile(r"\b(?:reveal|print|show|repeat)\b.{0,20}\bsystem\s+prompt\b", re.I | re.S)),
    (
        ("you are now",),
        re.compile(r"\byou\s+are\s+now\b.{0,30}\b(?:developer|dan|jailbreak|unrestricted)\b", re.I | re.S),
    ),
    # Swahili: "puuza maagizo/maelekezo yaliyotangulia", "onyesha maelekezo ya mfumo"
    (("puuza",), re.compile(r"\bpuuza\b.{0,30}\b(?:maagizo|maelekezo|amri)\b", re.I | re.S)),
    (("ya mfumo",), re.compile(r"\b(?:onyesha|toa)\b.{0,20}\b(?:maagizo|maelekezo)\s+ya\s+mfumo\b", re.I | re.S)),
    # Arabic: "تجاهل التعليمات السابقة", "اكشف تعليمات النظام"
    (("تجاهل",), re.compile(r"تجاهل.{0,30}(?:التعليمات|الأوامر|التوجيهات)", re.S)),
    (("النظام",), re.compile(r"(?:اكشف|اعرض|أظهر).{0,20}(?:تعليمات|موجه)\s+النظام", re.S)),
)


def injection_signals(texts: list[str]) -> list[str]:
    """Return ['prompt_injection_suspected'] if any text matches a known pattern."""
    for t in texts:
        low = t.casefold()
        for needles, pattern in INJECTION_PATTERNS:
            if any(n in low for n in needles) and pattern.search(t):
                return ["prompt_injection_suspected"]
    return []


def strings_in(obj: object) -> list[str]:
    """Every string leaf of a JSON-like value, for signal checks."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings_in(v)]
    if isinstance(obj, list | tuple):
        return [s for v in obj for s in strings_in(v)]
    return []
