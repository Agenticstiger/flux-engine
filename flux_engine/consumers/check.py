# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Consumer SQL is checked with sqlglot and rewritten onto the bundle's exposes (ADR 0028 section 2).

A query or view is text a bundle's author declared, and a bundle may come from someone else. This
module reads that text with sqlglot, refuses it with a named ``FLX-36nn`` error unless every part
of it is on a closed list, and rewrites it onto the exposes of the bundle. Nothing here runs SQL:
the canonical text it returns is what a locked DuckDB runs later, and the digest it carries is what
the scorecard and the profile request name. Every refusal is a :class:`FluxError` that names the
query or view and the construct. An error nobody named (``FLX-3636``) and a ``RecursionError``
(``FLX-3628``) are refusals too, so a hostile text ends in a refusal and never in a raise.

The checks, in order
--------------------

1. **Bounded read.** sqlglot's tokenizer runs first. A text of more than 20,000 tokens, or with
   brackets nested more than 64 deep, is refused before the parser runs.
2. **One query.** ``sqlglot.parse`` must find exactly one statement, and it must be a ``SELECT``, a
   set operation, or ``WITH`` before one.
3. **Closed lists** (:mod:`flux_engine.consumers._allow`). Every node, argument, function and type
   is on an allowlist; more than 10,000 nodes or 64 levels is refused. A table written as a string
   is refused here too.
4. **Bound values.** Each placeholder becomes a literal in the tree from the query's
   ``parameters`` (never text spliced into SQL), and the clock becomes the registry's ``asOf``.
5. **Tables.** Each table resolves to an expose, a view or a ``WITH`` entry, by the rule below.
   The views a text reads become ``WITH`` entries ahead of its own.
6. **Columns.** The names the registry's ``columns`` map lists become the contract's, and sqlglot
   qualifies the tree against the columns of the exposes, expands stars and resolves each column
   to its table.
7. **Total orders** (:mod:`flux_engine.consumers._order`).
8. **The same text, twice.** The canonical text is ``tree.sql(dialect="duckdb", comments=False)``,
   so a comment never carries a second statement. That text is parsed again in the duckdb
   dialect and passes every check once more (steps 1 to 3 and 7, with the larger caps of a
   qualified text), and the tables it reads must be the tables the first tree read. The tree that
   is returned is that second tree.

Table resolution
----------------

A table name is written as up to three parts. It resolves, in this order, to the first rule that
matches:

1. the registry's ``tables`` map, matched on all its parts;
2. a qualifier listed in ``prefixes`` is dropped, and the rest is matched against ``tables`` and
   then as an ``exposeId``;
3. a name of one part, as an ``exposeId``;
4. a name of one part, as a ``WITH`` entry the text defines before it, or a declared view.

Unquoted parts compare without regard to case (a dialect that folds case folds it; names are
folded with ``str.lower``, as sqlglot folds them), quoted parts exactly. A name that matches two
exposes is ``FLX-3625``, and so is an expose that a ``WITH`` entry or a view of the same name would
shadow, and an expose that has a twin differing only in case, since DuckDB reads both as one name
(that holds for a quoted name too). A name that matches nothing, or has more than three parts, is
``FLX-3624``. Views become ``WITH`` entries in dependency order, before the text's own entries; a
cycle is ``FLX-3631``, and a text that with its views holds more than 100,000 nodes is
``FLX-3627``.

Columns
-------

The registry's ``columns`` map gives, per expose, the names consumers write for contract columns.
A name it lists is read as the contract's name wherever a column name is written: a column, an
output name, the column list of a ``WITH`` entry. So the canonical text, every star, and every
name a query's rows carry are the contract's, and a unit that is an ``exposeId`` (``SELECT *`` over
that expose) and the rows of a query that groups by ``plan`` agree on the name. The contract's own
name reads the same column. Table names, table aliases and ``WITH`` names are not column names and
are left as written. Because the reading is by name, a listed name stands for one column, and it is
not the name of another column of the contract (``FLX-3635``). A name in no table is ``FLX-3633``.

The sqlglot version that wrote a canonical text is recorded on every result, since a text sqlglot
writes differently under another version is another text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import partial
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
)

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.tokens import Token, TokenType

from flux_engine.consumers import _allow, _dialect
from flux_engine.consumers._bounds import (
    refuse_alias_references,
    refuse_long_patterns,
    refuse_wide_expansion,
)
from flux_engine.consumers._order import check_total_orders
from flux_engine.consumers.errors import (
    FLX_3618,
    FLX_3619,
    FLX_3620,
    FLX_3623,
    FLX_3624,
    FLX_3625,
    FLX_3626,
    FLX_3627,
    FLX_3628,
    FLX_3629,
    FLX_3631,
    FLX_3632,
    FLX_3633,
    FLX_3634,
    FLX_3635,
    FLX_3636,
)
from flux_engine.consumers.schema import BundleSchema, ExposeSchema
from flux_engine.core.canonical import sha256_hex
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import ConsumerQuery, ConsumerRegistry

__all__ = [
    "CheckedQuery",
    "CheckedView",
    "RegistryCheck",
    "SQLGLOT_VERSION",
    "check_query",
    "check_registry",
    "digest_of_text",
]

#: The sqlglot version every canonical text is written by.
SQLGLOT_VERSION: str = str(sqlglot.__version__)

_OPEN = frozenset({TokenType.L_PAREN, TokenType.L_BRACKET, TokenType.L_BRACE})
_CLOSE = frozenset({TokenType.R_PAREN, TokenType.R_BRACKET, TokenType.R_BRACE})
_STRINGS = frozenset(
    {
        TokenType.STRING,
        TokenType.NATIONAL_STRING,
        TokenType.RAW_STRING,
        TokenType.NATIONAL_RAW_STRING,
        TokenType.HEREDOC_STRING,
        TokenType.UNICODE_STRING,
        TokenType.BYTE_STRING,
    }
)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_MOST_INTEGER = (1 << 63) - 1
#: A name SQL reads as written without quotes: letters, digits and underscores, no digit first.
_PLAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLACEHOLDER = "flux_placeholder"
_EXPOSE = "flux_expose"
_DUCKDB = "duckdb"

T = TypeVar("T")


@dataclass(frozen=True)
class CheckedView:
    """A view that passed the checks."""

    name: str
    #: The canonical text: the view over exposes, with the views it reads as ``WITH`` entries.
    text: str
    #: ``sha256:`` and the hex digest of the UTF-8 canonical text.
    digest: str
    #: The view's output columns: ``(name, DuckDB type)``, the type empty when sqlglot cannot tell.
    columns: Tuple[Tuple[str, str], ...]
    exposes: Tuple[str, ...]
    #: The views it reads, directly or through other views, in dependency order.
    views: Tuple[str, ...]
    sqlglot_version: str
    tree: exp.Expr = field(repr=False, compare=False)


@dataclass(frozen=True)
class CheckedQuery:
    """A query that passed the checks, with what the later tasks read from it."""

    id: str
    #: The canonical text: DuckDB's dialect, over exposeIds and contract names, comments dropped.
    text: str
    #: ``sha256:`` and the hex digest of the UTF-8 canonical text.
    digest: str
    unit: Optional[str]
    #: The unit's canonical text: ``SELECT * FROM "<exposeId>"``, or a view's own canonical text.
    unit_text: Optional[str]
    group_by: Optional[str]
    #: The exposeIds the query reads, directly or through its views, sorted.
    exposes: Tuple[str, ...]
    #: The views it reads, directly or through other views, in dependency order.
    views: Tuple[str, ...]
    sqlglot_version: str
    #: The qualified tree of the canonical text: the tree of the text DuckDB runs, in which every
    #: column names its table (or the alias of a table) and every star is expanded.
    tree: exp.Expr = field(repr=False, compare=False)


@dataclass(frozen=True)
class RegistryCheck:
    """Every view and query of a registry, checked in one pass."""

    views: Tuple[CheckedView, ...]
    queries: Tuple[CheckedQuery, ...]


def digest_of_text(text: str) -> str:
    """``sha256:`` and the lowercase hex of the UTF-8 *text*, as the engine's digests are written."""
    return "sha256:" + sha256_hex(text.encode("utf-8"))


def check_query(
    registry: ConsumerRegistry, query: ConsumerQuery, schema: BundleSchema
) -> CheckedQuery:
    """Check one query against *schema* and return its canonical form, or refuse it.

    The views it reads are checked on the way. Every refusal is a :class:`FluxError` whose message
    names the query and the construct, and nothing else escapes.
    """
    return _guarded(f"query {query.id!r}", lambda: _Checker(registry, schema).query(query))


def check_registry(registry: ConsumerRegistry, schema: BundleSchema) -> RegistryCheck:
    """Check every view and then every query of *registry*, each view once."""
    checker = _Checker(registry, schema)
    views = tuple(
        _guarded(f"view {view.name!r}", partial(checker.view, view.name)) for view in registry.views
    )
    queries = tuple(
        _guarded(f"query {query.id!r}", partial(checker.query, query)) for query in registry.queries
    )
    return RegistryCheck(views=views, queries=queries)


def _guarded(label: str, work: Callable[[], T]) -> T:
    """Run *work*, so that whatever it raises is a :class:`FluxError` that names *label*.

    A ``RecursionError`` is ``FLX-3628``, and any other error that is not already a refusal is
    ``FLX-3636``: a hostile text must end in a refusal and never in an uncaught exception, whatever
    sqlglot does with it.
    """
    try:
        return work()
    except FluxError:
        raise
    except RecursionError:
        raise FluxError(
            FLX_3628, f"{label} nests deeper than sqlglot can read or write it"
        ) from None
    except Exception as exc:
        raise FluxError(FLX_3636, f"{label}: the check stopped on a {type(exc).__name__}") from exc


# ---------------------------------------------------------------------------------------------
# The text: tokens, statements, caps
# ---------------------------------------------------------------------------------------------


def _first_problem(exc: BaseException) -> str:
    if isinstance(exc, ParseError) and exc.errors:
        first = exc.errors[0]
        text = str(first.get("description", "")).strip()
        return f"{text[:160]} (line {first.get('line')}, column {first.get('col')})"
    return (str(exc).splitlines() or [type(exc).__name__])[0][:200]


def _scan_tokens(
    dialect: Dialect, sql: str, label: str, caps: _allow.Caps
) -> FrozenSet[Tuple[int, int]]:
    """Count the tokens and the bracket depth of *sql*; return the spans of its string tokens."""
    try:
        tokens: List[Token] = dialect.tokenize(sql)
    except RecursionError:
        raise
    except Exception as exc:  # sqlglot's tokenizer raises its own errors and, on odd input, others
        raise FluxError(FLX_3618, f"{label} does not tokenize: {_first_problem(exc)}") from None
    if len(tokens) > caps.tokens:
        raise FluxError(FLX_3627, f"{label} holds more than {caps.tokens} tokens")
    depth = 0
    strings: Set[Tuple[int, int]] = set()
    for token in tokens:
        kind = token.token_type
        if kind in _OPEN:
            depth += 1
            if depth > caps.brackets:
                raise FluxError(FLX_3627, f"{label} nests brackets more than {caps.brackets} deep")
        elif kind in _CLOSE:
            depth = max(0, depth - 1)
        elif kind in _STRINGS:
            strings.add((token.start, token.end))
    return frozenset(strings)


def _refuse_string_tables(tree: exp.Expr, strings: FrozenSet[Tuple[int, int]], label: str) -> None:
    """Refuse a table that the text wrote as a string, such as ``FROM 'x.parquet'``.

    sqlglot reads such a table as a quoted name, which a quoted name cannot be told from, so the
    identifier's position is matched against the string tokens of the text.
    """
    for table in tree.find_all(exp.Table):
        for key in ("catalog", "db", "this"):
            part = table.args.get(key)
            if isinstance(part, exp.Identifier):
                span = (part.meta.get("start"), part.meta.get("end"))
                if span in strings:
                    raise FluxError(
                        FLX_3623, f"{label} names a table with a string, as a path or a file"
                    )


def _parse(sql: str, dialect: str, label: str) -> exp.Expr:
    try:
        parsed = sqlglot.parse(sql, read=dialect)
    except RecursionError:
        raise
    except Exception as exc:  # ParseError, TokenError and whatever else a hostile text provokes
        raise FluxError(FLX_3618, f"{label} does not parse: {_first_problem(exc)}") from None
    statements = [statement for statement in parsed if statement is not None]
    if not statements:
        raise FluxError(FLX_3618, f"{label} holds no statement")
    if len(statements) > 1:
        raise FluxError(
            FLX_3619, f"{label} holds {len(statements)} statements; a query is exactly one"
        )
    root = statements[0]
    if type(root) not in _allow.QUERY_ROOTS:
        kind = type(root).__name__
        if isinstance(root, exp.Command):
            kind = f"{kind} {str(root.this).upper()}"
        raise FluxError(FLX_3620, f"{label} is not a query: it is {kind}")
    return root


# ---------------------------------------------------------------------------------------------
# Values bound into the tree: placeholders and the clock
# ---------------------------------------------------------------------------------------------


def _placeholder_name(node: exp.Expr, label: str) -> str:
    if isinstance(node, exp.Placeholder):
        target = node.args.get("this")
        if isinstance(target, exp.Identifier):
            target = target.name
    else:
        target = node.args.get("this")
        if node.args.get("expression") is not None:
            target = None
        elif isinstance(target, (exp.Var, exp.Identifier)):
            target = target.name
        else:
            target = None
    if not isinstance(target, str) or not target:
        raise FluxError(
            FLX_3626, f"{label} holds a positional or unnamed placeholder; name each placeholder"
        )
    return target


def _literal(value: Any, label: str, name: str) -> exp.Expr:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int):
        if abs(value) > _MOST_INTEGER:
            raise FluxError(FLX_3626, f"{label}: the value of {name!r} is outside 64 bits")
        if value < 0:
            return exp.Neg(this=exp.Literal.number(-value))
        return exp.Literal.number(value)
    if isinstance(value, str):
        if "\x00" in value:
            raise FluxError(FLX_3626, f"{label}: the value of {name!r} holds a NUL character")
        literal = exp.Literal.string(value)
        literal.meta[_PLACEHOLDER] = True
        return literal
    raise FluxError(FLX_3626, f"{label}: the value of {name!r} is not a string, number or boolean")


def _bind_placeholders(tree: exp.Expr, label: str, values: Mapping[str, Any]) -> Set[str]:
    used: Set[str] = set()
    for node in list(tree.walk()):
        if not isinstance(node, (exp.Placeholder, exp.Parameter)):
            continue
        name = _placeholder_name(node, label)
        if name not in values:
            raise FluxError(FLX_3626, f"{label} has the placeholder {name!r} and no value for it")
        used.add(name)
        if isinstance(node.parent, (exp.Limit, exp.Offset)):
            count = values[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise FluxError(
                    FLX_3626,
                    f"{label}: the placeholder {name!r} is in LIMIT or OFFSET and takes a "
                    "non-negative whole number",
                )
        node.replace(_literal(values[name], label, name))
    return used


_DATE_CLOCKS = ("current_date", "curdate", "today")


def _clock_kind(node: exp.Expr) -> Optional[str]:
    if isinstance(node, exp.CurrentDate):
        return "date"
    if isinstance(node, _allow.CLOCK_CLASSES):
        return "timestamp"
    if isinstance(node, exp.Anonymous):
        return "date" if str(node.name).lower() in _DATE_CLOCKS else "timestamp"
    return None


def _bind_clock(tree: exp.Expr, label: str, as_of: Optional[str]) -> None:
    for node in list(tree.walk()):
        kind = _clock_kind(node)
        if kind is None:
            continue
        if as_of is None:
            raise FluxError(
                FLX_3629,
                f"{label} reads the current time ({_allow.describe(node)}) and asOf is not declared",
            )
        if kind == "date":
            node.replace(exp.Cast(this=exp.Literal.string(as_of), to=exp.DataType.build("DATE")))
        else:
            midnight = f"{as_of} 00:00:00"
            node.replace(
                exp.Cast(this=exp.Literal.string(midnight), to=exp.DataType.build("TIMESTAMP"))
            )


# ---------------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------------


def _visible_ctes(node: exp.Expr) -> Set[str]:
    """The WITH names, folded, that a table at *node* may read: those defined before it."""
    names: Set[str] = set()
    child: exp.Expr = node
    parent = child.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            for cte in parent.expressions:
                if cte is child:
                    break
                names.add(_fold(str(cte.alias)))
        else:
            with_ = parent.args.get("with_")
            if isinstance(with_, exp.With) and with_ is not child:
                names.update(_fold(str(cte.alias)) for cte in with_.expressions)
        child, parent = parent, parent.parent
    return names


def _fold(name: str) -> str:
    """The case a name is compared in: the one sqlglot folds to, so that both read one name."""
    return name.lower()


def _same_part(written: str, part: exp.Identifier) -> bool:
    """Quoted names compare exactly, unquoted names without regard to case."""
    if part.args.get("quoted"):
        return str(part.name) == written
    return _fold(str(part.name)) == _fold(written)


def _parts_match(key: Sequence[str], parts: Sequence[exp.Identifier]) -> bool:
    return len(key) == len(parts) and all(_same_part(k, p) for k, p in zip(key, parts, strict=True))


@dataclass(frozen=True)
class _Prepared:
    """A text that has been read, checked, bound and had its tables resolved."""

    label: str
    tree: exp.Expr
    views: FrozenSet[str]
    #: The nodes of the checked tree.
    nodes: int


@dataclass(frozen=True)
class _ExposeColumns:
    """The columns of one expose, in the contract's names."""

    #: A contract name (folded) to the contract's own spelling.
    exact: Mapping[str, str]
    #: The columns qualify sees: contract name to DuckDB type.
    visible: Mapping[str, str]


class _Checker:
    """One registry and schema, the views already read, and the checks of a text."""

    def __init__(self, registry: ConsumerRegistry, schema: BundleSchema) -> None:
        self._registry = registry
        self._schema = schema
        try:
            self._dialect = Dialect.get_or_raise(registry.dialect)
        except Exception as exc:
            raise FluxError(
                FLX_3635, f"the registry's dialect {registry.dialect!r} is not one sqlglot reads"
            ) from exc
        self._duckdb = Dialect.get_or_raise(_DUCKDB)
        self._exposes: Dict[str, ExposeSchema] = {item.expose_id: item for item in schema.exposes}
        self._tables = tuple((tuple(name.split(".")), expose) for name, expose in registry.tables)
        self._prefixes = tuple(tuple(prefix.split(".")) for prefix in registry.prefixes)
        self._views = {view.name: view for view in registry.views}
        self._columns: Dict[str, _ExposeColumns] = {}
        self._prepared: Dict[str, _Prepared] = {}
        self._checked: Dict[str, CheckedView] = {}
        self._check_registry_against_schema()
        self._written = self._written_names()

    # -- the registry against the schema ----------------------------------------------------

    def _check_registry_against_schema(self) -> None:
        for name, expose in self._registry.tables:
            if expose not in self._exposes:
                raise FluxError(
                    FLX_3635,
                    f"the registry maps the table {name!r} to {expose!r}, which the schema lacks",
                )
        for expose, pairs in self._registry.columns:
            if expose not in self._exposes:
                raise FluxError(
                    FLX_3635, f"the registry renames columns of {expose!r}, which the schema lacks"
                )
            known = {name for name, _ in self._exposes[expose].columns}
            for written, column in pairs:
                if column not in known:
                    raise FluxError(
                        FLX_3635,
                        f"the registry maps {written!r} to {column!r}, which is not a column of {expose!r}",
                    )

    def _written_names(self) -> Dict[str, str]:
        """Each name the registry's ``columns`` lists (folded) and the contract column it stands for.

        A written name is read as the contract's name wherever a column name is written, so it
        stands for one column: it may not stand for two, and it may not also be the name of another
        column of the contract.
        """
        contract: Set[str] = {
            _fold(name) for item in self._schema.exposes for name in item.column_names
        }
        found: Dict[str, str] = {}
        for _, pairs in self._registry.columns:
            for written, column in pairs:
                fold = _fold(written)
                if fold in contract and fold != _fold(column):
                    raise FluxError(
                        FLX_3635,
                        f"the registry's columns write {written!r} for {column!r}, and "
                        f"{written!r} is also the name of a column of the contract",
                    )
                if found.setdefault(fold, column) != column:
                    raise FluxError(
                        FLX_3635,
                        f"the registry's columns write {written!r} for two columns, "
                        f"{found[fold]!r} and {column!r}",
                    )
        return found

    def _expose_columns(self, expose_id: str) -> _ExposeColumns:
        cached = self._columns.get(expose_id)
        if cached is not None:
            return cached
        exact: Dict[str, str] = {}
        visible: Dict[str, str] = {}
        for name, sql_type in self._exposes[expose_id].columns:
            if _fold(name) in exact:
                raise FluxError(
                    FLX_3635,
                    f"{expose_id!r} has two columns that DuckDB reads as the name {name!r}",
                )
            exact[_fold(name)] = name
            visible[name] = sql_type
        found = _ExposeColumns(exact=exact, visible=visible)
        self._columns[expose_id] = found
        return found

    # -- reading a text ---------------------------------------------------------------------

    def _prepare(self, label: str, sql: str, values: Mapping[str, Any]) -> _Prepared:
        try:
            return self._read(label, sql, values)
        except FluxError as error:
            hint = self._quoting_hint(sql) if error.code in (FLX_3618, FLX_3624) else ""
            if not hint:
                raise
            raise FluxError(error.code, f"{error.detail}; {hint}") from None

    def _quoting_hint(self, sql: str) -> str:
        """What to write when *sql* holds a view or table name that needs quotes and has none.

        A view name, or a part of a ``tables`` name, that is not a plain SQL identifier (a hyphen,
        a space, a leading digit) reads as something else when it is written bare. A quoted name
        resolves exactly, so the fix is to quote it, and the hint shows how.
        """
        names = [view.name for view in self._registry.views]
        names += [name for name, _ in self._registry.tables]
        examples: List[str] = []
        for name in names:
            parts = name.split(".")
            bare = [part for part in parts if _PLAIN.fullmatch(part) is None]
            if not any(sql.count(part) > sql.count(f'"{part}"') for part in bare):
                continue
            example = ".".join(part if part not in bare else f'"{part}"' for part in parts)
            if example not in examples:
                examples.append(example)
        if not examples:
            return ""
        return (
            "a name that is not a plain identifier must be written quoted, for example "
            + ", ".join(examples[:3])
        )

    def _read(self, label: str, sql: str, values: Mapping[str, Any]) -> _Prepared:
        if "\x00" in sql:
            raise FluxError(FLX_3618, f"{label} holds a NUL character")
        strings = _scan_tokens(self._dialect, sql, label, _allow.SOURCE_CAPS)
        _dialect.refuse_misread_spellings(sql, self._registry.dialect, label)
        tree = _parse(sql, self._registry.dialect, label)
        _dialect.translate(tree, dialect=self._registry.dialect, label=label)
        nodes = _allow.vet_tree(tree, label=label, caps=_allow.SOURCE_CAPS, canonical=False)
        _refuse_string_tables(tree, strings, label)
        used = _bind_placeholders(tree, label, values)
        refuse_long_patterns(tree, label=label)
        unused = sorted(set(values) - used)
        if unused:
            raise FluxError(
                FLX_3626, f"{label} has the parameters {unused} and no placeholder uses them"
            )
        _bind_clock(tree, label, self._registry.as_of)
        _read_written_names(tree, self._written)
        views = self._resolve_tables(tree, label)
        return _Prepared(label=label, tree=tree, views=frozenset(views), nodes=nodes)

    def _prepare_view(self, name: str) -> _Prepared:
        found = self._prepared.get(name)
        if found is None:
            view = self._views[name]
            found = self._prepare(f"view {name!r}", view.sql, {})
            self._prepared[name] = found
        return found

    # -- tables -----------------------------------------------------------------------------

    def _resolve_tables(self, tree: exp.Expr, label: str) -> Set[str]:
        """Rewrite each table onto its expose and return the views the text reads."""
        views: Set[str] = set()
        for table in [node for node in tree.walk() if isinstance(node, exp.Table)]:
            parts = [
                part
                for part in (table.args.get("catalog"), table.args.get("db"), table.this)
                if part is not None
            ]
            kind, name = self._resolve(parts, _visible_ctes(table), label)
            if kind == "expose":
                self._rewrite_expose(table, parts, name)
            elif kind == "view":
                table.set("this", exp.to_identifier(name))
                views.add(name)
        return views

    @staticmethod
    def _rewrite_expose(table: exp.Expr, parts: List[exp.Identifier], expose: str) -> None:
        if table.args.get("alias") is None:
            last = parts[-1]
            table.set(
                "alias",
                exp.TableAlias(
                    this=exp.Identifier(this=str(last.name), quoted=bool(last.args.get("quoted")))
                ),
            )
        table.set("catalog", None)
        table.set("db", None)
        table.set("this", exp.Identifier(this=expose, quoted=True))
        table.meta[_EXPOSE] = expose

    def _resolve(
        self, parts: List[exp.Identifier], visible: Set[str], label: str
    ) -> Tuple[str, str]:
        written = ".".join(str(part.name) for part in parts)
        expose = self._match_expose(parts, written, label)
        view: Optional[str] = None
        cte = len(parts) == 1 and _fold(str(parts[0].name)) in visible
        if len(parts) == 1:
            matches = [name for name in self._views if _same_part(name, parts[0])]
            view = matches[0] if matches else None
        hits = [item for item in (expose, view, "cte" if cte else None) if item is not None]
        if len(hits) > 1:
            raise FluxError(
                FLX_3625,
                f"{label} names {written!r}, which is an expose, a view or a WITH entry that "
                "shadows another; rename one or quote the name",
            )
        if expose is not None:
            return "expose", expose
        if view is not None:
            return "view", view
        if cte:
            return "cte", _fold(str(parts[0].name))
        raise FluxError(
            FLX_3624,
            f"{label} names the table {written!r}, which resolves to no expose "
            f"(the bundle exposes {sorted(self._exposes)[:20]}) and no view "
            f"(views: {sorted(self._views)[:20]})",
        )

    def _match_expose(self, parts: List[exp.Identifier], written: str, label: str) -> Optional[str]:
        found: Set[str] = {name for key, name in self._tables if _parts_match(key, parts)}
        if not found:
            found = self._match_prefixed(parts)
        if not found and len(parts) == 1:
            found = {name for name in self._exposes if _same_part(name, parts[0])}
        if not found:
            return None
        if len(found) > 1:
            raise FluxError(
                FLX_3625, f"{label} names {written!r}, which matches the exposes {sorted(found)}"
            )
        expose = next(iter(found))
        twins = sorted(
            other
            for other in self._exposes
            if other != expose
            and (other.lower() == expose.lower() or other.casefold() == expose.casefold())
        )
        if twins:
            raise FluxError(
                FLX_3625,
                f"{label} names {written!r}, which is the expose {expose!r}; {twins} differs from "
                "it only in case, and DuckDB cannot tell them apart",
            )
        return expose

    def _match_prefixed(self, parts: List[exp.Identifier]) -> Set[str]:
        found: Set[str] = set()
        for prefix in self._prefixes:
            if len(parts) <= len(prefix) or not _parts_match(prefix, parts[: len(prefix)]):
                continue
            rest = parts[len(prefix) :]
            found.update(name for key, name in self._tables if _parts_match(key, rest))
            if len(rest) == 1:
                found.update(name for name in self._exposes if _same_part(name, rest[0]))
        return found

    # -- views ------------------------------------------------------------------------------

    def _closure(self, roots: Iterable[str]) -> List[str]:
        """Every view reachable from *roots*, dependencies first; a cycle is ``FLX-3631``."""
        deps: Dict[str, Set[str]] = {}
        work = sorted(set(roots), reverse=True)
        while work:
            name = work.pop()
            if name in deps:
                continue
            deps[name] = set(self._prepare_view(name).views)
            work.extend(sorted(deps[name] - deps.keys(), reverse=True))
        order: List[str] = []
        remaining = {name: set(found) for name, found in deps.items()}
        while remaining:
            ready = sorted(name for name, found in remaining.items() if not found)
            if not ready:
                raise FluxError(
                    FLX_3631, f"the views {sorted(remaining)} read each other in a cycle"
                )
            order.extend(ready)
            for name in ready:
                del remaining[name]
            for found in remaining.values():
                found.difference_update(ready)
        return order

    def _assemble(self, prepared: _Prepared, order: List[str]) -> exp.Expr:
        if not order:
            return prepared.tree.copy()
        nodes = prepared.nodes + sum(self._prepared[name].nodes for name in order)
        if nodes > _allow.CANONICAL_CAPS.nodes:
            raise FluxError(
                FLX_3627,
                f"{prepared.label} with the views it reads holds {nodes} expression nodes, "
                f"more than {_allow.CANONICAL_CAPS.nodes}",
            )
        tree = prepared.tree.copy()
        entries: List[exp.Expr] = [
            exp.CTE(
                this=self._prepared[name].tree.copy(),
                alias=exp.TableAlias(this=exp.to_identifier(name)),
            )
            for name in order
        ]
        existing = tree.args.get("with_")
        if isinstance(existing, exp.With):
            clash = sorted(
                {_fold(str(cte.alias)) for cte in existing.expressions} & {_fold(n) for n in order}
            )
            if clash:
                raise FluxError(
                    FLX_3625,
                    f"{prepared.label} defines the WITH entry {clash}, which is also a view it reads",
                )
            entries.extend(existing.expressions)
        tree.set("with_", exp.With(expressions=entries))
        return tree

    def view(self, name: str) -> CheckedView:
        found = self._checked.get(name)
        if found is not None:
            return found
        prepared = self._prepare_view(name)
        order = self._closure(prepared.views)
        built = self._build(prepared, order)
        checked = CheckedView(
            name=name,
            text=built.text,
            digest=digest_of_text(built.text),
            columns=built.columns,
            exposes=built.exposes,
            views=tuple(order),
            sqlglot_version=SQLGLOT_VERSION,
            tree=built.tree,
        )
        self._checked[name] = checked
        return checked

    # -- a query ----------------------------------------------------------------------------

    def query(self, query: ConsumerQuery) -> CheckedQuery:
        label = f"query {query.id!r}"
        prepared = self._prepare(label, query.sql, dict(query.parameters))
        order = self._closure(prepared.views)
        built = self._build(prepared, order)
        unit_text, unit_columns = self._unit(label, query.unit)
        if query.group_by is not None:
            self._check_group_by(label, query, built.tree, unit_columns)
        return CheckedQuery(
            id=query.id,
            text=built.text,
            digest=digest_of_text(built.text),
            unit=query.unit,
            unit_text=unit_text,
            group_by=query.group_by,
            exposes=built.exposes,
            views=tuple(order),
            sqlglot_version=SQLGLOT_VERSION,
            tree=built.tree,
        )

    def _unit(self, label: str, unit: Optional[str]) -> Tuple[Optional[str], Tuple[str, ...]]:
        if unit is None:
            return None, ()
        expose = self._exposes.get(unit)
        if expose is not None:
            table = exp.Table(this=exp.Identifier(this=unit, quoted=True))
            star = exp.Select(expressions=[exp.Star()], from_=exp.From(this=table))
            text = star.sql(dialect=_DUCKDB, comments=False)
            return text, expose.column_names
        if unit in self._views:
            checked = self.view(unit)
            return checked.text, tuple(name for name, _ in checked.columns)
        raise FluxError(
            FLX_3632,
            f"{label} has the unit {unit!r}, which is neither an expose of the schema nor a view",
        )

    @staticmethod
    def _check_group_by(
        label: str, query: ConsumerQuery, tree: exp.Expr, unit_columns: Tuple[str, ...]
    ) -> None:
        wanted = _fold(str(query.group_by))
        rows = {_fold(name) for name in _named_selects(tree)}
        if wanted not in rows:
            raise FluxError(
                FLX_3632,
                f"{label} groups by {query.group_by!r}, which is not a column of the rows it returns "
                f"(it returns {sorted(rows)})",
            )
        if wanted not in {_fold(name) for name in unit_columns}:
            raise FluxError(
                FLX_3632,
                f"{label} groups by {query.group_by!r}, which is not a column of its unit "
                f"{query.unit!r}",
            )

    # -- qualifying, renaming and the second reading ------------------------------------------

    def _build(self, prepared: _Prepared, order: List[str]) -> "_Built":
        label = prepared.label
        tree = self._assemble(prepared, order)
        exposes = sorted(
            {
                str(node.meta[_EXPOSE])
                for node in tree.walk()
                if isinstance(node, exp.Table) and _EXPOSE in node.meta
            }
        )
        schema: Dict[str, object] = {
            name: dict(self._expose_columns(name).visible) for name in exposes
        }
        widths = {name: len(self._expose_columns(name).visible) for name in exposes}
        qualified = self._qualify(tree, schema, widths, label)
        columns = _output_columns(qualified)
        _cast_placeholders(qualified)
        self._restore_names(qualified, exposes)
        by_fold = {_fold(name): self._exposes[name] for name in exposes}
        check_total_orders(qualified, by_fold, label=label)
        first = self._table_refs(qualified, label)
        text = qualified.sql(dialect=_DUCKDB, comments=False)
        second = self._reread(text, first, by_fold, label)
        return _Built(
            text=text,
            tree=second,
            columns=columns,
            exposes=tuple(sorted({name for kind, name in first if kind == "expose"})),
        )

    @staticmethod
    def _qualify(
        tree: exp.Expr, schema: Dict[str, object], widths: Mapping[str, int], label: str
    ) -> exp.Expr:
        # No alias is ever expanded: a text that reads one is refused first, and qualify is told
        # not to expand. The stars and positional GROUP BY keys qualify does write out are
        # counted first, so the tree it writes stays within the canonical caps.
        refuse_alias_references(tree, label=label)
        try:
            refuse_wide_expansion(tree, widths, label=label, most=_allow.CANONICAL_CAPS.nodes)
            qualified = qualify(tree, schema=schema, dialect=_DUCKDB, expand_alias_refs=False)
            annotate_types(qualified, schema=schema, dialect=_DUCKDB)
        except (FluxError, RecursionError):
            raise
        except Exception as exc:  # OptimizeError, and whatever else sqlglot raises on odd trees
            raise FluxError(FLX_3633, f"{label}: {_first_problem(exc)}") from None
        for star in qualified.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise FluxError(FLX_3633, f"{label} holds a star that cannot be expanded")
        return qualified

    def _restore_names(self, tree: exp.Expr, exposes: List[str]) -> None:
        """Tables take their exact ids and columns of an expose their exact contract names.

        qualify folds every name to lower case, which DuckDB reads as the same name; the canonical
        text spells each as the contract does.
        """
        known = {_fold(name): name for name in exposes}
        root = build_scope(tree)
        if root is not None:
            for scope in root.traverse():
                for column in scope.columns:
                    self._restore_column(scope, column, known)
        for table in tree.find_all(exp.Table):
            exact = known.get(_fold(str(table.name)))
            if exact is not None:
                table.set("this", exp.Identifier(this=exact, quoted=True))

    def _restore_column(self, scope: Scope, column: exp.Column, known: Mapping[str, str]) -> None:
        if not column.table:
            return
        source: Any = None
        walker: Optional[Scope] = scope
        while walker is not None and source is None:
            source = walker.sources.get(str(column.table))
            walker = walker.parent
        if not isinstance(source, exp.Table):
            return
        expose = known.get(_fold(str(source.name)))
        if expose is None:
            return
        columns = self._expose_columns(expose)
        exact = columns.exact.get(_fold(str(column.name)))
        if exact is not None:
            column.set("this", exp.Identifier(this=exact, quoted=True))

    def _table_refs(self, tree: exp.Expr, label: str) -> List[Tuple[str, str]]:
        refs: List[Tuple[str, str]] = []
        for node in tree.walk():
            if not isinstance(node, exp.Table):
                continue
            if node.args.get("db") is not None or node.args.get("catalog") is not None:
                raise FluxError(
                    FLX_3634, f"{label}: the canonical text qualifies a table by a schema"
                )
            name = str(node.name)
            if _fold(name) in _visible_ctes(node):
                refs.append(("cte", _fold(name)))
            elif name in self._exposes:
                refs.append(("expose", name))
            else:
                raise FluxError(
                    FLX_3634,
                    f"{label}: the canonical text reads {name!r}, which is no expose or WITH entry",
                )
        return sorted(refs)

    def _reread(
        self,
        text: str,
        first: List[Tuple[str, str]],
        by_fold: Mapping[str, ExposeSchema],
        label: str,
    ) -> exp.Expr:
        """Parse the canonical text again, in DuckDB's dialect, and check it as a new text."""
        try:
            strings = _scan_tokens(self._duckdb, text, label, _allow.CANONICAL_CAPS)
            second = _parse(text, _DUCKDB, label)
            _allow.vet_tree(second, label=label, caps=_allow.CANONICAL_CAPS, canonical=True)
            _refuse_string_tables(second, strings, label)
            again = self._table_refs(second, label)
            if again != first:
                raise FluxError(
                    FLX_3634,
                    f"{label}: the canonical text reads other tables than the checked tree",
                )
            check_total_orders(second, by_fold, label=label)
        except FluxError as exc:
            if exc.code.code == FLX_3634.code:
                raise
            raise FluxError(
                FLX_3634,
                f"{label}: the canonical text fails {exc.code.code} on the second reading: {exc.detail}",
            ) from None
        return second


@dataclass(frozen=True)
class _Built:
    text: str
    tree: exp.Expr
    columns: Tuple[Tuple[str, str], ...]
    exposes: Tuple[str, ...]


def _read_written_names(tree: exp.Expr, written: Mapping[str, str]) -> None:
    """Read each name the registry's ``columns`` lists as the contract's name.

    Wherever the text writes a column name (a column, an output name, a column alias of a WITH
    entry) a listed name is replaced by the contract's, so the canonical text and every name a
    query's rows carry are the contract's. Table names, table aliases and WITH names are not
    column names and are left as they are.
    """
    if not written:
        return
    names: List[Any] = []
    for node in tree.walk():
        if isinstance(node, exp.Column):
            names.append(node.args.get("this"))
        elif isinstance(node, exp.Alias):
            names.append(node.args.get("alias"))
        elif isinstance(node, exp.TableAlias):
            names.extend(node.args.get("columns") or [])
    for part in names:
        if isinstance(part, exp.Identifier) and _fold(str(part.name)) in written:
            part.set("this", written[_fold(str(part.name))])


def _named_selects(tree: exp.Expr) -> List[str]:
    """The names of the rows a query returns."""
    return [str(name) for name in tree.named_selects] if isinstance(tree, exp.Query) else []


def _output_columns(tree: exp.Expr) -> Tuple[Tuple[str, str], ...]:
    names = _named_selects(tree)
    selects = list(getattr(tree, "selects", []))
    found: List[Tuple[str, str]] = []
    for index, name in enumerate(names):
        kind = ""
        if index < len(selects):
            dtype = getattr(selects[index], "type", None)
            if dtype is not None and not dtype.is_type(exp.DType.UNKNOWN):
                kind = dtype.sql(dialect=_DUCKDB)
        found.append((name, kind))
    return tuple(found)


def _cast_placeholders(tree: exp.Expr) -> None:
    """A string placeholder compared with a DATE, TIMESTAMP or DECIMAL becomes a CAST to it."""
    for literal in [node for node in tree.find_all(exp.Literal) if node.meta.get(_PLACEHOLDER)]:
        other = _compared_with(literal)
        dtype = getattr(other, "type", None)
        if dtype is None or not dtype.is_type(
            exp.DType.DATE, exp.DType.TIMESTAMP, exp.DType.TIMESTAMPNTZ, exp.DType.DECIMAL
        ):
            continue
        literal.replace(exp.Cast(this=literal.copy(), to=dtype.copy()))


def _compared_with(literal: exp.Expr) -> Optional[exp.Expr]:
    parent = literal.parent
    if isinstance(parent, _COMPARISONS):
        return parent.left if parent.right is literal else parent.right
    if isinstance(parent, (exp.Between, exp.In)) and parent.this is not literal:
        return parent.this
    return None
