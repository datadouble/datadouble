# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

"""Inferring an object's dependencies from the text of its definition.

Engines record exactly what views and triggers depend on, but rarely what a
procedural routine's body uses. For those bodies an adapter calls
:func:`infer_dependencies`, which strips comments and string literals, reads
the identifiers left, and matches them against the catalog's tables and
objects by name. Every edge it returns is marked ``inferred``: it is a best
effort, which a name that happens to match a column or a variable can fool,
and a statement built from strings at run time escapes.

The matching is engine-neutral; the adapter describes its engine's
:class:`Dialect`: how it quotes identifiers and strings and how it comments.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from datadouble.catalog import Dependency, ObjectKind, ObjectRef, SourceObject, Table

_QUOTED = {
    '"': r'"(?:[^"]|"")+"',
    "`": r"`(?:[^`]|``)+`",
    "[": r"\[(?:[^\]]|\]\])+\]",
}
_WORD = r"[^\W\d][\w$#@]*"
"""An unquoted identifier: a letter or underscore, any script, then word characters."""

_NAMED_IN_CODE = frozenset(
    {
        ObjectKind.FUNCTION,
        ObjectKind.PROCEDURE,
        ObjectKind.PACKAGE,
        ObjectKind.VIEW,
        ObjectKind.MATERIALIZED_VIEW,
        ObjectKind.TYPE,
        ObjectKind.SEQUENCE,
    }
)
"""Kinds a body names to use them; triggers and events are never called by name."""


@dataclass(frozen=True)
class Dialect:
    """How an engine spells identifiers, literals and comments in its routine bodies."""

    quotes: str
    """The characters that open a quoted identifier: ``"``, a backtick, ``[``."""
    backslash_escapes: bool = False
    """Whether a backslash escapes the next character in a string literal."""
    hash_comments: bool = False
    """Whether ``#`` starts a comment, as in MySQL and PL/Python."""
    double_quoted_strings: bool = False
    """Whether ``"..."`` is a string, as in MySQL's default SQL mode."""
    dollar_quotes: bool = False
    """Whether ``$tag$...$tag$`` is a string, as in PostgreSQL; ``E'...'`` escapes too."""

    def pattern(self) -> re.Pattern[str]:
        """A name of one to three dotted parts."""
        part = "(?:" + "|".join([*(_QUOTED[q] for q in self.quotes), _WORD]) + ")"
        return re.compile(rf"({part})(?:\s*\.\s*({part}))?(?:\s*\.\s*({part}))?")

    def strip(self, text: str) -> str:
        """``text`` without comments and string literals, keeping quoted identifiers.

        One scan reads every token left to right, so a ``--`` inside a quoted
        identifier or a quote inside a dollar-quoted string is read correctly.
        """
        escaped = r"'(?:[^'\\]|\\.|'')*'"
        skipped = [r"--[^\n]*", r"/\*.*?\*/"]
        if self.hash_comments:
            skipped.append(r"#[^\n]*")
        if self.dollar_quotes:
            skipped += [r"\$(?P<tag>(?:[^\W\d]\w*)?)\$.*?\$(?P=tag)\$", r"[Ee]" + escaped]
        skipped.append(escaped if self.backslash_escapes else r"'(?:[^']|'')*'")
        if self.double_quoted_strings:
            skipped.append(r'"(?:[^"\\]|\\.|"")*"')
        kept = [_QUOTED[q] for q in self.quotes]
        scan = re.compile(
            "|".join([f"(?P<kept>{'|'.join(kept)})", *skipped]) if kept else "|".join(skipped),
            flags=re.DOTALL,
        )
        return scan.sub(lambda found: found["kept"] or " " if kept else " ", text)


def _unquote(part: str) -> str:
    if part[:1] in _QUOTED and len(part) > 1:
        closing = "]" if part[0] == "[" else part[0]
        return part[1:-1].replace(closing * 2, closing)
    return part


@dataclass
class Candidates:
    """The tables and objects a body might name, by lowercased name, with their namespaces."""

    by_name: defaultdict[str, list[tuple[tuple[str, ...], Dependency]]] = field(
        default_factory=lambda: defaultdict[str, list[tuple[tuple[str, ...], Dependency]]](list)
    )

    @classmethod
    def of(cls, tables: Iterable[Table], objects: Iterable[SourceObject]) -> "Candidates":
        """Every table, and every object a body can name."""
        found = cls()
        for table in tables:
            found.add(
                table.name.namespace, table.name.name, Dependency(table=table.name, inferred=True)
            )
        for item in objects:
            if item.ref.kind in _NAMED_IN_CODE:
                found.add(
                    item.ref.namespace, item.ref.name, Dependency(object=item.ref, inferred=True)
                )
        return found

    def add(self, namespace: tuple[str, ...], name: str, edge: Dependency) -> None:
        """Index ``edge`` under its name."""
        self.by_name[name.lower()].append((_lower(namespace), edge))

    def lookup(self, parts: tuple[str, ...], home: tuple[str, ...]) -> list[Dependency]:
        """The candidates a dotted name may mean.

        The name's qualifiers and a candidate's namespace must agree from the
        right, as spec names match: ``public.users`` fits ``shop.public``, and
        a database-qualified ``shop.dbo.users`` fits ``dbo``. Of those, the ones sharing the
        most leading namespaces with ``home`` (the object's own) win: an
        unqualified name means the object's own schema first, then its own
        database, then anywhere.
        """
        qualifiers = _lower(parts[:-1])
        found = [
            (namespace, edge)
            for namespace, edge in self.by_name.get(parts[-1].lower(), [])
            if _tail(namespace, len(qualifiers)) == _tail(qualifiers, len(namespace))
        ]
        scores = [_shared(namespace, _lower(home)) for namespace, _ in found]
        best = max(scores, default=0)
        return [edge for (_, edge), score in zip(found, scores, strict=True) if score == best]


def _lower(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(part.lower() for part in parts)


def _tail(parts: tuple[str, ...], count: int) -> tuple[str, ...]:
    return parts[len(parts) - count :] if count < len(parts) else parts


def _shared(one: tuple[str, ...], other: tuple[str, ...]) -> int:
    """How many leading parts two namespaces share."""
    count = 0
    for left, right in zip(one, other, strict=False):
        if left != right:
            break
        count += 1
    return count


def infer_dependencies(
    text: str, own: ObjectRef, candidates: Candidates, dialect: Dialect
) -> tuple[Dependency, ...]:
    """The tables and objects ``text`` names, as inferred edges, leaving out ``own``."""
    edges: dict[Dependency, None] = {}
    for match in dialect.pattern().finditer(dialect.strip(text)):
        parts = tuple(_unquote(part) for part in match.groups() if part)
        # `users.email` names the table users; `public.users` names it too.
        for end in range(len(parts), 0, -1):
            for edge in candidates.lookup(parts[:end], own.namespace):
                if edge.object != own:
                    edges[edge] = None
    return tuple(edges)


def merge(exact: Iterable[Dependency], inferred: Iterable[Dependency]) -> tuple[Dependency, ...]:
    """Exact edges, then inferred ones that no exact edge already covers."""
    known = {(edge.table, edge.object) for edge in exact}
    extra = [edge for edge in inferred if (edge.table, edge.object) not in known]
    return (*dict.fromkeys(exact), *dict.fromkeys(extra))
