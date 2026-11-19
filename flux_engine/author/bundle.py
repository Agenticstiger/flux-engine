# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Write a FLUX bundle from a FLUID contract and a profile: ``fluxsim init --contract --profile``.

The bundle holds four documents: the contract, copied byte for byte; a World and
a Persona, the least a run needs (generator emits read none of their numbers);
and a Simulation with one generator emit per expose the profile describes, the
profile's digest pinned, and a parameter register that says what every number
rests on. The documents are written into a scratch directory first and checked
by ``fluxsim validate``'s own stage, so the command never leaves a bundle behind
that does not validate.

The command reads two documents and no record. The profile holds aggregates
only, so every number this writes is a published aggregate, a fact of the
contract, or an assumption stated with its reason.

Prior art, as patterns: DataSynthesizer's describe-then-generate split (MIT), a
description document a generator reads; SDV's metadata workflow (BSL, pattern
only), detect, correct, then validate before use; dbldatagen's generated
generator specs (pattern only), handed over as a draft to edit; Deequ's
constraint suggestions (Apache-2.0), each with the rule and the value behind it.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import yaml

from flux_engine.author.basis import Basis, Label, assumed, default, from_profile
from flux_engine.author.children import spread_children
from flux_engine.author.columns import (
    MOST_PLACES,
    ColumnContext,
    ColumnPlan,
    PropertyPlan,
    assumed_check,
    declared_span,
    draw_within_key,
    plan_column,
    widen_key_span,
)
from flux_engine.author.given import choose_givens
from flux_engine.author.ledger import Ledger
from flux_engine.core.canonical import JCS_MAX_SAFE_INTEGER, nfc
from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.coverage import printable
from flux_engine.seam.keys import ContractKeys, ExposeKeys, compile_keys
from flux_engine.seam.parameters import (
    UNUSED_KEY,
    build_parameter_register,
    declared_numbers,
    parse_pointer,
    read_parameters_block,
    read_unused_block,
    render_pointer,
)
from flux_engine.seam.profile import load_profile
from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105, ExactLoader, read_bundle_file
from flux_engine.starter import StarterFile
from flux_stats.column_metrics import COUNT
from flux_stats.exact_yaml import load_documents

__all__ = [
    "AuthoredBundle",
    "AuthoredExpose",
    "AuthorError",
    "FLUX_VERSION",
    "Gap",
    "StrictFindings",
    "author_bundle",
    "strict_findings",
]

#: The FLUX version the authored documents declare: the starter's, so both
#: paths into a first run write documents of one version.
FLUX_VERSION = "0.5.0"

#: The seed every authored document starts from, as the starter's does.
SEED = 42

#: The run window when no date or timestamp column publishes one: a calendar
#: year, stated as an assumption for the author to replace.
ASSUMED_WINDOW = ("2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z")

#: Bounds on the size of a committed state, wide enough for any profiled table.
RECORDS = {"min": 1, "max": 1_000_000_000}

#: When the fidelity check runs. Not a number the register counts.
BUDGET = {"maxChecksPerState": 20, "maxStateAge": "P30D"}

_HEADER = (
    "# Written by `fluxsim init` from {contract} and {profile}.\n"
    "# Every number below has its source in the parameter register\n"
    "# (extensions.com.agentics.flux/1.parameters): the profile, the contract, an\n"
    "# assumption with its reason, or a default. Replace the assumptions you can.\n"
)


class AuthorError(Exception):
    """The bundle cannot be written: an input is unusable, or no expose can be generated."""


@dataclass
class AuthoredExpose:
    """One expose of the bundle, or one that was left out and why."""

    expose_id: str
    rows: int = 0
    #: What the row count rests on.
    rows_basis: Optional[Basis] = None
    #: What a person is told about the row count, when it is not the published rowCount.
    rows_note: Optional[str] = None
    columns: List[ColumnPlan] = field(default_factory=list)
    #: What each assumption is about (a column, ``rows``) and the sentence that states it.
    assumptions: List[Tuple[str, str]] = field(default_factory=list)
    properties: List[PropertyPlan] = field(default_factory=list)
    skipped: Optional[str] = None
    #: Each planned column's logical type, for the one assumed check a table may need.
    kinds: Dict[str, LogicalKind] = field(default_factory=dict)


@dataclass(frozen=True)
class Gap:
    """An assumption that stands in for something the profile did not publish (ADR 0014)."""

    #: The position in the Simulation the assumption decides, as a JSON Pointer.
    pointer: str
    reason: str

    @property
    def tokens(self) -> Tuple[str, ...]:
        return parse_pointer(self.pointer)


@dataclass
class AuthoredBundle:
    """The files to write, and what a person should be told about them."""

    files: Tuple[StarterFile, ...]
    exposes: Tuple[AuthoredExpose, ...]
    #: How many numbers rest on each source, as the register will count them.
    counts: Mapping[str, int]
    #: Assumptions that are not numbers the register counts (text, such as the time window),
    #: as (what it is about, the sentence that states it).
    notes: Tuple[Tuple[str, str], ...]
    profile_digest: str
    simulation_id: str
    #: Every statistic and metric of the profile, and what the bundle made of it.
    ledger: Ledger = field(default_factory=lambda: Ledger({}))
    #: The assumptions that stand in for what the profile did not publish, in order.
    gaps: Tuple[Gap, ...] = ()


@dataclass(frozen=True)
class StrictFindings:
    """What ``--strict`` refuses: unused statistics and gaps no allow entry exempts."""

    unused: Tuple[Tuple[str, Optional[str], str], ...]
    gaps: Tuple[Gap, ...]
    #: ``--allow`` pointers that exempt nothing.
    stale: Tuple[str, ...]

    @property
    def refused(self) -> bool:
        return bool(self.unused or self.gaps or self.stale)


def _exempts(allowed: Tuple[str, ...], tokens: Tuple[str, ...]) -> bool:
    return tokens[: len(allowed)] == allowed


def strict_findings(bundle: AuthoredBundle, allow: Sequence[str]) -> StrictFindings:
    """What ``fluxsim init --strict`` refuses, after the *allow* pointers exempt theirs.

    An allow pointer exempts every unused statistic of the profile and every gap
    of the Simulation at or below it. One that exempts nothing is a finding of
    its own, as mypy's ``--warn-unused-ignores`` reports an ignore that ignores
    nothing, so an allow-list cannot go stale. Raises ``ValueError`` for an
    allow entry that is not a JSON Pointer.
    """
    pointers = [(text, parse_pointer(text)) for text in allow]
    exempting = set()
    unused = []
    for entry in bundle.ledger.unused_entries():
        hits = [text for text, tokens in pointers if _exempts(tokens, entry.tokens)]
        exempting.update(hits)
        if not hits:
            unused.append((entry.pointer, entry.definition, entry.note))
    gaps = []
    for gap in bundle.gaps:
        hits = [text for text, tokens in pointers if _exempts(tokens, gap.tokens)]
        exempting.update(hits)
        if not hits:
            gaps.append(gap)
    stale = tuple(text for text, _ in pointers if text not in exempting)
    return StrictFindings(unused=tuple(unused), gaps=tuple(gaps), stale=stale)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _read_contract(path: Path) -> Tuple[Mapping[str, Any], bytes]:
    """The contract and its exact bytes, read once: the bundle copies the bytes it parsed.

    Parsed as the engine parses a bundle document
    (:func:`flux_stats.exact_yaml.load_documents`: no float, every number
    exact, within the read's bounds). A file that cannot be read, is not UTF-8
    or is not YAML is refused with what is wrong, and so is one that holds
    other than exactly one FLUID contract. A contract past one of the read's
    bounds, or with a scalar its explicit tag cannot build, is refused with
    the read's own code: ``FLX-3105``, or ``FLX-1003`` for an integer too
    long to read. So is a path that is not a regular file, or a file past a
    bundle file's size cap, before a byte is read: the contract becomes the
    bundle's FLUID document (:func:`flux_engine.seam.resolve.read_bundle_file`).
    """
    shown = printable(str(path))
    try:
        data = read_bundle_file(path)
    except OSError as error:
        raise AuthorError(f"cannot read the contract {shown}: {error.strerror or error}") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise AuthorError(f"the contract {shown} is not UTF-8 text") from None
    try:
        documents = load_documents(text, name=path.name)
    except FluxError as error:
        cause = error.__cause__
        if not isinstance(cause, yaml.YAMLError):
            raise
        problem = getattr(cause, "problem", None) or type(cause).__name__
        raise AuthorError(f"the contract {shown} is not YAML: {problem}") from None
    contracts = [
        document for document in documents if isinstance(document, dict) and "exposes" in document
    ]
    if len(contracts) != 1:
        raise AuthorError(
            f"{shown} holds {len(contracts)} FLUID contracts; give it a file with exactly one"
        )
    contract = contracts[0]
    if not isinstance(contract.get("id"), str) or not isinstance(contract.get("exposes"), list):
        raise AuthorError(f"{shown}: a FLUID contract has an id and a list of exposes")
    return contract, data


def _contract_file_name(path: Path) -> str:
    """The contract's name in the bundle: its own, when the bundle loader reads it by that name."""
    name = path.name
    if name.endswith((".fluid.yml", ".fluid.yaml")):
        return name
    return f"{path.stem}.fluid.yaml"


def _minimum_cell(profile: Mapping[str, Any]) -> int:
    """The smallest minimum cell any disclosure rule names; ACRO's 10 when none does."""
    rules = profile.get("disclosureRules")
    cells: List[int] = []
    for rule in rules.values() if isinstance(rules, Mapping) else ():
        cell = rule.get("minimumCell") if isinstance(rule, Mapping) else None
        if isinstance(cell, int) and not isinstance(cell, bool) and cell > 0:
            cells.append(cell)
    return min(cells) if cells else 10


# ---------------------------------------------------------------------------
# One expose
# ---------------------------------------------------------------------------


def _emit_rows(
    expose_id: str, table: Mapping[str, Any], minimum_cell: int
) -> Tuple[int, Basis, Optional[str]]:
    """The emit's row count, its basis, and the assumption a person is told about, if any."""
    row_count = table.get("rowCount")
    if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count > 0:
        return row_count, from_profile(("tables", expose_id, "rowCount"), None), None
    counts = []
    for name, column in (table.get("columns") or {}).items():
        metric = (column.get("metrics") or {}).get("count") if isinstance(column, Mapping) else None
        if isinstance(metric, Mapping) and metric.get("status") == "F":
            value = metric.get("value")
            if isinstance(value, int) and value > 0:
                counts.append((value, name))
    if counts:
        value, name = max(counts)
        return (
            value,
            from_profile(
                ("tables", expose_id, "columns", name, "metrics", "count"), COUNT.identifier
            ),
            f"The profile has no rowCount: {value} rows, the largest published count "
            f"(of {printable(name)}).",
        )
    rows = max(1, minimum_cell // 2)
    reason = (
        f"The profile publishes no row count and no column count: {rows} rows are assumed, half "
        f"the minimum cell ({minimum_cell})."
    )
    return rows, assumed(reason), reason


def _plan_expose(
    contract: Mapping[str, Any],
    expose_index: int,
    expose: Mapping[str, Any],
    table: Mapping[str, Any],
    expose_keys: Optional[ExposeKeys],
    *,
    minimum_cell: int,
    fractional_bits: int,
) -> AuthoredExpose:
    """Declare every column of one expose; its properties are chosen once every key is laid out."""
    expose_id = str(expose.get("exposeId"))
    compiled = compile_expose(expose)
    relationships = expose_keys.relationships if expose_keys is not None else ()
    one_column_keys = expose_keys.one_column_keys if expose_keys is not None else frozenset()
    keyed = {
        column
        for key in (
            (expose_keys.keys + expose_keys.instant_keys) if expose_keys is not None else ()
        )
        for column in key
    }
    keyed.update(
        column
        for relationship in relationships
        if relationship.cardinality == "one_to_one"
        for column in relationship.from_columns
    )
    inner = {
        column
        for relationship in relationships
        if relationship.join_type == "inner"
        for column in relationship.from_columns
    }
    foreign = {
        column: relationship.target
        for relationship in relationships
        for column in relationship.from_columns
    }
    # A composite foreign key is null on all its columns or none: its first decides.
    follow = {column for relationship in relationships for column in relationship.from_columns[1:]}
    rows, rows_basis, rows_note = _emit_rows(expose_id, table, minimum_cell)
    authored = AuthoredExpose(
        expose_id=expose_id, rows=rows, rows_basis=rows_basis, rows_note=rows_note
    )
    if rows_note:
        authored.assumptions.append(("rows", rows_note))
    profiled = table.get("columns") if isinstance(table.get("columns"), Mapping) else {}
    schema = expose["contract"]["schema"]
    for schema_index, column in enumerate(compiled.columns):
        if column.logical_type.kind.value in ("JSON", "BYTES"):
            authored.skipped = (
                f"column {printable(column.name)!r} is {column.logical_type.kind.value}, which "
                "the generator has no family for (FLX-3401)"
            )
            return authored
        places = column.logical_type.scale
        if isinstance(places, int) and places > MOST_PLACES:
            authored.skipped = (
                f"column {printable(column.name)!r} has {places} decimal places, more than the "
                f"{MOST_PLACES} a bundle declares"
            )
            return authored
        position = next(
            (
                index
                for index, declared in enumerate(schema)
                if isinstance(declared, Mapping) and nfc(str(declared.get("name"))) == column.name
            ),
            schema_index,
        )
        plan = plan_column(
            ColumnContext(
                column=column,
                expose_index=expose_index,
                schema_index=position,
                product_ref=str(contract["id"]),
                profile_tokens=("tables", expose_id, "columns", column.name),
                entry=profiled.get(column.name) if isinstance(profiled, Mapping) else None,
                rows=rows,
                minimum_cell=minimum_cell,
                key=column.name in one_column_keys,
                foreign=foreign.get(column.name),
                keyed=column.name in keyed,
                inner=column.name in inner,
                nulls_follow=column.name in follow,
                fractional_bits=fractional_bits,
            )
        )
        authored.columns.append(plan)
        authored.kinds[plan.name] = column.logical_type.kind
    choose_givens(
        authored.columns,
        authored.kinds,
        profiled if isinstance(profiled, Mapping) else {},
        ("tables", expose_id),
        minimum_cell=minimum_cell,
    )
    return authored


@dataclass(frozen=True)
class _Link:
    """One one-column relationship, as the spans see it."""

    child: ColumnPlan
    kind: LogicalKind
    child_rows: int
    parent: ColumnPlan
    parent_kind: LogicalKind
    parent_rows: int

    @property
    def numeric(self) -> bool:
        """Whether both ends are number columns, the only ones a span is read from."""
        numbers = (LogicalKind.INT, LogicalKind.DECIMAL)
        return self.kind in numbers and self.parent_kind in numbers


def _span_keys(exposes: Sequence[AuthoredExpose], keys: Optional[ContractKeys]) -> None:
    """Keep each number foreign key inside the span of the key it references.

    A column's span stands when the profile publishes it; an assumed one (a key
    with no published min and max, or a placeholder foreign key the profile
    says nothing of) is placed, in three passes, each repeated until nothing
    moves:

    1. down: an assumed column under a parent whose span stands takes the span
       the parent is drawn over, and then stands itself (the first such
       relationship of the contract decides);
    2. up: an assumed key that still floats widens to hold the values of every
       column that references it (:func:`widen_key_span`, the AWS harness's
       rule), and a chain of such keys widens to its last link;
    3. a placeholder foreign key under such a key then draws over its span
       (:func:`draw_within_key`).
    """
    if keys is None:
        return
    links = _links(exposes, keys)
    rounds = len(links) + 1  # one relationship settles per round at least, down any chain
    placed: Set[int] = set()
    for _ in range(rounds):
        if not _place(links, placed, floats=False):
            break
    for _ in range(rounds):
        if not _widen_once([link for link in links if id(link.parent) not in placed]):
            break
    for _ in range(rounds):
        if not _place(links, placed, floats=True):
            break


def _assumed(plan: ColumnPlan) -> bool:
    return plan.key_span or plan.placeholder


def _place(links: Sequence[_Link], placed: Set[int], *, floats: bool) -> bool:
    """Put assumed columns inside the spans of parents that stand; whether any was put.

    A parent stands when its span is published or was placed already. With
    *floats*, a floating key's settled span stands too, and only placeholders are
    placed under it: a floating key under another one was widened up instead.
    """
    moved = False
    for link in links:
        child, parent = link.child, link.parent
        if id(child) in placed or not _assumed(child) or (floats and not child.placeholder):
            continue
        if _assumed(parent) and id(parent) not in placed and not (floats and parent.key_span):
            continue
        if draw_within_key(child, parent, link.kind, link.child_rows):
            placed.add(id(child))
            moved = True
    return moved


def _links(exposes: Sequence[AuthoredExpose], keys: ContractKeys) -> List[_Link]:
    """Every one-column relationship between two planned exposes, as plans."""
    planned = {expose.expose_id: expose for expose in exposes}
    columns = {expose.expose_id: {plan.name: plan for plan in expose.columns} for expose in exposes}
    links: List[_Link] = []
    for expose in exposes:
        for relationship in keys.expose(expose.expose_id).relationships:
            if len(relationship.from_columns) != 1 or relationship.target not in planned:
                continue
            child = columns[expose.expose_id].get(relationship.from_columns[0])
            parent = columns[relationship.target].get(relationship.to_columns[0])
            if child is not None and parent is not None and child is not parent:
                target = planned[relationship.target]
                link = _Link(
                    child,
                    expose.kinds[child.name],
                    expose.rows,
                    parent,
                    target.kinds[parent.name],
                    target.rows,
                )
                if link.numeric:  # a text column's levels are not a span
                    links.append(link)
    return links


def _widen_once(links: Sequence[_Link]) -> bool:
    """Widen every floating key once to the columns that reference it; whether any moved."""
    referenced: Dict[int, Tuple[ColumnPlan, LogicalKind, int, List[Decimal]]] = {}
    for link in links:
        if link.parent.key_span:
            fresh: List[Decimal] = []
            entry = referenced.setdefault(
                id(link.parent), (link.parent, link.parent_kind, link.parent_rows, fresh)
            )
            entry[3].extend(declared_span(link.child))
    moved = False
    for plan, kind, rows, values in referenced.values():
        before = list(plan.declaration.get("bins") or ())
        # An integer key never takes a bound its rows could not carry (FLX-1003).
        bound = JCS_MAX_SAFE_INTEGER if kind is LogicalKind.INT else None
        widen_key_span(plan, values, rows, bound=bound)
        moved = moved or list(plan.declaration.get("bins") or ()) != before
    return moved


def _finish_expose(authored: AuthoredExpose) -> None:
    """Choose the expose's properties, and gather its assumptions, once its keys are laid out."""
    authored.properties = _choose_properties(authored)
    if not authored.properties:
        for plan in authored.columns:
            fallback = assumed_check(plan, authored.kinds[plan.name])
            if fallback is not None:
                authored.properties = [fallback]
                break
    if not authored.properties:
        authored.skipped = (
            "no column has a range or levels a property could read, so the fidelity block "
            "could carry no property (FLX-5034); declare one by hand"
        )
    authored.assumptions.extend(
        (plan.name, sentence) for plan in authored.columns for sentence in plan.assumptions
    )


def _choose_properties(authored: AuthoredExpose) -> List[PropertyPlan]:
    """The properties the block carries: every one the profile can score, else the best other."""
    candidates = [prop for column in authored.columns for prop in column.properties]
    scorable = [prop for prop in candidates if prop.scorable]
    if scorable:
        return scorable
    return candidates[:1]


# ---------------------------------------------------------------------------
# The documents
# ---------------------------------------------------------------------------


def _time_bounds(
    exposes: Sequence[AuthoredExpose],
) -> Tuple[Tuple[str, str], Optional[Tuple[str, str]]]:
    """The run window: the span of every published date or timestamp range, or an assumed year."""
    spans = [
        column.time_span
        for expose in exposes
        for column in expose.columns
        if column.time_span is not None
    ]
    if spans:
        start = min(span[0] for span in spans)
        end = max(span[1] for span in spans)
        if end <= start:
            end = start + 1
        return (format_rfc3339_ms(start), format_rfc3339_ms(end)), None
    note = (
        "No date or timestamp column publishes its range, so the run covers "
        f"{ASSUMED_WINDOW[0][:10]} to {ASSUMED_WINDOW[1][:10]}. Set spec.timeBounds to the "
        "span your data covers."
    )
    return ASSUMED_WINDOW, ("time window", note)


def _owner(contract: Mapping[str, Any]) -> Tuple[str, Optional[Tuple[str, str]]]:
    metadata = contract.get("metadata")
    owner = metadata.get("owner") if isinstance(metadata, Mapping) else None
    team = owner.get("team") if isinstance(owner, Mapping) else None
    if isinstance(team, str) and team.strip():
        return team, None
    return "unassigned", (
        "owner",
        "The contract names no owner team, so the documents name the team unassigned.",
    )


def _document(
    kind: str, doc_id: str, name: str, team: str, spec: Mapping[str, Any]
) -> Dict[str, Any]:
    return {
        "fluxVersion": FLUX_VERSION,
        "kind": kind,
        "id": doc_id,
        "name": name,
        "metadata": {"owner": {"team": team}},
        "spec": dict(spec),
    }


def _fidelity_block(authored: AuthoredExpose, encoding: Mapping[str, Any]) -> Dict[str, Any]:
    columns: Dict[str, Any] = {}
    for column in authored.columns:
        if column.declaration:
            columns[column.name] = column.declaration
    return {
        "encoding": {
            "fractionalBits": encoding.get("fractionalBits", 16),
            "rounding": encoding.get("rounding", "floor"),
        },
        "records": dict(RECORDS),
        "columns": columns,
        "properties": [prop.document for prop in authored.properties],
        "budget": dict(BUDGET),
        "failureAction": "withholdBundle",
    }


def _labels(authored: AuthoredExpose) -> List[Label]:
    """Every label of the expose, with tokens below the namespace's ``emits`` block."""
    base = ("emits", authored.expose_id)
    fidelity = base + ("fidelity",)
    assert authored.rows_basis is not None  # set for every expose that is written
    labels: List[Label] = [
        Label(base + ("rows",), authored.rows_basis),
        Label(
            fidelity + ("records",),
            assumed(
                "Bounds on the size of a committed state, wide on purpose so that any "
                "profiled table fits."
            ),
        ),
    ]
    for column in authored.columns:
        for label in column.labels:
            labels.append(Label(fidelity + ("columns", column.name) + label.tokens, label.basis))
    for position, prop in enumerate(authored.properties):
        for label in prop.labels:
            labels.append(
                Label(fidelity + ("properties", str(position)) + label.tokens, label.basis)
            )
    return labels


def _register(
    labels: Sequence[Tuple[str, Tuple[str, ...], Any]],
    documents: Mapping[str, Mapping[str, Any]],
    simulation_id: str,
) -> List[Dict[str, Any]]:
    """Register entries for the labels that reach a number the register counts, in order.

    A label on text (a timestamp bound, say) names no counted number and would be
    refused, so it is left out: what it says is in the command's report instead.
    """
    counted = declared_numbers(
        documents, simulation_id=simulation_id, namespace=EXTENSION_NAMESPACE
    )
    # Every place a counted number lies under: one set, so a label is looked up
    # once rather than compared with every number of a wide bundle.
    under = {
        (number.document, number.tokens[:depth])
        for number in counted
        for depth in range(len(number.tokens) + 1)
    }
    entries: List[Dict[str, Any]] = []
    for document_id, tokens, basis in labels:
        if (document_id, tokens) not in under:
            continue
        entries.append(
            {"document": document_id, "pointer": render_pointer(tokens), **basis.entry_fields()}
        )
    return entries


def _dump(document: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True, width=100)


def _as_read(document: Mapping[str, Any]) -> Mapping[str, Any]:
    """*document* as the engine reads it back from the file: no float, every number exact."""
    read = yaml.load(
        _dump(document), Loader=ExactLoader
    )  # noqa: S506 - ExactLoader is a SafeLoader
    assert isinstance(read, dict)
    return read


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def _cite(ledger: Ledger, basis: Optional[Basis]) -> None:
    if basis is not None and basis.statistic is not None:
        ledger.use(basis.statistic[0], "a number of the bundle rests on it")


def _ledger(
    profile: Mapping[str, Any],
    exposes: Sequence[AuthoredExpose],
    contract_ids: Set[str],
    extra: Tuple[List[_Note], List[_Note]] = ([], []),
) -> Ledger:
    """Every statistic of *profile*, marked by what the written *exposes* rest on and read.

    *extra* holds what the bundle made of statistics no column plan carries (a
    relationship's children per parent): the used ones, and the unused ones with
    their reasons.
    """
    ledger = Ledger(profile)
    for tokens, how in extra[0]:
        ledger.use(tokens, how)
    tables = profile.get("tables")
    for name in tables if isinstance(tables, Mapping) else ():
        if str(name) not in contract_ids:
            ledger.unused_under(
                ("tables", str(name)), "the contract declares no expose of this name"
            )
    for expose in exposes:
        if expose.skipped is not None:
            ledger.unused_under(
                ("tables", expose.expose_id), f"the expose was not written: {expose.skipped}"
            )
            continue
        # What a rule says it read comes first: it is more specific than a citation.
        for column in expose.columns:
            for tokens, how in column.reads:
                ledger.use(tokens, how)
        for prop in expose.properties:
            for tokens, how in prop.reads:
                ledger.use(tokens, how)
        _cite(ledger, expose.rows_basis)
        for column in expose.columns:
            for label in column.labels:
                _cite(ledger, label.basis)
            for tokens, why in column.passed:
                ledger.unused(tokens, why)
        for prop in expose.properties:
            for label in prop.labels:
                _cite(ledger, label.basis)
    for tokens, why in extra[1]:
        ledger.unused(tokens, why)
    return ledger


#: A pointer into the profile, with how it was used or why it was not.
_Note = Tuple[Tuple[str, ...], str]


def _spread_relationships(
    exposes: Sequence[AuthoredExpose],
    keys: Optional[ContractKeys],
    profile: Mapping[str, Any],
    *,
    minimum_cell: int,
    fractional_bits: int,
) -> Tuple[List[_Note], List[_Note]]:
    """Lay each relationship's children per parent out as its foreign key's spread (ADR 0014).

    A profile ``relationships`` entry is read when the contract declares the same
    one-column relationship between two exposes the bundle writes
    (:func:`flux_engine.author.children.spread_children`). Returns what was used
    and, with the reasons, what was not.
    """
    used: List[_Note] = []
    unused: List[_Note] = []
    found = profile.get("relationships")
    found_tables = profile.get("tables")
    tables: Mapping[str, Any] = found_tables if isinstance(found_tables, Mapping) else {}
    planned = {expose.expose_id: expose for expose in exposes if expose.skipped is None}
    for index, entry in enumerate(found if isinstance(found, list) else ()):
        if not isinstance(entry, Mapping):
            continue
        tokens = ("relationships", str(index))
        statistics = entry.get("statistics")
        every = [
            tokens + ("statistics", str(position))
            for position, _ in enumerate(statistics if isinstance(statistics, list) else ())
        ]
        found_parent, found_child = entry.get("parent"), entry.get("child")
        parent: Mapping[str, Any] = found_parent if isinstance(found_parent, Mapping) else {}
        child: Mapping[str, Any] = found_child if isinstance(found_child, Mapping) else {}
        parent_table, child_table = str(parent.get("table")), str(child.get("table"))
        parent_columns = [nfc(str(name)) for name in parent.get("columns") or ()]
        child_columns = [nfc(str(name)) for name in child.get("columns") or ()]
        why: Optional[str] = None
        if len(parent_columns) != 1 or len(child_columns) != 1:
            why = "a key of several columns: its children per parent are not laid out"
        elif child_table not in planned or parent_table not in planned:
            why = "the bundle writes no generator emit of one of its two tables"
        elif keys is None or not any(
            relationship.target == parent_table
            and relationship.from_columns == tuple(child_columns)
            and relationship.to_columns == tuple(parent_columns)
            for relationship in _relationships_of(keys, child_table)
        ):
            why = "the contract declares no such relationship between the two exposes"
        if why is not None:
            unused += [(pointer, why) for pointer in every]
            continue
        child_expose, parent_expose = planned[child_table], planned[parent_table]
        child_plan = next(plan for plan in child_expose.columns if plan.name == child_columns[0])
        parent_plan = next(plan for plan in parent_expose.columns if plan.name == parent_columns[0])
        child_entries = (
            tables.get(child_table, {}).get("columns")
            if isinstance(tables.get(child_table), Mapping)
            else None
        )
        spread = spread_children(
            entry,
            tokens,
            child_plan,
            child_expose.kinds[child_plan.name],
            child_expose.rows,
            child_entries.get(child_plan.name) if isinstance(child_entries, Mapping) else None,
            ("tables", child_table, "columns", child_plan.name),
            parent_plan,
            parent_expose.kinds[parent_plan.name],
            minimum_cell=minimum_cell,
            fractional_bits=fractional_bits,
        )
        used += spread.used
        unused += spread.unused
    return used, unused


def _relationships_of(keys: ContractKeys, expose_id: str) -> Sequence[Any]:
    try:
        return keys.expose(expose_id).relationships
    except (KeyError, LookupError):
        return ()


def _unused_block(ledger: Ledger) -> List[Dict[str, Any]]:
    """The register's ``unused`` list: each unused statistic with its definition and reason."""
    block: List[Dict[str, Any]] = []
    for entry in ledger.unused_entries():
        statistic: Dict[str, Any] = {"pointer": entry.pointer}
        if entry.definition is not None:
            statistic["definition"] = entry.definition
        block.append({"statistic": statistic, "reason": entry.note})
    return block


def _gaps(
    exposes: Sequence[AuthoredExpose], window_note: Optional[Tuple[str, str]]
) -> Tuple[Gap, ...]:
    """The assumptions that stand in for what the profile did not publish, by position.

    Every assumption a written expose states is one, but for the choices a
    column names (how to meet what the profile publishes, or where the contract
    overrules it). Each is placed at the emit's ``rows`` or at the column's
    block in the Simulation, so one ``--allow`` can accept a column's gaps.
    """
    namespace = ("extensions", EXTENSION_NAMESPACE, "emits")
    found: List[Gap] = []
    for expose in exposes:
        if expose.skipped is not None:
            continue
        base = namespace + (expose.expose_id,)
        choices = {plan.name: set(plan.choices) for plan in expose.columns}
        for subject, sentence in expose.assumptions:
            if subject == "rows" and sentence == expose.rows_note:
                found.append(Gap(render_pointer(base + ("rows",)), sentence))
            elif sentence not in choices.get(subject, set()):
                found.append(Gap(render_pointer(base + ("fidelity", "columns", subject)), sentence))
    if window_note is not None:
        found.append(Gap(render_pointer(("spec", "timeBounds")), window_note[1]))
    return tuple(found)


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def author_bundle(contract_path: Path, profile_path: Path) -> AuthoredBundle:
    """Read *contract_path* and *profile_path*, and return the checked files of a bundle.

    Nothing is written to the caller's disk here; the bundle is checked in a
    scratch directory. Raises :class:`AuthorError` when an input is unusable or
    no expose of the contract can be generated, and :class:`FluxError` from the
    readers it shares with the engine (an unreadable profile is ``FLX-3109``, a
    document that is not a profile ``FLX-5036``, a contract past the read's
    bounds ``FLX-3105``, and an integer in it too long to read ``FLX-1003``).
    """
    contract_path = Path(contract_path)
    contract, contract_bytes = _read_contract(contract_path)
    pinned = load_profile(Path(profile_path))
    profile = pinned.document
    found = profile.get("tables")
    tables: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    minimum_cell = _minimum_cell(profile)
    encoding = profile.get("encoding")
    bits = encoding.get("fractionalBits") if isinstance(encoding, Mapping) else None
    fractional_bits = bits if isinstance(bits, int) and not isinstance(bits, bool) else 16
    product_ref = str(contract["id"])
    try:
        keys = compile_keys(contract)
    except FluxError as error:
        raise AuthorError(f"the contract's keys do not resolve: {error}") from error

    exposes: List[AuthoredExpose] = []
    for expose_index, expose in enumerate(contract["exposes"]):
        if not isinstance(expose, Mapping) or not isinstance(expose.get("exposeId"), str):
            continue
        expose_id = expose["exposeId"]
        table = tables.get(expose_id)
        if not isinstance(table, Mapping):
            exposes.append(
                AuthoredExpose(expose_id=expose_id, skipped="the profile has no table for it")
            )
            continue
        try:
            exposes.append(
                _plan_expose(
                    contract,
                    expose_index,
                    expose,
                    table,
                    keys.expose(expose_id) if keys is not None else None,
                    minimum_cell=minimum_cell,
                    fractional_bits=fractional_bits,
                )
            )
        except FluxError as error:
            exposes.append(AuthoredExpose(expose_id=expose_id, skipped=printable(str(error))))
        except (ArithmeticError, ValueError) as error:
            exposes.append(AuthoredExpose(expose_id=expose_id, skipped=_beyond_note(error)))
    try:
        _span_keys([expose for expose in exposes if expose.skipped is None], keys)
        children = _spread_relationships(
            exposes,
            keys,
            profile,
            minimum_cell=minimum_cell,
            fractional_bits=fractional_bits,
        )
    except (ArithmeticError, ValueError) as error:
        raise AuthorError(_beyond_note(error)) from error
    for expose in exposes:
        if expose.skipped is None:
            try:
                _finish_expose(expose)
            except (ArithmeticError, ValueError) as error:
                expose.skipped = _beyond_note(error)
    authored = [expose for expose in exposes if expose.skipped is None]
    if not authored:
        reasons = "; ".join(
            f"{printable(expose.expose_id)}: {expose.skipped}" for expose in exposes
        )
        in_profile = ", ".join(sorted(printable(str(name)) for name in tables)) or "none"
        raise AuthorError(
            f"no expose of {printable(contract_path.name)} can be written from this profile "
            f"({reasons}). "
            f"The profile describes: {in_profile}"
        )

    team, owner_note = _owner(contract)
    window, window_note = _time_bounds(authored)
    contract_digest = digest_of_document(contract, subject=f"contractDigest:{product_ref}").value
    simulation_id = f"sim-{product_ref}"
    world_id = f"world-{product_ref}"
    persona_id = f"persona-{product_ref}"
    title = str(contract.get("name") or product_ref)

    world = _document(
        "World",
        world_id,
        f"World for {title}",
        team,
        {
            "seed": SEED,
            "vertical": "telco",
            "population": {"size": 1, "lifecycleMix": {"active": 1}},
        },
    )
    persona = _document(
        "Persona", persona_id, f"Persona for {title}", team, {"traits": {"digitalFluency": 0.5}}
    )
    namespace: Dict[str, Any] = {
        "profile": {"digest": pinned.digest.value},
        "emits": {
            expose.expose_id: {
                "rows": expose.rows,
                "fidelity": _fidelity_block(expose, profile.get("encoding") or {}),
            }
            for expose in authored
        },
    }
    simulation = _document(
        "Simulation",
        simulation_id,
        f"Synthetic {title}",
        team,
        {
            "worldRef": world_id,
            "fidelity": "deterministic",
            "seed": SEED,
            "timeBounds": {"start": window[0], "end": window[1]},
            "personaRefs": [persona_id],
            "emits": [
                {
                    "productRef": product_ref,
                    "exposeId": expose.expose_id,
                    "fluidVersion": str(contract.get("fluidVersion", "")),
                    "provenance": {"contractDigest": contract_digest},
                }
                for expose in authored
            ],
        },
    )
    simulation["extensions"] = {EXTENSION_NAMESPACE: namespace}
    contract_ids = {
        str(expose.get("exposeId")) for expose in contract["exposes"] if isinstance(expose, Mapping)
    }
    ledger = _ledger(profile, exposes, contract_ids, children)

    template = (
        "fluxsim init writes the least World a run needs: the generator emits read none of "
        "its numbers, so they change no row."
    )
    labels: List[Tuple[str, Tuple[str, ...], Any]] = [
        (world_id, ("spec",), default(template)),
        (
            persona_id,
            ("spec",),
            default(
                "fluxsim init writes the least Persona a run needs: the generator emits read "
                "none of its numbers, so they change no row."
            ),
        ),
    ]
    namespace_tokens = ("extensions", EXTENSION_NAMESPACE)
    for expose in authored:
        for label in _labels(expose):
            labels.append((simulation_id, namespace_tokens + label.tokens, label.basis))
    # The register counts numbers as the engine reads the files back (a YAML
    # float is an exact decimal there), so it is built from that reading.
    documents = {
        world_id: _as_read(world),
        persona_id: _as_read(persona),
        simulation_id: _as_read(simulation),
    }
    namespace["parameters"] = _register(labels, documents, simulation_id)
    namespace[UNUSED_KEY] = _unused_block(ledger)
    documents[simulation_id] = _as_read(simulation)

    # A file name is any text: printable() keeps a newline in one from ending the comment.
    header = _HEADER.format(
        contract=printable(contract_path.name), profile=printable(Path(profile_path).name)
    )
    contract_name = _contract_file_name(contract_path)
    files = (
        StarterFile(contract_name, "the FLUID contract, copied unchanged", contract_bytes),
        StarterFile(
            "world.flux.yml",
            "the least World a run needs: the generator emits read none of its numbers",
            (header + _dump(world)).encode("utf-8"),
        ),
        StarterFile(
            "persona.flux.yml",
            "the least Persona a run needs, for the same reason",
            (header + _dump(persona)).encode("utf-8"),
        ),
        StarterFile(
            "simulation.flux.yml",
            "the run: one generator emit per profiled expose, the profile pin and the register",
            (header + _dump(simulation)).encode("utf-8"),
        ),
    )
    _check(files, profile)
    notes = tuple(note for note in (window_note, owner_note) if note)
    counts = _counts(namespace["parameters"], namespace[UNUSED_KEY], documents, simulation_id)
    return AuthoredBundle(
        files=files,
        exposes=tuple(exposes),
        counts=counts,
        notes=notes,
        profile_digest=pinned.digest.value,
        simulation_id=simulation_id,
        ledger=ledger,
        gaps=_gaps(exposes, window_note),
    )


def _counts(
    entries: Sequence[Mapping[str, Any]],
    unused: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    simulation_id: str,
) -> Dict[str, int]:
    """How many counted numbers each source decides, as the register will count them."""
    register = build_parameter_register(
        simulation_id=simulation_id,
        documents=documents,
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block(entries, pinned=True, where="parameters", code=FLX_3105),
        profile_digest=None,
        code=FLX_3105,
        unused=read_unused_block(unused, pinned=True, where=UNUSED_KEY, code=FLX_3105),
    )
    return register.counts()


def _beyond_note(error: Exception) -> str:
    """What is said when a number of the inputs is too long or too fine to declare."""
    return (
        "a number of the contract or the profile is beyond what a bundle can declare "
        f"({type(error).__name__})"
    )


def _validated(directory: Path) -> Any:
    """``validate`` of the scratch bundle; a rule no refusal code covers is not a number too large."""
    from flux_engine.stages.validate import validate

    try:
        return validate(directory)
    except ValueError as error:
        raise AuthorError(
            f"the bundle written from these inputs does not validate: {error}"
        ) from error


def _planned_rules(resolved: Any, strict: bool, simulation_id: str) -> Any:
    """The contract's and the Simulation's rules, planned as `fluxsim run` plans them."""
    from flux_engine.stages.pipeline import plan_emit_rules

    try:
        return plan_emit_rules(resolved, strict=strict, where=simulation_id)
    except ValueError as error:  # a rule is not what its form allows
        raise AuthorError(
            f"the bundle written from these inputs would be refused by `fluxsim run`: {error}"
        ) from error


def _check(files: Sequence[StarterFile], profile: Mapping[str, Any]) -> None:
    """Check the bundle in a scratch directory as ``validate`` and ``plan`` would, before any write.

    ``validate`` first. Then what ``plan`` refuses from the documents alone,
    before a row is drawn: each fidelity block compiled as ``verify`` compiles
    it (an encoding error not smaller than its tolerance, ``FLX-5002``), each
    generator plan built (a family without its parameters, ``FLX-3402``), each
    contract's plans related (a key that could repeat, or a foreign key into a
    table the bundle does not generate, ``FLX-3403``), and every citation of
    the register read in *profile*, the document the pin was taken of
    (``FLX-3113``).
    A bundle that fails any of them is not written. What only drawing the rows
    would show, an integer beyond what a row carries, the column rules keep
    out of the bundle (:mod:`flux_engine.author.columns`).
    """
    from flux_engine.generator import build_plan
    from flux_engine.generator.relations import related_columns
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.parameters import check_profile_citations
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits, strict_coverage_declared
    from flux_engine.stages.pipeline import (
        check_flux_side_rule_ids,
        compile_declared_fidelity,
        parameter_register_of,
    )

    with tempfile.TemporaryDirectory(prefix="fluxsim-init-") as scratch:
        directory = Path(scratch)
        for entry in files:
            (directory / entry.name).write_bytes(entry.data)
        result = _validated(directory)
        if result.exit_code != 0:
            problems = result.artifacts.get("problems") or [result.stderr]
            raise AuthorError(
                "the bundle written from these inputs does not validate: "
                + "; ".join(str(problem) for problem in problems)
            )
        try:
            loaded = load_bundle(directory)
            for simulation_id in loaded.simulation_ids():
                cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
                spec = cast.simulation.get("spec", {})
                planned = []
                strict = strict_coverage_declared(cast.simulation, where=simulation_id)
                emits = resolve_all_emits(loaded, simulation_id=simulation_id)
                check_flux_side_rule_ids(emits, where=simulation_id)
                for resolved in emits:
                    compile_declared_fidelity(resolved)
                    rules = _planned_rules(resolved, strict, simulation_id)
                    plan = build_plan(
                        resolved.compile(),
                        sim_id=simulation_id,
                        seam_digest="sha256:" + "0" * 64,
                        rows=resolved.declared_row_count or 1,
                        fidelity=resolved.fidelity,
                        time_bounds=spec.get("timeBounds"),
                        related=related_columns(resolved.keys),
                        rules=rules,
                    )
                    planned.append((resolved, plan))
                _relate(planned)
                register = parameter_register_of(loaded, cast)
                if register is not None:
                    check_profile_citations(register, profile, where=simulation_id)
        except FluxError as error:
            raise AuthorError(
                f"the bundle written from these inputs would be refused by `fluxsim run`: {error}"
            ) from error
        except (ArithmeticError, ValueError) as error:
            raise AuthorError(_beyond_note(error)) from error


def _relate(planned: Sequence[Tuple[Any, Any]]) -> None:
    """Relate each contract's generator plans as ``plan`` relates them, refusing with ``FLX-3403``.

    The emits are grouped by the contract file they resolve to, and a contract
    that declares no key is passed over, as the pipeline's own relate step does
    (``flux_engine.stages.pipeline``). Each expose has one emit here, so no
    expose is planned twice.
    """
    from flux_engine.generator.relations import relate_plans

    contracts: Dict[str, Tuple[Mapping[str, Any], Dict[str, Any]]] = {}
    for resolved, plan in planned:
        if resolved.keys is None:
            continue
        _, plans = contracts.setdefault(
            str(resolved.contract_path), (resolved.contract_document, {})
        )
        plans[nfc(plan.expose.expose_id)] = plan
    for document, plans in contracts.values():
        keys = compile_keys(document)
        if keys is not None and keys.declares_keys:
            relate_plans(plans, keys)
