# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Keeping data values out of engine messages (decisions 54 and 139).

Engine messages quote object names (tables, columns, constraints, roles,
files) far more often than data values, and an error that names the object
is the one worth reading. Each engine adapter builds a :class:`Redactor` from
two lists: the message shapes its engine uses to report data values, whose
values are hidden, and the words that say a quoted string names an object
(``table "orders"``, ``constraint 'pk_orders'``), which is kept. Any other
quoted string is hidden too, so a message nobody has classified stays as
safe as before. A message a user raised (a trigger's or procedure's own
error) can quote anything, so every quoted string in it is hidden.
"""

import re
from collections.abc import Sequence

__all__ = ["MASK", "Redactor", "kinds"]

MASK = "…"
"""What a hidden value becomes."""


def _string(mark: str) -> str:
    """A pattern for one string between ``mark``s.

    A mark followed by a letter or digit belongs to the string (``'O'Brien'``),
    so a value holding the mark is hidden whole rather than cut short.
    """
    q = re.escape(mark)
    return rf"{q}(?:[^{q}]|{q}(?=\w))*{q}"


def kinds(words: str) -> str:
    """A pattern for any of the space-separated ``words`` and the space after it.

    Before a quoted string, it says the string names an object: ``table "orders"``.
    """
    return rf"\b(?:{'|'.join(words.split())})\s+"


# Introducers that may open a string literal: N'…', E'…', X'…', Oracle's
# q'[…]' and nq'…', MySQL's _utf8mb4'…'.
_PREFIX = r"(?<!\w)(?P<prefix>[nebxq]|nq|_\w+)"


def _hide(match: re.Match[str]) -> str:
    start, end = match.span("value")
    whole = match.group(0)
    offset = match.start()
    return f"{whole[: start - offset]}{MASK}{whole[end - offset :]}"


def _hide_spanning(text: str, line: re.Pattern[str], spanning: re.Pattern[str]) -> str:
    """``text`` with the values only ``spanning`` finds, those running over a line break, hidden.

    Where ``line`` matches at the same place, the value was on one line and is
    already hidden, so the greedy ``spanning`` match, which may run on into
    later lines, is passed over.
    """
    position = 0
    while (match := spanning.search(text, position)) is not None:
        if line.match(text, match.start()):
            position = match.start() + 1
            continue
        hidden = _hide(match)
        text = f"{text[: match.start()]}{hidden}{text[match.end() :]}"
        position = match.start() + len(hidden)
    return text


class Redactor:
    """Hides the data values in an engine's messages and keeps the object names.

    ``values`` are the shapes that report data values, each a pattern whose
    ``value`` group, written ``(?P<value>.*)`` or narrower, is hidden. Patterns
    ignore case and match within a line, greedily, so a value holding its own
    quote mark is hidden whole up to the text that follows it
    (``'(?P<value>.*)' for key``). A value that runs over a line break is
    then found by the same pattern spanning lines, greedily to its last
    closing text, so no part of it is left.
    ``names`` are patterns that, right before a quoted string, say it names an
    object (see :func:`kinds`). ``raised`` are patterns that mark a message a
    user raised, in which every quoted string is hidden. ``marks`` are the
    quote characters the engine uses for strings; identifiers in other marks
    (backticks, brackets, Oracle's double quotes) are never touched.
    """

    def __init__(
        self,
        *,
        values: Sequence[str],
        names: Sequence[str],
        marks: str,
        raised: Sequence[str] = (),
    ) -> None:
        """Compile the engine's lists."""
        self.values = tuple(re.compile(value, re.IGNORECASE) for value in values)
        self.spanning = tuple(re.compile(value, re.IGNORECASE | re.DOTALL) for value in values)
        self.raised = re.compile("|".join(raised) or "(?!)")
        strings = "|".join(_string(mark) for mark in marks)
        self.strings = re.compile(
            rf"(?P<name>{'|'.join(names) or '(?!)'})?(?:{_PREFIX}|(?<!\w))(?P<string>{strings})",
            re.IGNORECASE,
        )

    def raises(self, text: str) -> bool:
        """Whether ``text`` carries the mark of a message a user raised."""
        return self.raised.search(text) is not None

    def __call__(self, text: str, *, raised: bool = False) -> str:
        """``text`` with data values, and quoted strings that name nothing, replaced by ``…``.

        When ``raised`` is true, or ``text`` shows it (:meth:`raises`), every
        quoted string is replaced, named or not.
        """
        for shape in self.values:
            text = shape.sub(_hide, text)
        for line, spanning in zip(self.values, self.spanning, strict=True):
            text = _hide_spanning(text, line, spanning)
        everything = raised or self.raises(text)

        def replace(match: re.Match[str]) -> str:
            if match.group("name") is not None and not everything:
                return match.group(0)
            mark = match.group("string")[0]
            return f"{match.group('name') or ''}{match.group('prefix') or ''}{mark}{MASK}{mark}"

        return self.strings.sub(replace, text)
