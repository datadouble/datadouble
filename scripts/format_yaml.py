# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Rewrite YAML flow mappings in block style (decision 137).

The repository writes every YAML mapping in block style, one key per line,
never as a flow mapping such as ``{path: /srv, local: {node: a}}``. A sequence
item that is a mapping becomes ``- key: value``. Empty ``{}`` and ``[]`` stay,
and so does a flow sequence of plain scalars (``include: [billing.*]``); a flow
sequence that holds a mapping becomes a block sequence.

The rewrite is textual and line by line, so comments, key order, quoting and
everything that isn't a flow mapping stay byte for byte. It reads:

- every tracked ``*.yml`` and ``*.yaml`` file;
- every fenced ```` ```yaml ```` or ```` ```yml ```` block in a tracked
  ``*.md`` file;
- every triple-quoted plain (not f-, byte or ``.format``) string in a tracked
  ``*.py`` file that parses as YAML.

A rewrite must load to the same data as the original wherever the original
loads (a Python string must load), or the formatter leaves the text alone and
reports it. Lines inside block scalars (``|``, ``>``) are never touched. Go
template actions (``{{ ... }}``, as in Helm templates) are treated as opaque
scalars. A flow mapping it cannot rewrite safely (one with an anchor, tag,
complex key or a comment inside it) is reported as a problem for a human.

``uv run python scripts/format_yaml.py`` rewrites the repository in place;
``--check`` rewrites nothing and lists each offending ``file:line``, exiting 1.
Paths given as arguments are read instead of the tracked files.
"""

import argparse
import io
import re
import sys
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from datadouble.programs import run_program

_INDENT = 2
_MAX_CONTINUATION = 50
_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?:yaml|yml)[ \t]*$")
_KEY = re.compile(
    r"""(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#'"{}\[\],&*!|>%@`?-][^#]*?|-[^\s#][^#]*?)"""
    r":(?:[ ]+(?P<value>.*))?$"
)
_CHANGES = "the rewrite changes the data"
_ANCHOR = "an anchor, tag or complex key"
_UNEXPECTED = "an unexpected character in a flow collection"
_OPEN_FIELD = "an unterminated replacement field"
_OPEN_ACTION = "an unterminated template action"
_INNER_COMMENT = "a comment inside a flow collection"
_NO_VALUE = "a key without a value"
_LONE_BRACE = "a lone closing brace"
_PAIR_IN_SEQUENCE = "a single-pair mapping in a flow sequence"
_TRAILING_TEXT = "text after a flow collection"
_UNTERMINATED = "an unterminated flow collection"
_BLOCK_SCALAR = re.compile(r"^[|>][-+0-9]*$")


class FlowError(ValueError):
    """A flow mapping this formatter cannot rewrite without risk."""


class IncompleteError(FlowError):
    """A flow collection that continues on the next line."""

    def __init__(self) -> None:
        """An error naming the unterminated collection."""
        super().__init__(_UNTERMINATED)


@dataclass(frozen=True)
class Scalar:
    """A scalar, kept as its source text."""

    text: str


@dataclass(frozen=True)
class FlowMap:
    """A flow mapping: its keys' source text and their values."""

    items: tuple[tuple[str, "Node"], ...]


@dataclass(frozen=True)
class FlowSeq:
    """A flow sequence and its source text."""

    items: tuple["Node", ...]
    text: str


type Node = Scalar | FlowMap | FlowSeq


def _block(node: Node) -> bool:
    """Whether a node must be written in block style."""
    if isinstance(node, FlowMap):
        return bool(node.items)
    if isinstance(node, FlowSeq):
        return any(isinstance(item, FlowMap) or _block(item) for item in node.items)
    return False


def _inline(node: Node) -> str:
    """A node that stays on one line, as its text."""
    if isinstance(node, FlowMap):
        return "{}"
    return node.text


class _Parser:
    """A recursive-descent reader of one flow collection."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _space(self) -> None:
        while self._peek() in (" ", "\t") and self._peek():
            self.pos += 1

    def _need(self) -> None:
        self._space()
        if not self._peek():
            raise IncompleteError

    def node(self, ends: str) -> Node:
        """Read one node, stopping before any character in ``ends``."""
        self._need()
        char = self._peek()
        if char == "{" and not self.text.startswith("{{", self.pos):
            return self._mapping()
        if char == "[":
            return self._sequence()
        if char in ("&", "!", "?"):
            raise FlowError(_ANCHOR)
        return Scalar(self._scalar(ends))

    def _quoted(self) -> None:
        quote = self._peek()
        self.pos += 1
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if quote == '"' and char == "\\":
                self.pos += 2
                continue
            self.pos += 1
            if char == quote:
                if quote == "'" and self._peek() == "'":
                    self.pos += 1
                    continue
                return
        raise IncompleteError

    def _scalar(self, ends: str) -> str:
        start = self.pos
        if self._peek() in ('"', "'"):
            self._quoted()
            return self.text[start : self.pos]
        while self.pos < len(self.text):
            if self.text.startswith("{{", self.pos):
                close = self.text.find("}}", self.pos)
                if close < 0:
                    raise FlowError(_OPEN_ACTION)
                self.pos = close + 2
                continue
            char = self.text[self.pos]
            following = self.text[self.pos + 1 : self.pos + 2]
            if char in ends or (char == ":" and following in ("", " ", "\t", ",", "}", "]")):
                break
            if char == "#" and self.pos > start and self.text[self.pos - 1] in " \t":
                raise FlowError(_INNER_COMMENT)
            self.pos += 1
        return self.text[start : self.pos].rstrip()

    def _expect_separator(self, close: str) -> bool:
        """Consume ``,`` or ``close``; return whether the collection ended."""
        self._need()
        char = self._peek()
        self.pos += 1
        if char == close:
            return True
        if char != ",":
            raise FlowError(_UNEXPECTED)
        return False

    def _mapping(self) -> FlowMap:
        self.pos += 1
        items: list[tuple[str, Node]] = []
        self._need()
        if self._peek() == "}":
            self.pos += 1
            return FlowMap(())
        while True:
            self._need()
            if self._peek() in ("&", "!", "?", "{", "["):
                raise FlowError(_ANCHOR)
            key = self._scalar(",}")
            self._need()
            if not key or self._peek() != ":":
                raise FlowError(_NO_VALUE)
            self.pos += 1
            self._need()
            value: Node = Scalar("") if self._peek() in (",", "}") else self.node(",}")
            items.append((key, value))
            if self._expect_separator("}"):
                return FlowMap(tuple(items))

    def _sequence(self) -> FlowSeq:
        start = self.pos
        self.pos += 1
        items: list[Node] = []
        self._need()
        if self._peek() == "]":
            self.pos += 1
            return FlowSeq((), "[]")
        while True:
            items.append(self.node(",]"))
            self._need()
            if self._peek() == ":":
                raise FlowError(_PAIR_IN_SEQUENCE)
            if self._expect_separator("]"):
                return FlowSeq(tuple(items), self.text[start : self.pos])


def parse(text: str) -> tuple[Node, str]:
    """Read the flow collection at the start of ``text``; return it and the rest."""
    parser = _Parser(text)
    node = parser.node("")
    return node, text[parser.pos :]


def _map_lines(items: tuple[tuple[str, Node], ...], column: int) -> list[str]:
    lines: list[str] = []
    for key, value in items:
        lines.extend(_entry_lines(" " * column + key + ":", value, column + _INDENT))
    return lines


def _seq_lines(items: tuple[Node, ...], column: int) -> list[str]:
    lines: list[str] = []
    dash = " " * column + "- "
    for item in items:
        if not isinstance(item, Scalar) and _block(item):
            nested = _nested_lines(item, column + 2)
            lines.append(dash + nested[0][column + 2 :])
            lines.extend(nested[1:])
        else:
            lines.append(dash + _inline(item))
    return lines


def _nested_lines(node: FlowMap | FlowSeq, column: int) -> list[str]:
    if isinstance(node, FlowMap):
        return _map_lines(node.items, column)
    return _seq_lines(node.items, column)


def _entry_lines(head: str, value: Node, column: int) -> list[str]:
    """``head`` (``key:``) and its value, indented to ``column`` when in block style."""
    if not isinstance(value, Scalar) and _block(value):
        return [head, *_nested_lines(value, column)]
    text = _inline(value)
    return [f"{head} {text}" if text else head]


@dataclass
class Result:
    """The rewritten text, and the lines (1-based) it changed or could not."""

    text: str
    changed: list[int] = field(default_factory=list[int])
    problems: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])


def _strip_comment(rest: str) -> str:
    """The comment after a flow collection, if any."""
    stripped = rest.lstrip(" \t")
    if stripped and not (stripped.startswith("#") and len(stripped) < len(rest)):
        raise FlowError(_TRAILING_TEXT)
    return stripped


def _prefix(line: str) -> tuple[int, bool]:
    """The column after a line's indentation and ``- `` indicators, and whether it has any."""
    column = len(line) - len(line.lstrip(" "))
    dashes = False
    while line.startswith("- ", column):
        dashes = True
        column += 1
        while line.startswith(" ", column):
            column += 1
    return column, dashes


def _candidate(value: str) -> bool:
    if value.startswith("{") and not value.startswith("{{"):
        return True
    return value.startswith("[") and "{" in value.replace("{{", "")


def _parse_lines(value: str, lines: list[str], index: int) -> tuple[Node, str, int]:
    """Parse a flow collection that may continue over the lines after ``index``.

    Returns it, the text after it, and how many lines it spans.
    """
    text = value
    used = 1
    while True:
        try:
            node, after = parse(text)
        except IncompleteError:
            if index + used >= len(lines) or used >= _MAX_CONTINUATION or "#" in text:
                raise
            text = f"{text} {lines[index + used].strip()}"
            used += 1
        else:
            return node, after, used


def _rewrite_line(lines: list[str], index: int) -> tuple[list[str], int, int | None] | None:
    """Rewrite the flow collection starting on ``lines[index]``.

    Returns the new lines, how many source lines they replace, and the column
    after which a block scalar's content would start, or None when the line
    holds nothing to rewrite.
    """
    line = lines[index]
    column, dashes = _prefix(line)
    rest = line[column:]
    head: str | None
    if dashes and rest[:1] in ("{", "["):
        value = rest
        head = None
    else:
        key = _KEY.match(rest)
        if key is None:
            return None
        value = key["value"] or ""
        head = line[: column + key.end("key") + 1]
        if _BLOCK_SCALAR.match(value.split(" #")[0].rstrip()):
            return [line], 1, column
    if not _candidate(value):
        return None
    node, after, used = _parse_lines(value, lines, index)
    if isinstance(node, Scalar) or not _block(node):
        return None
    comment = _strip_comment(after)
    if head is None:
        nested = _nested_lines(node, column)
        out = [line[:column] + nested[0][column:], *nested[1:]]
    else:
        out = _entry_lines(head, node, column + _INDENT)
    if comment:
        # Keep the comment in its column, where it lines up with its neighbours.
        out[0] = out[0].ljust(len(line) - len(comment) - 2) + "  " + comment
    return out, used, None


def format_text(text: str) -> Result:
    """Rewrite the flow mappings in one YAML text."""
    lines = text.split("\n")
    out: list[str] = []
    result = Result(text)
    index = 0
    scalar_column: int | None = None
    while index < len(lines):
        line = lines[index]
        if scalar_column is not None:
            if not line.strip() or len(line) - len(line.lstrip(" ")) > scalar_column:
                out.append(line)
                index += 1
                continue
            scalar_column = None
        try:
            rewritten = _rewrite_line(lines, index)
        except FlowError as error:
            result.problems.append((index + 1, str(error)))
            rewritten = None
        if rewritten is None:
            out.append(line)
            index += 1
            continue
        new, used, scalar_column = rewritten
        if new != lines[index : index + used]:
            result.changed.append(index + 1)
        out.extend(new)
        index += used
    result.text = "\n".join(out)
    return result


def _loads(text: str) -> list[object] | None:
    """A text's YAML documents, or None when it isn't YAML."""
    try:
        return list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return None


def _same_data(before: str, result: Result) -> bool:
    """Whether the rewrite loads to the same data as the original, if that loads."""
    original = _loads(before)
    return original is None or _loads(result.text) == original


def format_yaml(text: str) -> Result:
    """Rewrite a YAML text, keeping it unchanged unless its data stays the same."""
    result = format_text(text)
    if result.changed and not _same_data(text, result):
        return Result(text, [], [(line, _CHANGES) for line in result.changed])
    return result


def format_markdown(text: str) -> Result:
    """Rewrite the YAML in a Markdown text's ``yaml`` and ``yml`` fences."""
    lines = text.split("\n")
    result = Result(text)
    index = shift = 0
    while index < len(lines):
        fence = _FENCE.match(lines[index])
        index += 1
        if fence is None:
            continue
        indent, marker = fence["indent"], fence["fence"]
        end = index
        while end < len(lines) and lines[end].strip() != marker:
            end += 1
        body = [line.removeprefix(indent) for line in lines[index:end]]
        block = format_yaml("\n".join(body))
        # Report lines as the original numbers them.
        first = index - shift
        result.problems.extend((first + line, problem) for line, problem in block.problems)
        if block.changed:
            result.changed.extend(first + line for line in block.changed)
            lines[index:end] = [indent + line if line else line for line in block.text.split("\n")]
            new_end = index + block.text.count("\n") + 1
            shift += new_end - end
            end = new_end
        index = end + 1
    result.text = "\n".join(lines)
    return result


def _offsets(text: str) -> list[int]:
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


_FIELD_OPEN, _LINE_OPEN, _FIELD_CLOSE = "\ue000", "#\ue002", "\ue001"
_FIELD = re.compile(f"(?:{_FIELD_OPEN}|{_LINE_OPEN})(\\d+){_FIELD_CLOSE}")


def _unformat(template: str) -> tuple[str, list[str]]:
    """A format template as YAML: ``{{`` and ``}}`` unescaped, fields as opaque scalars.

    Raises FlowError for a template whose braces don't pair up.
    """
    out: list[str] = []
    fields: list[str] = []
    index = 0
    while index < len(template):
        char = template[index]
        pair = template[index : index + 2]
        if pair in ("{{", "}}"):
            out.append(char)
            index += 2
        elif char == "{":
            depth, end = 0, index
            while end < len(template):
                depth += {"{": 1, "}": -1}.get(template[end], 0)
                if depth == 0:
                    break
                end += 1
            if depth:
                raise FlowError(_OPEN_FIELD)
            # A field alone on its line, such as more YAML, reads as a comment.
            alone = template[end + 1 : end + 2] in ("", "\n") and (
                not out or out[-1].endswith("\n")
            )
            out.append(f"{_LINE_OPEN if alone else _FIELD_OPEN}{len(fields)}{_FIELD_CLOSE}")
            fields.append(template[index : end + 1])
            index = end + 1
        elif char == "}":
            raise FlowError(_LONE_BRACE)
        else:
            out.append(char)
            index += 1
    return "".join(out), fields


def _reformat(text: str, fields: list[str]) -> str:
    """The inverse of :func:`_unformat`."""
    escaped = text.replace("{", "{{").replace("}", "}}")
    return _FIELD.sub(lambda match: fields[int(match[1])], escaped)


def _string_yaml(source: str, *, template: bool) -> Result | None:
    """Rewrite a triple-quoted literal's source, or None when it isn't one to read."""
    quote = source.lstrip("rRuUfF")
    prefix = source[: len(source) - len(quote)]
    if quote[:3] not in ('"""', "'''") or "\\" in quote or "b" in prefix.lower():
        return None
    body = quote[3:-3]
    fields: list[str] = []
    if template:
        try:
            body, fields = _unformat(body)
        except FlowError:
            return None
    if _loads(body) is None:
        # Doubled braces suggest a template that is formatted elsewhere.
        if template or "{{" not in body:
            return None
        return _string_yaml(source, template=True)
    block = format_yaml(body)
    if template:
        block.text = _reformat(block.text, fields)
    block.text = prefix + quote[:3] + block.text + quote[:3]
    return block


def format_python(text: str) -> Result:
    """Rewrite the YAML in a Python text's triple-quoted strings.

    An f-string, or a string followed by ``.format``, is a template: its
    doubled braces are literal braces and its replacement fields opaque
    scalars. A string followed by ``%`` is left alone.
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    starts = _offsets(text)
    result = Result(text)
    edits: list[tuple[int, int, Result]] = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        first, last = token, token
        if token.type == tokenize.FSTRING_START:
            depth = 1
            while depth:
                position += 1
                last = tokens[position]
                depth += {tokenize.FSTRING_START: 1, tokenize.FSTRING_END: -1}.get(last.type, 0)
        elif token.type != tokenize.STRING:
            position += 1
            continue
        position += 1
        following = [t.string for t in tokens[position : position + 2]]
        if following[:1] == ["%"]:
            continue
        begin = starts[first.start[0] - 1] + first.start[1]
        end = starts[last.end[0] - 1] + last.end[1]
        template = token.type == tokenize.FSTRING_START or following == [".", "format"]
        block = _string_yaml(text[begin:end], template=template)
        if block is None:
            continue
        # A string's own syntax, prose or JSON, isn't a problem: only a rewrite
        # that would change its data is.
        line = first.start[0] - 1
        changes = [(at, problem) for at, problem in block.problems if problem == _CHANGES]
        result.problems.extend((line + at, problem) for at, problem in changes)
        if block.changed:
            result.changed.extend(line + at for at in block.changed)
            edits.append((begin, end, block))
    for begin, end, block in reversed(edits):
        text = text[:begin] + block.text + text[end:]
    result.text = text
    return result


_FORMATTERS = {
    ".yml": format_yaml,
    ".yaml": format_yaml,
    ".md": format_markdown,
    ".py": format_python,
}


def tracked_files() -> list[Path]:
    """The files git tracks or would track (not ignored) that the formatter reads."""
    listing = run_program("git", ["ls-files", "--cached", "--others", "--exclude-standard"])
    if listing.status != 0:
        message = f"git ls-files failed: {listing.last_error()}"
        raise RuntimeError(message)
    return [
        Path(name)
        for name in listing.stdout.splitlines()
        if Path(name).suffix in _FORMATTERS and Path(name).is_file()
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Format, or with ``--check`` only report, the YAML in the given or tracked files."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="report, don't rewrite")
    parser.add_argument("paths", nargs="*", type=Path)
    arguments = parser.parse_args(argv)
    paths: list[Path] = arguments.paths or tracked_files()
    changed = problems = 0
    for path in paths:
        text = path.read_text("utf-8")
        result = _FORMATTERS[path.suffix](text)
        for line, problem in result.problems:
            sys.stdout.write(f"{path}:{line}: cannot rewrite: {problem}\n")
        problems += len(result.problems)
        if not result.changed:
            continue
        changed += 1
        if arguments.check:
            for line in result.changed:
                sys.stdout.write(f"{path}:{line}: flow mapping, write it in block style\n")
        else:
            path.write_text(result.text, "utf-8")
            sys.stdout.write(f"formatted {path}\n")
    verb = "need formatting" if arguments.check else "formatted"
    sys.stdout.write(f"{changed} file(s) {verb}, {problems} problem(s)\n")
    return 1 if problems or (arguments.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
