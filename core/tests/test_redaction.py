# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from datadouble.redaction import Redactor, kinds

REDACT = Redactor(
    values=[r"entry '(?P<value>.*)' for", r"row \((?P<value>.*)\)"],
    names=[kinds("table key")],
    marks="'\"",
    raised=[r"\bRAISED\b"],
)


def test_values_are_hidden_and_names_kept() -> None:
    assert REDACT("entry 'it's' for key 'pk'") == "entry '…' for key 'pk'"
    assert REDACT("table \"orders\" and 'other'") == "table \"orders\" and '…'"


def test_values_stay_on_their_line() -> None:
    assert REDACT("row (1)\ntable 't' row (2)") == "row (…)\ntable 't' row (…)"
    assert REDACT("row (1\n2) and table 't'") == "row (…) and table 't'"


def test_quote_marks_inside_words_are_not_strings() -> None:
    assert REDACT("can't open 'O'Brien' now") == "can't open '…' now"
    assert (
        REDACT("isn't N'x' or _latin1'y' or my_col'z'") == "isn't N'…' or _latin1'…' or my_col'z'"
    )


def test_raised_messages_hide_names_too() -> None:
    assert REDACT("RAISED: table 'secret'") == "RAISED: table '…'"
    assert REDACT("table 'secret'", raised=True) == "table '…'"
    assert REDACT.raises("RAISED")
    assert not REDACT.raises("raised")


def test_without_names_every_string_is_hidden() -> None:
    assert Redactor(values=[], names=[], marks="'")("table 'x'") == "table '…'"
