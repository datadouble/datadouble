# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Detectors for personal data that may have survived anonymization.

The PII scan reads text values from a restored golden image and flags any that
look like personal data. Values in reserved example domains and documentation
ranges, which the anonymizer generates, are not flagged. A spec can allow
values on purpose with ``LIKE`` patterns per column.
"""

import re
from collections.abc import Iterable
from enum import StrEnum


class Detector(StrEnum):
    """Kinds of personal data the scan looks for."""

    EMAIL = "email"
    PAYMENT_CARD = "payment-card"
    US_SSN = "us-ssn"
    PHONE = "phone"


_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
_CARD = re.compile(
    r"(?<![\d-])(?:\d{13,19}|\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?:\1\d{3})?|\d{4}([ -])\d{6}\2\d{5})"
    r"(?![\d-])"
)
_SSN = re.compile(r"(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)")
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)"
)
_FICTIONAL_PHONE = re.compile(r"555[ .-]01\d\d$")
_LUHN_CARRY = 9
_SSN_INVALID_AREA = ("000", "666")


def _reserved_domain(domain: str) -> bool:
    domain = domain.lower()
    return (
        domain in _RESERVED_DOMAINS
        or any(domain.endswith("." + reserved) for reserved in _RESERVED_DOMAINS)
        or domain.endswith(_RESERVED_SUFFIXES)
    )


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > _LUHN_CARRY:
                digit -= _LUHN_CARRY
        total += digit
    return total % 10 == 0


def _card(match: str) -> bool:
    digits = re.sub(r"[ -]", "", match)
    return len(set(digits)) > 1 and _luhn(digits)


def _ssn(area: str, group: str, serial: str) -> bool:
    return (
        area not in _SSN_INVALID_AREA
        and not area.startswith("9")
        and group != "00"
        and serial != "0000"
    )


def detect(value: str) -> tuple[Detector, ...]:
    """Every kind of personal data ``value`` appears to contain."""
    found: list[Detector] = []
    if any(not _reserved_domain(m[1]) for m in _EMAIL.finditer(value)):
        found.append(Detector.EMAIL)
    if any(_card(m[0]) for m in _CARD.finditer(value)):
        found.append(Detector.PAYMENT_CARD)
    if any(_ssn(*m.groups()) for m in _SSN.finditer(value)):
        found.append(Detector.US_SSN)
    if any(not _FICTIONAL_PHONE.search(m[0]) for m in _PHONE.finditer(value)):
        found.append(Detector.PHONE)
    return tuple(found)


def like_pattern(pattern: str) -> re.Pattern[str]:
    r"""Compile a SQL ``LIKE`` pattern (``%``, ``_``, ``\`` escapes) to a regex."""
    out: list[str] = []
    chars = iter(pattern)
    for char in chars:
        if char == "\\":
            out.append(re.escape(next(chars, "\\")))
        elif char == "%":
            out.append(".*")
        elif char == "_":
            out.append(".")
        else:
            out.append(re.escape(char))
    return re.compile("".join(out), re.DOTALL)


def allowed(value: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    """Whether ``value`` matches one of a column's allow patterns."""
    return any(pattern.fullmatch(value) for pattern in patterns)
