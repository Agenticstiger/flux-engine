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
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_DOWN, Context, Decimal
from fractions import Fraction
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import yaml

from flux_engine.author.basis import Basis, Label, assumed, default, from_profile
from flux_engine.author.catalogue import (
    catalogue_foreign_keys,
    link_givens,
    reference_table,
)
from flux_engine.author.children import count_children, declare_counted, spread_children
from flux_engine.author.children_given import (
    Given,
    GivenParent,
    given_children,
    with_summed_groups,
)
from flux_engine.author.cliques import draw_cliques
from flux_engine.author.columns import (
    MOST_PLACES,
    ColumnContext,
    ColumnPlan,
    PerEntity,
    PropertyPlan,
    UnspellableOneValue,
    assumed_check,
    bound_to_window,
    copy_column,
    declared_span,
    determine_column,
    draw_in_window,
    draw_within_key,
    drawn_span,
    few_int_levels,
    per_entity,
    plan_column,
    recheck_key,
    share_texts,
    widen_key_span,
)
from flux_engine.author.copula import CopulaPlan, declare_copula
from flux_engine.author.dates import (
    DATE_KINDS,
    DatedExpose,
    declare_dates,
    statistic_tokens,
    window_end_ms,
    window_start_ms,
)
from flux_engine.author.declared_history import (
    DeclaredHistory,
    DeclaredHistoryError,
    check_entries,
    declared_spans,
    parent_relationships,
    projected_emits,
)
from flux_engine.author.derived import (
    bounds,
    contract_aggregates,
    contract_copies,
    contract_dependencies,
    contract_dependents,
    contract_derives,
    contract_guarded,
    numeric,
)
from flux_engine.author.exact import EXACT_HOW, allot_small_tables
from flux_engine.author.given import choose_givens
from flux_engine.author.history import (
    HistoryReads,
    HistorySource,
    Published,
    declare_date_ranges,
    decline_history,
    restate_assumptions,
    write_history,
)
from flux_engine.author.history import _pointers as declared_pointers
from flux_engine.author.ledger import Ledger
from flux_engine.author.noisy import cite_noisy, point_estimates
from flux_engine.author.projection import (
    Projected,
    ProjectionSource,
    follow_history,
    history_target,
    project,
    release_counts,
    span_primary_key,
)
from flux_engine.author.ranges import column_range
from flux_engine.author.replace import replacements_for
from flux_engine.author.window import WINDOW_END_LIMIT_MS, declared_window, instant_ms
from flux_engine.core.canonical import JCS_MAX_SAFE_INTEGER, nfc
from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import FLX_3402
from flux_engine.governance.privacy import effective_floors, sensitivities_of
from flux_engine.seam.asof import ASOF_KEY
from flux_engine.seam.contract import LogicalKind, compile_expose
from flux_engine.seam.copies import COPIES_KEY, declares_copies
from flux_engine.seam.copula import COPULA_KEY
from flux_engine.seam.counts import COUNTS_KEY, count_entries_of
from flux_engine.seam.coverage import printable
from flux_engine.seam.dates import LIFETIMES_KEY, RATES_KEY, WITHIN_KEY
from flux_engine.seam.history import HISTORY_KEY, history_entries_of
from flux_engine.seam.keys import ContractKeys, ExposeKeys, compile_keys, window_ends
from flux_engine.seam.mean_reach import check_restated_means
from flux_engine.seam.overlay import OVERLAY_KEY, Overlay, overlaid_profile
from flux_engine.seam.parameters import (
    UNUSED_KEY,
    build_parameter_register,
    changed_by_overlay,
    cited_definition,
    counted_positions,
    declared_numbers,
    parse_pointer,
    read_parameters_block,
    read_unused_block,
    render_pointer,
)
from flux_engine.seam.profile import FLX_3125, load_profile
from flux_engine.seam.profile_request import column_domains
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY
from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105, ExactLoader, read_bundle_file
from flux_engine.starter import StarterFile
from flux_stats.cliques import CliqueEntry
from flux_stats.column_metrics import COUNT
from flux_stats.exact_yaml import load_documents
from flux_stats.rank_correlation import KENDALL_TAU_ID, SPEARMAN_RHO_ID
from flux_stats.request import ProfileRequest

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
    "# Written by `fluxsim init` from the contract {contract} and its profile.\n"
    "# Every number below has its source in the parameter register\n"
    "# (extensions.com.agentics.flux/1.parameters): the profile, the contract, an\n"
    "# assumption with its reason, or a default. Replace the assumptions you can.\n"
)

#: Added to the header of a bundle written with an overlay (ADR 0030 section 7).
_OVERLAY_HEADER = (
    "# The overlay {overlay} patches the profile: the numbers it changed cite it in the\n"
    "# register, and are a scenario the maintainers wrote, not a measurement.\n"
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
    #: The properties the profile has no statistic for (``FLX-5037``), left out of a block that
    #: carries no other property.
    left_out: List[PropertyPlan] = field(default_factory=list)
    #: The history a projected emit takes the values of some columns from, which are not scored.
    carries: Optional[str] = None
    skipped: Optional[str] = None
    #: The refusal to give when something the bundle writes needs this expose, which is left out
    #: because a column publishes one date or instant the engine cannot yet write on every row.
    needed_refusal: Optional[str] = None
    #: Each planned column's logical type, for the one assumed check a table may need.
    kinds: Dict[str, LogicalKind] = field(default_factory=dict)
    #: The columns a contract ``derive`` sets: column -> (invariant index, expr text).
    derives: Dict[str, Tuple[int, str]] = field(default_factory=dict)
    #: The columns a ``sharedColumns`` or ``sharedAttribute`` copies (kernel 0.2.0): column ->
    #: (the declaration's place in the contract, what the bundle says of it, its reason).
    copies: Dict[str, Tuple[Tuple[str, ...], str, str]] = field(default_factory=dict)
    #: The columns a contract ``dependency`` sets: one value per value of what they follow.
    dependents: Set[str] = field(default_factory=set)
    #: The columns a contract ``derive`` with ``aggregate`` sets from the rows of a child expose.
    aggregated: Set[str] = field(default_factory=set)
    #: The columns a contract ``derive`` with a ``when`` sets, an aggregate or not.
    guarded: Set[str] = field(default_factory=set)
    #: The dependents drawn from a statistic counted per entity, not per row.
    per_entity: Set[str] = field(default_factory=set)
    #: What each column was declared from, kept for the rules that run once keys are laid out.
    contexts: Dict[str, ColumnContext] = field(default_factory=dict)
    #: The expose's place in the contract's ``exposes``.
    index: int = 0
    #: The profile publishes the expose whole under ``references``: it is written as
    #: ``reference: true``, with no columns planned and no fidelity block (ADR 0023 section 5).
    reference: bool = False
    #: Why the profile's ``references`` entry for the expose is not used, when the expose is
    #: generated from its other statistics instead: a column's sensitivity sets a minimum grade.
    declined_reference: Optional[str] = None
    #: Why the published rowCount is not read, for an emit a ``snapshots``, ``events`` or ``periods``
    #: entry names: its rows follow from the history, so no ``rows`` is written.
    projected: Optional[str] = None
    #: The columns of linked rows a property of this expose is grouped by, under their
    #: ``<relationship>.<column>`` names: the levels the check counts the groups over.
    linked: Dict[str, ColumnPlan] = field(default_factory=dict)
    #: The columns a clique of the profile draws, which a copula leaves to the cliques.
    drawn_by_cliques: Set[str] = field(default_factory=set)
    #: What the table's published rank correlations came to: its copula, and what was said of it.
    copula: Optional[CopulaPlan] = None


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
    #: The overlay the bundle was written with (ADR 0030 section 7), or ``None``.
    overlay: Optional[Overlay] = None
    #: The profile request for exactly the statistics that would replace the bundle's assumptions
    #: (ADR 0030 section 8), or ``None`` when no assumption has one.
    assumptions_request: Optional[ProfileRequest] = None
    #: What ``--consumers`` made of the consumers' requirements, or ``None`` when none were given
    #: (:class:`flux_engine.author.consumers.ConsumerReport`).
    consumers: Optional[Any] = None
    #: The name of the ``--history`` file the bundle's histories and projections come from, or ``None``.
    history_file: Optional[str] = None


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
    if any(not tokens for _, tokens in pointers):
        raise ValueError(
            "the empty pointer names the whole profile and the whole Simulation, so it would "
            "accept every finding; name the part you accept"
        )
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


def _rounding_base(profile: Mapping[str, Any]) -> int:
    """The largest rounding base any disclosure rule names: a published count is off by less."""
    rules = profile.get("disclosureRules")
    bases = [
        rule["roundingBase"]
        for rule in (rules.values() if isinstance(rules, Mapping) else ())
        if isinstance(rule, Mapping)
        and isinstance(rule.get("roundingBase"), int)
        and not isinstance(rule.get("roundingBase"), bool)
        and rule["roundingBase"] > 0
    ]
    return max(bases) if bases else 1


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
    expose_id: str,
    table: Mapping[str, Any],
    minimum_cell: int,
    noisy: Optional[Mapping[Tuple[str, ...], Any]] = None,
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
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
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
    estimates = [
        (note.records, tokens, note)
        for tokens, note in sorted((noisy or {}).items())
        if tokens[:2] == ("tables", expose_id) and note.records
    ]
    if estimates:
        value, tokens, note = max(estimates, key=lambda item: item[0])
        return (
            value,
            from_profile(
                tokens
                + (("noisyCounts",) if tokens[2] == "cliques" else ("groups", "0", "noisyCounts")),
                note.definition,
            ),
            f"The profile has no rowCount: {value} rows, the records a noisy "
            + (
                f"clique ({printable(str(tokens[3]))}) estimates"
                if tokens[2] == "cliques"
                else f"statistic estimates (of {printable(tokens[3])})"
            )
            + ", which is itself noisy.",
        )
    rows = max(1, minimum_cell // 2)
    reason = (
        f"The profile publishes no row count and no column count: {rows} rows are assumed, half "
        f"the minimum cell ({minimum_cell})."
    )
    return rows, assumed(reason), reason


def _noise_clear(
    noisy: Optional[Mapping[Tuple[str, ...], Any]], expose_id: str, column: str
) -> Tuple[Tuple[int, Tuple[bool, ...]], ...]:
    """The cells of each noisy histogram of *column* that stand clear of the noise, by statistic."""
    prefix = ("tables", expose_id, "columns", column, "statistics")
    found = [
        (int(tokens[-1]), note.clear)
        for tokens, note in (noisy or {}).items()
        if tokens[:-1] == prefix and getattr(note, "clear", None) is not None
    ]
    return tuple(sorted(found))


def _plan_expose(
    contract: Mapping[str, Any],
    expose_index: int,
    expose: Mapping[str, Any],
    table: Mapping[str, Any],
    expose_keys: Optional[ExposeKeys],
    *,
    minimum_cell: int,
    fractional_bits: int,
    rounding_base: int = 1,
    noisy: Optional[Mapping[Tuple[str, ...], Any]] = None,
    cliques: Sequence[CliqueEntry] = (),
    asked: Optional[Mapping[str, Tuple[Tuple[str, Tuple[str, ...]], ...]]] = None,
) -> AuthoredExpose:
    """Declare every column of one expose; its properties are chosen once every key is laid out."""
    expose_id = str(expose.get("exposeId"))
    compiled = compile_expose(expose)
    sets = _key_sets(expose_keys)
    rows, rows_basis, rows_note = _emit_rows(expose_id, table, minimum_cell, noisy)
    profiled = table.get("columns") if isinstance(table.get("columns"), Mapping) else {}
    grouping = _grouping_columns(profiled if isinstance(profiled, Mapping) else {})
    follows = contract_dependencies(expose)
    copies = _copies_of(contract, expose_id, expose_index, expose, expose_keys)
    set_by_rules = set(contract_derives(expose)) | contract_dependents(expose) | set(copies)
    authored = AuthoredExpose(
        expose_id=expose_id, rows=rows, rows_basis=rows_basis, rows_note=rows_note
    )
    if rows_note:
        authored.assumptions.append(("rows", rows_note))
    schema = expose["contract"]["schema"]
    contexts: Dict[str, ColumnContext] = {}
    for schema_index, column in enumerate(compiled.columns):
        reason = _unsupported_reason(column)
        if reason is not None:
            authored.skipped = reason
            return authored
        position = _schema_position(schema, column.name, schema_index)
        context = ColumnContext(
            column=column,
            expose_index=expose_index,
            schema_index=position,
            product_ref=str(contract["id"]),
            profile_tokens=("tables", expose_id, "columns", column.name),
            entry=profiled.get(column.name) if isinstance(profiled, Mapping) else None,
            rows=rows,
            minimum_cell=minimum_cell,
            key=column.name in sets.one_column_keys,
            foreign=sets.foreign.get(column.name),
            keyed=column.name in sets.keyed,
            inner=column.name in sets.inner,
            nulls_follow=column.name in sets.follow,
            fractional_bits=fractional_bits,
            rounding_base=rounding_base,
            range_rule=column_range(schema[position]),
            asked_levels=_levels_asked(asked, column.name),
            noise_clear=_noise_clear(noisy, expose_id, column.name),
        )
        if _draws_int_levels(context, grouping, set_by_rules):
            context = replace(context, int_levels=True)
        context, swapped = _swap_for_per_entity(context, column.name, follows, sets.entity_keys)
        plan = plan_column(context)
        if swapped is not None:
            _read_per_entity(plan, context, follows[column.name], swapped)
        elif column.name in follows:
            _pass_over_per(
                plan,
                context,
                follows[column.name],
                frozenset(follows[column.name]) in sets.entity_keys,
            )
        if swapped is not None:
            authored.per_entity.add(plan.name)
        contexts[plan.name] = context
        authored.columns.append(plan)
        authored.kinds[plan.name] = column.logical_type.kind
    _settle_expose(
        authored,
        expose,
        copies,
        contexts,
        cliques,
        profiled,
        minimum_cell,
        rounding_base,
        expose_index,
    )
    return authored


def _schema_position(schema: Sequence[Any], name: str, default: int) -> int:
    """The index of the schema entry that names column *name*, or *default* when none does."""
    return next(
        (
            index
            for index, declared in enumerate(schema)
            if isinstance(declared, Mapping) and nfc(str(declared.get("name"))) == name
        ),
        default,
    )


def _asks_for_copies(contract: Mapping[str, Any]) -> bool:
    """Whether the Simulation written for *contract* declares ``copies: true`` (ADR 0022 section 4).

    Only a contract that declares a ``sharedColumns`` or a ``sharedAttribute``, and only where a
    new plan is kernel 0.2.0's, which alone copies them; any other contract's bundle is unchanged.
    """
    from flux_engine.core.kernels import new_plan_is_counter_based
    from flux_engine.seam.keys import declared_exposes

    return new_plan_is_counter_based() and any(
        declares_copies(expose) for expose in declared_exposes(contract).values()
    )


def _copies_of(
    contract: Mapping[str, Any],
    expose_id: str,
    expose_index: int,
    expose: Mapping[str, Any],
    expose_keys: Optional[ExposeKeys],
) -> Dict[str, Any]:
    """The columns of one expose a rule copies from a linked row, under kernel 0.2.0 only."""
    from flux_engine.core.kernels import new_plan_is_counter_based
    from flux_engine.generator.relations import related_columns

    if not new_plan_is_counter_based():
        return {}
    return contract_copies(
        contract,
        expose_id,
        expose_index,
        set(related_columns(expose_keys)),
        set(contract_derives(expose)) | contract_dependents(expose),
    )


def _draws_int_levels(context: ColumnContext, grouping: Set[str], set_by_rules: Set[str]) -> bool:
    """Whether an integer grouping column no key or rule sets is drawn from its few levels."""
    name = context.column.name
    return (
        context.column.logical_type.kind is LogicalKind.INT
        and name in grouping
        and name not in set_by_rules
        and not (context.key or context.keyed or context.foreign or context.inner)
        and few_int_levels(context.entry, context.range_rule) is not None
    )


class _KeySets(NamedTuple):
    """The columns of one expose its keys and relationships single out."""

    relationships: Sequence[Any]
    one_column_keys: Any
    keyed: Set[str]
    inner: Set[str]
    foreign: Dict[str, str]
    follow: Set[str]
    entity_keys: Set[FrozenSet[str]]


def _key_sets(expose_keys: Optional[ExposeKeys]) -> _KeySets:
    """Which columns are keys, inner-joined, foreign, null together, or an entity's key."""
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
    entity_keys = {frozenset(key) for key in (expose_keys.keys if expose_keys else ())}
    entity_keys |= {frozenset(relationship.from_columns) for relationship in relationships}
    return _KeySets(relationships, one_column_keys, keyed, inner, foreign, follow, entity_keys)


def _unsupported_reason(column: Any) -> Optional[str]:
    """Why the generator has no family for a column, or ``None`` when it has one."""
    if column.logical_type.kind.value in ("JSON", "BYTES"):
        return (
            f"column {printable(column.name)!r} is {column.logical_type.kind.value}, which "
            "the generator has no family for (FLX-3401)"
        )
    places = column.logical_type.scale
    if isinstance(places, int) and places > MOST_PLACES:
        return (
            f"column {printable(column.name)!r} has {places} decimal places, more than the "
            f"{MOST_PLACES} a bundle declares"
        )
    return None


def _swap_for_per_entity(
    context: ColumnContext,
    name: str,
    follows: Mapping[str, Sequence[str]],
    entity_keys: Set[FrozenSet[str]],
) -> Tuple[ColumnContext, Optional[PerEntity]]:
    """The context of a dependent column reading its statistic counted per entity, when it has one."""
    if name in follows and not (context.key or context.keyed or context.foreign):
        if frozenset(follows[name]) in entity_keys:
            swapped = per_entity(context.entry, follows[name])
            if swapped is not None:
                return replace(context, entry=swapped.entry), swapped
    return context, None


def _settle_expose(
    authored: AuthoredExpose,
    expose: Mapping[str, Any],
    copies: Mapping[str, Any],
    contexts: Dict[str, ColumnContext],
    cliques: Sequence[CliqueEntry],
    profiled: Any,
    minimum_cell: int,
    rounding_base: int,
    expose_index: int,
) -> None:
    """Record the rules and copies on the planned columns, then draw the cliques and choose givens."""
    from flux_engine.core.kernels import new_plan_is_counter_based

    expose_id = authored.expose_id
    authored.derives = {
        name: rule
        for name, rule in contract_derives(expose).items()
        if any(plan.name == name for plan in authored.columns)
    }
    authored.copies = {
        name: copied
        for name, copied in copies.items()
        if any(plan.name == name for plan in authored.columns)
    }
    authored.dependents = contract_dependents(expose)
    authored.aggregated = contract_aggregates(expose)
    authored.guarded = contract_guarded(expose)
    authored.contexts = contexts
    authored.index = expose_index
    # The noisy cliques decide how their columns are drawn; the Chow-Liu forest draws the rest.
    drawn = draw_cliques(
        authored.columns,
        authored.kinds,
        cliques,
        expose_id,
        order=[plan.name for plan in authored.columns],
        determined=set(authored.derives) | set(authored.copies),
        contexts=contexts,
        binned_parents=new_plan_is_counter_based(),
    )
    authored.drawn_by_cliques = set(drawn)
    choose_givens(
        authored.columns,
        authored.kinds,
        profiled if isinstance(profiled, Mapping) else {},
        ("tables", expose_id),
        minimum_cell=minimum_cell,
        determined=set(authored.derives) | set(authored.copies) | drawn,
        rounding_base=rounding_base,
    )


#: Why a column a dependency sets is drawn from its statistic per entity.
_PER_ENTITY_CHOICE = (
    "A dependency of the contract sets this column once for each value of {keys}, so it is "
    "drawn from the profile's statistic counted per {keys}, in place of the one that counts rows."
)


def _read_per_entity(
    plan: ColumnPlan, context: ColumnContext, keys: Sequence[str], swapped: PerEntity
) -> None:
    """Say that a dependent column was drawn from its per-entity statistic."""
    names = ", ".join(printable(name) for name in keys)
    sentence = _PER_ENTITY_CHOICE.format(keys=names)
    plan.choices.append(sentence)
    plan.assumptions.append(sentence)
    for index in swapped.superseded:
        plan.passed.append(
            (
                context.profile_tokens + ("statistics", str(index)),
                f"a dependency of the contract draws this column once per {names}, so its "
                "statistic counted per row is not the distribution of the draw",
            )
        )


def _pass_over_per(
    plan: ColumnPlan, context: ColumnContext, keys: Sequence[str], is_key: bool
) -> None:
    """Say why a per-entity statistic of a dependent column is not the one its draw follows."""
    names = ", ".join(printable(name) for name in keys)
    statistics = context.entry.get("statistics") if isinstance(context.entry, Mapping) else None
    for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
        if not isinstance(statistic, Mapping) or "per" not in statistic:
            continue
        if not is_key:
            why = (
                f"the dependency of the contract that sets this column follows {names}, which "
                "is not a key of the table"
            )
        elif set(map(str, statistic["per"])) != set(keys):
            why = (
                f"the dependency of the contract that sets this column follows {names}, which "
                "is not what it is counted per"
            )
        else:
            why = "no rule reads this statistic of the column, so it is drawn from its per-row one"
        plan.passed.append((context.profile_tokens + ("statistics", str(index)), why))


def _grouping_columns(columns: Mapping[str, Any]) -> Set[str]:
    """The columns of a table that a statistic of the profile is grouped by (not by a link)."""
    found: Set[str] = set()
    for entry in columns.values():
        statistics = entry.get("statistics") if isinstance(entry, Mapping) else None
        for statistic in statistics if isinstance(statistics, list) else ():
            if isinstance(statistic, Mapping) and "groupBy" in statistic:
                found.add(str(statistic["groupBy"]))
    return found


def _span_of(plan: ColumnPlan) -> Optional[Tuple[Fraction, Fraction]]:
    """The values a column can take, for a rule that reads it: its range, else its draw."""
    checked = plan.declaration.get("range")
    if isinstance(checked, Mapping):
        low, high = numeric(checked.get("min")), numeric(checked.get("max"))
        if low is not None and high is not None:
            return low, high
    values = drawn_span(plan)
    if values:
        return Fraction(min(values)), Fraction(max(values))
    return None


def _determine(authored: AuthoredExpose) -> None:
    """Make every column a contract ``derive`` sets a column the rule decides (ADR 0014).

    It runs once the keys are laid out (:func:`_span_keys`), so a rule that reads
    a key reads the span the key is drawn over. A rule may read a column another
    rule sets, so the bounds are worked out in rounds until none moves; a rule
    whose inputs never get bounds gets none.
    """
    derives, contexts, expose_index = authored.derives, authored.contexts, authored.index
    plans = {plan.name: plan for plan in authored.columns}
    determined = {name for name in derives if name in plans}
    spans: Dict[str, Tuple[Fraction, Fraction]] = {}
    for name, plan in plans.items():
        if name not in determined and name not in authored.copies:
            span = _span_of(plan)
            if span is not None:
                spans[name] = span
    found: Dict[str, Optional[Tuple[Fraction, Fraction]]] = {}
    for _ in range(len(determined) + 1):
        moved = False
        for name in sorted(determined):
            interval = bounds(derives[name][1], spans)
            if interval is not None and spans.get(name) != interval:
                spans[name] = interval
                moved = True
            found[name] = interval
        if not moved:
            break
    for name in sorted(determined):
        index = derives[name][0]
        tokens = ("exposes", str(expose_index), "contract", "invariants", str(index), "expr")
        determine_column(contexts[name], plans[name], tokens, found.get(name))
    for name in sorted(authored.copies):
        _, how, reason = authored.copies[name]
        copy_column(contexts[name], plans[name], how=how, reason=reason)


#: Why a column a contract dependency sets has no mean property.
_DEPENDENT_REASON = (
    "A dependency of the contract sets this column: the run draws one value for each value "
    "of the columns it follows, so its mean rests on those few draws, and no mean property "
    "holds it to a margin."
)


#: Why a column drawn from its per-entity statistic has no histogram property.
_PER_ENTITY_REASON = (
    "The column is drawn from the profile's statistics counted per entity, and a run's mean and "
    "histogram count rows, so no property holds the run to them."
)

_COUNTED = ("mean", "marginalHistogram", "levelHistogram")


def _follow_dependencies(authored: AuthoredExpose) -> None:
    """A column a contract ``dependency`` sets keeps its draw and loses its mean property.

    One drawn from its statistic per entity loses the histogram properties too: the check
    counts the run's rows, the statistic counts entities.
    """
    for plan in authored.columns:
        if plan.name not in authored.dependents:
            continue
        per_entity = plan.name in authored.per_entity
        kept = [prop for prop in plan.properties if not _drops(prop.document, per_entity)]
        if len(kept) != len(plan.properties):
            plan.properties = kept
            reason = _PER_ENTITY_REASON if per_entity else _DEPENDENT_REASON
            plan.choices.append(reason)
            plan.assumptions.append(reason)


def _drops(document: Mapping[str, Any], per_entity: bool) -> bool:
    """Whether a column a dependency sets keeps no property of this document."""
    kind = document["property"]
    if kind == "mean":
        return True
    return per_entity and kind in _COUNTED and "groupBy" not in document


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
    """Choose the expose's properties, and gather its assumptions, once its keys are laid out.

    The block carries only a property the profile can score. One it holds no statistic for
    would be refused by ``verify`` (``FLX-5037``), so it is left out, and the expose records it
    in ``left_out``. A table left with no property declares an empty list, which ``verify``
    reports as not scored.
    """
    authored.properties, left_out = _choose_properties(authored)
    # What is left out is told only of a table that has no other property to verify, and a
    # histogram of a column's minimum and maximum alone checks no more than its range does.
    authored.left_out = (
        []
        if authored.properties
        else [prop for prop in left_out if not _histogram_of_two_edges(prop)]
    )
    if not authored.properties:
        # No property the profile can score: an assumed check it can score takes its place.
        for plan in authored.columns:
            fallback = assumed_check(plan, authored.kinds[plan.name], scorable_only=True)
            if fallback is not None:
                authored.properties = [fallback]
                break
    if not authored.properties and not _declares_a_column(authored):
        authored.skipped = (
            "no column has a range or levels the fidelity block could declare, so it would "
            "declare no column (FLX-5034); declare one by hand"
        )
    authored.assumptions.extend(
        (plan.name, sentence) for plan in authored.columns for sentence in plan.assumptions
    )


def _declares_a_column(authored: AuthoredExpose) -> bool:
    """Whether the expose's fidelity block declares a column: one a column or a link declares."""
    return bool(authored.linked) or any(column.declaration for column in authored.columns)


#: The properties that compare their columns over declared levels; every other kind compares
#: them over a declared range (``flux_stats.fidelity``).
_LEVEL_PROPERTIES = ("support", "levelHistogram")


def _property_columns(document: Mapping[str, Any]) -> List[str]:
    """The columns a property is over, not the ones it groups by."""
    named = [str(name) for name in document.get("columns") or ()]
    if "column" in document:
        named.append(str(document["column"]))
    return named


def _kept_declarations(authored: AuthoredExpose) -> Dict[str, str]:
    """What the fidelity compiler keeps of each column the block declares: ``range`` or ``support``.

    A declaration with neither (a draw's parameters alone), or with both, is kept as nothing.
    """
    declared = [(plan.name, plan.declaration) for plan in authored.columns]
    declared += [(name, linked.declaration) for name, linked in authored.linked.items()]
    return {
        name: "range" if "range" in declaration else "support"
        for name, declaration in declared
        if ("range" in declaration) != ("support" in declaration)
    }


def _unmet_columns(document: Mapping[str, Any], kept: Mapping[str, str]) -> List[str]:
    """The columns a property needs a declaration of, as a range or as levels, that the block lacks."""
    own = "support" if document.get("property") in _LEVEL_PROPERTIES else "range"
    needs = [(name, own) for name in _property_columns(document)]
    if "groupBy" in document:
        needs.append((str(document["groupBy"]), "support"))
    grouped = document.get("groupByBins")
    if isinstance(grouped, Mapping) and "column" in grouped:
        needs.append((str(grouped["column"]), "range"))
    return [name for name, wanted in needs if kept.get(name) != wanted]


def _drop_unkept_properties(authored: AuthoredExpose) -> None:
    """Leave out each property that names a column the fidelity block declares no range or levels for.

    ``verify`` refuses such a property (``FLX-5034``), and init never writes a bundle its own
    validation refuses. This is the last place the properties are chosen before the documents
    are drafted, so a writer that made one for a column the declarations do not cover cannot
    leave it in: it is dropped, and the column's notes say which declaration it needed. An expose
    left with no property takes the first column's assumed check, as :func:`_finish_expose` does.
    """
    kept = _kept_declarations(authored)
    unmet = {id(prop): _unmet_columns(prop.document, kept) for prop in authored.properties}
    dropped = [prop for prop in authored.properties if unmet[id(prop)]]
    if not dropped:
        return
    authored.properties = [prop for prop in authored.properties if not unmet[id(prop)]]
    plans = {plan.name: plan for plan in authored.columns}
    for prop in dropped:
        over = _property_columns(prop.document)
        sentence = (
            f"No {prop.document.get('property')} property is written for "
            f"{', '.join(printable(name) for name in over)}: the fidelity block declares "
            f"no range or levels of {', '.join(printable(name) for name in unmet[id(prop)])} "
            "for it to compare over (FLX-5034)."
        )
        plan = next((plans[name] for name in over if name in plans), None)
        if plan is not None and sentence not in plan.choices:
            plan.assumptions.append(sentence)
            plan.choices.append(sentence)
            authored.assumptions.append((plan.name, sentence))
    if not authored.properties:
        for plan in authored.columns:
            before = len(plan.assumptions)
            fallback = assumed_check(plan, authored.kinds[plan.name], scorable_only=True)
            if fallback is not None:
                authored.properties = [fallback]
                authored.assumptions.extend((plan.name, text) for text in plan.assumptions[before:])
                break


def _histogram_of_two_edges(prop: PropertyPlan) -> bool:
    """Whether *prop* is a histogram of one bin, over a column's published minimum and maximum."""
    document = prop.document
    return document["property"] == "marginalHistogram" and len(document.get("bins", ())) == 2


def _choose_properties(
    authored: AuthoredExpose,
) -> Tuple[List[PropertyPlan], List[PropertyPlan]]:
    """The properties the block carries (every one the profile can score), and those left out."""
    candidates = [prop for column in authored.columns for prop in column.properties]
    return (
        [prop for prop in candidates if prop.scorable],
        [prop for prop in candidates if not prop.scorable],
    )


# ---------------------------------------------------------------------------
# The documents
# ---------------------------------------------------------------------------


def _time_bounds(
    exposes: Sequence[AuthoredExpose],
    contract: Optional[Mapping[str, Any]] = None,
    given: Optional[Tuple[str, str]] = None,
    declared: Sequence[Tuple[int, int]] = (),
) -> Tuple[Tuple[str, str], Optional[Tuple[str, str]]]:
    """The run window: *given*, or the hull of every date or timestamp range, or an assumed year.

    The hull runs from the earliest start to the latest end of each column's published range
    (``time_span``), of its own bins, levels and range as declared, of the range its contract
    column declares, and of each *declared* span (a history file's start bins and ``until``), so
    no column's bins leave it. A column whose every value lies at or after
    :data:`WINDOW_END_LIMIT_MS` (an open end of 9999-12-31 on every row) gives the window nothing,
    as a contract range that ends there gives nothing. A contract range that reaches beyond every
    span the profile and the bundle give, or the hull of contract ranges alone, is a ``time
    window`` note, which is a gap at ``/spec/timeBounds``; one that makes the window longer than
    :data:`PLAUSIBLE_WINDOW_YEARS` is refused (:func:`_refuse_a_stretching_contract_range`). A
    window the profile and the bundle make that long is a ``time window`` note too, naming the
    columns that reach its ends (:func:`_a_long_window`). With nothing to go on, a year is
    assumed.
    """
    if given is not None:
        return given, None
    named: List[Tuple[str, Tuple[int, int]]] = [("the --history file", span) for span in declared]
    contract_ranges: List[Tuple[str, Tuple[int, int]]] = []
    for expose in exposes:
        for column in expose.columns:
            found, from_contract = _spans_of(expose, column, contract)
            named += [(f"{expose.expose_id}.{column.name}", span) for span in found]
            if from_contract is not None:
                contract_ranges.append((f"{expose.expose_id}.{column.name}", from_contract))
    own = [span for _, span in named]
    spans = own + [span for _, span in contract_ranges]
    if spans:
        start = min(span[0] for span in spans)
        end = max(span[1] for span in spans)
        if end <= start:
            end = start + 1
        _refuse_a_stretching_contract_range(own, contract_ranges, (start, end))
        window = (format_rfc3339_ms(start), format_rfc3339_ms(end))
        widening = sorted(
            {
                name
                for name, span in contract_ranges
                if not own
                or span[0] < min(item[0] for item in own)
                or span[1] > max(item[1] for item in own)
            }
        )
        reasons: List[str] = []
        if widening:
            columns = ", ".join(widening)
            if own:
                given_span = (
                    f"{format_rfc3339_ms(min(item[0] for item in own))} to "
                    f"{format_rfc3339_ms(max(item[1] for item in own))}"
                )
                reasons.append(
                    f"the contract range of {columns} reaches beyond the span the profile and "
                    f"the bundle give, {given_span}"
                )
            else:
                reasons.append(
                    f"it comes only from the contract range of {columns}: nothing the profile or "
                    "the bundle gives holds a span"
                )
        long_window = _a_long_window(named)
        if long_window is not None:
            reasons.append(long_window)
        if not reasons:
            return window, None
        note = (
            f"The run covers {window[0]} to {window[1]}, since {'; and '.join(reasons)}. "
            "Set spec.timeBounds to the span your data covers."
        )
        return window, ("time window", note)
    noise = any(column.noise_floored for expose in exposes for column in expose.columns)
    note = (
        "No date or timestamp column publishes its range"
        + (", or a released histogram with a bin that stands clear of the noise" if noise else "")
        + f", so the run covers {ASSUMED_WINDOW[0][:10]} to {ASSUMED_WINDOW[1][:10]}. Set "
        "spec.timeBounds to the span your data covers."
    )
    return ASSUMED_WINDOW, ("time window", note)


#: The longest run window that is not remarked on. A few hundred years holds the dates a data
#: product records; a longer window comes from what marks no data: a contract range with an open
#: end spelled before the year 9999 or a placeholder, which is refused, or a published span that
#: reaches that far, which is a ``time window`` note and a gap.
PLAUSIBLE_WINDOW_YEARS = 300

#: A Gregorian year of 365.2425 days, in milliseconds.
_YEAR_MS = 31_556_952_000


#: How many columns a time window note names before it counts the rest.
_NAMED_COLUMNS = 5


def _a_long_window(named: Sequence[Tuple[str, Tuple[int, int]]]) -> Optional[str]:
    """Why the spans the profile and the bundle give make a window past :data:`PLAUSIBLE_WINDOW_YEARS`.

    *named* is each span with what gives it (a column, or the history file). ``None`` when their
    hull is no longer than that. Otherwise the reason names what reaches either end of the hull, so
    a person can see which column, or which noise in a column, stretches the window.
    """
    if not named:
        return None
    low = min(first for _, (first, _) in named)
    high = max(last for _, (_, last) in named)
    if high - low <= PLAUSIBLE_WINDOW_YEARS * _YEAR_MS:
        return None
    reaching = sorted({name for name, (first, last) in named if first == low or last == high})
    listed = ", ".join(reaching[:_NAMED_COLUMNS])
    if len(reaching) > _NAMED_COLUMNS:
        listed += f" and {len(reaching) - _NAMED_COLUMNS} more"
    return (
        f"the span the profile and the bundle give runs longer than {PLAUSIBLE_WINDOW_YEARS} "
        f"years, from {format_rfc3339_ms(low)[:10]} to {format_rfc3339_ms(high)[:10]}: {listed} "
        "reach its ends"
    )


def _refuse_a_stretching_contract_range(
    own: Sequence[Tuple[int, int]],
    contract_ranges: Sequence[Tuple[str, Tuple[int, int]]],
    window: Tuple[int, int],
) -> None:
    """Refuse a contract range that makes the run's window longer than :data:`PLAUSIBLE_WINDOW_YEARS`.

    *own* is the spans the profile and the bundle give, *contract_ranges* each contract range by
    its column, and *window* the hull of both. Each range that reaches beyond *own* is named. A
    window *own* already makes that long is not the contract's doing, so nothing is refused.
    """
    longest = PLAUSIBLE_WINDOW_YEARS * _YEAR_MS
    if window[1] - window[0] <= longest:
        return
    if own and max(last for _, last in own) - min(first for first, _ in own) > longest:
        return
    low = min((first for first, _ in own), default=None)
    high = max((last for _, last in own), default=None)
    named = [
        f"{name} ({format_rfc3339_ms(first)[:10]} to {format_rfc3339_ms(last)[:10]})"
        for name, (first, last) in contract_ranges
        if low is None or high is None or first < low or last > high
    ]
    raise AuthorError(
        f"the contract range of {', '.join(named)} would make the run's window longer than "
        f"{PLAUSIBLE_WINDOW_YEARS} years, from {format_rfc3339_ms(window[0])[:10]} to "
        f"{format_rfc3339_ms(window[1])[:10]}, and such a window is refused; declare the range "
        "the data covers, or, when its high end marks an open end, write that end in the year "
        "9999, which no window reads"
    )


def _weighted_edges(edges: List[Any], weights: Any) -> List[Any]:
    """The edges of the bins that carry weight: the weightless bins at either end do not count.

    A bin of no weight draws nothing, so an edge only it reaches is no part of the column's span
    (the edge that closes a one-value date, say). A weightless bin between weighted ones lies inside
    the span anyway. The edges are all of them when no weight is given or none is positive.
    """
    if not isinstance(weights, list) or len(weights) != len(edges) - 1:
        return edges
    try:
        positive = [position for position, weight in enumerate(weights) if Decimal(str(weight)) > 0]
    except (ArithmeticError, ValueError):
        return edges
    if not positive:
        return edges
    return edges[positive[0] : positive[-1] + 2]


def _spans_of(
    expose: AuthoredExpose, column: ColumnPlan, contract: Optional[Mapping[str, Any]]
) -> Tuple[List[Tuple[int, int]], Optional[Tuple[int, int]]]:
    """The spans (ms) a column gives the window: its range and declaration, and its contract's range."""
    found: List[Tuple[int, int]] = []
    if column.time_span is not None:
        found.append(column.time_span)
    if expose.kinds.get(column.name) not in DATE_KINDS:
        return found, None
    declaration = column.declaration
    instants: List[int] = []
    for key in ("bins", "levels"):
        listed = declaration.get(key)
        if key == "bins" and isinstance(listed, list):
            listed = _weighted_edges(listed, declaration.get("binWeights"))
        if isinstance(listed, list):
            instants.extend(moment for moment in map(instant_ms, listed) if moment is not None)
    declared = declaration.get("range")
    if isinstance(declared, Mapping) and not column.noise_floored:
        # A noisy histogram's range is its whole grid (its property partitions it), so it says
        # nothing of where the data lies: the clear bins and time_span do.
        instants.extend(
            moment
            for moment in (instant_ms(declared.get("min")), instant_ms(declared.get("max")))
            if moment is not None
        )
    if instants:
        found.append((min(instants), max(instants)))
    if found and min(first for first, _ in found) >= WINDOW_END_LIMIT_MS:
        # Every value is in the year 9999 (an open end of 9999-12-31 on every row, say): like a
        # contract range that ends there, it says only that the column holds a date.
        found = []
    context = expose.contexts.get(column.name)
    try:
        schema = contract["exposes"][expose.index]["contract"]["schema"]  # type: ignore[index]
        contract_range = (
            declared_window(schema[context.schema_index], expose.kinds.get(column.name))
            if context
            else None
        )
    except (KeyError, IndexError, TypeError):
        contract_range = None
    return found, contract_range


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
    for name, linked in authored.linked.items():
        columns[name] = linked.declaration
    block: Dict[str, Any] = {
        "encoding": {
            "fractionalBits": encoding.get("fractionalBits", 16),
            "rounding": encoding.get("rounding", "floor"),
        },
        "records": dict(RECORDS),
        "columns": columns,
    }
    if authored.copula is not None and authored.copula.declaration is not None:
        block[COPULA_KEY] = authored.copula.declaration
    block["properties"] = [prop.document for prop in authored.properties]
    block["budget"] = dict(BUDGET)
    block["failureAction"] = "withholdBundle"
    return block


def _labels(authored: AuthoredExpose) -> List[Label]:
    """Every label of the expose, with tokens below the namespace's ``emits`` block."""
    base = ("emits", authored.expose_id)
    fidelity = base + ("fidelity",)
    assert authored.rows_basis is not None  # set for every expose that is written
    labels: List[Label] = (
        [] if authored.projected is not None else [Label(base + ("rows",), authored.rows_basis)]
    )
    labels.append(
        Label(
            fidelity + ("records",),
            assumed(
                "Bounds on the size of a committed state, wide on purpose so that any "
                "profiled table fits."
            ),
        )
    )
    for column in authored.columns:
        for label in column.labels:
            labels.append(Label(fidelity + ("columns", column.name) + label.tokens, label.basis))
    for linked in authored.linked.values():
        for label in linked.labels:
            labels.append(Label(fidelity + ("columns", linked.name) + label.tokens, label.basis))
    for position, prop in enumerate(authored.properties):
        for label in prop.labels:
            labels.append(
                Label(fidelity + ("properties", str(position)) + label.tokens, label.basis)
            )
    labels.extend(_copula_labels(authored, fidelity))
    return labels


def _copula_labels(authored: AuthoredExpose, fidelity: Tuple[str, ...]) -> List[Label]:
    """The value of each pair of the copula cites the rank correlation of the pair it was read from."""
    copula = authored.copula
    if copula is None or copula.declaration is None:
        return []
    definition = KENDALL_TAU_ID if copula.measure == "kendall" else SPEARMAN_RHO_ID
    where = fidelity + (COPULA_KEY, "rankCorrelations", "pairs")
    return [
        Label(where + (str(index), "value"), from_profile(tokens, definition))
        for index, (tokens, _how) in enumerate(copula.reads)
    ]


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


class _Scoped(NamedTuple):
    """The profile a bundle is written from: the pinned one, or the one an overlay makes of it."""

    profile: Any
    document: Mapping[str, Any]
    overlay: Optional[Overlay]

    @property
    def name(self) -> Optional[str]:
        return self.overlay.name if self.overlay is not None else None

    @property
    def citing(self) -> Optional[Tuple[str, Mapping[str, Any]]]:
        """The overlay's name and the overlaid document, as the register's check reads them."""
        return (self.overlay.name, self.document) if self.overlay is not None else None

    @property
    def member(self) -> Dict[str, Any]:
        """The namespace's ``overlay`` key, or nothing."""
        return {OVERLAY_KEY: self.overlay.to_document()} if self.overlay is not None else {}

    @property
    def header(self) -> str:
        """The line a bundle written with an overlay adds to its files' header."""
        return _OVERLAY_HEADER.format(overlay=self.name) if self.overlay is not None else ""


def _scoped(pinned: Any, overlay: Optional[Overlay]) -> _Scoped:
    """The profile the bundle is written from: *pinned*, or what *overlay* makes of it."""
    if overlay is None:
        return _Scoped(pinned.profile, pinned.document, None)
    profile, document = overlaid_profile(pinned.document, overlay, code=FLX_3105)
    check_restated_means(profile, overlay, code=FLX_3402)
    return _Scoped(profile, document, overlay)


def _cite_overlay(
    entries: List[Dict[str, Any]],
    pinned: Mapping[str, Any],
    overlaid: Mapping[str, Any],
    name: str,
) -> List[Dict[str, Any]]:
    """*entries* with each ``profile`` citation of a statistic the patch changed citing the overlay.

    A number that rests on a statistic the overlay changed is the overlay's, not a measurement of
    the pinned profile, so its entry says ``source: overlay`` and names *name*. The citation is
    the same pointer, read in the overlaid profile.
    """
    cited: List[Dict[str, Any]] = []
    for entry in entries:
        statistic = entry.get("statistic")
        if entry.get("source") == "profile" and isinstance(statistic, Mapping):
            tokens = parse_pointer(str(statistic["pointer"]))
            if changed_by_overlay(pinned, overlaid, tokens):
                entry = {
                    key: ("overlay" if key == "source" else value)
                    for key, value in entry.items()
                    if key != "statistic"
                } | {"overlay": name, "statistic": statistic}
        cited.append(entry)
    return cited


def _cite_and_replace(
    entries: List[Dict[str, Any]],
    profiles: Tuple[Mapping[str, Any], Mapping[str, Any], Optional[Overlay]],
    contract: Tuple[Mapping[str, Any], str, int],
    simulation_id: str,
    ledger: Ledger,
) -> Tuple[List[Dict[str, Any]], Optional[ProfileRequest]]:
    """*entries* citing the overlay where it changed a number, and naming what would replace an assumption.

    *profiles* are the pinned profile's document, the overlaid one and the overlay, if any.
    *contract* is the contract's document, its digest and the profile's fractional bits. Each
    assumed number a profile statistic would replace says which, the ledger lists the statistics,
    and the profile request for them is returned, to be written beside the bundle (ADR 0030
    section 8).
    """
    pinned, scored, overlay = profiles
    document, digest, bits = contract
    if overlay is not None:
        entries = _cite_overlay(entries, pinned, scored, overlay.name)
    cited, asked, replaceable = replacements_for(
        entries,
        column_domains(document),
        EXTENSION_NAMESPACE,
        simulation_id,
        contract_digest=digest,
        fractional_bits=bits,
    )
    ledger.replacements = replaceable
    return cited, asked


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


def _projected_unused(ledger: Ledger, exposes: Sequence[AuthoredExpose]) -> None:
    """Say why the published rowCount of each projected emit is not read."""
    for expose in exposes:
        if expose.projected is not None:
            ledger.unused(("tables", expose.expose_id, "rowCount"), expose.projected)


def _ledger(
    profile: Mapping[str, Any],
    exposes: Sequence[AuthoredExpose],
    contract_ids: Set[str],
    extra: Tuple[List[_Note], List[_Note], List[_Note]] = ([], [], []),
    noisy: Optional[Mapping[Tuple[str, ...], Any]] = None,
) -> Ledger:
    """Every statistic of *profile*, marked by what the written *exposes* rest on and read.

    *extra* holds what the bundle made of statistics no column plan carries (a
    relationship's children per parent): the used ones, and the unused ones with
    their reasons, then the decisions about a relationship that are no statistic (its
    structural classes), which the ledger lists as used.
    """
    ledger = Ledger(profile, {tokens: note.as_document() for tokens, note in (noisy or {}).items()})
    for tokens, how in extra[0]:
        ledger.use(tokens, how)
    for tokens, how in extra[2]:
        ledger.record(tokens, how)
    tables = profile.get("tables")
    for name in tables if isinstance(tables, Mapping) else ():
        if str(name) not in contract_ids:
            ledger.unused_under(
                ("tables", str(name)), "the contract declares no expose of this name"
            )
    for expose in exposes:
        _ledger_expose(ledger, expose)
    for tokens, why in extra[1]:
        ledger.unused(tokens, why)
    _projected_unused(ledger, exposes)
    _reset_unwritten_relationships(ledger, profile, exposes)
    return ledger


def _ledger_expose(ledger: Ledger, expose: AuthoredExpose) -> None:
    """Mark the statistics one written, skipped or reference expose rests on and reads."""
    if expose.declined_reference is not None:
        ledger.add_unused(("references", expose.expose_id), expose.declined_reference)
    if expose.skipped is not None:
        ledger.unused_under(
            ("tables", expose.expose_id), f"the expose was not written: {expose.skipped}"
        )
        return
    if expose.reference:
        ledger.unused_under(
            ("tables", expose.expose_id),
            "the expose is a reference expose, released whole as the profile publishes it, "
            "so nothing is drawn from its statistics",
        )
        return
    # What a rule says it read comes first: it is more specific than a citation.
    for column in expose.columns:
        for tokens, how in column.reads:
            ledger.use(tokens, how)
    if expose.copula is not None:
        for tokens, how in expose.copula.reads:
            ledger.use(tokens, how)
    for prop in expose.properties:
        for tokens, how in prop.reads:
            ledger.use(tokens, how)
    _cite_expose(ledger, expose)


def _cite_expose(ledger: Ledger, expose: AuthoredExpose) -> None:
    """Cite the basis of the rows and of each label of *expose*, and say why a column passed."""
    _cite(ledger, None if expose.projected is not None else expose.rows_basis)
    for column in expose.columns:
        for label in column.labels:
            _cite(ledger, label.basis)
        for tokens, why in column.passed:
            ledger.unused(tokens, why)
    for prop in expose.properties:
        for label in prop.labels:
            _cite(ledger, label.basis)
    if expose.copula is not None:
        for tokens, why in expose.copula.passed:
            ledger.unused(tokens, why)


def _reset_unwritten_relationships(
    ledger: Ledger, profile: Mapping[str, Any], exposes: Sequence[AuthoredExpose]
) -> None:
    """Reset a profile relationship when the bundle writes no emit for one of its two tables."""
    written = {
        expose.expose_id for expose in exposes if expose.skipped is None and not expose.reference
    }
    found = profile.get("relationships")
    for index, entry in enumerate(found if isinstance(found, list) else ()):
        tables = (
            [
                side.get("table")
                for side in (entry.get("parent"), entry.get("child"))
                if isinstance(side, Mapping)
            ]
            if isinstance(entry, Mapping)
            else []
        )
        if any(str(table) not in written for table in tables):
            ledger.reset_under(
                ("relationships", str(index)),
                "the bundle writes no generator emit of one of its two tables",
            )


#: A pointer into the profile, with how it was used or why it was not.
_Note = Tuple[Tuple[str, ...], str]


def _floored_columns(expose: Mapping[str, Any], expose_id: str) -> List[Tuple[str, str]]:
    """The columns of an expose whose sensitivity sets a minimum grade, with their labels.

    The floors are the engine's own: ``init`` writes no ``privacy`` block, so it sees no floor a
    Simulation raises, and the contract's labels alone decide (the same rule as the governance
    gate, :func:`flux_engine.governance.privacy.effective_floors`).
    """
    floors = effective_floors(None)
    labels = sensitivities_of([(expose_id, expose)])
    return sorted((name, label) for (_, name), label in labels.items() if label in floors)


def _declined_reference(expose: Mapping[str, Any], expose_id: str) -> Optional[str]:
    """Why a published table is no reference expose, when a column's sensitivity asks a grade."""
    held = _floored_columns(expose, expose_id)
    if not held:
        return None
    shown = ", ".join(f"{printable(name)} ({label})" for name, label in held[:5])
    return (
        "a reference expose is released whole, with the rows the profile publishes and no noise, "
        f"and a column's sensitivity asks for a guarantee ({shown}); the expose is generated "
        "from its other statistics instead"
    )


def _reference_expose(
    expose_index: int, expose: Mapping[str, Any], published: Mapping[str, Any]
) -> AuthoredExpose:
    """An expose the profile publishes whole: written as ``reference: true``, its rows the profile's."""
    compiled = compile_expose(expose)
    rows = published.get("rows")
    authored = AuthoredExpose(
        expose_id=str(expose["exposeId"]),
        rows=len(rows) if isinstance(rows, list) else 0,
        index=expose_index,
        reference=True,
        kinds={column.name: column.logical_type.kind for column in compiled.columns},
    )
    if not authored.rows:
        authored.reference = False
        authored.skipped = "the profile publishes no row of it under references"
    return authored


def _skip_unreadable_references(
    exposes: Sequence[AuthoredExpose], keys: Optional[ContractKeys]
) -> None:
    """Leave out a reference expose whose relationship points at an expose that is not one.

    A relationship of a reference expose names another reference expose (``FLX-3403``).
    """
    whole = {expose.expose_id for expose in exposes if expose.reference}
    for expose in exposes:
        if not expose.reference or keys is None:
            continue
        outside = sorted(
            {
                relationship.target
                for relationship in keys.expose(expose.expose_id).relationships
                if relationship.target not in whole
            }
        )
        if outside:
            expose.reference = False
            expose.skipped = (
                "its relationships point at "
                + ", ".join(printable(name) for name in outside)
                + ", which the profile does not publish whole, and a reference expose points "
                "only at another (FLX-3403)"
            )


class _Spreads(NamedTuple):
    """What :func:`_spread_relationships` made of the profile's relationships."""

    used: List[_Note]
    unused: List[_Note]
    #: The ``counts`` entries, one for each relationship the layout places.
    entries: List[Dict[str, Any]]
    #: The decisions about a relationship that no statistic holds, listed as used.
    records: List[_Note]
    #: What is assumed of a relationship, by its name: a sentence a person reads.
    notes: List[Tuple[str, str]]
    #: What each number of a ``counts`` entry rests on, by the entry (see :data:`_Cited`).
    cited: Tuple[Tuple[Dict[str, Any], "_Cited"], ...] = ()
    #: What each laid-out relationship wrote, so one can be taken back (see :func:`_without_unwritten`).
    laid: Tuple["_Laid", ...] = ()


class _Laid(NamedTuple):
    """What :func:`_spread_relationships` wrote for one relationship, by the two tables it joins."""

    parent: str
    child: str
    #: The ``counts`` entry, when the counted layout placed the relationship.
    entry: Optional[Dict[str, Any]]
    #: The sentences and the decision about its structural classes that it added to the report.
    notes: Tuple[Tuple[str, str], ...]
    records: Tuple[_Note, ...]


#: What a number of a written entry rests on: the member of the entry (tokens below it), the
#: statistic of the profile it was read from (tokens into the profile; ``None`` when init chose
#: the number itself) and the sentence that says how, or why.
_Cited = Tuple[Tuple[str, ...], Optional[Tuple[str, ...]], str]


class _Placed(NamedTuple):
    """How :func:`_lay_out_children` laid one relationship's children out."""

    used: List[_Note]
    unused: List[_Note]
    #: The ``counts`` entry, when the counted layout placed the relationship.
    entry: Optional[Dict[str, Any]]
    notes: List[Tuple[str, str]]
    counted: bool
    #: Why the counted layout did not place it, when it did not.
    laid: Optional[str]
    #: What each number of the ``counts`` entry rests on.
    cited: Tuple[_Cited, ...] = ()
    #: What :func:`_settle_given` needs to choose the entry's given column, once the parent's
    #: columns that are drawn given a linked row are known.
    pending: Optional["_Pending"] = None


class _Slot(NamedTuple):
    """One profile relationship, as :func:`_spread_relationships` laid it out."""

    tokens: Tuple[str, ...]
    entry: Mapping[str, Any]
    declared: Any
    child_table: str
    #: ``None`` when the contract's relationship gave no layout.
    placed: Optional[_Placed]
    #: The statistics of the entry that nothing laid out, with the reason.
    passed: List[_Note]


class _Pending(NamedTuple):
    """What a ``counts`` entry waits on before it can be given a column of the parent."""

    #: The profile's relationship entry, with the ungrouped histogram its groups add up to.
    relationship: Mapping[str, Any]
    counted: Any
    child_plan: ColumnPlan
    parent_expose: AuthoredExpose
    #: The relationship, as ``<child table>.<name>``, that its assumptions are said under.
    name: str


def _given_parent(parent: AuthoredExpose) -> GivenParent:
    """The parent's columns as a ``counts`` entry's given column reads them, with why some cannot be."""
    held: Dict[str, str] = {}
    for plan in parent.columns:
        name = plan.name
        if plan.fixed is not None:
            held[name] = plan.fixed
        elif name in parent.aggregated:
            held[name] = "set by a summary over the children of the parent"
        elif name in parent.derives or name in parent.dependents or name in parent.copies:
            held[name] = "set by a rule of the contract"
        elif name in parent.guarded:
            held[name] = "set by a rule of the contract on some rows"
        elif _is_link_given(plan):
            held[name] = "drawn given a column of a linked row"
    return GivenParent(
        plans={plan.name: plan for plan in parent.columns}, kinds=parent.kinds, held_back=held
    )


def _is_link_given(plan: ColumnPlan) -> bool:
    given = plan.declaration.get("given")
    names = [given] if isinstance(given, str) else given if isinstance(given, list) else []
    return any(isinstance(name, str) and "." in name for name in names)


def _drop_zero_nulls(parent: AuthoredExpose, column: str) -> None:
    """Take a zero ``nullFraction`` off *column*: a column that is a class to read declares none."""
    for plan in parent.columns:
        if plan.name == column and plan.declaration.get("nullFraction") is not None:
            plan.declaration.pop("nullFraction", None)
            plan.labels = [label for label in plan.labels if label.tokens != ("nullFraction",)]


def _take_given(
    given: Given,
    child_plan: ColumnPlan,
    parent_expose: AuthoredExpose,
    said: Tuple[List[Tuple[str, str]], str],
) -> None:
    """Record what a given column of a ``counts`` entry assumes, and the null share it drops."""
    notes, name = said
    for sentence in given.assumptions:
        child_plan.assumptions.append(sentence)
        notes.append((name, sentence))
    if given.column is None:
        return
    if given.drop_null_fraction:
        _drop_zero_nulls(parent_expose, given.column)
    child_plan.how += f", its counts given {printable(given.column)} of the parent"


def _lay_out_children(
    entry: Mapping[str, Any],
    tokens: Tuple[str, ...],
    keys: Optional[ContractKeys],
    declared: Any,
    child_expose: AuthoredExpose,
    parent_expose: AuthoredExpose,
    child_plan: ColumnPlan,
    parent_plan: ColumnPlan,
    child_entries: Any,
    *,
    minimum_cell: int,
    fractional_bits: int,
) -> _Placed:
    """Place one relationship by counts when the layout can, and as its foreign key's spread if not."""
    child_table = child_expose.expose_id
    notes: List[Tuple[str, str]] = []
    held = _counting_hold(keys, child_expose, child_plan, declared)
    entry = with_summed_groups(entry) if held is None else entry
    counted = (
        count_children(
            entry,
            tokens,
            child_expose.rows,
            parent_expose.rows,
            minimum_cell=minimum_cell,
            fractional_bits=fractional_bits,
            one_to_one=declared.cardinality == "one_to_one",
        )
        if held is None
        else None
    )
    if counted is not None and counted.why_not is not None:
        notes.append(
            (f"{child_table}.{declared.name}", f"not laid out by counts: {counted.why_not}")
        )
        held, counted = counted.why_not, None
    if counted is not None:
        declare_counted(child_plan, counted.assumption)
        counts = {"emit": child_table, "relationship": declared.name, **counted.members}
        # Its given column is chosen by `_settle_given`, after the columns drawn given a linked
        # row are known: the generator refuses a given column that is one.
        pending = _Pending(
            entry, counted, child_plan, parent_expose, f"{child_table}.{declared.name}"
        )
        cited = _counts_cited(counted, None)
        return _Placed(
            list(counted.used), list(counted.unused), counts, notes, True, None, cited, pending
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
    laid = held or "the histogram of children per parent is not one the layout can read"
    return _Placed(list(spread.used), list(spread.unused), None, notes, False, laid)


def _settle_given(placed: _Placed, tokens: Tuple[str, ...], *, minimum_cell: int) -> _Placed:
    """*placed* with the given column of its ``counts`` entry chosen, when the profile groups one.

    This reads which columns of the parent are drawn given a column of a linked row, so it runs
    once those are declared. A grouping by one of them is passed over, with the reason.
    """
    pending = placed.pending
    if pending is None or placed.entry is None:
        return placed
    given = given_children(
        pending.relationship,
        tokens,
        pending.counted,
        _given_parent(pending.parent_expose),
        minimum_cell=minimum_cell,
    )
    if given is None:
        return placed
    placed.entry.update(given.members)
    notes = list(placed.notes)
    _take_given(given, pending.child_plan, pending.parent_expose, (notes, pending.name))
    return placed._replace(
        used=[*placed.used, *given.used],
        unused=[*placed.unused, *given.unused],
        notes=notes,
        cited=_counts_cited(pending.counted, given),
    )


def _counts_cited(counted: Any, given: Optional[Any]) -> Tuple[_Cited, ...]:
    """What each number of a ``counts`` entry rests on (ADR 0016 section 7).

    The classes and their weights rest on the profile's histogram of children per parent, an open
    last class's upper edge on the total of children (a published mean, or the child rows),
    ``minChildren`` on the class the profile marks structural, and the weights given a column of the
    parent on the statistic grouped by it.
    """
    shape = counted.members["children"]
    pointer, how = counted.used[0]
    found: List[_Cited] = [(("children",), pointer, how)]
    if "levels" in shape:
        found.append(
            (
                ("children", "levels"),
                None,
                "A one_to_one relationship gives a parent none or one child, so the entry's "
                "levels are 0 and 1.",
            )
        )
    elif len(counted.used) > 1:
        last = ("children", "bins", str(len(shape["bins"]) - 1))
        found.append((last, counted.used[1][0], counted.used[1][1]))
    else:
        found.append(
            (
                ("children", "bins", str(len(shape["bins"]) - 1)),
                None,
                "The open last class's upper edge is chosen so that its even spread holds the "
                "child rows, which the layout gives each a parent.",
            )
        )
    if "minChildren" in counted.members:
        found.append(
            (
                ("minChildren",),
                None,
                "The profile marks the class of parents with no child as structural, so every "
                "parent has a child and the entry's minChildren is 1.",
            )
        )
    if given is not None and given.members:
        grouped, how = given.used[-1]
        found.append((("childrenGiven",), grouped, how))
    return tuple(found)


def _resolve(
    entry: Mapping[str, Any], keys: Optional[ContractKeys], planned: Mapping[str, AuthoredExpose]
) -> Tuple[Optional[str], Optional[Any], Tuple[str, str], Tuple[List[str], List[str]]]:
    """What a profile relationship names: why it is not read, or the contract's relationship.

    Returns the reason (``None`` when it is read), the declared relationship, the two table names
    and the two column lists.
    """
    found_parent, found_child = entry.get("parent"), entry.get("child")
    parent: Mapping[str, Any] = found_parent if isinstance(found_parent, Mapping) else {}
    child: Mapping[str, Any] = found_child if isinstance(found_child, Mapping) else {}
    parent_table, child_table = str(parent.get("table")), str(child.get("table"))
    parent_columns = [nfc(str(name)) for name in parent.get("columns") or ()]
    child_columns = [nfc(str(name)) for name in child.get("columns") or ()]
    tables = (parent_table, child_table)
    columns = (parent_columns, child_columns)
    if len(parent_columns) != 1 or len(child_columns) != 1:
        return (
            "a key of several columns: its children per parent are not laid out",
            None,
            tables,
            columns,
        )
    if child_table not in planned or parent_table not in planned:
        return "the bundle writes no generator emit of one of its two tables", None, tables, columns
    declared = next(
        (
            relationship
            for relationship in (_relationships_of(keys, child_table) if keys is not None else ())
            if relationship.target == parent_table
            and relationship.from_columns == tuple(child_columns)
            and relationship.to_columns == tuple(parent_columns)
        ),
        None,
    )
    if declared is None:
        return (
            "the contract declares no such relationship between the two exposes",
            None,
            tables,
            columns,
        )
    return None, declared, tables, columns


def _structural_classes(entry: Mapping[str, Any]) -> List[str]:
    """The classes of children per parent a ``relationships`` entry marks structural."""
    structural = entry.get("structural")
    return [str(name) for name in structural] if isinstance(structural, list) else []


def _resolved(
    entry: Mapping[str, Any],
    keys: Optional[ContractKeys],
    planned: Mapping[str, AuthoredExpose],
    held_back: Optional[Callable[[str, str], Optional[str]]],
) -> Tuple[Optional[str], Optional[Any], Tuple[str, str], Tuple[List[str], List[str]]]:
    """:func:`_resolve`, with the reason *held_back* gives for a relationship the contract declares."""
    why, declared, tables, columns = _resolve(entry, keys, planned)
    if why is None and declared is not None and held_back is not None:
        why = held_back(tables[1], declared.name)
    return why, declared, tables, columns


def _spread_relationships(
    exposes: Sequence[AuthoredExpose],
    keys: Optional[ContractKeys],
    profile: Mapping[str, Any],
    *,
    minimum_cell: int,
    fractional_bits: int,
    held_back: Optional[Callable[[str, str], Optional[str]]] = None,
    declare_links: Optional[Callable[[], None]] = None,
) -> _Spreads:
    """Lay each relationship's children per parent out (ADR 0014 section 5, ADR 0016 section 7).

    A profile ``relationships`` entry is read when the contract declares the same
    one-column relationship between two exposes the bundle writes. One the counted layout can
    place, with a histogram it can read, becomes a ``counts`` entry
    (:func:`flux_engine.author.children.count_children`); any other lays its children out as
    its foreign key's spread (:func:`flux_engine.author.children.spread_children`). Returns what
    was used and, with the reasons, what was not, and the ``counts`` entries, in relationship
    order. A relationship that *held_back* gives a reason for, by its child table and name, is laid
    out by no entry: every statistic of its profile entry is unused for that reason.

    The given column of each ``counts`` entry is chosen last, after *declare_links* has run: it
    declares the columns drawn given a column of a linked row, and a grouping by one of them is
    passed over.
    """
    used: List[_Note] = []
    unused: List[_Note] = []
    entries: List[Dict[str, Any]] = []
    records: List[_Note] = []
    notes: List[Tuple[str, str]] = []
    cited: List[Tuple[Dict[str, Any], _Cited]] = []
    laid: List[_Laid] = []
    found = profile.get("relationships")
    found_tables = profile.get("tables")
    tables: Mapping[str, Any] = found_tables if isinstance(found_tables, Mapping) else {}
    planned = {expose.expose_id: expose for expose in exposes if expose.skipped is None}
    slots: List[_Slot] = []
    for index, entry in enumerate(found if isinstance(found, list) else ()):
        if not isinstance(entry, Mapping):
            continue
        tokens = ("relationships", str(index))
        every = statistic_tokens(entry, tokens)
        why, declared, (parent_table, child_table), (parent_columns, child_columns) = _resolved(
            entry, keys, planned, held_back
        )
        if why is not None or declared is None:
            slots.append(_Slot(tokens, entry, None, "", None, [(p, why or "") for p in every]))
            continue
        child_expose, parent_expose = planned[child_table], planned[parent_table]
        child_plan = next(plan for plan in child_expose.columns if plan.name == child_columns[0])
        parent_plan = next(plan for plan in parent_expose.columns if plan.name == parent_columns[0])
        child_table_entry = tables.get(child_table)
        child_entries = (
            child_table_entry.get("columns") if isinstance(child_table_entry, Mapping) else None
        )
        placed = _lay_out_children(
            entry,
            tokens,
            keys,
            declared,
            child_expose,
            parent_expose,
            child_plan,
            parent_plan,
            child_entries,
            minimum_cell=minimum_cell,
            fractional_bits=fractional_bits,
        )
        slots.append(_Slot(tokens, entry, declared, child_table, placed, []))
    if declare_links is not None:
        declare_links()
    for slot in slots:
        unused += slot.passed
        if slot.placed is None:
            continue
        settled = _settle_given(slot.placed, slot.tokens, minimum_cell=minimum_cell)
        used += settled.used
        unused += settled.unused
        written_notes = list(settled.notes)
        written_records: List[_Note] = []
        if settled.entry is not None:
            entries.append(settled.entry)
            cited += [(settled.entry, item) for item in settled.cited]
        classes = _structural_classes(slot.entry)
        if classes:
            recorded, sentence = _structural_note(classes, settled.counted, settled.laid)
            if recorded:
                written_records.append((slot.tokens + ("structural",), sentence))
            else:
                written_notes.append((f"{slot.child_table}.{slot.declared.name}", sentence))
        notes += written_notes
        records += written_records
        laid.append(
            _Laid(
                slot.declared.target,
                slot.child_table,
                settled.entry,
                tuple(written_notes),
                tuple(written_records),
            )
        )
    return _Spreads(used, unused, entries, records, notes, tuple(cited), tuple(laid))


def _without_unwritten(spread: _Spreads, written: Set[str]) -> _Spreads:
    """*spread* without what it wrote for a relationship whose parent or child emit is not written.

    The layout runs before each expose is finished, and an expose is left out at that point when
    the layout has set its keys and nothing is left for its fidelity block to declare (a table of
    foreign keys alone, whose ``counts`` entries set them all). A ``counts`` entry names an emit
    the Simulation then does not write, which ``plan`` refuses (FLX-3105), so each such
    relationship's entry, its numbers' citations and its sentences are taken back. Its
    statistics are listed unused, with the reason, by :func:`_reset_unwritten_relationships`.
    """
    gone = [item for item in spread.laid if not {item.parent, item.child} <= written]
    if not gone:
        return spread
    gone_ids = {id(item) for item in gone}
    entries = {id(item.entry) for item in gone if item.entry is not None}
    notes = {id(note) for item in gone for note in item.notes}
    records = {id(record) for item in gone for record in item.records}
    return _Spreads(
        spread.used,
        spread.unused,
        [entry for entry in spread.entries if id(entry) not in entries],
        [record for record in spread.records if id(record) not in records],
        [note for note in spread.notes if id(note) not in notes],
        tuple(pair for pair in spread.cited if id(pair[0]) not in entries),
        tuple(item for item in spread.laid if id(item) not in gone_ids),
    )


class _Histories(NamedTuple):
    """What :func:`_histories` made of the tables that publish a history."""

    #: The ``history`` entries, one for each expose that has one.
    entries: List[Dict[str, Any]]
    used: List[_Note]
    unused: List[_Note]
    notes: List[Tuple[str, str]]

    def declare_in(self, namespace: Dict[str, Any]) -> None:
        """Declare the entries under the namespace's ``history`` key, in emit order."""
        if self.entries:
            namespace[HISTORY_KEY] = sorted(self.entries, key=lambda item: item["emit"])


def _asof_entries(
    exposes: Sequence[AuthoredExpose],
    keys: Optional[ContractKeys],
    contract: Mapping[str, Any],
    histories: _Histories,
) -> List[Dict[str, str]]:
    """The ``asOf`` entry of each relationship into a history's emit from an expose with a default time dimension.

    ADR 0017 section 13: an expose that declares a ``defaultAggTimeDimension`` is dated by it, so a
    path through a relationship that reaches a versioned entity reads the version valid on that
    date. Only a to-one relationship that is not a key of the expose and whose ``toColumns`` are the
    history's natural entity is written, so the plan accepts every entry.
    """
    if keys is None or not histories.entries:
        return []
    written = {str(entry["emit"]) for entry in histories.entries}
    found: List[Dict[str, str]] = []
    for expose in exposes:
        if expose.reference or expose.skipped is not None:
            continue
        column = _default_date_column(expose, contract)
        if column is not None:
            found.extend(_asof_relationships(expose, column, keys, written))
    return sorted(found, key=lambda item: (item["emit"], item["relationship"]))


def _default_date_column(expose: AuthoredExpose, contract: Mapping[str, Any]) -> Optional[str]:
    """The ``defaultAggTimeDimension`` of *expose* when it names a date or timestamp column."""
    semantics = contract["exposes"][expose.index].get("semantics")
    column = semantics.get("defaultAggTimeDimension") if isinstance(semantics, Mapping) else None
    if not isinstance(column, str):
        return None
    if expose.kinds.get(column) not in (LogicalKind.DATE, LogicalKind.TIMESTAMP):
        return None
    return column


def _asof_relationships(
    expose: AuthoredExpose, column: str, keys: ContractKeys, written: Set[str]
) -> List[Dict[str, str]]:
    """The ``asOf`` entries of *expose*: its to-one relationships into a history's emit."""
    held = keys.expose(expose.expose_id)
    key_sets = {frozenset(key) for key in (*held.keys, *held.instant_keys)}
    return [
        {"emit": expose.expose_id, "relationship": relationship.name, "date": column}
        for relationship in held.relationships
        if relationship.target in written
        and relationship.cardinality == "many_to_one"
        and frozenset(relationship.from_columns) not in key_sets
        and tuple(relationship.to_columns) == _natural_of(keys, relationship.target)
    ]


def _natural_of(keys: ContractKeys, expose_id: str) -> Tuple[str, ...]:
    """The columns of *expose_id*'s natural entities."""
    return tuple(
        entity.column for entity in keys.expose(expose_id).entities if entity.kind == "natural"
    )


def _check_histories(
    files: Sequence[StarterFile],
    pinned: Any,
    histories: _Histories,
    projections: Projected,
    declined: Optional[str],
    overlay: Optional[Tuple[str, Mapping[str, Any]]] = None,
    declared: Optional[DeclaredHistory] = None,
    windowed: bool = False,
) -> None:
    """Check the files as :func:`_check` does; a bundle refused with projections or histories is written again without.

    A bundle written with a history file is never written again without it: its refusal is
    :class:`_DeclaredRefused`, which names the file and keeps the refusal's code. A bundle written
    with a ``--window`` is the same when it holds histories or projections: the window decides the
    rows they are laid over, so its refusal is reported, naming the window.

    The plan refuses a history the layout cannot place (a natural entity that is also a foreign key,
    say), which no statistic of the profile shows, and a projection its history cannot feed. The
    first refusal raises :class:`_ProjectionRefused` when the bundle holds one, and
    :class:`_HistoryRefused` otherwise, which :func:`author_bundle` answers by writing without.
    """
    try:
        _check(files, pinned.document, pinned.profile.references, overlay=overlay)
    except AuthorError as error:
        if declared is not None:
            cause = error.__cause__
            code = cause.code.code if isinstance(cause, FluxError) else None
            raise _DeclaredRefused(
                f"the bundle written with the history of {declared.name} is refused: {error}",
                code,
            ) from error
        if windowed and (projections.emits or (declined is None and histories.entries)):
            cause = error.__cause__
            code = cause.code.code if isinstance(cause, FluxError) else None
            raise _DeclaredRefused(
                f"the bundle written with the window given with --window is refused: {error}",
                code,
            ) from error
        if projections.emits:
            raise _ProjectionRefused(str(error)) from error
        if declined is None and histories.entries:
            raise _HistoryRefused(str(error)) from error
        raise


def _projections(
    exposes: Sequence[AuthoredExpose],
    keys: Optional[ContractKeys],
    contract: Mapping[str, Any],
    histories: _Histories,
    published: Published,
    declined: Optional[str],
) -> Projected:
    """The ``snapshots``, ``events`` and ``periods`` entries of the written exposes (ADR 0027 section 9).

    Each expose without a validity window is read against each expose that has a history
    (:func:`flux_engine.author.projection.project`). With *declined*, the reason plan refuses the
    bundle that holds them, none is written and the note says so.
    """
    if keys is None or not histories.entries:
        return Projected()
    index = {expose.expose_id: expose for expose in exposes}
    targets = [
        history_target(
            entry,
            keys.expose(str(entry["emit"])),
            compile_expose(contract["exposes"][index[str(entry["emit"])].index]).columns,
        )
        for entry in histories.entries
    ]
    sources = [
        ProjectionSource(
            expose.expose_id,
            contract["exposes"][expose.index],
            keys.expose(expose.expose_id),
            counts=tuple(
                entry for entry in published.spread.entries if entry["emit"] == expose.expose_id
            ),
        )
        for expose in exposes
        if not expose.reference
        and expose.skipped is None
        and not keys.expose(expose.expose_id).versioned
    ]
    found = project(sources, targets, {expose.expose_id: expose.kinds for expose in exposes})
    if declined is not None:
        why = (
            f"the bundle written with these projections is refused, so none is written: {declined}"
        )
        return Projected(notes=[(f"{emit}: projection", why) for emit in sorted(found.emits)])
    freed = release_counts(found, histories.entries, keys.expose, published)
    for emit, reason in found.emits.items():
        index[emit].projected = reason
        if emit in found.carried:
            _release_carried(index[emit], *found.carried[emit])
    for emit, column in freed:
        follow_history(index[emit], column, found.carried[emit][0])
    for entry in (*found.events, *found.periods):
        emit = str(entry["emit"])
        span_primary_key(index[emit].columns, keys.expose(emit))
    return found


#: The keys of a column's declaration that ``verify`` holds the rows to, as against the ones the draw reads.
_CHECKED_DECLARATION = ("range", "support", "maxCardinality")


def _release_carried(expose: AuthoredExpose, history: str, names: Sequence[str]) -> None:
    """Declare no column of a projected emit that holds the history's values, and score none.

    The run gives such a column the value of a version of *history*, so neither its own
    statistics nor the range or levels they would declare describe it: a value of the history
    outside them would fail the check (``FLX-5005``). The projection's own checks hold the
    column to the history.
    """
    held = set(names)
    expose.carries = history
    for plan in expose.columns:
        if plan.name not in held:
            continue
        for key in _CHECKED_DECLARATION:
            plan.declaration.pop(key, None)
        plan.labels = [
            label for label in plan.labels if label.tokens[0] not in _CHECKED_DECLARATION
        ]
        plan.properties = []
    expose.properties = [
        prop for prop in expose.properties if not _property_on(prop.document, held)
    ]


def _property_on(document: Mapping[str, Any], names: Set[str]) -> bool:
    """Whether a property reads a column in *names*."""
    columns = document.get("columns") or [document.get("column")]
    return any(name in names for name in columns)


def _histories(
    exposes: Sequence[AuthoredExpose],
    keys: Optional[ContractKeys],
    contract: Mapping[str, Any],
    tables: Mapping[str, Any],
    window: Tuple[str, str],
    *,
    minimum_cell: int,
    fractional_bits: int,
    published: Published,
    declined: Optional[str] = None,
) -> _Histories:
    """Write the ``history`` entry of each written expose whose profile table publishes one.

    ADR 0017 section 13: :func:`flux_engine.author.history.write_history` reads the table's
    history. An expose it writes an entry for also gives each date column that declares no range
    the window as its range, which a history's emit needs (ADR 0017 section 11). With *declined*,
    the reason plan refuses the bundle that holds them, no entry is written and each statistic is
    unused for that reason.
    """
    result = _Histories([], [], [], [])
    reads = HistoryReads(exposes, published)
    for expose in exposes:
        table = tables.get(expose.expose_id)
        if expose.reference or not isinstance(table, Mapping) or "history" not in table:
            continue
        plans = {plan.name: plan for plan in expose.columns}
        held = keys.expose(expose.expose_id) if keys is not None else None
        source = HistorySource(
            expose_id=expose.expose_id,
            table=table,
            rows=expose.rows,
            plans=plans,
            kinds=expose.kinds,
            contract_expose=contract["exposes"][expose.index],
            keys=held,
            window=window,
            minimum_cell=minimum_cell,
            fractional_bits=fractional_bits,
            set_by_rules=frozenset(expose.derives) | frozenset(expose.copies) | expose.dependents,
            parents=reads.candidates(expose.expose_id, held),
            catalogues=reads.catalogues(held),
        )
        written = write_history(source) if declined is None else decline_history(source, declined)
        if written.entry is not None:
            result.entries.append(written.entry)
            restate_assumptions(expose)
            reads.release(expose.expose_id, written)
            declare_date_ranges(plans, expose.kinds, window, written.exempt)
        result.used.extend(written.used)
        result.unused.extend(written.unused)
        result.notes.extend(
            [(f"{expose.expose_id}: {subject}", text) for subject, text in written.notes]
        )
    return result


#: Why no counts entry lays out a projected emit's relationships.
_HELD_PROJECTED = "the emit is projected from the declared history, whose layout places its rows"

#: Why no counts entry lays out the relationship a declared history's parent uses.
_HELD_PARENT = "the declared history's parent lays its entities out over this relationship"

#: Why a projected emit's published rowCount is not read, when a history file declares it.
_DECLARED_ROWS = (
    "the emit is projected from the declared history, so its rows follow from the history and "
    "the published rowCount is not read"
)

#: Why no statistic of a table's published history is used when a history file declares them.
_DECLARED_UNUSED = "--history declares the bundle's histories, so none is written from the profile"

_ENDED_STATUS = "the ended status sits on the last version only, so a first version never draws it"


def _held_back(declared: DeclaredHistory) -> Callable[[str, str], Optional[str]]:
    """The reason no counts entry lays out a relationship, by its child emit and its name, or ``None``.

    A projected emit's rows follow from its history, and a declared history's parent lays the
    entities out over its relationship, so a counted layout over either would be refused
    (``FLX-3403``).
    """
    projected = projected_emits(declared)
    parents = parent_relationships(declared)

    def reason(emit: str, relationship: str) -> Optional[str]:
        if emit in projected:
            return _HELD_PROJECTED
        if (emit, relationship) in parents:
            return _HELD_PARENT
        return None

    return reason


def _declared_exempt(
    expose: AuthoredExpose,
    entry: Mapping[str, Any],
    keys: ContractKeys,
    contract: Mapping[str, Any],
) -> FrozenSet[str]:
    """The columns of a declared history's emit that no date range is given to.

    The validity start and end, the natural entity and every key column, the ``activation``, the
    tracked and the end columns, the parent relationship's foreign key, and the columns a rule, a
    dependency or a copy sets.
    """
    held = keys.expose(expose.expose_id)
    exempt: Set[str] = {
        name
        for name in window_ends(contract["exposes"][expose.index].get("semantics")).values()
        if isinstance(name, str)
    }
    exempt.update(item.column for item in held.entities)
    for key in (*held.keys, *held.instant_keys):
        exempt.update(key)
    if isinstance(entry.get("activation"), str):
        exempt.add(str(entry["activation"]))
    for tracked in entry.get("tracked") or ():
        if isinstance(tracked, Mapping) and isinstance(tracked.get("column"), str):
            exempt.add(str(tracked["column"]))
    end = entry.get("end")
    if isinstance(end, Mapping) and isinstance(end.get("column"), str):
        exempt.add(str(end["column"]))
    parent = entry.get("parent")
    if isinstance(parent, Mapping):
        for relationship in held.relationships:
            if relationship.name == parent.get("relationship"):
                exempt.update(relationship.from_columns)
    return (
        frozenset(exempt) | frozenset(expose.derives) | frozenset(expose.copies) | expose.dependents
    )


def _ended_status(expose: AuthoredExpose, entry: Mapping[str, Any]) -> None:
    """Give the ended status no weight in a carried end column, so no first version draws it.

    A history that ends an entity with ``finalVersion`` sets the value on its last version; the
    plan refuses a column that also draws it elsewhere (``FLX-3403``). On an end column the entry
    does not track, the level's weight in the column's declaration becomes 0 and the other levels
    are renormalised. A column with no such level is left to the plan's own refusal.
    """
    end = entry.get("end")
    if not isinstance(end, Mapping):
        return
    column, value = end.get("column"), end.get("value")
    if not isinstance(column, str) or not isinstance(value, str):
        return
    tracked = {
        str(item.get("column")) for item in entry.get("tracked") or () if isinstance(item, Mapping)
    }
    plan = next((item for item in expose.columns if item.name == column), None)
    levels = plan.declaration.get("levels") if plan is not None else None
    if column in tracked or plan is None or not isinstance(levels, dict) or value not in levels:
        return
    weights = {str(level): Fraction(Decimal(str(text))) for level, text in levels.items()}
    rest = [level for level in weights if level != value]
    if weights[value] == 0 or not rest or sum((weights[level] for level in rest), Fraction(0)) <= 0:
        return
    shares = dict(zip(rest, share_texts([weights[level] for level in rest]), strict=True))
    plan.declaration["levels"] = {level: shares.get(level, "0") for level in weights}
    plan.labels = [label for label in plan.labels if label.tokens[0] != "levels"] + [
        Label(("levels", level), assumed(_ENDED_STATUS)) for level in weights
    ]
    plan.assumptions.append(_ENDED_STATUS)
    plan.choices.append(_ENDED_STATUS)


_PARENT_REASON = (
    "A history's parent key: the declared history lays its entities out over the relationship, "
    "from the children per parent it declares, so the foreign key declares no draw of its own "
    "(ADR 0017 section 3)."
)


def _parent_key(expose: AuthoredExpose, entry: Mapping[str, Any], keys: ContractKeys) -> None:
    """Take the foreign key of a declared history's ``parent`` off every draw of its own.

    The history sets the key from its parent's rows, so the plan refuses a key that also declares a
    draw (``FLX-3403``), as it does one the counted layout sets.
    """
    parent = entry.get("parent")
    if not isinstance(parent, Mapping):
        return
    for relationship in keys.expose(expose.expose_id).relationships:
        if relationship.name != parent.get("relationship") or len(relationship.from_columns) != 1:
            continue
        for plan in expose.columns:
            if plan.name == relationship.from_columns[0]:
                declare_counted(
                    plan,
                    None,
                    reason=_PARENT_REASON,
                    how="a foreign key, set by the declared history's parent layout",
                    sets=(
                        "the declared history's parent layout sets the foreign key from the "
                        "parent rows, so this statistic of its own values adds no bound to it"
                    ),
                )


def _declared_histories(
    exposes: Sequence[AuthoredExpose],
    keys: ContractKeys,
    contract: Mapping[str, Any],
    tables: Mapping[str, Any],
    window: Tuple[str, str],
    declared: DeclaredHistory,
) -> _Histories:
    """The ``history`` entries of a history file, as given, with the bundle written to fit them.

    Each declared history's emit declares the window as the range of every date column that has
    none, as :func:`_histories` does, and an ended status loses its weight where it is carried.
    Every statistic a profile table publishes of its history is unused, since the file declares
    them.
    """
    entries = declared.history_entries()
    result = _Histories(
        entries,
        [],
        [],
        [
            (f"{entry['emit']}: history", f"declared by --history {declared.name}")
            for entry in entries
        ],
    )
    for expose in exposes:
        table = tables.get(expose.expose_id)
        if expose.reference or not isinstance(table, Mapping) or "history" not in table:
            continue
        pointers = declared_pointers(("tables", expose.expose_id, "history"), table["history"])
        result.unused.extend((pointer, _DECLARED_UNUSED) for pointer in pointers)
    by_emit = {expose.expose_id: expose for expose in exposes}
    for entry in entries:
        expose = by_emit[str(entry["emit"])]
        plans = {plan.name: plan for plan in expose.columns}
        declare_date_ranges(
            plans, expose.kinds, window, _declared_exempt(expose, entry, keys, contract)
        )
        _ended_status(expose, entry)
        _parent_key(expose, entry, keys)
    return result


def _declared_projections(
    exposes: Sequence[AuthoredExpose],
    keys: ContractKeys,
    contract: Mapping[str, Any],
    declared: DeclaredHistory,
) -> Projected:
    """The ``snapshots``, ``events`` and ``periods`` entries of a history file, as given.

    As :func:`_projections` does for the ones it infers: each projected emit declares no ``rows``,
    holds no statistic of its own on a column that carries its history's values, and an event or
    periodic emit's primary key spans the bound of a committed state.
    """
    index = {expose.expose_id: expose for expose in exposes}

    def columns_of(emit: str) -> List[str]:
        return [
            column.name for column in compile_expose(contract["exposes"][index[emit].index]).columns
        ]

    found = Projected(
        snapshots=declared.projection_entries("snapshots"),
        events=declared.projection_entries("events"),
        periods=declared.projection_entries("periods"),
    )
    for entry in found.snapshots:
        history = set(columns_of(str(entry["of"])))
        names = tuple(name for name in columns_of(str(entry["emit"])) if name in history)
        found.carried[str(entry["emit"])] = (str(entry["of"]), names)
    for entry in found.events:
        history = set(columns_of(str(entry["of"])))
        mapped = [entry.get(side) for side in ("before", "after")]
        named = {
            str(name) for mapping in mapped if isinstance(mapping, Mapping) for name in mapping
        }
        named.update(
            name
            for name in columns_of(str(entry["emit"]))
            if name in history and name not in (entry.get("op"), entry.get("date"))
        )
        found.carried[str(entry["emit"])] = (str(entry["of"]), tuple(sorted(named)))
    for entry in found.periods:
        history = set(columns_of(str(entry["of"])))
        names = tuple(
            name
            for name in columns_of(str(entry["emit"]))
            if name in history and name != entry.get("column")
        )
        found.carried[str(entry["emit"])] = (str(entry["of"]), names)
    for kind in ("snapshots", "events", "periods"):
        for entry in getattr(found, kind):
            found.notes.append(
                (f"{entry['emit']}: {kind}", f"declared by --history {declared.name}")
            )
    for emit, carried in found.carried.items():
        found.emits[emit] = _DECLARED_ROWS
        index[emit].projected = _DECLARED_ROWS
        _release_carried(index[emit], *carried)
    for entry in (*found.events, *found.periods):
        emit = str(entry["emit"])
        span_primary_key(index[emit].columns, keys.expose(emit))
    return found


def _follows_the_entity(exposes: Sequence[AuthoredExpose], histories: _Histories) -> Dict[str, str]:
    """The emits whose values follow an entity, not the row, each with why no allocation is exact.

    A history's emit takes its tracked columns from the matrix and the rest from the entity's first
    version row, and a projected emit takes its rows from the history, so a per-row allotment cannot
    hold on either (ADR 0017 section 6).
    """
    found = {
        str(entry["emit"]): (
            "a history's columns follow the entity, a tracked one along its matrix and the rest "
            "from its first version, so no column takes allocation: exact (ADR 0017 section 6)"
        )
        for entry in histories.entries
    }
    for expose in exposes:
        if expose.projected is not None:
            found[expose.expose_id] = (
                "its rows follow from the history, so no column takes allocation: exact"
            )
    return found


def _structural_note(
    classes: Sequence[str], counted: bool, why_not: Optional[str]
) -> Tuple[bool, str]:
    """How a relationship's ``structural`` classes were read: whether they were, and one sentence.

    ``orphan`` is checked wherever the relationship is read, since FLUX builds every foreign key
    so that it finds its parent (KERNEL-SPEC section 8.1). ``childless`` is written as
    ``minChildren: 1`` by the counted layout and by nothing else.
    """
    parts: List[str] = []
    if "orphan" in classes:
        parts.append(
            "orphan: checked, since the bundle builds every foreign key so that it finds its parent"
        )
    if "childless" in classes:
        parts.append(
            "childless: written as minChildren 1 on the counts entry"
            if counted
            else f"childless: not written, because the relationship is not laid out by counts ({why_not})"
        )
    held = "orphan" in classes or counted
    return held, "; ".join(parts)


def _counting_hold(
    keys: Optional[ContractKeys],
    expose: AuthoredExpose,
    plan: ColumnPlan,
    relationship: Any,
) -> Optional[str]:
    """Why the counted layout cannot place *relationship* of *expose*, or ``None`` when it can.

    The checks of ``flux_engine.generator.relations._check_counts``, read from what init has
    written: a key relationship, one implied by another, one that points at its
    own expose, a foreign key that declares a ``nullFraction``, is drawn given a column or has
    one drawn given it, or is set by a contract rule. ``init`` writes no ``links`` entry and no
    bridge, so those hold nothing here. A foreign key's own levels, bins and range are not a
    reason: the counted layout replaces them.
    """
    expose_keys = keys.expose(expose.expose_id) if keys is not None else None
    sets = _key_sets(expose_keys)
    name = plan.name
    keyed = _keyed_columns(keys, expose, expose_keys)
    if relationship.target == expose.expose_id:
        return "it points at its own expose"
    if name in keyed:
        return "its column is part of a key of the expose"
    if any(
        other is not relationship and name in other.from_columns for other in sets.relationships
    ):
        return "another relationship of the expose sets its column"
    if name in expose.derives or name in expose.dependents or name in expose.copies:
        return "a rule of the contract sets its column"
    declaration = plan.declaration
    nulls = declaration.get("nullFraction")
    if nulls is not None and Decimal(str(nulls)) != 0:
        return "its column declares a nullFraction, and the layout gives every child a parent"
    if any(key in declaration for key in ("given", "binWeightsGiven", "levelWeightsGiven")):
        return "its column is drawn given another"
    return _drawn_given(expose, name)


def _keyed_columns(
    keys: Optional[ContractKeys], expose: AuthoredExpose, expose_keys: Any
) -> Set[str]:
    """The columns that are part of a key of *expose*, but for a one_to_one column the layout keeps."""
    # A one_to_one relationship makes its own column a key, which the layout keeps unique by
    # giving no parent a second child; a key of the expose that anything else declares stays one.
    if expose_keys is None or keys is None:
        return set()
    every = expose_keys.keys + expose_keys.instant_keys
    own = {
        other.from_columns
        for other in expose_keys.relationships
        if other.cardinality == "one_to_one"
    }
    own -= {
        (entity.column,) for entity in expose_keys.entities if entity.kind in ("primary", "unique")
    }
    own -= {
        other.to_columns
        for declared in keys.exposes.values()
        for other in declared.relationships
        if other.target == expose.expose_id
    }
    return {column for key in every if key not in own for column in key}


def _drawn_given(expose: AuthoredExpose, name: str) -> Optional[str]:
    """Why column *name* is drawn given another column, or ``None``."""
    for other in expose.columns:
        given = other.declaration.get("given")
        names = [given] if isinstance(given, str) else given if isinstance(given, list) else []
        if name in names:
            return f"column {other.name!r} is drawn given it"
    return None


def _relationships_of(keys: ContractKeys, expose_id: str) -> Sequence[Any]:
    try:
        return keys.expose(expose_id).relationships
    except (KeyError, LookupError):
        return ()


def _unused_block(ledger: Ledger) -> List[Dict[str, Any]]:
    """The register's ``unused`` list: each unused statistic with its definition and reason.

    Every statistic the ledger holds as unused is listed, a rank correlation and a binned clique
    included: the register holds their definitions.
    """
    block: List[Dict[str, Any]] = []
    for entry in ledger.unused_entries():
        statistic: Dict[str, Any] = {"pointer": entry.pointer}
        if entry.definition is not None:
            statistic["definition"] = entry.definition
        block.append({"statistic": statistic, "reason": entry.note})
    return block


def _expose_gaps(expose: AuthoredExpose) -> List[Gap]:
    """The assumptions of one written expose that stand in for what the profile did not publish."""
    base = ("extensions", EXTENSION_NAMESPACE, "emits", expose.expose_id)
    choices = {plan.name: set(plan.choices) for plan in expose.columns}
    found: List[Gap] = []
    for subject, sentence in expose.assumptions:
        if subject == "rows" and expose.projected is not None:
            continue
        if subject == "rows" and sentence == expose.rows_note:
            found.append(Gap(render_pointer(base + ("rows",)), sentence))
        elif subject == "copula":
            found.append(Gap(render_pointer(base + ("fidelity", COPULA_KEY)), sentence))
        elif sentence not in choices.get(subject, set()):
            found.append(Gap(render_pointer(base + ("fidelity", "columns", subject)), sentence))
    return found


def _gaps(
    exposes: Sequence[AuthoredExpose], window_note: Optional[Tuple[str, str]]
) -> Tuple[Gap, ...]:
    """The assumptions that stand in for what the profile did not publish, by position.

    Every assumption a written expose states is one, but for the choices a
    column names (how to meet what the profile publishes, or where the contract
    overrules it). Each is placed at the emit's ``rows`` or at the column's
    block in the Simulation, so one ``--allow`` can accept a column's gaps.
    """
    found: List[Gap] = []
    for expose in exposes:
        if expose.skipped is None:
            found.extend(_expose_gaps(expose))
    if window_note is not None:
        found.append(Gap(render_pointer(("spec", "timeBounds")), window_note[1]))
    return tuple(found)


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


class _HistoryRefused(AuthorError):
    """The bundle is refused once its histories are written: it is written again without them."""


class _DeclaredRefused(AuthorError):
    """The bundle written with a ``--history`` file is refused; it is reported, never written without it."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class _ProjectionRefused(AuthorError):
    """The bundle is refused once its projections are written: it is written again without them."""


class _RangesWidened(Exception):
    """The plan refuses an enforced ``within`` date its declared range can leave without a value.

    ``ranges`` holds, per ``(exposeId, column)``, the range in milliseconds that the plan's own
    check accepts, so the bundle is written again with it (ADR 0024 section 5).
    """

    def __init__(self, ranges: Mapping[Tuple[str, str], Tuple[int, int]]) -> None:
        super().__init__("a declared date range is widened to the parent's lifetime")
        self.ranges = dict(ranges)


class _WithinDeclined(Exception):
    """A child date's entry cannot be written: no widening makes the plan accept it, or it leaves no property to compare.

    Either no range repairs the plan's check of the entry (the parent's latest start is not
    declared), or a widened range leaves the expose no property ``verify`` can compare.

    ``reasons`` holds, per ``(exposeId, column)`` of the child date, why no ``within`` entry dates
    it, so the bundle is written again without the entry and without the widened range.
    """

    def __init__(self, reasons: Mapping[Tuple[str, str], str]) -> None:
        super().__init__("a child date whose range cannot be widened is not dated by an entry")
        self.reasons = dict(reasons)


class _ExposuresDeclined(Exception):
    """A trial run scores a declared exposure or rates entry outside its tolerance: the bundle is written again without it.

    ``reasons`` holds, per ``(exposeId, relationship)`` of the ``within`` entry that would carry the
    exposure, why the bundle does not declare it. ``rates`` holds, per ``(exposeId, column)`` of a
    ``rates`` entry, why the bundle does not declare it. Both are declined together, so the bundle
    is written again once for every item one trial run shows failing.
    """

    def __init__(
        self,
        reasons: Mapping[Tuple[str, str], str],
        rates: Optional[Mapping[Tuple[str, str], str]] = None,
    ) -> None:
        super().__init__("an exposure or a rates entry the run cannot reproduce is not declared")
        self.reasons = dict(reasons)
        self.rates = dict(rates or {})


#: The most times a bundle is written again with wider ranges: each pass widens a range or ends.
_MOST_WIDENINGS = 8

#: The most rows a trial run draws. A bundle of more is tried at a scale that brings it to this many.
_MOST_TRIAL_ROWS = 6000

#: The most rows the plan of a trial run may hold, derived and hidden emits included. A trial whose
#: plan holds more is not run, because the rows a history projection derives are not set by the
#: scale of the emits that declare ``rows``, and a profile can make them very many.
_MOST_TRIAL_PLANNED_ROWS = 60000

_WIDENED_RANGE = (
    "declared range widened to the parent's lifetime so every child date can lie inside it"
)


@dataclass(frozen=True)
class AuthorInputs:
    """What a bundle is authored from: the two documents and the options that shape it.

    *overlay*, *consumers_path*, *window* and *history* are each described at
    :func:`author_bundle`.
    """

    contract_path: Path
    profile_path: Path
    overlay: Optional[Overlay] = None
    consumers_path: Optional[Path] = None
    window: Optional[Tuple[str, str]] = None
    history: Optional[DeclaredHistory] = None


@dataclass(frozen=True)
class _Fallbacks:
    """What the passes of :func:`author_bundle` leave out or widen, each keyed by its exposeId and column."""

    widened: Dict[Tuple[str, str], Tuple[int, int]]
    declined: Dict[Tuple[str, str], str]
    undated: Dict[Tuple[str, str], str]
    unrated: Dict[Tuple[str, str], str]


def author_bundle(inputs: AuthorInputs) -> AuthoredBundle:
    """Read the contract and the profile of *inputs*, and return the checked files of a bundle.

    With an *overlay* (ADR 0030 section 7) the bundle is written from the overlaid profile,
    ``MergePatch(profile, patch)``, and its Simulation declares the overlay beside the pin of the
    profile as published. Each number that rests on a statistic the patch changed cites the
    overlay in the register (``source: overlay``) and no other number does.

    With *consumers_path*, the consumers' questions it holds are embedded in the Simulation and
    their levels are declared (:mod:`flux_engine.author.consumers`, ADR 0028 section 5).

    With *window*, two RFC 3339 texts, the Simulation's ``spec.timeBounds`` is exactly it and a
    date column's bins that leave it are cut at it. Without it the window is the hull of every
    date column's range, so no column's bins are cut.

    With *history* (``--history``, :func:`flux_engine.author.declared_history.read_history_file`),
    the bundle holds the file's histories, projections and ``asOf`` in place of any init would
    infer, with its ``timeBounds`` as the window when *window* gives none; its counted layouts, its
    exact allotments and the weight of an ended status are written to fit them. A refusal of the
    plan is raised as it is, never answered by writing the bundle without the history.

    An enforced ``within`` date whose declared range the plan would refuse (``FLX-6204``) is
    written with the range widened just enough to meet the plan's check, and the column's used
    note says so.

    Nothing is written to the caller's disk here; the bundle is checked in a
    scratch directory. Raises :class:`AuthorError` when an input is unusable or
    no expose of the contract can be generated, and :class:`FluxError` from the
    readers it shares with the engine (an unreadable profile is ``FLX-3109``, a
    document that is not a profile ``FLX-5036``, a contract past the read's
    bounds ``FLX-3105``, and an integer in it too long to read ``FLX-1003``).
    """
    fallbacks = _Fallbacks({}, {}, {}, {})
    last_was_trial = False
    for _ in range(_MOST_WIDENINGS):
        try:
            return _author_with_fallbacks(inputs, fallbacks)
        except _WithinDeclined as fewer_dates:
            last_was_trial = True
            _leave_undated(fallbacks.undated, fallbacks.widened, fewer_dates.reasons)
        except _ExposuresDeclined as fewer:
            last_was_trial = True
            fallbacks.declined.update(fewer.reasons)
            fallbacks.unrated.update(fewer.rates)
        except _RangesWidened as more:
            last_was_trial = False
            _widen_to_hold(fallbacks.widened, more.ranges)
    if last_was_trial:
        raise AuthorError(
            "the trial runs did not settle which exposures, rates entries or within entries the "
            f"run reproduces within {_MOST_WIDENINGS} passes"
        )
    raise AuthorError("the declared date ranges cannot be widened to meet the plan's check")


def _leave_undated(
    undated: Dict[Tuple[str, str], str],
    widened: Dict[Tuple[str, str], Tuple[int, int]],
    reasons: Mapping[Tuple[str, str], str],
) -> None:
    """Leave each column in *reasons* without its ``within`` date, and drop any widening kept for it."""
    undated.update(reasons)
    for key in reasons:
        widened.pop(key, None)


def _widen_to_hold(
    widened: Dict[Tuple[str, str], Tuple[int, int]],
    asked: Mapping[Tuple[str, str], Tuple[int, int]],
) -> None:
    """Widen the ranges kept in *widened* so that each one also holds the range *asked* for its column."""
    for key, (low, high) in asked.items():
        held = widened.get(key)
        widened[key] = (low, high) if held is None else (min(held[0], low), max(held[1], high))


def _author_with_fallbacks(inputs: AuthorInputs, fallbacks: _Fallbacks) -> AuthoredBundle:
    """:func:`author_bundle` once, writing without the projections or the history the plan refuses."""
    if inputs.history is not None or inputs.window is not None:
        # The history file and the window are the person's: a refusal is theirs to read, not a
        # reason to drop the layers.
        return _author_bundle(inputs, fallbacks)
    try:
        return _author_bundle(inputs, fallbacks)
    except _ProjectionRefused as refused:
        projection = str(refused)
    except _HistoryRefused as refused:
        # The bundle holds no projection, so the history is the layer the plan refuses.
        return _author_bundle(inputs, fallbacks, str(refused))
    try:
        return _author_bundle(inputs, fallbacks, None, projection)
    except _HistoryRefused as refused:
        return _author_bundle(inputs, fallbacks, str(refused), projection)


@dataclass(frozen=True)
class _Reading:
    """What the pinned profile says, read once for the whole bundle."""

    pinned: Any
    scoped: _Scoped
    profile: Mapping[str, Any]
    noisy: Any
    tables: Mapping[str, Any]
    references: Mapping[str, Any]
    minimum_cell: int
    fractional_bits: int

    @property
    def scored(self) -> Any:
        """The profile the numbers are read from: the pinned one, or what the overlay makes of it."""
        return self.scoped.profile

    @classmethod
    def of(cls, pinned: Any, scoped: _Scoped) -> "_Reading":
        # A noisy statistic is read as its point estimate, at its own place in the scored
        # document, so the rules below read it as they read an exact one
        # (docs/spec/PROFILE-SPEC.md section 3a).
        profile, noisy = point_estimates(scoped.document)
        found = profile.get("tables")
        published_rows = profile.get("references")
        encoding = profile.get("encoding")
        bits = encoding.get("fractionalBits") if isinstance(encoding, Mapping) else None
        return cls(
            pinned=pinned,
            scoped=scoped,
            profile=profile,
            noisy=noisy,
            tables=found if isinstance(found, Mapping) else {},
            references=published_rows if isinstance(published_rows, Mapping) else {},
            minimum_cell=_minimum_cell(profile),
            fractional_bits=bits if isinstance(bits, int) and not isinstance(bits, bool) else 16,
        )


def _plan_exposes(
    contract: Mapping[str, Any], reading: _Reading, keys: Optional[ContractKeys], needs: Any
) -> List[AuthoredExpose]:
    """One :class:`AuthoredExpose` for each named expose of the contract, written or skipped."""
    exposes: List[AuthoredExpose] = []
    for expose_index, expose in enumerate(contract["exposes"]):
        if not isinstance(expose, Mapping) or not isinstance(expose.get("exposeId"), str):
            continue
        exposes.append(_plan_one_expose(contract, reading, keys, expose_index, expose, needs))
    return exposes


def _plan_one_expose(
    contract: Mapping[str, Any],
    reading: _Reading,
    keys: Optional[ContractKeys],
    expose_index: int,
    expose: Mapping[str, Any],
    needs: Any,
) -> AuthoredExpose:
    expose_id = expose["exposeId"]
    try:
        contract_columns = compile_expose(expose).column_names
    except FluxError:
        contract_columns = ()  # `_plan_expose` says why the expose cannot be written
    whole, whole_refused = reference_table(reading.references, expose_id, contract_columns)
    declined = _declined_reference(expose, expose_id) if whole is not None else None
    if whole is not None and declined is None:
        return _reference_expose(expose_index, expose, whole)
    table = reading.tables.get(expose_id)
    if not isinstance(table, Mapping):
        return AuthoredExpose(
            expose_id=expose_id,
            skipped=whole_refused or "the profile has no table for it",
            declined_reference=declined,
        )
    planned = _plan_guarded(contract, reading, keys, expose_index, expose, table, needs)
    planned.declined_reference = declined
    return planned


def _plan_guarded(
    contract: Mapping[str, Any],
    reading: _Reading,
    keys: Optional[ContractKeys],
    expose_index: int,
    expose: Mapping[str, Any],
    table: Mapping[str, Any],
    needs: Any,
) -> AuthoredExpose:
    expose_id = expose["exposeId"]
    scored_tables = reading.scored.tables
    try:
        return _plan_expose(
            contract,
            expose_index,
            expose,
            table,
            keys.expose(expose_id) if keys is not None else None,
            minimum_cell=reading.minimum_cell,
            fractional_bits=reading.fractional_bits,
            rounding_base=_rounding_base(reading.profile),
            noisy=reading.noisy,
            cliques=_cliques_of(scored_tables.get(expose_id)),
            asked=_asked(needs, expose_id, reading.scored),
        )
    except FluxError as error:
        return AuthoredExpose(expose_id=expose_id, skipped=printable(str(error)))
    except UnspellableOneValue as error:
        return AuthoredExpose(expose_id=expose_id, skipped=str(error), needed_refusal=str(error))
    except (ArithmeticError, ValueError) as error:
        return AuthoredExpose(expose_id=expose_id, skipped=_beyond_note(error))


def _cliques_of(table: Any) -> Tuple[CliqueEntry, ...]:
    """The cliques of a profile's *table* of either definition, in the order the document lists them."""
    if table is None:
        return ()
    return tuple(sorted((*table.cliques, *table.cliques2), key=lambda clique: clique.index))


def _draw_and_link(
    reading: _Reading,
    keys: Optional[ContractKeys],
    exposes: Sequence[AuthoredExpose],
    drawn: Sequence[AuthoredExpose],
    held_back: Optional[Callable[[str, str], Optional[str]]] = None,
) -> _Spreads:
    """Span the keys of the drawn exposes, lay their relationships out and link their givens."""
    try:
        _span_keys(drawn, keys)
        for expose in drawn:
            for plan in expose.columns:
                recheck_key(expose.contexts[plan.name], plan)
        for expose in drawn:
            _determine(expose)
            _follow_dependencies(expose)
        written = [expose for expose in exposes if expose.skipped is None]

        def declare_links() -> None:
            catalogue_foreign_keys(written, keys, reading.references, reading.profile)
            link_givens(
                written,
                keys,
                reading.profile,
                reading.references,
                minimum_cell=reading.minimum_cell,
            )

        spread = _spread_relationships(
            drawn,
            keys,
            reading.profile,
            minimum_cell=reading.minimum_cell,
            fractional_bits=reading.fractional_bits,
            held_back=held_back,
            declare_links=declare_links,
        )
    except (ArithmeticError, ValueError) as error:
        raise AuthorError(_beyond_note(error)) from error
    return spread


def _authored_exposes(
    contract_path: Path,
    reading: _Reading,
    exposes: Sequence[AuthoredExpose],
    drawn: Sequence[AuthoredExpose],
) -> List[AuthoredExpose]:
    """Finish each drawn expose; the exposes that are written, or the refusal when none is."""
    for expose in drawn:
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
        in_profile = ", ".join(sorted(printable(str(name)) for name in reading.tables)) or "none"
        raise AuthorError(
            f"no expose of {printable(contract_path.name)} can be written from this profile "
            f"({reasons}). "
            f"The profile describes: {in_profile}"
        )
    return authored


def _namespace_block(
    contract: Mapping[str, Any],
    reading: _Reading,
    keys: Optional[ContractKeys],
    authored: Sequence[AuthoredExpose],
    spread: _Spreads,
    histories: _Histories,
    projections: Projected,
    needs: Any,
    dated: Any,
    declared_asof: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The namespace block of the simulation, before its register and unused statistics.

    *declared_asof* is the ``asOf`` list a history file gives, which is written in place of the
    entries init would find.
    """
    encoding = reading.profile.get("encoding") or {}
    namespace: Dict[str, Any] = {
        "profile": {"digest": reading.pinned.digest.value},
        **reading.scoped.member,
        "emits": {
            expose.expose_id: (
                {"reference": True}
                if expose.reference
                else {
                    **({} if expose.projected is not None else {"rows": expose.rows}),
                    "fidelity": _fidelity_block(expose, encoding),
                }
            )
            for expose in authored
        },
    }
    namespace.update(_consumers_block(needs))
    if _asks_for_copies(contract):
        namespace[COPIES_KEY] = True
    if spread.entries:
        namespace[COUNTS_KEY] = sorted(
            spread.entries, key=lambda item: (item["emit"], item["relationship"])
        )
    if dated.lifetimes:
        namespace[LIFETIMES_KEY] = dated.lifetimes
    if dated.within:
        namespace[WITHIN_KEY] = dated.within
    if dated.rates:
        namespace[RATES_KEY] = dated.rates
    histories.declare_in(namespace)
    projections.declare_in(namespace)
    asof = (
        declared_asof
        if declared_asof is not None
        else _asof_entries(authored, keys, contract, histories)
    )
    if asof:
        namespace[ASOF_KEY] = asof
    return namespace


def _contract_digest(contract: Mapping[str, Any]) -> str:
    """The digest the Simulation's provenance and the profile request name the contract by."""
    return digest_of_document(contract, subject=f"contractDigest:{contract['id']}").value


def _simulation_document(
    contract: Mapping[str, Any],
    authored: Sequence[AuthoredExpose],
    namespace: Mapping[str, Any],
    window: Tuple[str, str],
    team: str,
) -> Dict[str, Any]:
    """The Simulation document: one generator emit per written expose, with the namespace."""
    product_ref = str(contract["id"])
    contract_digest = _contract_digest(contract)
    world_id = f"world-{product_ref}"
    persona_id = f"persona-{product_ref}"
    title = str(contract.get("name") or product_ref)
    simulation = _document(
        "Simulation",
        f"sim-{product_ref}",
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
    return simulation


def _world_and_persona(
    contract: Mapping[str, Any], team: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The least World and Persona a run needs."""
    product_ref = str(contract["id"])
    title = str(contract.get("name") or product_ref)
    world = _document(
        "World",
        f"world-{product_ref}",
        f"World for {title}",
        team,
        {
            "seed": SEED,
            "vertical": "telco",
            "population": {"size": 1, "lifecycleMix": {"active": 1}},
        },
    )
    persona = _document(
        "Persona",
        f"persona-{product_ref}",
        f"Persona for {title}",
        team,
        {"traits": {"digitalFluency": 0.5}},
    )
    return world, persona


def _basis_of(profile: Mapping[str, Any], tokens: Optional[Tuple[str, ...]], how: str) -> Basis:
    """The source of a number read from the statistic at *tokens* of *profile*, or assumed.

    A statistic that lands in a registered definition is cited. A member of the profile with none
    (an order, a structural class), and a number init chose itself (*tokens* ``None``), are assumed,
    for the reason *how* gives.
    """
    definition = cited_definition(profile, tokens) if tokens is not None else None
    if tokens is None or definition is None:
        return assumed(how)
    return from_profile(tokens, definition)


#: Why a number of a history the ``--history`` file declares is assumed: no statistic of the profile gave it.
_DECLARED_BY_HISTORY = "declared by --history"


def _history_cited(entry: Mapping[str, Any]) -> List[_Cited]:
    """What each number of a ``history`` entry rests on: the history statistic it was read from."""
    base = ("tables", str(entry["emit"]), "history")
    found: List[_Cited] = []
    for member, statistic, how in (
        (
            ("entities",),
            "versionsPerEntity",
            "the entities, from the profile's versions per entity",
        ),
        (("versions",), "versionsPerEntity", "the classes of versions per entity, with weights"),
        (("start",), "firstStart", "the histogram of first starts, as the entry's start"),
        (("length",), "versionLength", "the histogram of version lengths, as the entry's length"),
        (("reasons",), "reasons", "the share of changes each tracked column makes"),
        (("end", "share"), "lastVersion", "the closed share of last versions, as the end share"),
    ):
        if member[0] in entry:
            found.append((member, base + (statistic,), how))
    for index, tracked in enumerate(entry.get("tracked", ())):
        where = base + ("columns", str(tracked["column"]))
        for key, how in (
            ("initial", "the level a first version takes"),
            ("transitions", "the moves between levels of the tracked column, per level"),
        ):
            if key in tracked:
                found.append((("tracked", str(index), key), where + (key,), how))
    return found


def _namespace_labels(
    namespace: Mapping[str, Any],
    spread: _Spreads,
    dated: Any,
    profile: Mapping[str, Any],
    declared: bool = False,
) -> List[Tuple[Tuple[str, ...], Basis]]:
    """The source of the numbers written under the namespace's keys beside ``emits``.

    Each is at a position below the namespace: a ``counts`` entry's numbers rest on the profile's
    children per parent, a ``history`` entry's on the history it publishes, a ``lifetimes``,
    ``within`` or ``rates`` entry's on what the date rules read, and a number init chose itself
    (a periodic row's count) is assumed. The entries are found by identity in the lists the
    namespace holds.
    """
    found: List[Tuple[Tuple[str, ...], Basis]] = []

    def place(key: str, entry: Any, cites: Sequence[_Cited]) -> None:
        listed = namespace.get(key) or ()
        index = next((at for at, item in enumerate(listed) if item is entry), None)
        if index is not None:
            for member, tokens, how in cites:
                found.append(((key, str(index)) + member, _basis_of(profile, tokens, how)))

    for entry, cite in spread.cited:
        place(COUNTS_KEY, entry, (cite,))
    if declared:
        # The numbers under the keys the file declares are its own, not statistics of the
        # profile's history: every counted position under them is assumed, one entry for each.
        found.extend(_declared_labels(namespace, profile))
    else:
        for entry in namespace.get(HISTORY_KEY) or ():
            place(HISTORY_KEY, entry, _history_cited(entry))
    for key, entry, member, tokens, how in _dated_cited(dated):
        place(key, entry, ((member, tokens, how),))
    if not declared:
        for entry in namespace.get(PERIODS_KEY) or ():
            place(
                PERIODS_KEY,
                entry,
                [
                    (
                        ("kinds", str(position), "count"),
                        None,
                        "fluxsim init writes one periodic row for each period a version "
                        "overlaps, so each kind's count is 1 (ADR 0027).",
                    )
                    for position in range(len(entry.get("kinds", ())))
                ],
            )
    return found


#: The keys of the namespace a ``--history`` file declares.
_DECLARED_KEYS = frozenset({HISTORY_KEY, SNAPSHOTS_KEY, EVENTS_KEY, PERIODS_KEY, ASOF_KEY})


def _declared_labels(
    namespace: Mapping[str, Any], profile: Mapping[str, Any]
) -> List[Tuple[Tuple[str, ...], Basis]]:
    """One assumed label for each entry (or key) of the declared keys that holds a counted number."""
    places: List[Tuple[str, ...]] = []
    for path in counted_positions(namespace, _DECLARED_KEYS):
        at = path[:2] if isinstance(namespace.get(path[0]), list) else path[:1]
        if at not in places:
            places.append(at)
    return [(at, _basis_of(profile, None, _DECLARED_BY_HISTORY)) for at in places]


def _dated_cited(
    dated: Any,
) -> Iterator[Tuple[str, Dict[str, Any], Tuple[str, ...], Tuple[str, ...], str]]:
    """Each number the date rules wrote, with the list its entry is in."""
    keys = (
        *((LIFETIMES_KEY, item) for item in dated.lifetimes),
        *((WITHIN_KEY, item) for item in dated.within),
        *((RATES_KEY, item) for item in dated.rates),
    )
    for entry, member, tokens, how in dated.cited:
        key = next((name for name, item in keys if item is entry), None)
        if key is not None:
            yield key, entry, member, tokens, how


def _register_labels(
    world_id: str,
    persona_id: str,
    simulation_id: str,
    authored: Sequence[AuthoredExpose],
    noisy: Any,
    others: Sequence[Tuple[Tuple[str, ...], Basis]] = (),
) -> List[Tuple[str, Tuple[str, ...], Any]]:
    """The label of every number of the documents, for the parameter register.

    *others* are the numbers of the namespace's keys beside ``emits`` (:func:`_namespace_labels`).
    """
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
        if expose.reference:
            continue
        for label in _labels(expose):
            labels.append(
                (simulation_id, namespace_tokens + label.tokens, cite_noisy(label.basis, noisy))
            )
    for tokens, basis in others:
        labels.append((simulation_id, namespace_tokens + tokens, cite_noisy(basis, noisy)))
    return labels


def _starter_files(
    contract_path: Path,
    contract_bytes: bytes,
    world: Mapping[str, Any],
    persona: Mapping[str, Any],
    simulation: Mapping[str, Any],
    scoped: _Scoped,
) -> Tuple[StarterFile, ...]:
    # A file name is any text: printable() keeps a newline in one from ending the comment. The
    # profile's name is left out: the header is bytes of every document, so the digest of the plan
    # and the run id would follow the name the profile happens to be saved under.
    header = _HEADER.format(contract=printable(contract_path.name)) + scoped.header
    return (
        StarterFile(
            _contract_file_name(contract_path),
            "the FLUID contract, copied unchanged",
            contract_bytes,
        ),
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


def _author_bundle(
    inputs: AuthorInputs,
    fallbacks: _Fallbacks,
    declined_history: Optional[str] = None,
    declined_projection: Optional[str] = None,
) -> AuthoredBundle:
    """:func:`author_bundle`, writing no history when *declined_history* is why plan refuses the bundle.

    Each range in ``fallbacks.widened`` (milliseconds, per exposeId and column) is declared in place
    of the one the profile gives. When the plan would refuse a date's range, :class:`_RangesWidened`
    says which ranges it accepts.

    With *declined_projection*, the reason plan refuses it with its projections, none is written.

    A date of ``fallbacks.undated`` (per exposeId and column, with its reason) is dated by no
    ``within`` entry. When no range repairs a date's entry, or a widened range leaves the expose no
    property to compare, :class:`_WithinDeclined` says which dates, so the bundle is written again
    without them.

    An exposure of ``fallbacks.declined`` (per exposeId and relationship, with its reason) and a
    ``rates`` entry of ``fallbacks.unrated`` (per exposeId and column, with its reason) are not
    declared. When a trial run scores a declared exposure, or the calendar of a column a ``rates``
    entry shapes, outside its tolerance, :class:`_ExposuresDeclined` says which, so the bundle is
    written again without them.
    """
    declared = inputs.history
    planned = _plan_bundle(inputs)
    team, owner_note = _owner(planned.contract)
    window, window_note, given_window = _bundle_window(planned, inputs)
    _widen_ranges(planned.authored, fallbacks.widened)
    if given_window is not None:
        _refuse_a_window_that_misses_ranges(planned, window, inputs, declared)
    histories, projections = _histories_and_projections(
        planned, window, declared, (declined_history, declined_projection)
    )
    exact_notes = allot_small_tables(
        [expose for expose in planned.drawn if expose.skipped is None],
        _follows_the_entity(planned.authored, histories),
    )
    dated = _dated_columns(planned, window, fallbacks, histories, projections)
    _release_dated_columns(planned.authored, dated)
    _declare_copulas(planned.authored, planned.reading, dated, histories)
    drafted = _draft_documents(
        planned, inputs, _Layers(window, histories, projections, dated), team
    )
    contract_path = planned.contract_path
    files = _starter_files(
        contract_path,
        planned.contract_bytes,
        drafted.world,
        drafted.persona,
        drafted.simulation,
        planned.scoped,
    )
    _refuse_unless_widened(files, planned.authored, fallbacks.widened)
    _check_histories(
        files,
        planned.pinned,
        histories,
        projections,
        declined_history,
        planned.scoped.citing,
        declared,
        inputs.window is not None,
    )
    _refuse_unless_reproduced(
        files, dated.within, dated.rates, planned.keys, Path(inputs.profile_path)
    )
    spread = planned.spread
    notes = (
        tuple(note for note in (window_note, owner_note) if note)
        + tuple(spread.notes)
        + tuple(histories.notes)
        + tuple(projections.notes)
        + tuple(exact_notes)
    )
    namespace = drafted.namespace
    counts = _counts(
        namespace["parameters"],
        namespace[UNUSED_KEY],
        drafted.documents,
        drafted.simulation_id,
        planned.scoped.name,
    )
    return AuthoredBundle(
        files=files,
        exposes=tuple(planned.exposes),
        counts=counts,
        notes=notes,
        profile_digest=planned.pinned.digest.value,
        simulation_id=drafted.simulation_id,
        ledger=drafted.ledger,
        gaps=_gaps(planned.exposes, None if given_window is not None else window_note),
        overlay=inputs.overlay,
        assumptions_request=drafted.asked,
        consumers=_consumers_report(planned.needs, planned.exposes, window),
        history_file=declared.name if declared is not None else None,
    )


@dataclass(frozen=True)
class _Planned:
    """What an authoring run read and drew before it fixed the window, the histories or the dates."""

    contract_path: Path
    contract: Any
    contract_bytes: bytes
    pinned: Any
    scoped: _Scoped
    reading: _Reading
    keys: Optional[ContractKeys]
    needs: Optional[Any]
    exposes: List[AuthoredExpose]
    drawn: List[AuthoredExpose]
    spread: _Spreads
    authored: List[AuthoredExpose]


class _Layers(NamedTuple):
    """What the bundle's documents are drafted over: the window and each declared layer."""

    window: Tuple[str, str]
    histories: _Histories
    projections: Projected
    dated: Any


class _Drafted(NamedTuple):
    """The documents of a bundle, with its register, ledger and request, before they are checked."""

    world: Dict[str, Any]
    persona: Dict[str, Any]
    simulation: Dict[str, Any]
    namespace: Dict[str, Any]
    documents: Dict[str, Any]
    simulation_id: str
    ledger: Any
    asked: Any


def _plan_bundle(inputs: AuthorInputs) -> _Planned:
    """Read the contract and the profile of *inputs*, plan each expose and draw what links them."""
    contract_path = Path(inputs.contract_path)
    contract, contract_bytes = _read_contract(contract_path)
    pinned = load_profile(Path(inputs.profile_path))
    scoped = _scoped(pinned, inputs.overlay)
    reading = _Reading.of(pinned, scoped)
    try:
        keys = compile_keys(contract)
    except FluxError as error:
        raise AuthorError(f"the contract's keys do not resolve: {error}") from error
    needs = _consumers_plan(inputs.consumers_path, contract, keys)

    exposes = _plan_exposes(contract, reading, keys, needs)
    _skip_unreadable_references(exposes, keys)
    drawn = [expose for expose in exposes if expose.skipped is None and not expose.reference]
    declared = inputs.history
    held_back = _held_back(declared) if declared is not None else None
    spread = _draw_and_link(reading, keys, exposes, drawn, held_back)
    authored = _authored_exposes(contract_path, reading, exposes, drawn)
    _refuse_a_needed_one_value(exposes, keys)
    spread = _without_unwritten(
        spread, {expose.expose_id for expose in authored if not expose.reference}
    )
    return _Planned(
        contract_path,
        contract,
        contract_bytes,
        pinned,
        scoped,
        reading,
        keys,
        needs,
        exposes,
        drawn,
        spread,
        authored,
    )


def _refuse_a_needed_one_value(
    exposes: Sequence[AuthoredExpose], keys: Optional[ContractKeys]
) -> None:
    """Refuse a one-value date when a written expose points at its table, which is then not written.

    A table whose column publishes one date or instant the engine cannot yet write is left out. It
    is refused, with the reason it was left out, only when a relationship of an expose the bundle
    writes targets it: plan would refuse that bundle (``FLX-3403``). Nothing is written then, and
    the refusal is ``FLX-3125``, with the reason the table was left out as its detail.
    """
    left_out = {
        expose.expose_id: expose.needed_refusal
        for expose in exposes
        if expose.needed_refusal is not None
    }
    if not left_out or keys is None:
        return
    for expose in exposes:
        if expose.skipped is not None:
            continue
        for relationship in keys.expose(expose.expose_id).relationships:
            if relationship.target in left_out:
                raise FluxError(FLX_3125, left_out[relationship.target])


def _bundle_window(
    planned: _Planned, inputs: AuthorInputs
) -> Tuple[Tuple[str, str], Optional[Tuple[str, str]], Optional[Tuple[str, str]]]:
    """The run window, the note that it was assumed, and the window given (by ``--window`` or the history file).

    A history file is checked against the exposes it names. With a given window, the bins of each
    date column that leave it are cut at it; without one the window is the hull of every date
    column's range, so no column's bins are cut.
    """
    declared = inputs.history
    authored = planned.authored
    given_window, given_by = inputs.window, "--window"
    if given_window is None and declared is not None:
        given_window, given_by = declared.time_bounds, declared.name
    if declared is not None:
        _check_declared_entries(planned, declared)
    window, window_note = _time_bounds(
        authored,
        planned.contract,
        given_window,
        declared_spans(declared) if declared is not None else (),
    )
    for expose in authored:
        for column in expose.columns:
            draw_in_window(column, expose.kinds[column.name], window)
    if given_window is not None:
        for expose in authored:
            for column in expose.columns:
                bound_to_window(column, expose.kinds[column.name], window)
        window_note = (
            "time window",
            f"The run covers {window[0]} to {window[1]}, as {given_by} gives.",
        )
    return window, window_note, given_window


def _refuse_a_window_that_misses_ranges(
    planned: _Planned,
    window: Tuple[str, str],
    inputs: AuthorInputs,
    declared: Optional[DeclaredHistory],
) -> None:
    """Refuse a given window that does not hold the range a date column declares.

    A range (published, widened to a parent's lifetime, or declared by the contract) is not cut:
    a property's bins partition it. A window that leaves it would put rows outside
    ``spec.timeBounds``, so it is refused, naming each column, its range and the window. A range
    that starts at or after :data:`WINDOW_END_LIMIT_MS` is not one a window can hold, and the
    window ``init`` works out leaves it out too (:func:`_spans_of`), so it refuses nothing.
    """
    low, high = instant_ms(window[0]), instant_ms(window[1])
    if low is None or high is None:
        return
    given_by = "--window" if inputs.window is not None else (declared.name if declared else "")
    missed: List[str] = []
    for expose in planned.authored:
        for column in expose.columns:
            if expose.kinds.get(column.name) not in DATE_KINDS:
                continue
            for first, last in _declared_ranges_of(expose, column, planned.contract):
                if first < WINDOW_END_LIMIT_MS and (first < low or last > high):
                    missed.append(
                        f"{expose.expose_id}.{column.name} ranges "
                        f"{format_rfc3339_ms(first)} to {format_rfc3339_ms(last)}"
                    )
    if missed:
        raise AuthorError(
            f"the window {window[0]} to {window[1]} given by {given_by} does not hold "
            f"{'; '.join(missed)}; give a window that holds each range"
        )


def _declared_ranges_of(
    expose: AuthoredExpose, column: ColumnPlan, contract: Optional[Mapping[str, Any]]
) -> List[Tuple[int, int]]:
    """The ranges (milliseconds) a date column declares: its own ``range`` and its contract's.

    A column read from a noisy histogram draws only inside the bins that stand clear of the noise,
    so their span stands in for its range, the histogram's whole grid.
    """
    found: List[Tuple[int, int]] = []
    declared = column.declaration.get("range")
    if column.noise_floored:
        if column.time_span is not None:
            found.append(column.time_span)
    elif isinstance(declared, Mapping):
        ends = (instant_ms(declared.get("min")), instant_ms(declared.get("max")))
        if ends[0] is not None and ends[1] is not None:
            found.append((ends[0], ends[1]))
    context = expose.contexts.get(column.name)
    try:
        schema = contract["exposes"][expose.index]["contract"]["schema"]  # type: ignore[index]
        contract_range = (
            declared_window(schema[context.schema_index], expose.kinds.get(column.name))
            if context
            else None
        )
    except (KeyError, IndexError, TypeError):
        contract_range = None
    if contract_range is not None:
        found.append(contract_range)
    return found


def _check_declared_entries(planned: _Planned, declared: DeclaredHistory) -> None:
    """Refuse a history file that names an expose the bundle does not write, or one with no validity window."""
    keys = planned.keys
    if keys is None:
        raise AuthorError(
            f"{declared.name}: the contract declares no keys, so no expose of it has a "
            "validity window for a history"
        )
    written = [expose for expose in planned.authored if not expose.reference]
    left_out = {
        expose.expose_id: expose.needed_refusal
        for expose in planned.exposes
        if expose.needed_refusal is not None
    }
    for entry in (*declared.history, *declared.snapshots, *declared.events, *declared.periods):
        for name in (entry.get("emit"), entry.get("of")):
            if isinstance(name, str) and name in left_out:
                raise FluxError(FLX_3125, left_out[name])
    try:
        check_entries(
            declared,
            {expose.expose_id for expose in written},
            {expose.expose_id for expose in written if keys.expose(expose.expose_id).versioned},
        )
    except DeclaredHistoryError as error:
        raise AuthorError(str(error)) from None


def _histories_and_projections(
    planned: _Planned,
    window: Tuple[str, str],
    declared: Optional[DeclaredHistory],
    declined: Tuple[Optional[str], Optional[str]],
) -> Tuple[_Histories, Projected]:
    """The histories and the projections: the history file's, or those the profile and the contract give.

    *declined* is the reason plan refuses the histories, and the reason it refuses the projections.
    """
    authored, keys, contract = planned.authored, planned.keys, planned.contract
    if declared is not None and keys is not None:
        return (
            _declared_histories(authored, keys, contract, planned.reading.tables, window, declared),
            _declared_projections(authored, keys, contract, declared),
        )
    published = Published(
        planned.reading.profile.get("relationships"), planned.reading.references, planned.spread
    )
    histories = _histories(
        authored,
        keys,
        contract,
        planned.reading.tables,
        window,
        minimum_cell=planned.reading.minimum_cell,
        fractional_bits=planned.reading.fractional_bits,
        published=published,
        declined=declined[0],
    )
    projections = _projections(authored, keys, contract, histories, published, declined[1])
    return histories, projections


def _history_columns(
    planned: _Planned, histories: _Histories, projections: Projected
) -> Dict[str, Dict[str, str]]:
    """The columns each written history and its projection entries set, by emit, with who sets each.

    A history sets its emit's validity start and end and its ``activation`` column. An ``events``
    entry sets the columns plan counts as its own (``op``, ``at``, ``date``, the ``type`` column,
    the ``before`` and ``after`` images and each column that names a column of the history), a
    ``periods`` entry its period column, the columns its kinds override and each column that names
    a column of the history. A ``snapshots`` entry sets none that a rule could set a second time.
    A history or a projection that is not written (the plan refuses it, or the profile publishes
    none) sets none. Each value is what a date rule calls what sets the column.
    """
    exposes = planned.contract["exposes"]
    index = {expose.expose_id: expose.index for expose in planned.authored}
    found: Dict[str, Dict[str, str]] = {}
    for entry in histories.entries:
        emit = str(entry["emit"])
        names = {
            name
            for name in window_ends(exposes[index[emit]].get("semantics")).values()
            if isinstance(name, str)
        }
        if isinstance(entry.get("activation"), str):
            names.add(nfc(str(entry["activation"])))
        found[emit] = {nfc(name): _HISTORY_LABEL for name in names}
    for key, entries, projected in (
        (EVENTS_KEY, projections.events, _projected_by_events),
        (PERIODS_KEY, projections.periods, _projected_by_periods),
    ):
        for entry in entries:
            emit = str(entry["emit"])
            label = f"the history's {key} entry"
            names = {*projected(entry), *projections.carried.get(emit, ("", ()))[1]}
            held = found.setdefault(emit, {})
            for name in names:
                held.setdefault(nfc(name), label)
    return found


#: What a date rule calls the written history of a table; a projection entry is "the history's
#: events entry" or "the history's periods entry".
_HISTORY_LABEL = "the table's history"


def _projected_by_events(entry: Mapping[str, Any]) -> List[str]:
    """The columns an ``events`` entry names as its own: its ``op``, ``at``, ``date`` and ``type`` columns, and the images' ones."""
    names = [entry.get(key) for key in ("op", "at", "date")]
    kind = entry.get("type")
    if isinstance(kind, Mapping):
        names.append(kind.get("column"))
    for side in ("before", "after"):
        images = entry.get(side)
        if isinstance(images, Mapping):
            names.extend(images)
    return [name for name in names if isinstance(name, str)]


def _projected_by_periods(entry: Mapping[str, Any]) -> List[str]:
    """The columns a ``periods`` entry names as its own: its period column and the columns its kinds override."""
    names = [entry.get("column")]
    for kind in entry.get("kinds") or ():
        overrides = kind.get("overrides") if isinstance(kind, Mapping) else None
        if isinstance(overrides, Mapping):
            names.extend(overrides)
    return [name for name in names if isinstance(name, str)]


def _setters(
    expose: AuthoredExpose, history_columns: Mapping[str, Mapping[str, str]]
) -> List[Tuple[str, str]]:
    """Each column of *expose* that a written history or projection entry sets, with what sets it."""
    held = history_columns.get(expose.expose_id, {})
    return [(name, held[nfc(name)]) for name in expose.kinds if nfc(name) in held]


def _dated_columns(
    planned: _Planned,
    window: Tuple[str, str],
    fallbacks: _Fallbacks,
    histories: _Histories,
    projections: Projected,
) -> Any:
    """Declare the dates of the written exposes: ``within`` entries, exposures and ``rates``.

    A column a written history, or a projection entry of it, sets is dated by no lifetime and no
    ``within`` entry.
    """
    history_columns = _history_columns(planned, histories, projections)
    return declare_dates(
        planned.reading.profile,
        {
            expose.expose_id: DatedExpose(
                kinds={name: kind for name, kind in expose.kinds.items() if kind in DATE_KINDS},
                blocked=frozenset(expose.derives)
                | frozenset(expose.dependents)
                | frozenset(expose.copies)
                | frozenset(plan.name for plan in expose.columns if plan.fixed),
                columns=frozenset(expose.kinds),
                history={
                    name: f"{label} sets {name!r}"
                    for name, label in _setters(expose, history_columns)
                },
                projected_keys={
                    name: f"{label} sets its foreign key {name!r}"
                    for name, label in _setters(expose, history_columns)
                    if label != _HISTORY_LABEL
                },
            )
            for expose in planned.authored
            if not expose.reference
        },
        planned.keys,
        window_end=window_end_ms(window[1]),
        window_start=window_start_ms(window[0]),
        minimum_cell=planned.reading.minimum_cell,
        declined_exposures=fallbacks.declined,
        declined_within=fallbacks.undated,
        declined_rates=fallbacks.unrated,
    )


def _draft_documents(
    planned: _Planned, inputs: AuthorInputs, layers: _Layers, team: str
) -> _Drafted:
    """The World, the Persona and the Simulation, with the register that counts their numbers."""
    contract, reading, spread = planned.contract, planned.reading, planned.spread
    histories, dated = layers.histories, layers.dated
    declared = inputs.history
    product_ref = str(contract["id"])
    children = (
        spread.used + histories.used + dated.used,
        spread.unused + histories.unused + dated.unused,
        spread.records,
    )
    simulation_id = f"sim-{product_ref}"
    world, persona = _world_and_persona(contract, team)
    for expose in planned.authored:
        if not expose.reference:
            _drop_unkept_properties(expose)
    namespace = _namespace_block(
        contract,
        reading,
        planned.keys,
        planned.authored,
        spread,
        histories,
        layers.projections,
        planned.needs,
        dated,
        declared.as_of_entries() if declared is not None else None,
    )
    simulation = _simulation_document(contract, planned.authored, namespace, layers.window, team)
    contract_ids = {
        str(expose.get("exposeId")) for expose in contract["exposes"] if isinstance(expose, Mapping)
    }
    ledger = _ledger(reading.profile, planned.exposes, contract_ids, children, reading.noisy)
    labels = _register_labels(
        f"world-{product_ref}",
        f"persona-{product_ref}",
        simulation_id,
        planned.authored,
        reading.noisy,
        _namespace_labels(namespace, spread, dated, reading.profile, declared is not None),
    )
    # The register counts numbers as the engine reads the files back (a YAML
    # float is an exact decimal there), so it is built from that reading.
    documents = {
        f"world-{product_ref}": _as_read(world),
        f"persona-{product_ref}": _as_read(persona),
        simulation_id: _as_read(simulation),
    }
    namespace["parameters"] = _register(labels, documents, simulation_id)
    namespace["parameters"], asked = _cite_and_replace(
        namespace["parameters"],
        (planned.pinned.document, planned.scoped.document, inputs.overlay),
        (contract, _contract_digest(contract), reading.fractional_bits),
        simulation_id,
        ledger,
    )
    namespace[UNUSED_KEY] = _unused_block(ledger)
    documents[simulation_id] = _as_read(simulation)
    return _Drafted(world, persona, simulation, namespace, documents, simulation_id, ledger, asked)


def _consumers_plan(consumers_path: Optional[Path], contract: Any, keys: Any) -> Optional[Any]:
    """The plan ``--consumers`` gives, or ``None`` when no consumers file was named."""
    if consumers_path is None:
        return None
    from flux_engine.author.consumers import plan_consumers

    return plan_consumers(Path(consumers_path), contract, keys)


def _consumers_block(needs: Optional[Any]) -> Dict[str, Any]:
    """The ``consumers`` block the Simulation embeds, as one entry; nothing without a plan."""
    return {} if needs is None else {"consumers": dict(needs.block)}


def _consumers_report(needs: Optional[Any], exposes: Sequence[Any], window: Any) -> Optional[Any]:
    """What the plan's requirements came to once the exposes were authored, or ``None``."""
    if needs is None:
        return None
    from flux_engine.author.consumers import assess

    return assess(needs, exposes, window=window)


def _levels_asked(
    asked: Optional[Mapping[str, Tuple[Tuple[str, Tuple[str, ...]], ...]]], name: str
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """The levels the consumers' queries filter column *name* on."""
    return asked.get(name, ()) if asked is not None else ()


def _asked(
    needs: Optional[Any], expose_id: str, scored: Any
) -> Optional[Mapping[str, Tuple[Tuple[str, Tuple[str, ...]], ...]]]:
    """The levels the consumers' queries filter on, per column of *expose_id*.

    A column a clique of the profile draws is left out: the clique's counts hold no other level.
    """
    if needs is None:
        return None
    from flux_engine.author.consumers import asked_levels_of

    table = scored.tables.get(expose_id)
    drawn = {name for clique in _cliques_of(table) for name in clique.columns}
    return asked_levels_of(needs, expose_id, skip=drawn)


def _counts(
    entries: Sequence[Mapping[str, Any]],
    unused: Sequence[Mapping[str, Any]],
    documents: Mapping[str, Mapping[str, Any]],
    simulation_id: str,
    overlay: Optional[str] = None,
) -> Dict[str, int]:
    """How many counted numbers each source decides, as the register will count them."""
    register = build_parameter_register(
        simulation_id=simulation_id,
        documents=documents,
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block(
            entries, pinned=True, where="parameters", code=FLX_3105, overlay=overlay
        ),
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


def _planned_rules(
    resolved: Any,
    strict: bool,
    simulation_id: str,
    counted: FrozenSet[Tuple[str, str]],
    emits: Sequence[Any] = (),
    time_bounds: Optional[Mapping[str, Any]] = None,
) -> Any:
    """The contract's and the Simulation's rules, planned as `fluxsim run` plans them."""
    from flux_engine.core.kernels import new_plan_is_counter_based
    from flux_engine.stages.pipeline import plan_emit_rules

    try:
        return plan_emit_rules(
            resolved,
            strict=strict,
            where=simulation_id,
            counter_based=new_plan_is_counter_based(),
            counted=counted,
            emits=emits,
            time_bounds=time_bounds,
        )
    except ValueError as error:  # a rule is not what its form allows
        raise AuthorError(
            f"the bundle written from these inputs would be refused by `fluxsim run`: {error}"
        ) from error


def _range_text(kind: LogicalKind, milliseconds: int) -> str:
    """A range end as the fidelity block spells it: RFC 3339 for a timestamp, ``YYYY-MM-DD`` for a date."""
    if kind is LogicalKind.TIMESTAMP:
        return format_rfc3339_ms(milliseconds)
    return (date(1970, 1, 1) + timedelta(days=milliseconds // _DAY_MILLISECONDS)).isoformat()


_DAY_MILLISECONDS = 86_400_000


def _widen_ranges(
    exposes: Sequence[AuthoredExpose], widened: Mapping[Tuple[str, str], Tuple[int, int]]
) -> None:
    """Declare each range in *widened* on its column, with the note that it was widened.

    An end that does not move keeps its basis. An end that moves rests on an assumption, and the
    statistic it came from is recorded as used, with the note.
    """
    for expose in exposes:
        plans = {plan.name: plan for plan in expose.columns}
        for (expose_id, name), (low, high) in widened.items():
            if expose.expose_id != expose_id:
                continue
            plan, kind, declared = _declared_range_of(expose, plans.get(name), name)
            if plan is None or kind is None or declared is None:
                continue
            before = dict(declared)
            moved = _move_range_ends(declared, kind, low, high)
            if not moved:
                continue
            _drop_widened_histograms(expose, plan, before, moved)
            _widen_edges(plan, before, declared, moved)
            _note_widened_range(plan, declared, moved)


def _declared_range_of(
    expose: AuthoredExpose, plan: Optional[ColumnPlan], name: str
) -> Tuple[Optional[ColumnPlan], Optional[LogicalKind], Optional[Dict[str, Any]]]:
    """The plan, kind and declared ``range`` of column *name*, each ``None`` when the column has none."""
    declared = None if plan is None else plan.declaration.get("range")
    return plan, expose.kinds.get(name), declared if isinstance(declared, dict) else None


def _move_range_ends(declared: Dict[str, Any], kind: LogicalKind, low: int, high: int) -> List[str]:
    """Write the widened ends into the declared range and list the ends (``min``, ``max``) that moved."""
    moved = []
    for end, value in (("min", low), ("max", high)):
        text = _range_text(kind, value)
        if text != declared.get(end):
            declared[end] = text
            moved.append(end)
    return moved


def _note_widened_range(
    plan: ColumnPlan, declared: Mapping[str, Any], moved: Sequence[str]
) -> None:
    """Record that the ends in *moved* were widened: the labels, the used statistics, the span and the notes.

    A label of the whole range is replaced by one of the whole range; a label of one end by one of
    that end. A statistic a replaced label cited is recorded as used, with the note.
    """
    labels = [item for item in plan.labels if item.tokens != ("range",)]
    whole = len(labels) != len(plan.labels)
    for end in moved:
        cited = [item for item in plan.labels if item.tokens == ("range", end)]
        for item in cited:
            if item.basis.statistic is not None:
                plan.reads.append((item.basis.statistic[0], _WIDENED_RANGE))
        labels = [item for item in labels if item.tokens != ("range", end)]
        if not whole:
            labels.append(Label(("range", end), assumed(_WIDENED_RANGE)))
    plan.labels = labels + ([Label(("range",), assumed(_WIDENED_RANGE))] if whole else [])
    plan.published_range = (
        Decimal(_milliseconds(declared["min"])),
        Decimal(_milliseconds(declared["max"])),
    )
    plan.time_span = (_milliseconds(declared["min"]), _milliseconds(declared["max"]))
    plan.assumptions.append(_WIDENED_RANGE)
    plan.choices.append(_WIDENED_RANGE)


_WIDENED_HISTOGRAM = (
    "the declared range was widened to the parent's lifetime, so no histogram of the column can "
    "partition it with the bins the profile publishes (FLX-5032); the draw keeps the profile's "
    "shape and no histogram property is written"
)


def _drop_widened_histograms(
    expose: AuthoredExpose, plan: ColumnPlan, before: Mapping[str, Any], moved: Sequence[str]
) -> None:
    """Write no scorable histogram property of a column whose range end an edge sat on and that end moved.

    A histogram property's bins run from the column's declared minimum to its declared maximum
    (``FLX-5032``), and ``verify`` compares them with the bins the profile publishes (``FLX-5037``),
    so the two cannot both hold once a range end moves. The draw keeps the profile's shape, and the
    property that could not be compared is left out, with the reason. An expose left with no
    property takes the first column's assumed check, as :func:`_finish_expose` does.
    """
    dropped = []
    for item in plan.properties:
        edges = item.document.get("bins")
        # A property the profile cannot score is refused by verify whatever its bins are.
        if (
            item.scorable
            and isinstance(edges, list)
            and any(
                str(edges[position]) == str(before[end])
                for end, position in (("min", 0), ("max", len(edges) - 1))
                if end in moved
            )
        ):
            dropped.append(item)
    if not dropped:
        return
    plan.properties = [item for item in plan.properties if item not in dropped]
    expose.properties = [item for item in expose.properties if item not in dropped]
    plan.choices.append(_WIDENED_HISTOGRAM)
    plan.assumptions.append(_WIDENED_HISTOGRAM)
    if not expose.properties:
        for other in expose.columns:
            fallback = assumed_check(other, expose.kinds[other.name], scorable_only=True)
            if fallback is not None:
                expose.properties = [fallback]
                break
    if not expose.properties:
        raise _WithinDeclined(
            {
                (expose.expose_id, plan.name): (
                    "its child date's declared range would have to be widened to the parent's "
                    "lifetime for the entry to plan (FLX-6204), and then no histogram of the "
                    "column can be compared with the profile's (FLX-5037), which leaves the "
                    "table no property to verify"
                )
            }
        )


def _bin_holders(plan: ColumnPlan) -> List[Tuple[List[Any], List[Label]]]:
    """The bin lists of a column, each with the labels that describe it: its draw's, then each property's."""
    holders: List[Tuple[List[Any], List[Label]]] = []
    if isinstance(plan.declaration.get("bins"), list):
        holders.append((plan.declaration["bins"], plan.labels))
    for item in plan.properties:
        if isinstance(item.document.get("bins"), list):
            holders.append((item.document["bins"], item.labels))
    return holders


def _widen_edges(
    plan: ColumnPlan, before: Mapping[str, Any], after: Mapping[str, Any], moved: Sequence[str]
) -> None:
    """Move the outer bin edges that sat on a range end to the widened end, in the draw and in each property.

    The bins of a histogram partition the declared range (``FLX-5032``), so an end of the range
    that moves takes the edge on it along. Such an edge rests on an assumption, and a statistic it
    was read from is recorded as used, with the note.
    """
    for edges, labels in _bin_holders(plan):
        for end, position in (("min", 0), ("max", len(edges) - 1)):
            if end in moved and str(edges[position]) == str(before[end]):
                edges[position] = after[end]
                _relabel_edge(plan, labels, str(position))


def _relabel_edge(plan: ColumnPlan, labels: List[Label], position: str) -> None:
    """Label the bin edge at *position* as assumed, recording the statistic its old label cited."""
    token = ("bins", position)
    for label in [item for item in labels if item.tokens == token]:
        if label.basis.statistic is not None:
            plan.reads.append((label.basis.statistic[0], _WIDENED_RANGE))
    labels[:] = [item for item in labels if item.tokens != token]
    labels.append(Label(token, assumed(_WIDENED_RANGE)))


def _milliseconds(text: str) -> int:
    """A range end spelled as a date or an RFC 3339 timestamp, in milliseconds since the epoch."""
    from flux_engine.core.clock import parse_rfc3339_ms

    if len(text) == 10:
        return (date.fromisoformat(text) - date(1970, 1, 1)).days * _DAY_MILLISECONDS
    return parse_rfc3339_ms(text)


def _refuse_unless_widened(
    files: Sequence[StarterFile],
    exposes: Sequence[AuthoredExpose],
    widened: Mapping[Tuple[str, str], Tuple[int, int]],
) -> None:
    """Raise :class:`_RangesWidened` when the plan's check of the declared ranges asks for wider ones.

    The files are read as ``plan`` reads them, and each enforced ``within`` date whose range the
    plan would refuse (``FLX-6204``) is given the range the plan's own check accepts
    (:func:`flux_engine.stages.pipeline.widened_ranges_of`). A bundle the plan accepts, or refuses
    for another reason, raises nothing here: :func:`_check` reports it.
    """
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits
    from flux_engine.stages.pipeline import unrepairable_withins_of, widened_ranges_of

    kinds = {expose.expose_id: expose.kinds for expose in exposes}
    asked: Dict[Tuple[str, str], Tuple[int, int]] = {}
    unrepairable: Dict[Tuple[str, str], str] = {}
    with tempfile.TemporaryDirectory(prefix="fluxsim-init-") as scratch:
        directory = Path(scratch)
        for entry in files:
            (directory / entry.name).write_bytes(entry.data)
        try:
            loaded = load_bundle(directory)
            for simulation_id in loaded.simulation_ids():
                cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
                emits = resolve_all_emits(loaded, simulation_id=simulation_id)
                bounds = cast.simulation.get("spec", {}).get("timeBounds")
                unrepairable.update(unrepairable_withins_of(emits, bounds, where=simulation_id))
                if unrepairable:
                    continue
                found = widened_ranges_of(emits, bounds, where=simulation_id)
                for (expose_id, column), (low, high) in found.items():
                    unit = _DAY_MILLISECONDS if kinds[expose_id][column] is LogicalKind.DATE else 1
                    asked[(expose_id, column)] = (low * unit, high * unit)
        except (FluxError, ArithmeticError, ValueError, KeyError):
            return  # what is wrong is not a range: the check that follows says what
    if unrepairable:
        raise _WithinDeclined(unrepairable)
    if asked:
        raise _RangesWidened(asked)


def _chained_exposures(
    within: Sequence[Mapping[str, Any]], keys: Optional[ContractKeys]
) -> List[Tuple[str, str]]:
    """The ``(emit, relationship)`` of each entry with ``exposure: true`` whose parent's lifetime lies within another's.

    A ``within`` entry with ``lifetime: true`` cuts the child's lifetime to its parent's, so the
    days the child is active are no longer the ones its own durations give. Every exposure is
    tried, not only these; a reason that names the chain does so only for an entry listed here.
    """
    if keys is None:
        return []
    bounded = {str(entry["emit"]) for entry in within if entry.get("lifetime") is True}
    found: List[Tuple[str, str]] = []
    for entry in within:
        if entry.get("exposure") is not True:
            continue
        emit, name = str(entry["emit"]), str(entry["relationship"])
        try:
            target = next(
                relationship.target
                for relationship in keys.expose(emit).relationships
                if relationship.name == name
            )
        except (KeyError, LookupError, StopIteration):
            continue
        if target in bounded:
            found.append((emit, name))
    return found


def _trial_scale(simulation: Mapping[str, Any]) -> Optional[str]:
    """The ``--scale`` that brings a trial run to at most :data:`_MOST_TRIAL_ROWS` rows, or ``None`` for the whole bundle.

    The scale is a plain decimal, which is the only form ``run`` accepts, and is
    ``_MOST_TRIAL_ROWS / rows`` rounded down to three significant digits, so the scaled rows never
    pass the cap. It has no floor. A bundle so large that the scale cannot be written as a plain
    decimal ``run`` accepts gives a scale ``run`` refuses, which the caller reports as a trial
    that could not be made.
    """
    emits = simulation.get("extensions", {}).get(EXTENSION_NAMESPACE, {}).get("emits", {})
    rows = sum(
        int(item["rows"])
        for item in emits.values()
        if isinstance(item, Mapping) and isinstance(item.get("rows"), int)
    )
    if rows <= _MOST_TRIAL_ROWS:
        return None
    rounded = Context(prec=3, rounding=ROUND_DOWN).divide(Decimal(_MOST_TRIAL_ROWS), Decimal(rows))
    return format(rounded, "f")


def _named(code: str, message: str) -> str:
    """*message*'s first line, led by *code* unless it already starts with it."""
    first = message.splitlines()[0] if message else "no message"
    return first if first.startswith(code) else f"{code}: {first}"


@dataclass(frozen=True)
class _Trial:
    """What a trial run of the bundle formed: its exposure and calendar lines, or why it formed none.

    ``unmade`` is empty when the trial ran and ``verify`` scored it. Otherwise it names the code the
    run refused with, or the exception it raised, so a reader can tell a refusal from a bundle
    whose line was never formed.
    """

    exposure: Sequence[Mapping[str, Any]] = ()
    calendar: Sequence[Mapping[str, Any]] = ()
    unmade: str = ""


def _lines_of(document: Any) -> Sequence[Mapping[str, Any]]:
    """The ``lines`` of a verify document of the trial, or none when it holds none."""
    found = document.get("lines") if isinstance(document, Mapping) else None
    return found if isinstance(found, list) else ()


def _trial_run(files: Sequence[StarterFile], profile_path: Path, scale: Optional[str]) -> _Trial:
    """Run the bundle in a scratch directory and return the exposure and calendar lines ``verify`` scored.

    The trial is :attr:`_Trial.unmade` when the run could not be made or scored nothing the bundle
    declares, and the reason names the code the run refused with.
    """
    from flux_engine.stages.pipeline import PlannedRowsAboveCeiling
    from flux_engine.stages.run import run

    with tempfile.TemporaryDirectory(prefix="fluxsim-init-") as scratch:
        directory = Path(scratch) / "bundle"
        directory.mkdir()
        for entry in files:
            (directory / entry.name).write_bytes(entry.data)
        try:
            result = run(
                directory,
                state_dir=Path(scratch) / "state",
                profile=profile_path,
                verify=True,
                scale=scale,
                most_rows=_MOST_TRIAL_PLANNED_ROWS,
            )
        except PlannedRowsAboveCeiling as above:
            return _Trial(
                unmade=f"the trial would generate {above.rows} rows, more than {above.ceiling}"
            )
        except FluxError as error:
            return _Trial(unmade=_named(error.code.code, str(error)))
        except (OSError, ValueError, ArithmeticError) as error:
            return _Trial(unmade=f"{type(error).__name__}: {error}")
    verify_document = result.artifacts.get("verify")
    if isinstance(verify_document, Mapping) and (
        isinstance(verify_document.get("exposure"), Mapping)
        or isinstance(verify_document.get("calendar"), Mapping)
    ):
        return _Trial(
            exposure=_lines_of(verify_document.get("exposure")),
            calendar=_lines_of(verify_document.get("calendar")),
        )
    return _Trial(unmade=_named(result.error_code or f"exit {result.exit_code}", result.stderr))


def _failed_line_reason(line: Mapping[str, Any], scale: Optional[str], chained: bool) -> str:
    """Why a trial run's exposure line, outside its tolerance, is not declared, with the trial's numbers."""
    run_rate = f"{line['children']} over {line['activeDays']}"
    profile_rate = f"{line['profileChildren']} over {line['profileActiveDays']}"
    gap = Fraction(str(line["gap"]))
    return (
        "the run cannot reproduce this exposure's rate under the declared lifetimes"
        + (f", {_CHAINED_NOTE}" if chained else "")
        + ": a trial run"
        + (f" at scale {scale}" if scale is not None else "")
        + f" has children per active day {run_rate} against the profile's {profile_rate}, "
        f"gap {float(gap):.4f} (tolerance {line['tolerance']})"
    )


_CHAINED_NOTE = "whose parent's lifetime lies within its own parent's lifetime"


def _line_of(
    lines: Sequence[Mapping[str, Any]], emit: str, name: str
) -> Optional[Mapping[str, Any]]:
    """The line *name* of *emit* in *lines*, or ``None`` when the trial formed none."""
    for line in lines:
        if line.get("emit") == emit and line.get("line") == name:
            return line
    return None


def _exposure_reason(
    key: Tuple[str, str], trial: _Trial, scale: Optional[str], chained: bool
) -> Optional[str]:
    """Why the exposure *key* is left out, or ``None`` when a trial line shows it reproduced."""
    note = f", {_CHAINED_NOTE}," if chained else ""
    if trial.unmade:
        return (
            f"a trial run of the bundle could not be made ({trial.unmade}), so this "
            f"exposure{note} is not declared"
        )
    line = _line_of(trial.exposure, key[0], f"exposure:{key[1]}")
    if line is None:
        return (
            "a trial run of the bundle formed no line for this exposure"
            f"{note or ','} so it is not declared"
        )
    return None if line.get("outcome") == "within" else _failed_line_reason(line, scale, chained)


def _outside_histograms(line: Mapping[str, Any]) -> List[str]:
    """Each histogram of a calendar line the trial scored outside its tolerance, with its numbers."""
    found: List[str] = []
    for part in line.get("histograms", ()):
        if part.get("outcome") != "outside":
            continue
        refusal = part.get("refusal")
        if isinstance(refusal, Mapping):
            found.append(f"{part['histogram']} refused ({refusal.get('code')})")
            continue
        distance = float(Fraction(str(part["distance"])))
        found.append(f"{part['histogram']} distance {distance:.4f}, tolerance {part['tolerance']}")
    return found


_NOT_GATING = "so its rates are not declared and its calendar line does not gate"


def _rates_reason(key: Tuple[str, str], trial: _Trial, scale: Optional[str]) -> Optional[str]:
    """Why the ``rates`` entry of column *key* is left out, or ``None`` when its gated calendar line is within."""
    if trial.unmade:
        return f"a trial run of the bundle could not be made ({trial.unmade}), {_NOT_GATING}"
    line = _line_of(trial.calendar, key[0], f"calendar:{key[1]}")
    if line is None or line.get("gated") is False:
        return f"a trial run of the bundle formed no gated calendar line for this column, {_NOT_GATING}"
    if line.get("outcome") == "within":
        return None
    found = "; ".join(_outside_histograms(line)) or "no histogram was scored"
    return (
        "a trial run"
        + (f" at scale {scale}" if scale is not None else "")
        + f" puts this column's calendar outside its tolerance ({found}), {_NOT_GATING}"
    )


def _refuse_unless_reproduced(
    files: Sequence[StarterFile],
    within: Sequence[Mapping[str, Any]],
    rates: Sequence[Mapping[str, Any]],
    keys: Optional[ContractKeys],
    profile_path: Path,
) -> None:
    """Raise :class:`_ExposuresDeclined` for each exposure and ``rates`` entry no trial run shows the run reproduces.

    A bundle never carries a line its own ``verify`` would fail. Every ``within`` entry with
    ``exposure: true`` and every ``rates`` entry is tried: the bundle is run once in a scratch
    directory (at a smaller scale when it has many rows) and the line ``exposure:<relationship>``
    and the gated line ``calendar:<column>`` are read as ``verify`` scores them. An exposure is kept
    only when its line is formed and within its tolerance, and a ``rates`` entry only when its
    column's gated calendar line is. Every other case leaves the item out and records it as unused
    with a reason: the line is outside its tolerance (the reason carries the trial's numbers), the
    trial formed no line for it, or the trial could not be made (the reason names the code). Every
    failing item of the trial is declined together.
    """
    exposures = [
        (str(entry["emit"]), str(entry["relationship"]))
        for entry in within
        if entry.get("exposure") is True
    ]
    columns = [(str(entry["emit"]), str(entry["column"])) for entry in rates]
    if not exposures and not columns:
        return
    scale = _trial_scale(_simulation_of(files))
    trial = _trial_run(files, profile_path, scale)
    chained = set(_chained_exposures(within, keys))
    reasons = {
        key: reason
        for key in exposures
        if (reason := _exposure_reason(key, trial, scale, key in chained)) is not None
    }
    declined = {
        key: reason for key in columns if (reason := _rates_reason(key, trial, scale)) is not None
    }
    if reasons or declined:
        raise _ExposuresDeclined(reasons, declined)


def _simulation_of(files: Sequence[StarterFile]) -> Mapping[str, Any]:
    """The Simulation document the bundle's files hold."""
    for entry in files:
        if entry.name == "simulation.flux.yml":
            loaded = yaml.safe_load(entry.data)
            return loaded if isinstance(loaded, Mapping) else {}
    return {}


def _release_dated_columns(exposes: Sequence[AuthoredExpose], dated: Any) -> None:
    """Take ``allocation: exact`` off a column a lifetime's end or a ``within`` date now sets.

    Exact allocation is given to small tables before the date declarations are read, and a column
    a date rule sets is one it cannot read (``FLX-3402``), so the declaration is withdrawn here.
    """
    set_by_dates = _columns_set_by_dates(dated)
    for expose in exposes:
        held = set_by_dates.get(expose.expose_id, ())
        for plan in expose.columns:
            if plan.name in held and plan.declaration.get("allocation") == "exact":
                del plan.declaration["allocation"]
                if plan.how.endswith(EXACT_HOW):
                    plan.how = plan.how[: -len(EXACT_HOW)].rstrip("; ")


def _declare_copulas(
    exposes: Sequence[AuthoredExpose], reading: _Reading, dated: Any, histories: _Histories
) -> None:
    """Write one copula for each written expose whose table publishes rank correlations.

    Run last of the declarations, once the cliques, the Chow-Liu forest, the dates, the small
    tables' allocation and the histories have each taken the columns they draw, so that the copula
    gets only the columns nothing else decides (ADR 0035 decision 9). Under kernel 0.1.0, which
    has no copula path, no key is written.
    """
    from flux_engine.core.kernels import new_plan_is_counter_based

    named = _columns_named_by_dates(dated)
    with_history = {str(entry["emit"]) for entry in histories.entries}
    for expose in exposes:
        if expose.reference or expose.skipped is not None:
            continue
        whole = None
        if expose.expose_id in with_history or expose.projected is not None or expose.carries:
            whole = "its columns follow a history, which sets them"
        planned = declare_copula(
            expose,
            reading.scored.tables.get(expose.expose_id),
            by_dates=named.get(expose.expose_id, set()),
            whole=whole,
            counter_based=new_plan_is_counter_based(),
        )
        if planned is None:
            continue
        expose.copula = planned
        expose.properties.extend(planned.properties)
        expose.assumptions.extend(("copula", sentence) for sentence in planned.assumptions)


def _columns_named_by_dates(dated: Any) -> Dict[str, Set[str]]:
    """The columns of each emit that a ``lifetimes``, ``within`` or ``rates`` entry names at all."""
    named = _columns_set_by_dates(dated)
    for entry in dated.lifetimes:
        named.setdefault(entry["emit"], set()).update(
            name for name in (entry.get("start"), entry.get("end")) if name is not None
        )
    for entry in dated.within:
        if entry.get("date") is not None:
            named.setdefault(entry["emit"], set()).add(entry["date"])
    for entry in dated.rates:
        if entry.get("column") is not None:
            named.setdefault(entry["emit"], set()).add(entry["column"])
    return named


def _columns_set_by_dates(dated: Any) -> Dict[str, Set[str]]:
    """The columns of each emit that a lifetime's end or a ``within`` date sets."""
    set_by_dates: Dict[str, Set[str]] = {}
    for entry in dated.lifetimes:
        if entry.get("end") is not None:
            set_by_dates.setdefault(entry["emit"], set()).add(entry["end"])
    for entry in dated.within:
        if entry.get("date") is not None:
            set_by_dates.setdefault(entry["emit"], set()).add(entry["date"])
        if entry.get("lifetime") is True:  # a period: the child's own start and end
            for life in dated.lifetimes:
                if life["emit"] == entry["emit"]:
                    set_by_dates.setdefault(entry["emit"], set()).update(
                        name for name in (life["start"], life.get("end")) if name is not None
                    )
    return set_by_dates


def _check(
    files: Sequence[StarterFile],
    profile: Mapping[str, Any],
    references: Optional[Mapping[str, Any]] = None,
    overlay: Optional[Tuple[str, Mapping[str, Any]]] = None,
) -> None:
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
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.parameters import check_profile_citations
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits, strict_coverage_declared
    from flux_engine.stages.pipeline import (
        check_flux_side_rule_ids,
        counted_relationships,
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
                counted = counted_relationships(cast.simulation, where=simulation_id)
                version = _generator_version(emits, counted)
                for resolved in emits:
                    planned.append(
                        (
                            resolved,
                            _plan_emit(
                                resolved,
                                simulation_id,
                                spec,
                                strict,
                                version,
                                references,
                                counted,
                                emits,
                            ),
                        )
                    )
                _relate(planned)
                register = parameter_register_of(loaded, cast)
                if register is not None:
                    check_profile_citations(register, profile, where=simulation_id, overlay=overlay)
        except FluxError as error:
            raise AuthorError(
                f"the bundle written from these inputs would be refused by `fluxsim run`: {error}"
            ) from error
        except (ArithmeticError, ValueError) as error:
            raise AuthorError(_beyond_note(error)) from error


def _generator_version(emits: Sequence[Any], counted: FrozenSet[Tuple[str, str]]) -> str:
    """The kernel the emits are planned under.

    A reference expose, a column drawn given a linked row, a column under exact allocation, a
    copula, a column given a bin, a relationship on the counted layout and a history generate under
    kernel 0.2.0 only, so one of any puts every plan of the simulation there.
    """
    from flux_engine.generator.copula import declares_copula
    from flux_engine.generator.rows import GENERATOR_VERSION, GENERATOR_VERSION_2

    return (
        GENERATOR_VERSION_2
        if counted
        or any(
            resolved.is_reference
            or _given_across_link(resolved)
            or _allocated(resolved)
            or _given_a_bin(resolved)
            or declares_copula(resolved.fidelity)
            or HISTORY_KEY in (resolved.extension or {})
            or ASOF_KEY in (resolved.extension or {})
            for resolved in emits
        )
        else GENERATOR_VERSION
    )


def _plan_emit(
    resolved: Any,
    simulation_id: str,
    spec: Mapping[str, Any],
    strict: bool,
    version: str,
    references: Optional[Mapping[str, Any]],
    counted: FrozenSet[Tuple[str, str]],
    emits: Sequence[Any] = (),
) -> Any:
    """The generator plan of one emit: a reference plan, or the plan of a drawn expose.

    The fidelity block of a drawn expose is compiled first, as ``verify`` does.
    """
    from flux_engine.generator import build_plan
    from flux_engine.generator.reference import ReferenceRows, build_reference_plan
    from flux_engine.generator.relations import related_columns
    from flux_engine.stages.pipeline import compile_declared_fidelity

    if resolved.is_reference:
        return build_reference_plan(
            resolved.compile(),
            sim_id=simulation_id,
            seam_digest="sha256:" + "0" * 64,
            table=ReferenceRows.of((references or {})[resolved.expose_id]),
            generator_version=version,
            where=f"{simulation_id}: reference expose {resolved.expose_id!r}",
        )
    compile_declared_fidelity(resolved)
    rules = _planned_rules(resolved, strict, simulation_id, counted, emits, spec.get("timeBounds"))
    return build_plan(
        resolved.compile_planned(),
        sim_id=simulation_id,
        seam_digest="sha256:" + "0" * 64,
        rows=resolved.declared_row_count or 1,
        fidelity=resolved.fidelity,
        time_bounds=spec.get("timeBounds"),
        related=related_columns(resolved.keys),
        rules=rules,
        generator_version=version,
    )


def _allocated(resolved: Any) -> bool:
    """Whether an emit's fidelity block declares ``allocation`` on a column."""
    columns = (resolved.fidelity or {}).get("columns")
    return isinstance(columns, Mapping) and any(
        isinstance(declared, Mapping) and "allocation" in declared for declared in columns.values()
    )


def _given_a_bin(resolved: Any) -> bool:
    """Whether an emit's fidelity block draws a column ``given`` a column that is drawn from ``bins``."""
    columns = (resolved.fidelity or {}).get("columns")
    if not isinstance(columns, Mapping):
        return False
    binned = {
        nfc(str(name))
        for name, declared in columns.items()
        if isinstance(declared, Mapping) and "bins" in declared
    }
    for declared in columns.values():
        given = declared.get("given") if isinstance(declared, Mapping) else None
        names = [given] if isinstance(given, str) else given if isinstance(given, list) else []
        if any(isinstance(name, str) and nfc(name) in binned for name in names):
            return True
    return False


def _given_across_link(resolved: Any) -> bool:
    """Whether an emit's fidelity block draws a column ``given: <relationship>.<column>``.

    A name with a dot that is exactly a column of the emit is that column and not a link.
    """
    columns = (resolved.fidelity or {}).get("columns")
    own = set(resolved.compile().column_names)
    return isinstance(columns, Mapping) and any(
        isinstance(declared, Mapping)
        and isinstance(declared.get("given"), str)
        and "." in declared["given"]
        and nfc(declared["given"]) not in own
        for declared in columns.values()
    )


def _relate(planned: Sequence[Tuple[Any, Any]]) -> None:
    """Relate each contract's generator plans as ``plan`` relates them, refusing with ``FLX-3403``.

    The emits are grouped by the contract file they resolve to, and a contract
    that declares no key is passed over, as the pipeline's own relate step does
    (``flux_engine.stages.pipeline``). Each expose has one emit here, so no
    expose is planned twice.
    """
    from flux_engine.generator.history import HistoryBinding
    from flux_engine.generator.relations import relate_plans

    contracts: Dict[str, Tuple[Mapping[str, Any], Dict[str, Any]]] = {}
    counts: Dict[str, Dict[str, Dict[str, Any]]] = {}
    histories: Dict[str, Dict[str, Any]] = {}
    for resolved, plan in planned:
        if resolved.keys is None:
            continue
        _, plans = contracts.setdefault(
            str(resolved.contract_path), (resolved.contract_document, {})
        )
        plans[nfc(plan.expose.expose_id)] = plan
        entries = {
            entry.relationship: entry
            for entry in count_entries_of(resolved.extension, resolved.expose_id)
        }
        if entries:
            counts.setdefault(str(resolved.contract_path), {})[nfc(plan.expose.expose_id)] = entries
        declared = history_entries_of(resolved.extension, resolved.expose_id)
        if declared:
            start = window_ends(resolved.expose.get("semantics")).get("start")
            if start is not None:
                histories.setdefault(str(resolved.contract_path), {})[
                    nfc(plan.expose.expose_id)
                ] = HistoryBinding(entry=declared[0], entities=declared[0].entities, start=start)
    for path, (document, plans) in contracts.items():
        keys = compile_keys(document)
        if keys is not None and keys.declares_keys:
            relate_plans(
                plans,
                keys,
                None,
                None,
                counts.get(path),
                document,
                None,
                histories.get(path),
            )
