# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Replacement values for the strategies that work value by value.

Faker, hash and format-preserving rules map each distinct original value to a
replacement here, in the Datadouble process; the engine adapter then applies
the mapping in one native update per table. Every replacement is keyed by the
run's anonymization key, so the same input gets the same output in every
table and every image group of a build, and nothing about the original can be
recovered without the key.

Generated emails, URLs, phone numbers and IP addresses use reserved example
domains and documentation ranges, so the PII scan does not mistake them for
real data.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterable
from typing import assert_never

from faker import Faker

from datadouble.catalog import ColumnKind
from datadouble.pii import detect
from datadouble.spec import FakerKind, FakerRule, FormatPreservingRule, HashRule

_MAX_ATTEMPTS = 100
_INT32_MAX = 2**31 - 1
_LUHN_CARRY = 9


class AnonymizeError(Exception):
    """Replacement values that cannot be generated as the rules require."""


type ValueRule = FakerRule | HashRule | FormatPreservingRule
"""Rules whose replacements are computed here, value by value."""


def new_key() -> bytes:
    """A random anonymization key, for runs whose spec does not supply one."""
    return secrets.token_bytes(32)


class Anonymizer:
    """Computes replacements under one anonymization key."""

    def __init__(self, key: bytes, locale: str = "en_US") -> None:
        """Use ``key`` for every replacement; ``locale`` picks Faker's data."""
        self._key = key
        self._faker = Faker(locale)

    def _digest(self, *parts: str) -> bytes:
        message = "\x1f".join(parts).encode()
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def _seed(self, *parts: str) -> int:
        return int.from_bytes(self._digest(*parts)[:8], "big")

    def fake(self, kind: FakerKind, value: str, attempt: int = 0) -> str:
        """A fake value of ``kind`` for ``value``; ``attempt`` varies it on collisions."""
        fake = self._faker
        fake.seed_instance(self._seed("faker", kind, str(attempt), value))
        generators: dict[FakerKind, Callable[[], str]] = {
            FakerKind.FIRST_NAME: fake.first_name,
            FakerKind.LAST_NAME: fake.last_name,
            FakerKind.NAME: fake.name,
            FakerKind.EMAIL: lambda: f"{fake.user_name()}@{fake.safe_domain_name()}",
            FakerKind.USER_NAME: fake.user_name,
            FakerKind.PHONE_NUMBER: lambda: (
                f"+1-{fake.random_int(200, 999)}-555-01{fake.random_int(0, 99):02d}"
            ),
            FakerKind.STREET_ADDRESS: fake.street_address,
            FakerKind.CITY: fake.city,
            FakerKind.POSTCODE: fake.postcode,
            FakerKind.COMPANY: fake.company,
            FakerKind.IPV4: lambda: f"192.0.2.{fake.random_int(1, 254)}",
            FakerKind.URL: lambda: f"https://{fake.safe_domain_name()}/{fake.slug()}",
            FakerKind.TEXT: lambda: fake.sentence(nb_words=8),
            FakerKind.DATE_OF_BIRTH: lambda: fake.date_of_birth(
                minimum_age=18, maximum_age=90
            ).isoformat(),
        }
        return generators[kind]()

    def hash(self, value: str, length: int, kind: ColumnKind, attempt: int = 0) -> str:
        """A keyed hash of ``value``: hex text, or a non-negative 32-bit integer."""
        digest = self._digest("hash", str(attempt), value)
        if kind is ColumnKind.INTEGER:
            return str(int.from_bytes(digest, "big") % _INT32_MAX)
        return digest.hex()[:length]

    def preserve_format(self, value: str, *, luhn: bool, attempt: int = 0) -> str:
        """Replace digits with digits and letters with letters of the same case."""
        stream = self._digest("format", str(attempt), value)
        while len(stream) < len(value):
            stream += hashlib.sha256(stream).digest()
        out: list[str] = []
        for char, byte in zip(value, stream, strict=False):
            if char.isascii() and char.isdigit():
                out.append(str(byte % 10))
            elif char.isascii() and char.isalpha():
                base = ord("a") if char.islower() else ord("A")
                out.append(chr(base + byte % 26))
            else:
                out.append(char)
        result = "".join(out)
        return _with_luhn_check(result) if luhn else result

    def replacement(self, rule: ValueRule, kind: ColumnKind, value: str, attempt: int) -> str:
        """The replacement ``rule`` gives ``value`` on a given attempt."""
        match rule:
            case FakerRule():
                return self.fake(rule.faker, value, attempt)
            case HashRule():
                return self.hash(value, rule.hash.length, kind, attempt)
            case FormatPreservingRule():
                luhn = rule.format_preserving.luhn
                return self.preserve_format(value, luhn=luhn, attempt=attempt)
            case _:
                assert_never(rule)

    def mapping(
        self, rule: ValueRule, kind: ColumnKind, values: Iterable[str], *, unique: bool
    ) -> dict[str, str]:
        """Replacements for distinct ``values``; with ``unique``, no two share one."""
        result: dict[str, str] = {}
        taken: set[str] = set()
        for value in sorted(set(values)):
            for attempt in range(_MAX_ATTEMPTS):
                replacement = self.replacement(rule, kind, value, attempt)
                # A hash is meant to look like nothing; hex text can still hold a
                # digit run the PII scan reads as a card number, so try again.
                if isinstance(rule, HashRule) and detect(replacement):
                    continue
                if not unique or replacement not in taken:
                    break
            else:
                message = (
                    f"could not find a unique replacement after {_MAX_ATTEMPTS} attempts; "
                    "the rule produces too few distinct values for this column"
                )
                raise AnonymizeError(message)
            taken.add(replacement)
            result[value] = replacement
        return result


def _with_luhn_check(digits_and_more: str) -> str:
    """Set the last digit so the digits pass the Luhn check, as card numbers do."""
    positions = [i for i, c in enumerate(digits_and_more) if c.isascii() and c.isdigit()]
    if not positions:
        return digits_and_more
    digits = [int(digits_and_more[i]) for i in positions[:-1]]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        doubled = digit * 2 if index % 2 == 0 else digit
        total += doubled - _LUHN_CARRY if doubled > _LUHN_CARRY else doubled
    check = (10 - total % 10) % 10
    chars = list(digits_and_more)
    chars[positions[-1]] = str(check)
    return "".join(chars)
