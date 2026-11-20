"""Closed-set matching of OCR text against the active stamp codes."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# Characters Tesseract commonly confuses on rubber stamps. Substitution cost
# between two members of the same group is low.
_CONFUSION_GROUPS = (
    "0ODQU",
    "1IL7JT",
    "2Z",
    "5S",
    "8B3",
    "6GC",
    "4A",
    "9G",
    "MNW",
    "VY",
    "EF",
    "PR",
    "HN",
    "KX",
)
_LOW_COST = 0.35
# Thin glyphs ("1", "I") often vanish on faint or blurred stamps.
_INDEL_COST = {"1": 0.4, "I": 0.4, "L": 0.7, "J": 0.7}

# Coercions applied by position (code format = letter + 2 digits).
_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B", "9": "G", "3": "B"}
_TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "U": "0", "I": "1", "L": "1", "T": "7", "J": "1",
             "Z": "2", "S": "5", "B": "8", "G": "6", "A": "4", "E": "3", "C": "0"}


def _pair_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    for g in _CONFUSION_GROUPS:
        if a in g and b in g:
            return _LOW_COST
    return 1.0


def weighted_distance(s: str, t: str) -> float:
    """Levenshtein distance with cheap substitutions for confusable chars."""
    def indel(c: str) -> float:
        return _INDEL_COST.get(c, 1.0)

    prev = [0.0]
    for ct in t:
        prev.append(prev[-1] + indel(ct))
    for cs in s:
        cur = [prev[0] + indel(cs)] + [0.0] * len(t)
        for j, ct in enumerate(t, 1):
            cur[j] = min(
                prev[j] + indel(cs),  # extra char in OCR text
                cur[j - 1] + indel(ct),  # char of the code missing in OCR text
                prev[j - 1] + _pair_cost(cs, ct),
            )
        prev = cur
    return prev[-1]


def clean(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def coerce(s: str) -> str:
    """Coerce a 2-3 char string to letter+digit(s) using confusion maps."""
    if len(s) not in (2, 3):
        return s
    first = _TO_LETTER.get(s[0], s[0])
    rest = "".join(_TO_DIGIT.get(c, c) for c in s[1:])
    return first + rest


def candidates(text: str, n: int = 3) -> list[str]:
    """Strings worth matching from one OCR line: the whole and n-windows."""
    s = clean(text)
    out = {s} if s else set()
    if len(s) > n:
        out.update(s[i : i + n] for i in range(len(s) - n + 1))
    out.update({coerce(c) for c in list(out) if len(c) in (2, 3)})
    return sorted(out)


def similarity(text: str, code: str) -> float:
    """0..1 similarity between an OCR line and a stamp code."""
    best = 0.0
    whole = clean(text)
    n = len(code)
    for c in candidates(text, n):
        d = weighted_distance(c, code)
        # penalise length mismatch less than wrong characters
        sim = max(0.0, 1.0 - d / max(n, 1))
        if c != whole and len(whole) > n:
            # extracted from a longer noisy line; a short code inside a
            # longer reading ("A1" in "A15") is the usual confusion
            sim *= 0.92 if n >= 3 else 0.85
        best = max(best, sim)
    return best


def match_scores(texts: Iterable[str], active: Sequence[str]) -> dict[str, float]:
    """Best similarity per active code over several OCR readings."""
    scores = {code: 0.0 for code in active}
    for t in texts:
        if not clean(t):
            continue
        for code in active:
            scores[code] = max(scores[code], similarity(t, code))
    return scores
