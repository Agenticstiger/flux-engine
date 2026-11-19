# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The flux-expr/2 and flux-expr/3 syntax tree, its JSON form and its canonical text (EXPR-SPEC-2 §11, EXPR-SPEC-3 §5).

Every node is a frozen dataclass over tuples, so a tree is hashable and two
spellings of one text compare equal. A node's ``position`` (its first
token's offset) and the parser's notes for ``FLX-6105`` are excluded from
equality, from the hash and from the JSON form: they say where a node was
written, not what it is.

:func:`to_json` gives a core node one ``{"node": ...}`` object per construct
of RFC-data-rules.md §2.1, in the shapes of forge-cli's
``fluid-sql-vectors.json`` (its ``nodes``: ``round``, ``coalesce`` and
``date_diff`` for the calls, ``is_null`` and ``distinct_from`` for the
tests, ``left`` for the tested value), and the four extension nodes the
shapes EXPR-SPEC-2 §11 fixes. flux-expr/3 adds ``extract``, ``date_add`` and
``concat`` (EXPR-SPEC-3 §5). No JSON number appears: a number keeps its
literal text, so its declared scale survives, as in flux-expr/1.

:func:`unparse` writes the canonical text that ``plan.json`` stores, and
``parse(unparse(tree))`` gives *tree* back: keywords and function names in
upper case, names bare unless RFC §2.1 needs quotes, one space around each
binary operator and after each comma, the fewest parentheses that keep the
tree, ``ROUND``'s default mode, ``ASC`` and ``NULLS LAST`` left out. A
FLUX-side text writes ``row.<column>`` as the bare column and a path from
``link`` without ``link.``, except where the next name is itself a root
(EXPR-SPEC-2 §3); a ``PARTITION BY`` relationship gets ``link.`` where its
name is a root or also a column, which is why :func:`unparse` takes the
expose's column names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import AbstractSet, Any, Dict, List, Optional, Tuple

__all__ = [
    "AGGREGATES",
    "DATE_ADD_FUNCTIONS",
    "EXTRACT_FIELDS",
    "LANGUAGES",
    "And",
    "Arith",
    "Between",
    "Bool",
    "CORE_FUNCTIONS",
    "CORE_KEYWORDS",
    "Call",
    "Case",
    "Children",
    "Column",
    "Compare",
    "Concat",
    "DEFAULT_ROUND_MODE",
    "Date",
    "DateAdd",
    "Extract",
    "In",
    "IsDistinctFrom",
    "IsNull",
    "Neg",
    "Node",
    "Not",
    "Null",
    "Number",
    "Or",
    "Path",
    "ROOTS",
    "ROUND_MODES",
    "SortKey",
    "String",
    "Aggregate",
    "WINDOWS",
    "Window",
    "is_extension",
    "is_other_row",
    "to_json",
    "unparse",
]

#: The words that RFC §2.1's grammar spells as terminals. A name that is one
#: of them is double-quoted. Function names are not keywords: ``round`` is a
#: column, ``round(`` the call.
CORE_KEYWORDS = frozenset(
    {
        "AND",
        "BETWEEN",
        "CASE",
        "DATE",
        "DISTINCT",
        "ELSE",
        "END",
        "FALSE",
        "FROM",
        "IN",
        "IS",
        "NOT",
        "NULL",
        "OR",
        "THEN",
        "TRUE",
        "WHEN",
    }
)
CORE_FUNCTIONS = frozenset({"ROUND", "COALESCE", "DAYS_BETWEEN", "MONTHS_BETWEEN", "YEARS_BETWEEN"})
AGGREGATES = frozenset({"COUNT", "SUM", "MIN", "MAX", "AVG"})
WINDOWS = frozenset(
    {"ROW_NUMBER", "RANK", "DENSE_RANK", "FIRST_VALUE", "LAST_VALUE", "LAG", "LEAD"}
)
#: The roots of a FLUX-side text (EXPR-SPEC-2 §3), compared case-insensitively.
ROOTS = frozenset({"row", "link", "previous", "children"})
DEFAULT_ROUND_MODE = "HALF_AWAY_FROM_ZERO"
ROUND_MODES = frozenset({DEFAULT_ROUND_MODE, "HALF_TO_EVEN"})

#: The languages a text may be read as. A FLUX-side text declares one
#: (EXPR-SPEC-2 §1, EXPR-SPEC-3 §1); flux-expr/3 is flux-expr/2 plus three
#: extensions, and a core (contract) text is FLUID SQL/1 whichever is named.
LANGUAGES = ("flux-expr/2", "flux-expr/3")
#: ``EXTRACT``'s fields (the SQL standard's extract expression, EXPR-SPEC-3 §2).
EXTRACT_FIELDS = frozenset({"YEAR", "MONTH", "DAY"})
#: Db2's date arithmetic, with the unit each moves by, spelled as ``date_diff``'s units.
DATE_ADD_FUNCTIONS = {"ADD_DAYS": "day", "ADD_MONTHS": "month", "ADD_YEARS": "year"}

_BARE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Node:
    """Base class of every syntax-tree node."""

    __slots__ = ()


def _pos() -> Any:
    return field(default=0, compare=False, repr=False)


def _note() -> Any:
    return field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Number(Node):
    """A number literal: ASCII digits with an optional fraction, unsigned, as written."""

    text: str
    position: int = _pos()

    @property
    def value(self) -> Fraction:
        return Fraction(self.text)

    @property
    def scale(self) -> int:
        _, _, fraction = self.text.partition(".")
        return len(fraction)


@dataclass(frozen=True)
class String(Node):
    value: str
    position: int = _pos()


@dataclass(frozen=True)
class Bool(Node):
    value: bool
    position: int = _pos()


@dataclass(frozen=True)
class Null(Node):
    position: int = _pos()


@dataclass(frozen=True)
class Date(Node):
    """``DATE 'YYYY-MM-DD'``; the parser has checked it is a calendar date."""

    value: str
    position: int = _pos()


@dataclass(frozen=True)
class Column(Node):
    """A column of the row (``net``) or of the row one relationship reaches (``product.speed``).

    ``root_note`` is the parser's note that the first name was a root word
    written bare, which the checker turns into ``FLX-6105`` when the expose
    also has a column or relationship of that name.
    """

    name: str
    relationship: Optional[str] = None
    root_note: Optional[str] = _note()
    position: int = _pos()


@dataclass(frozen=True)
class Path(Node):
    """An extension path: ``previous.<column>``, ``previous.<rel>.<column>``, or two links or more.

    ``parts`` is the relationships in order, then the column.
    """

    root: str
    parts: Tuple[str, ...]
    root_note: Optional[str] = _note()
    position: int = _pos()


@dataclass(frozen=True)
class Children(Node):
    """``CHILDREN.<expose>[.<relationship>]``, only as an aggregate's first argument."""

    expose: str
    relationship: Optional[str] = None
    root_note: Optional[str] = _note()
    position: int = _pos()


@dataclass(frozen=True)
class Neg(Node):
    operand: Node
    position: int = _pos()


@dataclass(frozen=True)
class Arith(Node):
    op: str
    left: Node
    right: Node
    position: int = _pos()


@dataclass(frozen=True)
class Compare(Node):
    """``=``, ``<>``, ``<``, ``<=``, ``>``, ``>=``; ``!=`` is read as ``<>``."""

    op: str
    left: Node
    right: Node
    position: int = _pos()


@dataclass(frozen=True)
class In(Node):
    value: Node
    items: Tuple[Node, ...]
    negated: bool = False
    position: int = _pos()


@dataclass(frozen=True)
class Between(Node):
    value: Node
    low: Node
    high: Node
    negated: bool = False
    position: int = _pos()


@dataclass(frozen=True)
class IsNull(Node):
    value: Node
    negated: bool = False
    position: int = _pos()


@dataclass(frozen=True)
class IsDistinctFrom(Node):
    """``a IS DISTINCT FROM b``; ``negated`` is ``IS NOT DISTINCT FROM``."""

    left: Node
    right: Node
    negated: bool = False
    position: int = _pos()


@dataclass(frozen=True)
class Not(Node):
    operand: Node
    position: int = _pos()


@dataclass(frozen=True)
class And(Node):
    left: Node
    right: Node
    position: int = _pos()


@dataclass(frozen=True)
class Or(Node):
    left: Node
    right: Node
    position: int = _pos()


@dataclass(frozen=True)
class Case(Node):
    whens: Tuple[Tuple[Node, Node], ...]
    otherwise: Optional[Node] = None
    position: int = _pos()


@dataclass(frozen=True)
class Call(Node):
    """A core call; ``mode`` is ROUND's rounding mode, filled in when not written."""

    name: str
    args: Tuple[Node, ...]
    mode: Optional[str] = None
    position: int = _pos()


@dataclass(frozen=True)
class Aggregate(Node):
    name: str
    children: Children
    arg: Optional[Node] = None
    position: int = _pos()


@dataclass(frozen=True)
class SortKey:
    key: Node
    desc: bool = False
    nulls_first: bool = False


@dataclass(frozen=True)
class Window(Node):
    """A window over the row's siblings; ``partition`` is the relationship's name.

    ``partition_bare`` notes that the partition was written without
    ``link.``, which is ``FLX-6105`` when the name is also a column.
    """

    name: str
    args: Tuple[Node, ...]
    partition: str
    order: Tuple[SortKey, ...]
    partition_bare: bool = field(default=False, compare=False, repr=False)
    position: int = _pos()


@dataclass(frozen=True)
class Extract(Node):
    """``EXTRACT(<field> FROM <date>)``, the SQL standard's date part (EXPR-SPEC-3 §2).

    ``part`` is the field in lower case (``year``, ``month``, ``day``).
    """

    part: str
    arg: Node
    position: int = _pos()


@dataclass(frozen=True)
class DateAdd(Node):
    """``ADD_DAYS``, ``ADD_MONTHS`` or ``ADD_YEARS`` (Db2's names, EXPR-SPEC-3 §2).

    ``unit`` is ``day``, ``month`` or ``year``, as ``date_diff`` spells them.
    """

    unit: str
    date: Node
    count: Node
    position: int = _pos()


@dataclass(frozen=True)
class Concat(Node):
    """``left || right``, the SQL standard's concatenation (EXPR-SPEC-3 §2); a chain is left-nested."""

    left: Node
    right: Node
    position: int = _pos()


def is_other_row(node: Node) -> bool:
    """Whether *node* names a row other than its own: a path, children, an aggregate or a window."""
    return isinstance(node, (Path, Children, Aggregate, Window))


def is_extension(node: Node) -> bool:
    """Whether *node* is one of the extension nodes of EXPR-SPEC-2 or EXPR-SPEC-3."""
    return is_other_row(node) or isinstance(node, (Extract, DateAdd, Concat))


# --------------------------------------------------------------------- JSON


def to_json(node: Node) -> Dict[str, Any]:
    """The canonical JSON object of *node* (EXPR-SPEC-2 §11)."""
    if isinstance(node, Number):
        return {"node": "number", "value": node.text}
    if isinstance(node, String):
        return {"node": "string", "value": node.value}
    if isinstance(node, Bool):
        return {"node": "bool", "value": node.value}
    if isinstance(node, Null):
        return {"node": "null"}
    if isinstance(node, Date):
        return {"node": "date", "value": node.value}
    if isinstance(node, Column):
        out: Dict[str, Any] = {"node": "column", "name": node.name}
        if node.relationship is not None:
            out["relationship"] = node.relationship
        return out
    if isinstance(node, Path):
        return {"node": "path", "root": node.root, "parts": list(node.parts)}
    if isinstance(node, Children):
        return {"node": "children", "expose": node.expose, "relationship": node.relationship}
    if isinstance(node, Neg):
        return {"node": "neg", "operand": to_json(node.operand)}
    if isinstance(node, Arith):
        return {
            "node": "arith",
            "op": node.op,
            "left": to_json(node.left),
            "right": to_json(node.right),
        }
    if isinstance(node, Compare):
        return {
            "node": "compare",
            "op": node.op,
            "left": to_json(node.left),
            "right": to_json(node.right),
        }
    if isinstance(node, In):
        return {
            "node": "in",
            "left": to_json(node.value),
            "items": [_in_item_json(item) for item in node.items],
            "negated": node.negated,
        }
    if isinstance(node, Between):
        return {
            "node": "between",
            "left": to_json(node.value),
            "low": to_json(node.low),
            "high": to_json(node.high),
            "negated": node.negated,
        }
    if isinstance(node, IsNull):
        return {"node": "is_null", "operand": to_json(node.value), "negated": node.negated}
    if isinstance(node, IsDistinctFrom):
        return {
            "node": "distinct_from",
            "left": to_json(node.left),
            "right": to_json(node.right),
            "negated": node.negated,
        }
    if isinstance(node, Not):
        return {"node": "not", "operand": to_json(node.operand)}
    if isinstance(node, (And, Or)):
        return {
            "node": "and" if isinstance(node, And) else "or",
            "left": to_json(node.left),
            "right": to_json(node.right),
        }
    if isinstance(node, Case):
        out = {
            "node": "case",
            "whens": [{"when": to_json(c), "then": to_json(v)} for c, v in node.whens],
        }
        if node.otherwise is not None:
            out["else"] = to_json(node.otherwise)
        return out
    if isinstance(node, Call):
        if node.name == "ROUND":
            places = node.args[1]
            if not isinstance(places, Number):
                raise TypeError(f"ROUND's places are a number literal, not {places!r}")
            return {
                "node": "round",
                "arg": to_json(node.args[0]),
                "scale": places.text,
                "mode": node.mode or DEFAULT_ROUND_MODE,
            }
        if node.name == "COALESCE":
            return {"node": "coalesce", "args": [to_json(a) for a in node.args]}
        if node.name not in _DATE_DIFF_UNITS:
            raise TypeError(f"not a flux-expr/2 function: {node.name!r}")
        return {
            "node": "date_diff",
            "unit": _DATE_DIFF_UNITS[node.name],
            "from": to_json(node.args[0]),
            "to": to_json(node.args[1]),
        }
    if isinstance(node, Extract):
        return {"node": "extract", "field": node.part, "arg": to_json(node.arg)}
    if isinstance(node, DateAdd):
        return {
            "node": "date_add",
            "unit": node.unit,
            "date": to_json(node.date),
            "count": to_json(node.count),
        }
    if isinstance(node, Concat):
        return {"node": "concat", "left": to_json(node.left), "right": to_json(node.right)}
    if isinstance(node, Aggregate):
        return {
            "node": "aggregate",
            "name": node.name,
            "children": to_json(node.children),
            "arg": None if node.arg is None else to_json(node.arg),
        }
    if isinstance(node, Window):
        return {
            "node": "window",
            "name": node.name,
            "args": [to_json(a) for a in node.args],
            "partition": node.partition,
            "order": [
                {"key": to_json(k.key), "desc": k.desc, "nullsFirst": k.nulls_first}
                for k in node.order
            ],
        }
    raise TypeError(f"not a flux-expr/2 node: {node!r}")


_DATE_DIFF_UNITS = {"DAYS_BETWEEN": "day", "MONTHS_BETWEEN": "month", "YEARS_BETWEEN": "year"}


def _in_item_json(item: Node) -> Dict[str, Any]:
    # RFC §2.1's in_item is ``[ "-" ] number``: one literal, so ``-1`` is one number node.
    if isinstance(item, Neg) and isinstance(item.operand, Number):
        return {"node": "number", "value": "-" + item.operand.text}
    return to_json(item)


# ------------------------------------------------------------ canonical text

# Precedence levels, loosest first: the level a node is written at, and the
# least level an operand position accepts without parentheses.
_OR, _AND, _NOT, _PREDICATE, _CONCAT, _ADD, _MUL, _NEG, _PRIMARY = range(1, 10)


def _level(node: Node) -> int:
    if isinstance(node, Or):
        return _OR
    if isinstance(node, And):
        return _AND
    if isinstance(node, Not):
        return _NOT
    if isinstance(node, (Compare, In, Between, IsNull, IsDistinctFrom)):
        return _PREDICATE
    if isinstance(node, Concat):
        return _CONCAT
    if isinstance(node, Arith):
        return _ADD if node.op in ("+", "-") else _MUL
    if isinstance(node, Neg):
        return _NEG
    return _PRIMARY


def quote_name(name: str) -> str:
    """A name as RFC §2.1 writes it: bare when it can be, else double-quoted."""
    if _BARE_NAME.fullmatch(name) and name.upper() not in CORE_KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def _quote_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class _Writer:
    def __init__(self, flux_side: bool, columns: AbstractSet[str]) -> None:
        self.flux_side = flux_side
        self.columns = columns

    def at(self, node: Node, least: int) -> str:
        text = self.write(node)
        return text if _level(node) >= least else f"({text})"

    def _is_root_word(self, name: str) -> bool:
        return self.flux_side and name.lower() in ROOTS

    def write(self, node: Node) -> str:
        if isinstance(node, Number):
            return node.text
        if isinstance(node, String):
            return _quote_string(node.value)
        if isinstance(node, Bool):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, Null):
            return "NULL"
        if isinstance(node, Date):
            return f"DATE {_quote_string(node.value)}"
        if isinstance(node, Column):
            if node.relationship is None:
                prefix = "row." if self._is_root_word(node.name) else ""
                return prefix + quote_name(node.name)
            prefix = "link." if self._is_root_word(node.relationship) else ""
            return f"{prefix}{quote_name(node.relationship)}.{quote_name(node.name)}"
        if isinstance(node, Path):
            joined = ".".join(quote_name(part) for part in node.parts)
            if node.root == "previous":
                return f"previous.{joined}"
            return f"link.{joined}" if self._is_root_word(node.parts[0]) else joined
        if isinstance(node, Children):
            text = f"CHILDREN.{quote_name(node.expose)}"
            if node.relationship is not None:
                text += f".{quote_name(node.relationship)}"
            return text
        if isinstance(node, Neg):
            return "-" + self.at(node.operand, _PRIMARY)
        if isinstance(node, Arith):
            tight = _ADD if node.op in ("+", "-") else _MUL
            return f"{self.at(node.left, tight)} {node.op} {self.at(node.right, tight + 1)}"
        if isinstance(node, Compare):
            return f"{self.at(node.left, _CONCAT)} {node.op} {self.at(node.right, _CONCAT)}"
        if isinstance(node, In):
            keyword = "NOT IN" if node.negated else "IN"
            items = ", ".join(self.write(item) for item in node.items)
            return f"{self.at(node.value, _CONCAT)} {keyword} ({items})"
        if isinstance(node, Between):
            keyword = "NOT BETWEEN" if node.negated else "BETWEEN"
            return (
                f"{self.at(node.value, _CONCAT)} {keyword} "
                f"{self.at(node.low, _CONCAT)} AND {self.at(node.high, _CONCAT)}"
            )
        if isinstance(node, IsNull):
            return f"{self.at(node.value, _CONCAT)} IS {'NOT ' if node.negated else ''}NULL"
        if isinstance(node, IsDistinctFrom):
            keyword = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
            return f"{self.at(node.left, _CONCAT)} {keyword} {self.at(node.right, _CONCAT)}"
        if isinstance(node, Not):
            return "NOT " + self.at(node.operand, _NOT)
        if isinstance(node, And):
            return f"{self.at(node.left, _AND)} AND {self.at(node.right, _NOT)}"
        if isinstance(node, Or):
            return f"{self.at(node.left, _OR)} OR {self.at(node.right, _AND)}"
        if isinstance(node, Case):
            parts: List[str] = ["CASE"]
            for condition, value in node.whens:
                parts += ["WHEN", self.write(condition), "THEN", self.write(value)]
            if node.otherwise is not None:
                parts += ["ELSE", self.write(node.otherwise)]
            parts.append("END")
            return " ".join(parts)
        if isinstance(node, Call):
            least = _OR if node.name == "COALESCE" else _ADD
            args = [self.at(a, least) for a in node.args]
            if node.name == "ROUND" and node.mode != DEFAULT_ROUND_MODE:
                args.append(_quote_string(node.mode or DEFAULT_ROUND_MODE))
            return f"{node.name}({', '.join(args)})"
        if isinstance(node, Extract):
            return f"EXTRACT({node.part.upper()} FROM {self.write(node.arg)})"
        if isinstance(node, DateAdd):
            function = {unit: name for name, unit in DATE_ADD_FUNCTIONS.items()}[node.unit]
            return f"{function}({self.write(node.date)}, {self.write(node.count)})"
        if isinstance(node, Concat):
            return f"{self.at(node.left, _CONCAT)} || {self.at(node.right, _ADD)}"
        if isinstance(node, Aggregate):
            args = [self.write(node.children)]
            if node.arg is not None:
                args.append(self.write(node.arg))
            return f"{node.name}({', '.join(args)})"
        if isinstance(node, Window):
            arguments = ", ".join(self.write(a) for a in node.args)
            partition = node.partition
            if self._is_root_word(partition) or partition in self.columns:
                partition_text = f"link.{quote_name(partition)}"
            else:
                partition_text = quote_name(partition)
            keys = []
            for key in node.order:
                text = self.write(key.key)
                if key.desc:
                    text += " DESC"
                if key.nulls_first:
                    text += " NULLS FIRST"
                keys.append(text)
            return (
                f"{node.name}({arguments}) OVER (PARTITION BY {partition_text} "
                f"ORDER BY {', '.join(keys)})"
            )
        raise TypeError(f"not a flux-expr/2 node: {node!r}")


def unparse(node: Node, *, flux_side: bool, columns: AbstractSet[str] = frozenset()) -> str:
    """The canonical text of *node*.

    *flux_side* says whether the text is FLUX-side, where roots exist, or a
    contract's core text, which writes every name bare. *columns* is the
    expose's column names, which decide when a ``PARTITION BY`` name needs
    ``link.``.
    """
    return _Writer(flux_side, columns).write(node)
