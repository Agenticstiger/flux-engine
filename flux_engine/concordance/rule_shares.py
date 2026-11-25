# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Every contract rule, checked on the output, as the share of rows that holds it.

Rules are enforced at generation (:mod:`flux_engine.generator.rules`), and until now
``verify`` never re-checked them. This module reads a run's rows back and counts, for each rule
of an emit, the rows it covers and the rows that satisfy it: a
:class:`~flux_engine.concordance.shares.Holds`. The forms are the ones a contract declares
(EXPR-SPEC-2 section 1):

* a ``derive``: the column holds the value its ``expr`` gives on the row;
* a ``check``: the row satisfies the predicate;
* a ``dependency``: rows that share the ``from`` columns' values share the ``to`` columns' values.
  The key's value is its most frequent ``to`` tuple, the first one met on a tie, and a row that
  holds another violates the dependency; a row with a NULL in ``from`` reaches no key and is not
  covered.

A rule with a ``when`` covers the rows where it is true. A text that is unknown on a row (a NULL
in a comparison) does not satisfy it: truth is two-valued (EXPR-SPEC-2 section 4). A rule that
reads another row reads it through the rows of the exposes it names, so a check over a link needs
the parent's rows.

The FLUX-side rules of an emit (EXPR-SPEC-2 section 1, the extension's ``rules``) are checked the
same way. A contract ``derive`` with ``aggregate`` is checked on every parent row as a ``derive``
is when its ``via`` relationship is on the counted layout (ADR 0016 section 8): each child is
joined to its parent through the relationship in the run's rows, and the rule is enforced. An
aggregate that does not generate is not checked here either: it follows the generator.

Each ``counts`` entry of an emit adds up to two lines of the emit's rules (ADR 0016 section 8), both
over the parents of the relationship and both enforced: ``counts:<relationship>.minChildren``
covers the eligible parents (those ``parentWhen`` makes TRUE, or every parent when there is no
condition) and holds for those with at least ``minChildren`` children, and
``counts:<relationship>.parentWhen`` covers the parents outside the condition and holds for those
with no child.

A rule that has a ``band`` (``mustBe``, ``mustBeLessThan`` and the rest, with ``unit`` of ``rows``
or ``percent``) states how many violating rows it allows; :func:`within_band` reads the share
against it. A rule with no band that the engine enforces holds on every row. A rule it does not
enforce (a contract ``check`` with no ``generates`` setting, a FLUX-side ``check``) is reported
and never fails a run on its own: it is a fact about the data, not a promise of the generator.

Nothing here quotes a row or a value: the result is counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    FrozenSet,
    Hashable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from flux_engine.concordance.history_checks import (
    WITHIN_PARENT,
    AsOfRows,
    history_shares,
    within_parent_share,
)
from flux_engine.concordance.projection_checks import (
    entity_columns,
    projected_history,
    projection_entry_of,
    projection_shares,
    window_of,
)
from flux_engine.concordance.shares import Holds
from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.expr2.check import PREDICATE, VALUE, Checked, check
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.evaluate import decoder, key_value
from flux_engine.expr2.parser import Side, parse
from flux_engine.expr2.rows import Rows
from flux_engine.expr2.rules import aggregate_tree, check_flux_side_rule
from flux_engine.expr2.scope import Model
from flux_engine.expr2.types import ColumnType, Kind
from flux_engine.generator.rules import (
    FORM_DEPENDENCY,
    FORM_DERIVE,
    Band,
    model_from_contract,
    sentinel_value,
)
from flux_engine.seam.asof import asof_entries_of
from flux_engine.seam.bridges import BridgeEntry, bridge_entries_of
from flux_engine.seam.counts import CountsEntry
from flux_engine.seam.facts import fact_name
from flux_engine.seam.history import HistoryEntry, history_entries_of
from flux_engine.seam.keys import declared_exposes
from flux_engine.seam.links import LinkEntry, link_entries_of
from flux_stats.spelling import render_exact

__all__ = [
    "FORM_CHECK",
    "RuleShare",
    "RuleSource",
    "rule_shares",
    "stored_rows",
    "stored_rows_of",
    "within_band",
]

FORM_CHECK = "check"

#: Where a rule was declared: the contract (``exposes[].contract.invariants[]``) or the bundle.
ORIGIN_CONTRACT = "contract"
ORIGIN_BUNDLE = "bundle"
#: A copy read from a ``sharedColumns`` or ``sharedAttribute``, scored as the ``derive`` it is.
ORIGIN_SHARED = "shared"

#: The names of the lines a link's configuration earns (the origin is the bundle's): the line of a
#: conditional link is ``link:<relationship>``, that of a sentinel ``sentinel:<relationship>.<column>``
#: and that of a bridge ``bridge:<a>,<b>``.
BRIDGE_RULE_PREFIX = "bridge:"
COUNTS_RULE_PREFIX = "counts:"
#: The line an as-of link earns, ``asOf:<relationship>.<date>``: how many rows read a version.
ASOF_RULE_PREFIX = "asOf:"
LINK_RULE_PREFIX = "link:"
SENTINEL_RULE_PREFIX = "sentinel:"


@dataclass(frozen=True)
class RuleShare:
    """One rule on one emit's rows: its share, its band and whether the engine enforces it.

    ``enforced`` is whether generation applies the rule, so that a violating row is a defect of
    the run rather than a property of the data. ``band`` is the rule's declared band as the plan
    records it, or ``None``.
    """

    expose_id: str
    rule_id: str
    form: str
    origin: str
    holds: Holds
    enforced: bool
    band: Optional[Mapping[str, Any]] = None
    #: The percent of covered rows the rule may violate, when it states one (a fact over dates,
    #: ADR 0024 section 8), and the largest gap from it, as shares, the check allows.
    target: Optional[Fraction] = None
    allowed: Optional[Fraction] = None

    @property
    def name(self) -> str:
        """The fact's name, ``<exposeId>.<ruleId>``."""
        return fact_name(self.expose_id, self.rule_id)

    @property
    def met(self) -> Optional[bool]:
        """Whether the share is what the rule promises; ``None`` when it promises nothing.

        A rule with a band is met when its violating rows are within the band. A rule with
        none is met when no row violates it, if the engine enforces it, and promises nothing
        when it does not. A rule with a target is met when the violating share is not above the
        target by more than the allowed gap: a draw that finds no value outside the interval keeps
        a value that holds, so a share below the target is not a miss.
        """
        if self.target is not None:
            share = self.holds.share
            if share is None:
                return True
            excess = (1 - share) - self.target / 100
            return excess <= (self.allowed if self.allowed is not None else Fraction(0))
        if self.band is not None:
            return within_band(self.band, self.holds)
        if self.enforced:
            return self.holds.whole
        return None

    def to_document(self) -> Dict[str, Any]:
        """The rule's row in a verify result."""
        document: Dict[str, Any] = {
            "rule": self.rule_id,
            "form": self.form,
            "origin": self.origin,
            "enforced": self.enforced,
            **self.holds.to_document(),
            "met": self.met,
        }
        if self.band is not None:
            document["band"] = dict(self.band)
        if self.target is not None:
            document["target"] = render_exact(self.target)
            document["allowed"] = render_exact(
                self.allowed if self.allowed is not None else Fraction(0)
            )
        return document


@dataclass(frozen=True)
class RuleSource:
    """What the rules of one emit need beyond the contract: its extension and whether it generates."""

    extension: Optional[Mapping[str, Any]] = None
    #: Whether generation applies this emit's rules (it has a fidelity block, so its rows come from
    #: the generator rather than from the run's journeys).
    generated: bool = True
    #: The copies the emit makes (kernel 0.2.0), each ``(id, column, text, language)``, ``language``
    #: being ``None`` for a core text (:func:`flux_engine.generator.rules.shared_copy_texts`).
    copies: Tuple[Tuple[str, str, str, Optional[str]], ...] = ()
    #: The copies the emit declares and the run did not make (kernel 0.1.0, a key or foreign key
    #: member), in the same shape: each is reported as the ``derive`` it would be and enforces nothing.
    reported: Tuple[Tuple[str, str, str, Optional[str]], ...] = ()
    #: The ``(child exposeId, relationship)`` pairs the run laid out with a ``counts`` entry, which a
    #: contract ``derive`` with ``aggregate`` generates over (kernel 0.2.0 only; empty otherwise).
    counted: FrozenSet[Tuple[str, str]] = frozenset()
    #: The ``counts`` entries of this emit's relationships, whose minimum and condition are scored.
    counts: Tuple[CountsEntry, ...] = ()
    #: The Simulation's ``spec.timeBounds``, which lays the periods of a periodic emit (ADR 0027 section 5).
    time_bounds: Optional[Mapping[str, Any]] = None
    #: The history of each emit that declares one (kernel 0.2.0), which an as-of link reads as versions.
    histories: Tuple[HistoryEntry, ...] = ()
    #: The start and end column of each emit's ``lifetimes`` entry, which a history that keeps to its
    #: parent's lifetime reads in the parent's rows (ADR 0017, amendment of 2026-11-24).
    lifetimes: Mapping[str, Tuple[str, Optional[str]]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Stored values
# ---------------------------------------------------------------------------


def _stored(column: ColumnType, value: Any) -> Any:
    """A value of an emitted row as the evaluator holds it (KERNEL-SPEC section 9's spellings read back)."""
    if value is None:
        return None
    kind = column.kind
    if kind is Kind.NUMBER:
        if isinstance(value, bool):
            raise TypeError("a number column holds a number")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return Decimal(value)
            except InvalidOperation:
                raise TypeError("a number column holds a decimal string") from None
        raise TypeError("a number column holds an integer or a decimal string")
    if kind is Kind.DATE:
        if not isinstance(value, str):
            raise TypeError("a date column holds YYYY-MM-DD")
        return date.fromisoformat(value)
    if kind in (Kind.TIMESTAMP, Kind.TIMESTAMPTZ):
        if not isinstance(value, str):
            raise TypeError("a timestamp column holds an RFC 3339 instant")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def stored_rows(
    model: Model, expose_id: str, names: Sequence[str], rows: Sequence[Sequence[Any]]
) -> List[Dict[str, Any]]:
    """An emit's rows as mappings of the values the evaluator reads.

    Raises ``ValueError`` or ``TypeError`` (never quoting a value) for a row whose width is not
    the header's or a cell that is not the spelling its column's type gives.
    """
    columns = model.exposes[expose_id].columns
    out: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        if len(row) != len(names):
            raise ValueError(f"row {index} holds {len(row)} values for {len(names)} columns")
        out.append(
            {
                name: (_stored(columns[name], cell) if name in columns else cell)
                for name, cell in zip(names, row, strict=True)
            }
        )
    return out


def stored_rows_of(model: Model, expose_id: str, table: Any) -> List[Dict[str, Any]]:
    """The rows of a :class:`~flux_engine.concordance.decoded.DecodedRows` as the evaluator holds them.

    Each column is converted once from the cells the emit was decoded to (nothing is parsed
    again), then the rows are joined. A table that is not a ``DecodedRows`` (the row reader's
    own mappings) goes through :func:`stored_rows`.
    """
    from flux_engine.concordance.decoded import DecodedRows

    if not isinstance(table, DecodedRows):
        names = list(table[0]) if len(table) else []
        return stored_rows(model, expose_id, names, [[row[n] for n in names] for row in table])
    columns = model.exposes[expose_id].columns
    names = list(table.names)
    converted = [
        (
            [_stored(columns[name], cell) for cell in table.cells(name)]
            if name in columns
            else list(table.cells(name))
        )
        for name in names
    ]
    return [dict(zip(names, values, strict=True)) for values in zip(*converted, strict=True)]


# ---------------------------------------------------------------------------
# Bands
# ---------------------------------------------------------------------------


def within_band(band: Mapping[str, Any], holds: Holds) -> bool:
    """Whether *holds* has as many violating rows as *band* allows.

    The band is :meth:`flux_engine.generator.rules.Band.as_document`: an ODCS operator, its
    values and ``unit``, which is ``rows`` (a count) or ``percent`` (a share of the covered rows).
    A rule that covers no row has no violating row, which every band reads as zero.
    """
    values = [Fraction(Decimal(str(value))) for value in band["values"]]
    violating = Fraction(holds.violating)
    if band["unit"] == "percent":
        violating = Fraction(100 * holds.violating, holds.total) if holds.total else Fraction(0)
    operator = band["operator"]
    first = values[0]
    if operator == "mustBe":
        return violating == first
    if operator == "mustBeLessThan":
        return violating < first
    if operator == "mustBeLessOrEqualTo":
        return violating <= first
    if operator == "mustBeGreaterThan":
        return violating > first
    if operator == "mustBeGreaterOrEqualTo":
        return violating >= first
    return first <= violating <= values[1]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _predicate(text: Any, model: Model, expose_id: str) -> Checked:
    return check(parse(str(text), side=Side.CORE), model, expose_id, position=PREDICATE)


def _covers(when: Optional[Checked], view: Any) -> bool:
    return when is None or bool(when.evaluate(view))


def _check_share(rule: Checked, when: Optional[Checked], rows: Rows, expose_id: str) -> Holds:
    covered = satisfied = 0
    for view in rows.rows(expose_id):
        if not _covers(when, view):
            continue
        covered += 1
        if rule.evaluate(view):
            satisfied += 1
    return Holds(satisfied, covered)


def _link_path(checked: Checked) -> Tuple[str, ...]:
    """The relationships a text that is one path crosses, or ``()`` when it crosses none."""
    for read in checked.reads:
        if read.reach == "link":
            return read.path
    return ()


def _sentinel_path(entries: Sequence[LinkEntry], column: str, value: Checked) -> Tuple[str, ...]:
    """The path of a ``derive`` of *column* a link's sentinel covers, else ``()``.

    A row whose path reaches no row holds the sentinel, and the sentinel's own line decides it:
    the ``derive`` covers the rows whose path reaches a row.
    """
    path = _link_path(value)
    for entry in entries:
        if path and path[0] == entry.relationship and column in dict(entry.sentinels):
            return path
    return ()


def _condition_share(entry: LinkEntry, rows: Rows, expose_id: str, model: Model) -> Holds:
    """A conditional link: of the rows whose link reaches a row, the share on which its condition is TRUE.

    A row that holds a link where the condition is not TRUE violates it (ADR 0022 section 9).
    """
    assert entry.when is not None
    condition = _predicate(entry.when, model, expose_id)
    covered = satisfied = 0
    for view in rows.rows(expose_id):
        if not _reaches(view, (entry.relationship,)):
            continue
        covered += 1
        if condition.evaluate(view) is True:
            satisfied += 1
    return Holds(satisfied, covered)


def _bridge_share(entry: BridgeEntry, rows: Rows, expose_id: str, model: Model) -> Holds:
    """A bridge: of every row, the share whose pair of parent keys occurs once (ADR 0022 section 8)."""
    scope = model.exposes[expose_id]
    columns = [name for side in entry.relationships for name in scope.links[side].from_columns]
    pairs: Dict[Tuple[Hashable, ...], int] = {}
    keys: List[Tuple[Hashable, ...]] = []
    for row in rows.tables[expose_id]:
        key = tuple(key_value(scope.columns[name], row.get(name)) for name in columns)
        keys.append(key)
        pairs[key] = pairs.get(key, 0) + 1
    return Holds(sum(1 for key in keys if pairs[key] == 1), len(keys))


def _sentinel_share(
    column: str, text: str, path: Sequence[str], rows: Rows, expose_id: str, model: Model
) -> Holds:
    """A sentinel: of the rows whose path reaches no row, the share whose *column* holds it."""
    column_type = model.exposes[expose_id].columns[column]
    read = decoder(column_type)
    value = sentinel_value(text, column_type, column)
    # The emit writer writes a string cell in NFC, so the sentinel is compared in NFC too.
    expected = read(nfc(value) if column_type.kind is Kind.STRING else value)
    covered = satisfied = 0
    for view in rows.rows(expose_id):
        if _reaches(view, path):
            continue
        covered += 1
        if read(view.get(column)) == expected:
            satisfied += 1
    return Holds(satisfied, covered)


def _note(
    copied: Dict[Tuple[str, str], Tuple[str, ...]],
    entries: Sequence[LinkEntry],
    column: str,
    value: Checked,
) -> None:
    """Remember the path of a copy a sentinel covers, by its relationship and column."""
    path = _sentinel_path(entries, column, value)
    if path:
        copied[(path[0], column)] = path


def _reaches(view: Any, path: Sequence[str]) -> bool:
    """Whether *path*, followed from *view*, reaches a row at every hop."""
    here = view
    for relationship in path:
        here = here.follow(relationship)
        if here is None:
            return False
    return True


def _derive_share(
    value: Checked,
    column: str,
    when: Optional[Checked],
    rows: Rows,
    expose_id: str,
    model: Model,
    reach: Sequence[str] = (),
) -> Holds:
    """The share of covered rows whose *column* is what *value* reads.

    A row is covered where *when* holds and, for a copy (*reach* names its path), where the path
    reaches a row: a row whose link reaches none is not the copy's to decide.
    """
    read = decoder(model.exposes[expose_id].columns[column])
    covered = satisfied = 0
    for view in rows.rows(expose_id):
        if not _covers(when, view) or not _reaches(view, reach):
            continue
        covered += 1
        if read(view.get(column)) == value.evaluate(view):
            satisfied += 1
    return Holds(satisfied, covered)


def _dependency_share(
    sources: Sequence[str], targets: Sequence[str], rows: Rows, expose_id: str, model: Model
) -> Holds:
    columns = model.exposes[expose_id].columns
    held: Dict[Tuple[Hashable, ...], Dict[Tuple[Hashable, ...], int]] = {}
    first_seen: Dict[Tuple[Hashable, ...], Dict[Tuple[Hashable, ...], int]] = {}
    for position, row in enumerate(rows.tables[expose_id]):
        key = tuple(key_value(columns[name], row.get(name)) for name in sources)
        if None in key:
            continue
        found = tuple(key_value(columns[name], row.get(name)) for name in targets)
        counts = held.setdefault(key, {})
        counts[found] = counts.get(found, 0) + 1
        first_seen.setdefault(key, {}).setdefault(found, position)
    covered = satisfied = 0
    for key, counts in held.items():
        modal = min(counts, key=lambda found: (-counts[found], first_seen[key][found]))
        covered += sum(counts.values())
        satisfied += counts[modal]
    return Holds(satisfied, covered)


def _declared_band(invariant: Mapping[str, Any], where: str) -> Optional[Dict[str, Any]]:
    band = Band.read(invariant, where)
    return None if band is None else band.as_document()


def _invariants(entry: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    contract = entry.get("contract")
    raw = contract.get("invariants") if isinstance(contract, Mapping) else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping) and isinstance(item.get("id"), str)]


def _flux_rules(extension: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    raw = extension.get("rules") if extension is not None else None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _exposes_read(model: Model, start: str, checked: Checked) -> Set[str]:
    """The exposes *checked* reads: each read's own, and every one a path crosses on its way to it."""
    found: Set[str] = set()
    for read in checked.reads:
        found.add(read.expose)
        if read.reach not in ("link", "siblings"):
            continue
        current = start
        # A read inside a window starts with the partition's relationship, which stays on the
        # row's own expose: the siblings are its rows.
        for step in read.path[1:] if read.reach == "siblings" else read.path:
            link = model.exposes[current].links.get(step)
            if link is None:
                break
            found.add(link.target)
            current = link.target
    return found


def tables_needed(
    contract_document: Mapping[str, Any],
    expose_id: str,
    extension: Optional[Mapping[str, Any]] = None,
    copies: Sequence[Tuple[str, str, str, Optional[str]]] = (),
    counted: FrozenSet[Tuple[str, str]] = frozenset(),
    counts: Sequence[CountsEntry] = (),
    histories: Collection[str] = (),
) -> Set[str]:
    """The exposes whose rows the rules of *expose_id* read: itself, and every expose a rule's text names.

    A path through several links needs the rows of each expose it crosses. *extension* is the
    emit's FLUX block, whose own rules are read as well, *copies* the copies the emit makes and
    *counted* the relationships the run laid out with a ``counts`` entry, whose generated
    aggregates read the child's rows. *histories* is the exposes that declare a history, which a
    path may cross (ADR 0017 section 8), and an as-of entry's target is read to score its line.
    """
    model = model_from_contract(contract_document, open_to=frozenset(histories) - {nfc(expose_id)})
    entry = declared_exposes(contract_document).get(nfc(expose_id))
    needed: Set[str] = {nfc(expose_id)}
    if entry is None:
        return needed
    needed |= _contract_reads(entry, model, expose_id, counted)
    needed |= _bundle_rule_reads(extension, model, expose_id)
    for link_entry in link_entries_of(extension, expose_id):
        link = model.exposes[nfc(expose_id)].links.get(link_entry.relationship)
        if link is not None and link_entry.when is not None:
            needed.add(link.target)  # a conditional link is scored on the rows it reaches
    needed |= _copy_reads(copies, model, expose_id)
    for asof_entry in asof_entries_of(extension, expose_id):
        link = model.exposes[nfc(expose_id)].links.get(asof_entry.relationship)
        if link is not None:
            needed.add(link.target)  # an as-of link is measured on the rows it reads
    needed |= _projection_reads(extension, model, expose_id)
    needed |= _parent_lifetime_reads(extension, model, expose_id)
    for counts_entry in counts:
        link = model.exposes[nfc(expose_id)].links.get(counts_entry.relationship)
        if link is not None and _scored_counts(counts_entry):
            needed.add(link.target)  # the minimum and the condition are scored on the parents' rows
    return needed


def _parent_lifetime_reads(
    extension: Optional[Mapping[str, Any]], model: Model, expose_id: str
) -> Set[str]:
    """The parent of each history bound by its parent's lifetime: its rows are read to score the bound."""
    found: Set[str] = set()
    for held in history_entries_of(extension, expose_id):
        if held.parent is not None and held.parent.lifetime:
            link = model.exposes[nfc(expose_id)].links.get(held.parent.relationship)
            if link is not None:
                found.add(link.target)
    return found


def _generated_aggregate(invariant: Mapping[str, Any], counted: FrozenSet[Tuple[str, str]]) -> bool:
    """Whether *invariant* is a ``derive`` with ``aggregate`` over a relationship on the counted layout."""
    aggregate = invariant.get("aggregate")
    return (
        "derive" in invariant
        and isinstance(aggregate, Mapping)
        and (str(aggregate.get("from")), str(aggregate.get("via"))) in counted
    )


def _contract_reads(
    entry: Mapping[str, Any],
    model: Model,
    expose_id: str,
    counted: FrozenSet[Tuple[str, str]] = frozenset(),
    counts: Sequence[CountsEntry] = (),
) -> Set[str]:
    """The exposes the contract's own rule texts of *expose_id* name."""
    needed: Set[str] = set()
    for invariant in _invariants(entry):
        if _generated_aggregate(invariant, counted):
            try:
                checked = _checked_aggregate(invariant, model, expose_id)
            except (
                Exception
            ):  # noqa: BLE001 - a text this cannot read is reported as unreadable later
                continue
            needed |= _exposes_read(model, nfc(expose_id), checked)
        for key in ("check", "expr", "when"):
            if isinstance(invariant.get(key), str):
                try:
                    checked = _checked_text(invariant, key, model, expose_id)
                except (
                    Exception
                ):  # noqa: BLE001 - a text this cannot read is reported as unreadable later
                    continue
                needed |= _exposes_read(model, nfc(expose_id), checked)
    return needed


def _bundle_rule_reads(
    extension: Optional[Mapping[str, Any]], model: Model, expose_id: str
) -> Set[str]:
    """The exposes the bundle's own rules for *expose_id* name."""
    needed: Set[str] = set()
    for flux_entry in _flux_rules(extension):
        try:
            flux_rule = check_flux_side_rule(flux_entry, model, nfc(expose_id))
        except Exception:  # noqa: BLE001 - reported as unreadable when the rule is scored
            continue
        for text in (flux_rule.value, flux_rule.when):
            if text is not None:
                needed |= _exposes_read(model, nfc(expose_id), text)
    return needed


def _copy_reads(
    copies: Sequence[Tuple[str, str, str, Optional[str]]], model: Model, expose_id: str
) -> Set[str]:
    """The exposes the texts of *copies* name."""
    needed: Set[str] = set()
    for _, column, copy_text, language in copies:
        try:
            checked_copy = _checked_copy(copy_text, language, column, model, nfc(expose_id))
        except (Expr2Error, FluxError):
            continue  # a copy of columns that do not fit is reported as unreadable when scored
        needed |= _exposes_read(model, nfc(expose_id), checked_copy)
    return needed


def _checked_copy(
    text: str, language: Optional[str], column: str, model: Model, expose_id: str
) -> Checked:
    parsed = (
        parse(text, side=Side.CORE)
        if language is None
        else parse(text, side=Side.FLUX, language=language)
    )
    return check(parsed, model, expose_id, position=VALUE, target=column)


def _checked_aggregate(invariant: Mapping[str, Any], model: Model, expose_id: str) -> Checked:
    """A contract ``derive`` with ``aggregate`` as the summary text it maps to, checked on the parent."""
    return check(
        aggregate_tree(invariant["aggregate"]),
        model,
        expose_id,
        position=VALUE,
        target=nfc(str(invariant["derive"])),
    )


def _checked_text(invariant: Mapping[str, Any], key: str, model: Model, expose_id: str) -> Checked:
    if key == "expr":
        return check(
            parse(str(invariant["expr"]), side=Side.CORE),
            model,
            expose_id,
            position=VALUE,
            target=nfc(str(invariant["derive"])),
        )
    return _predicate(invariant[key], model, expose_id)


def rule_shares(
    contract_document: Mapping[str, Any],
    expose_id: str,
    tables: Mapping[str, Sequence[Mapping[str, Any]]],
    source: Optional[RuleSource] = None,
) -> Tuple[RuleShare, ...]:
    """The share of rows that holds each rule of *expose_id*, in declared order: the contract's, then the bundle's.

    *tables* holds the stored rows (:func:`stored_rows`) of *expose_id* and of every expose its
    rules read. Raises what the contract's own texts raise when one does not check (the plan
    refused such a contract before the run existed), and ``KeyError`` when a table is missing.
    """
    source = source if source is not None else RuleSource()
    target = nfc(expose_id)
    entry = declared_exposes(contract_document).get(target)
    if entry is None:
        return ()
    model = model_from_contract(
        contract_document,
        open_to=frozenset(entry.emit for entry in source.histories if entry.emit != target),
    )
    held = {name: tables[name] for name in tables if name in model.exposes}
    asof = asof_entries_of(source.extension, target)
    scoring = _Scoring(
        source=source,
        target=target,
        model=model,
        rows=(
            AsOfRows(model, held, asof, {entry.emit: entry for entry in source.histories})
            if asof
            else Rows(model, held)
        ),
        links=link_entries_of(source.extension, target),
    )
    _score_contract_rules(scoring, entry)
    _score_bundle_rules(scoring)
    _score_copies(scoring)
    _score_links(scoring)
    _score_bridges(scoring)
    _score_counts(scoring)
    _score_history(scoring)
    _score_asof(scoring)
    _score_projection(scoring, tables)
    return tuple(scoring.found)


@dataclass
class _Scoring:
    """What :func:`rule_shares` scores one expose against, and the shares it has found so far."""

    source: RuleSource
    target: str
    model: Model
    rows: Rows
    links: Sequence[Any]
    copied: Dict[Tuple[str, str], Tuple[str, ...]] = field(default_factory=dict)
    found: List[RuleShare] = field(default_factory=list)

    def add(
        self, rule_id: str, form: str, origin: str, holds: Holds, enforced: bool, band: Any = None
    ) -> None:
        """Record one rule's share; a rule is enforced only when the run generated under it."""
        self.found.append(
            RuleShare(
                expose_id=self.target,
                rule_id=nfc(rule_id),
                form=form,
                origin=origin,
                holds=holds,
                enforced=enforced and self.source.generated,
                band=band,
            )
        )


def _score_contract_rules(scoring: _Scoring, entry: Mapping[str, Any]) -> None:
    """Score the contract's own rules of the expose, in declared order."""
    settings = (scoring.source.extension or {}).get("invariants")
    settings = settings if isinstance(settings, Mapping) else {}
    for invariant in _invariants(entry):
        rule_id = str(invariant["id"])
        band = _declared_band(invariant, f"rule {rule_id!r}")
        when = (
            _predicate(invariant["when"], scoring.model, scoring.target)
            if invariant.get("when") is not None
            else None
        )
        if "dependency" in invariant:
            _score_contract_dependency(scoring, invariant, rule_id, band)
        elif "derive" in invariant:
            _score_contract_derive(scoring, invariant, rule_id, when, band)
        elif "check" in invariant:
            generated = isinstance(settings.get(rule_id), Mapping) and bool(
                settings[rule_id].get("generates")
            )
            holds = _check_share(
                _predicate(invariant["check"], scoring.model, scoring.target),
                when,
                scoring.rows,
                scoring.target,
            )
            scoring.add(rule_id, FORM_CHECK, ORIGIN_CONTRACT, holds, generated, band)


def _score_contract_dependency(
    scoring: _Scoring, invariant: Mapping[str, Any], rule_id: str, band: Any
) -> None:
    """Score one contract dependency rule."""
    raw = invariant["dependency"]
    sources = [nfc(str(name)) for name in raw["from"]]
    targets = [nfc(str(name)) for name in raw["to"]]
    holds = _dependency_share(sources, targets, scoring.rows, scoring.target, scoring.model)
    scoring.add(rule_id, FORM_DEPENDENCY, ORIGIN_CONTRACT, holds, True, band)


def _score_contract_derive(
    scoring: _Scoring,
    invariant: Mapping[str, Any],
    rule_id: str,
    when: Any,
    band: Any,
) -> None:
    """Score one contract derive rule, an aggregate only when the run generated it."""
    model, rows, target, links = scoring.model, scoring.rows, scoring.target, scoring.links
    column = nfc(str(invariant["derive"]))
    if "expr" not in invariant:
        if _generated_aggregate(invariant, scoring.source.counted):
            scoring.add(
                rule_id,
                FORM_DERIVE,
                ORIGIN_CONTRACT,
                _derive_share(
                    _checked_aggregate(invariant, model, target),
                    column,
                    when,
                    rows,
                    target,
                    model,
                ),
                True,
                band,
            )
        return  # an aggregate that does not generate follows the generator
    value = _checked_text(invariant, "expr", model, target)
    _note(scoring.copied, links, column, value)
    scoring.add(
        rule_id,
        FORM_DERIVE,
        ORIGIN_CONTRACT,
        _derive_share(
            value, column, when, rows, target, model, _sentinel_path(links, column, value)
        ),
        True,
        band,
    )


def _score_bundle_rules(scoring: _Scoring) -> None:
    """Score the rules the bundle declares for the expose."""
    model, rows, target, links = scoring.model, scoring.rows, scoring.target, scoring.links
    for flux_entry in _flux_rules(scoring.source.extension):
        flux_rule = check_flux_side_rule(flux_entry, model, target)
        if flux_rule.derive is None:
            holds = _check_share(flux_rule.value, flux_rule.when, rows, target)
            scoring.add(flux_rule.id, FORM_CHECK, ORIGIN_BUNDLE, holds, False)
        else:
            _note(scoring.copied, links, nfc(flux_rule.derive), flux_rule.value)
            holds = _derive_share(
                flux_rule.value,
                nfc(flux_rule.derive),
                flux_rule.when,
                rows,
                target,
                model,
                _sentinel_path(links, nfc(flux_rule.derive), flux_rule.value),
            )
            scoring.add(flux_rule.id, FORM_DERIVE, ORIGIN_BUNDLE, holds, True)


def _score_copies(scoring: _Scoring) -> None:
    """Score the copies the run made, then the ones it reports without having made them."""
    model, rows, target, links = scoring.model, scoring.rows, scoring.target, scoring.links
    for copy_id, column, text, language in scoring.source.copies:
        value = _checked_copy(text, language, column, model, target)
        _note(scoring.copied, links, nfc(column), value)
        holds = _derive_share(value, nfc(column), None, rows, target, model, _link_path(value))
        scoring.add(copy_id, FORM_DERIVE, ORIGIN_SHARED, holds, True)
    for copy_id, column, text, language in scoring.source.reported:
        try:
            value = _checked_copy(text, language, column, model, target)
            holds = _derive_share(value, nfc(column), None, rows, target, model, _link_path(value))
        except (Expr2Error, FluxError):
            continue  # a copy the run never made, of columns that do not fit: nothing to report
        scoring.add(copy_id, FORM_DERIVE, ORIGIN_SHARED, holds, False)


def _score_links(scoring: _Scoring) -> None:
    """Score each conditional link's condition and each of its sentinels."""
    model, rows, target = scoring.model, scoring.rows, scoring.target
    for link_entry in scoring.links:
        if link_entry.when is not None:
            holds = _condition_share(link_entry, rows, target, model)
            scoring.add(
                f"{LINK_RULE_PREFIX}{link_entry.relationship}",
                FORM_CHECK,
                ORIGIN_BUNDLE,
                holds,
                True,
            )
        for column, text in link_entry.sentinels:
            path = scoring.copied.get((link_entry.relationship, column))
            if path is None:
                continue  # `plan` refuses a sentinel no copy through the relationship holds
            holds = _sentinel_share(column, text, path, rows, target, model)
            scoring.add(
                f"{SENTINEL_RULE_PREFIX}{link_entry.relationship}.{column}",
                FORM_CHECK,
                ORIGIN_BUNDLE,
                holds,
                True,
            )


def _score_bridges(scoring: _Scoring) -> None:
    """Score each bridge the bundle declares for the expose."""
    for bridge in bridge_entries_of(scoring.source.extension, scoring.target):
        holds = _bridge_share(bridge, scoring.rows, scoring.target, scoring.model)
        scoring.add(
            f"{BRIDGE_RULE_PREFIX}{','.join(bridge.relationships)}",
            FORM_CHECK,
            ORIGIN_BUNDLE,
            holds,
            True,
        )


def _scored_counts(entry: CountsEntry) -> bool:
    """Whether a counts entry states a minimum above 0 or a condition, which are lines of the rules."""
    return bool(entry.min_children) or entry.parent_when is not None


def _children_per_parent(
    entry: CountsEntry, rows: Rows, expose_id: str, model: Model
) -> List[Tuple[int, bool]]:
    """For each parent row: how many children it has, and whether it is eligible to have any."""
    link = model.exposes[expose_id].links[entry.relationship]
    condition = (
        _predicate(entry.parent_when, model, link.target) if entry.parent_when is not None else None
    )
    return [
        (len(view.children(expose_id, entry.relationship)), _covers(condition, view))
        for view in rows.rows(link.target)
    ]


def _score_counts(scoring: _Scoring) -> None:
    """Score the minimum and the condition of each counts entry of the expose, on its parents."""
    for entry in scoring.source.counts:
        if not _scored_counts(entry) or entry.relationship not in (
            scoring.model.exposes[scoring.target].links
        ):
            continue
        held = _children_per_parent(entry, scoring.rows, scoring.target, scoring.model)
        prefix = f"{COUNTS_RULE_PREFIX}{entry.relationship}"
        if entry.min_children:
            eligible = [children for children, admitted in held if admitted]
            holds = Holds(sum(1 for n in eligible if n >= entry.min_children), len(eligible))
            scoring.add(f"{prefix}.minChildren", FORM_CHECK, ORIGIN_BUNDLE, holds, True)
        if entry.parent_when is not None:
            outside = [children for children, admitted in held if not admitted]
            scoring.add(
                f"{prefix}.parentWhen",
                FORM_CHECK,
                ORIGIN_BUNDLE,
                Holds(sum(1 for n in outside if n == 0), len(outside)),
                True,
            )


def _score_history(scoring: _Scoring) -> None:
    """Score the lines of the expose's history (ADR 0017 section 14), each enforced, on its entities."""
    versions = scoring.model.exposes[scoring.target].versions
    if versions is None:
        return
    for entry in history_entries_of(scoring.source.extension, scoring.target):
        for rule_id, holds in history_shares(entry, scoring.rows.tables[scoring.target], versions):
            scoring.add(rule_id, FORM_CHECK, ORIGIN_BUNDLE, holds, True)
        _score_within_parent(scoring, entry, versions)


def _score_within_parent(scoring: _Scoring, entry: HistoryEntry, versions: Any) -> None:
    """``history:within_parent``: the entities that keep to their parent's lifetime, when the entry asks."""
    parent = entry.parent
    if parent is None or not parent.lifetime:
        return
    link = scoring.model.exposes[scoring.target].links.get(parent.relationship)
    lifetime = None if link is None else scoring.source.lifetimes.get(link.target)
    if link is None or lifetime is None or link.target not in scoring.rows.tables:
        return
    parents = {
        tuple(row.get(column) for column in link.to_columns): row
        for row in scoring.rows.tables[link.target]
    }
    holds = within_parent_share(
        entry,
        scoring.rows.tables[scoring.target],
        versions,
        parents,
        link.from_columns,
        lifetime,
    )
    scoring.add(WITHIN_PARENT, FORM_CHECK, ORIGIN_BUNDLE, holds, True)


def _score_asof(scoring: _Scoring) -> None:
    """Measure each as-of link: how many rows read a version of the linked entity (ADR 0017 section 8).

    The line is measured and not scored: the date is the row's own, and this record does not move it.
    """
    rows = scoring.rows
    if not isinstance(rows, AsOfRows):
        return
    total = len(rows.tables[scoring.target])
    for entry in asof_entries_of(scoring.source.extension, scoring.target):
        if entry.relationship not in scoring.model.exposes[scoring.target].links:
            continue
        missed = rows.misses(scoring.target, entry.relationship)
        scoring.add(
            f"{ASOF_RULE_PREFIX}{entry.relationship}.{entry.date}",
            FORM_CHECK,
            ORIGIN_BUNDLE,
            Holds(total - missed, total),
            False,
        )


def _projection_reads(
    extension: Optional[Mapping[str, Any]], model: Model, expose_id: str
) -> Set[str]:
    """The history emit that the expose's projection reads, when the model holds it."""
    entry = projection_entry_of(extension, expose_id)
    return {entry.of} if entry is not None and entry.of in model.exposes else set()


def _when_over(
    entry: Any, scoring: _Scoring, of: str
) -> Optional[Callable[[Mapping[str, Any]], bool]]:
    """The entry's ``when`` as a condition over a row of the history's emit, or ``None`` without one."""
    text = getattr(entry, "when", None)
    if text is None:
        return None
    checked = check(parse(str(text), side=Side.FLUX), scoring.model, of, position=PREDICATE)
    table = scoring.rows.tables[of]
    views = {id(row): scoring.rows.row(of, index) for index, row in enumerate(table)}
    return lambda row: bool(checked.evaluate(views[id(row)]))


def _score_projection(scoring: _Scoring, tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
    """Score the lines of the expose's projection (ADR 0027 section 10), each enforced.

    The lines read the rows of the history the projection is of. A history whose rows the run did
    not write leaves the projection unscored; planning says so in a note.
    """
    extension = scoring.source.extension
    entry = projection_entry_of(extension, scoring.target)
    history = projected_history(extension, entry.of) if entry is not None else None
    if entry is None or history is None or entry.of not in tables:
        return
    versions = scoring.model.exposes[entry.of].versions
    if versions is None:
        return
    entity = entity_columns(scoring.model, scoring.target, entry.of, versions) or ()
    for rule_id, holds in projection_shares(
        entry,
        history,
        scoring.rows.tables[scoring.target],
        scoring.rows.tables[entry.of],
        versions,
        entity,
        _when_over(entry, scoring, entry.of),
        window_of(scoring.source.time_bounds),
    ):
        scoring.add(rule_id, FORM_CHECK, ORIGIN_BUNDLE, holds, True)
