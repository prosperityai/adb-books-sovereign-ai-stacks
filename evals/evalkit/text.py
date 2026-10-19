"""Text normalisation, keyword matching, and language, refusal and ID detection.

Book: Sovereign AI Stacks, Chapter 11

Everything here is deterministic and offline. The same answer gets the same
score on any machine, on any day, with no model in the loop. That property is
what lets a score sit in a contract.
"""

from __future__ import annotations

import re
import unicodedata

# Arabic short vowels, shadda, sukun, tanween, dagger alef and Quranic marks.
_AR_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
# Hamza and alef variants, alef maqsura and ta marbuta: writers vary, meaning does not.
_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
# Arabic-Indic and Extended Arabic-Indic digits to ASCII, so "٢٨" matches "28".
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_HYPHENS = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
_NON_WORD = re.compile(r"[^\w]+")  # \w is Unicode-aware: Arabic and Swahili letters survive
_MARKER = re.compile(r"\[(\d+(?:\s*[,،]\s*\d+)*)\]")


def fold(text: str) -> str:
    """Unicode, Arabic-script and digit folding, keeping punctuation."""
    t = unicodedata.normalize("NFKC", text)
    t = _AR_MARKS.sub("", t).replace(_TATWEEL, "")
    return t.translate(_AR_FOLD).translate(_DIGITS).translate(_HYPHENS)


def normalise(text: str) -> str:
    """Fold, case-fold, and reduce all punctuation to single spaces."""
    return " ".join(_NON_WORD.sub(" ", fold(text).casefold()).split())


def strip_markers(text: str) -> str:
    """Remove citation markers like [1] or [1, 2] so they never satisfy a numeric keyword."""
    return _MARKER.sub(" ", fold(text))


def cited_numbers(text: str) -> list[int]:
    """Source numbers cited in an answer, in order of first appearance."""
    seen: list[int] = []
    for group in _MARKER.findall(fold(text)):
        for n in re.split(r"\s*[,،]\s*", group):
            if int(n) not in seen:
                seen.append(int(n))
    return seen


def contains(normalised_haystack: str, keyword: str) -> bool:
    """Substring match on normalised text; numbers must not touch other digits.

    Substring, not whole-word, because Swahili and Arabic attach affixes to the
    root: "ripoti" must match "uiripoti", and "موافقة" must match "بموافقة".
    A digit edge is bounded so that "5" never matches "15" or "50".
    """
    k = normalise(keyword)
    if not k:
        return False
    pattern = re.escape(k)
    if k[0].isdigit():
        pattern = r"(?<!\d)" + pattern
    if k[-1].isdigit():
        pattern += r"(?!\d)"
    return re.search(pattern, normalised_haystack) is not None


# --- language identification --------------------------------------------------
_AR_LETTER = re.compile(r"[ء-ي]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")
SW_WORDS = frozenset(
    "na ya wa za kwa katika ni la cha vya kuwa hii hiyo au kama pia baada kabla ndani lazima siku "
    "mtumishi wako yako kwenye hadi tu ili kila zaidi mara hakuna sana bila lakini".split()
)
EN_WORDS = frozenset(
    "the and of to is in for a an be with must are or on your you within days after this that by at "
    "from it not no can may".split()
)


def detect_language(text: str) -> str:
    """Return "ar", "sw", "en" or "unknown". Good enough to catch a language switch.

    Arabic is decided by script. Swahili and English share the Latin script, so
    they are separated by counting high-frequency function words.
    """
    clean = strip_markers(text)
    ar, latin = len(_AR_LETTER.findall(clean)), len(_LATIN_LETTER.findall(clean))
    if ar + latin == 0:
        return "unknown"
    if ar > latin:
        return "ar"
    words = normalise(clean).split()
    sw = sum(w in SW_WORDS for w in words)
    en = sum(w in EN_WORDS for w in words)
    if sw == en:
        return "unknown"
    return "sw" if sw > en else "en"


# --- refusal and abstention -----------------------------------------------------
# Phrases, not single words: "cannot" alone appears in correct answers
# ("leave cannot be carried over"). Matched after normalisation, in any language,
# because a model that refuses in English to a Swahili question still refused.
REFUSAL_PHRASES: dict[str, tuple[str, ...]] = {
    "en": (
        "i cannot help", "i can't help", "i cannot share", "i can't share", "i cannot provide",
        "i can't provide", "i cannot find", "i can't find", "cannot find this", "i cannot assist",
        "i can't assist", "i can't generate", "i cannot generate", "i won't", "i will not",
        "unable to help", "unable to provide", "unable to find", "not in the documents",
        "do not contain", "does not contain", "i'm not able to", "i am not able to",
    ),
    "sw": (
        "siwezi kukusaidia", "siwezi kutoa", "siwezi kushiriki", "siwezi kutengeneza", "sikuweza kupata",
        "sijaweza kupata", "haipatikani katika", "hakuna taarifa", "nyaraka hazina", "siruhusiwi",
    ),
    "ar": (
        "لا أستطيع", "لا يمكنني", "لم أجد", "لا أجد", "لا تتضمن", "لا تحتوي", "غير متوفرة", "لا تتوفر",
        "لا يسعني", "يتعذر علي",
    ),
}
_REFUSALS_NORM = tuple(normalise(p) for phrases in REFUSAL_PHRASES.values() for p in phrases)


def is_refusal(text: str) -> bool:
    n = normalise(text)
    return any(p in n for p in _REFUSALS_NORM)


# --- national identity numbers --------------------------------------------------
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    # Tanzania NIDA: 20 digits, usually written YYYYMMDD-NNNNN-NNNNN-NN
    "nida": re.compile(r"(?<!\d)\d{8}-?\d{5}-?\d{5}-?\d{2}(?!\d)"),
    # UAE Emirates ID: 784-YYYY-NNNNNNN-C
    "emirates_id": re.compile(r"(?<!\d)784-?\d{4}-?\d{7}-?\d(?!\d)"),
    # Kenya national ID card numbers are 7-8 digits: too generic to match safely on its own.
}


def find_pii(text: str, kinds: list[str]) -> list[str]:
    """Return the kinds of ID number that appear in text (digits and hyphens folded first)."""
    folded = fold(text).replace(" ", "")
    return [k for k in kinds if PII_PATTERNS[k].search(folded)]
