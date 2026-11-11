# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from pathlib import Path

import pytest

from datadouble.anonymize import AnonymizeError, Anonymizer, ValueRule, new_key
from datadouble.catalog import ColumnKind
from datadouble.pii import Detector, allowed, detect, like_pattern
from datadouble.secrets import SecretError, resolve_secret
from datadouble.spec import (
    EnvSecret,
    FakerKind,
    FakerRule,
    FileSecret,
    FormatPreservingOptions,
    FormatPreservingRule,
    HashOptions,
    HashRule,
)

KEY = b"k" * 32


@pytest.mark.parametrize("kind", list(FakerKind))
def test_fake_values_are_deterministic_and_keyed(kind: FakerKind) -> None:
    one, two = Anonymizer(KEY), Anonymizer(KEY)
    assert one.fake(kind, "alice") == two.fake(kind, "alice")
    other = Anonymizer(b"x" * 32)
    values = {other.fake(kind, f"v{i}") for i in range(20)} | {one.fake(kind, "alice")}
    assert len(values) > 1
    assert detect(one.fake(kind, "alice")) == ()


def test_fake_values_look_right() -> None:
    a = Anonymizer(KEY)
    assert a.fake(FakerKind.EMAIL, "x").split("@")[1] in {
        "example.com",
        "example.net",
        "example.org",
    }
    assert a.fake(FakerKind.PHONE_NUMBER, "x").startswith("+1-")
    assert a.fake(FakerKind.IPV4, "x").startswith("192.0.2.")
    assert a.fake(FakerKind.URL, "x").startswith("https://example.")
    assert len(a.fake(FakerKind.DATE_OF_BIRTH, "x")) == 10
    assert a.fake(FakerKind.FIRST_NAME, "x") != a.fake(FakerKind.FIRST_NAME, "x", attempt=1)


def test_hash() -> None:
    a = Anonymizer(KEY)
    assert a.hash("x", 12, ColumnKind.TEXT) == Anonymizer(KEY).hash("x", 12, ColumnKind.TEXT)
    assert len(a.hash("x", 12, ColumnKind.TEXT)) == 12
    number = int(a.hash("x", 12, ColumnKind.INTEGER))
    assert 0 <= number < 2**31
    assert a.hash("x", 12, ColumnKind.TEXT) != a.hash("x", 12, ColumnKind.TEXT, attempt=1)


def test_format_preserving() -> None:
    a = Anonymizer(KEY)
    out = a.preserve_format("Ab-12 cD/" + "9" * 40, luhn=False)
    assert len(out) == 49
    assert out[0].isupper()
    assert out[1].islower()
    assert out[2] == "-"
    assert out[3:5].isdigit()
    assert out[5] == " "
    assert out[8] == "/"
    card = a.preserve_format("4111 1111 1111 1111", luhn=True)
    assert detect(card) == (Detector.PAYMENT_CARD,)
    assert a.preserve_format("no digits", luhn=True).replace(" ", "").isalpha()


def test_mapping_unique_and_shared() -> None:
    a = Anonymizer(KEY)
    rule = FakerRule(faker=FakerKind.FIRST_NAME)
    values = [f"name{i}" for i in range(300)]
    mapping = a.mapping(rule, ColumnKind.TEXT, values, unique=True)
    assert len(set(mapping.values())) == 300
    assert mapping == a.mapping(rule, ColumnKind.TEXT, reversed(values), unique=True)
    plain = a.mapping(
        HashRule(hash=HashOptions(length=8)), ColumnKind.TEXT, ["a", "a", "b"], unique=False
    )
    assert set(plain) == {"a", "b"}
    fpe = a.mapping(
        FormatPreservingRule(format_preserving=FormatPreservingOptions()),
        ColumnKind.TEXT,
        ["12"],
        unique=False,
    )
    assert fpe["12"].isdigit()


def test_mapping_fails_when_values_run_out() -> None:
    class Constant(Anonymizer):
        def replacement(self, rule: ValueRule, kind: ColumnKind, value: str, attempt: int) -> str:
            return "same"

    rule = HashRule(hash=HashOptions())
    with pytest.raises(AnonymizeError, match="unique replacement"):
        Constant(KEY).mapping(rule, ColumnKind.TEXT, ["a", "b"], unique=True)
    assert Constant(KEY).mapping(rule, ColumnKind.TEXT, ["a", "b"], unique=False) == {
        "a": "same",
        "b": "same",
    }


def test_new_key() -> None:
    first, second = new_key(), new_key()
    assert len(first) == 32
    assert first != second


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mary.smith@sakilacustomer.org", (Detector.EMAIL,)),
        ("write to jo@example.com or a@b.test", ()),
        ("x@sub.example.org", ()),
        ("card 4111 1111 1111 1111 ok", (Detector.PAYMENT_CARD,)),
        ("4111-1111-1111-1112", ()),
        ("4111111111111111", (Detector.PAYMENT_CARD,)),
        ("3782 822463 10005", (Detector.PAYMENT_CARD,)),
        ("4111 1111-1111 1111", ()),
        ("0000000000000000", ()),
        ("ssn 123-45-6789", (Detector.US_SSN,)),
        ("123-45-6789", (Detector.US_SSN,)),
        ("000-12-3456 666-12-3456 900-12-3456 123-00-4567 123-45-0000", ()),
        ("call (617) 222-3344", (Detector.PHONE,)),
        ("+44 617.222.3344", (Detector.PHONE,)),
        ("+1-617-555-0142", ()),
        ("6172223344", ()),
        ("film 2006-02-15", ()),
    ],
)
def test_detect(value: str, expected: tuple[Detector, ...]) -> None:
    assert detect(value) == expected


def test_like_patterns() -> None:
    company = like_pattern("%@company.com")
    assert allowed("ops@company.com", [company])
    assert not allowed("ops@company.co", [company])
    assert like_pattern("a_c").fullmatch("abc")
    assert like_pattern(r"100\%").fullmatch("100%")
    assert not like_pattern(r"100\%").fullmatch("1000")
    assert like_pattern("x\\").fullmatch("x\\")
    assert not allowed("x", [])


def test_secrets(tmp_path: Path) -> None:
    assert resolve_secret(EnvSecret(env="K"), {"K": "v"}) == "v"
    with pytest.raises(SecretError, match="K is not set"):
        resolve_secret(EnvSecret(env="K"), {"K": ""})
    path = tmp_path / "key"
    path.write_text("value\n", encoding="utf-8")
    assert resolve_secret(FileSecret(file=path)) == "value"
    with pytest.raises(SecretError, match="cannot read secret file"):
        resolve_secret(FileSecret(file=tmp_path / "missing"))
