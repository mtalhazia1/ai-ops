"""ISO 6346 container number validation (owner code + category + serial + check digit)."""

import re

_PATTERN = re.compile(r"^[A-Z]{3}[UJZ]\d{7}$")


def _letter_values() -> dict[str, int]:
    values, n = {}, 10
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if n % 11 == 0:  # multiples of 11 are skipped
            n += 1
        values[ch] = n
        n += 1
    return values


_LETTERS = _letter_values()


def normalize(number: str) -> str:
    return re.sub(r"[\s\-/.]", "", number or "").upper()


def check_digit(first10: str) -> int:
    total = 0
    for i, ch in enumerate(first10):
        value = _LETTERS[ch] if ch.isalpha() else int(ch)
        total += value * (2**i)
    return total % 11 % 10


def is_valid(number: str) -> bool:
    n = normalize(number)
    if not _PATTERN.match(n):
        return False
    return check_digit(n[:10]) == int(n[10])
