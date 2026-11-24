# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What ``verify`` checks on a run's rows beyond the declared properties (ADR 0018).

The rules of every emit's contract, and the bundle's own rules, are checked on the output as the
share of rows that holds them (:mod:`flux_engine.concordance.rule_shares`), and every declared
relationship as four shares (:mod:`flux_engine.concordance.integrity`).

The work is in three steps, so that it joins ``verify``'s single read of each emit
(:func:`flux_engine.stages.verify._emit_pass`): :func:`plan_module_checks` reads the bundle once and says what
each emit needs, :func:`module_pass` is the per-emit work, run by whichever worker holds the emit's
decoded columns, and :func:`finish_module_checks` joins the emits' answers in the order of the emits, so
the shares and every byte of the scorecard are the same for any number of jobs. :func:`module_checks` runs the
three in one process, for ``ship``. An emit the decoded reader does not take is read by the row path, which
reads every cell as it always did.

The bundle is read as ``verify`` reads it for ``inputDigest`` (:func:`recompute_input_digest`), and
the run's rows are read back from the run directory, through the emit's seam attestation, never
following a link. A run with nothing to check, which is every run of a bundle that declares no
rule, adds no line and no key to ``verify``'s result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from flux_engine.concordance.date_facts import date_shares
from flux_engine.concordance.integrity import (
    CHECK_WORDING,
    KeyColumns,
    LinkIntegrity,
    grouped_spread,
    key_columns,
    link_integrity,
    link_integrity_columns,
)
from flux_engine.concordance.projection_checks import projection_entry_of
from flux_engine.concordance.rule_shares import (
    RuleShare,
    RuleSource,
    rule_shares,
    stored_rows_of,
    tables_needed,
)
from flux_engine.concordance.shares import Holds
from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.rows import Rows
from flux_engine.generator.reference import ReferenceRows, reference_rows
from flux_engine.generator.relations import related_columns
from flux_engine.generator.rules import (
    model_from_contract,
    shared_copy_texts,
    shared_copy_texts_not_generated,
)
from flux_engine.safeio import is_regular_file, json_lines, read_text_nofollow
from flux_engine.seam.asof import asof_entries_of
from flux_engine.seam.bridges import bridge_entries_of
from flux_engine.seam.copies import copies_of
from flux_engine.seam.counts import CountsEntry
from flux_engine.seam.date_lines import DateLine, contract_step, date_lines
from flux_engine.seam.dates import lifetime_entries_of, within_entries_of
from flux_engine.seam.emit import frame_rows
from flux_engine.seam.facts import Fact, contract_rule_ids, rule_ids_of
from flux_engine.seam.history import HistoryEntry, history_entries_of
from flux_engine.seam.keys import Relationship, compile_keys, declared_exposes
from flux_engine.seam.links import link_entries_of
from flux_engine.stages.layout import RunLayout

if TYPE_CHECKING:
    from flux_engine.concordance.scorecard import PropertyScore
    from flux_stats.profile import Profile

__all__ = ["ModuleChecks", "REFERENCE_FORM", "REFERENCE_RULE_ID", "module_checks"]

#: The rule a reference expose is held to: its output equals the rows its profile published
#: (ADR 0023 section 2). A line of the expose's rules in the result, enforced, never a scorecard row.
REFERENCE_RULE_ID = "published-rows"
REFERENCE_FORM = "reference"

#: What reading a run's records can raise besides a catalogued refusal.
_UNREADABLE = (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError)


@dataclass(frozen=True)
class ModuleChecks:
    """The shares ``verify`` found on a run's rows, and what it could not check."""

    rules: Tuple[RuleShare, ...] = ()
    #: Referential integrity of every declared relationship both of whose tables the run emits.
    links: Tuple[LinkIntegrity, ...] = ()
    #: The facts the Simulation declares: each rule's tolerance (ADR 0018).
    facts: Tuple[Fact, ...] = ()
    #: One line for each emit the checks could not read or evaluate, and why. Never a value.
    notes: Tuple[str, ...] = ()
    #: The facts over dates the run's streams cannot score, each ``(name, reason)``: they read a
    #: hidden column or a hidden table (ADR 0024 section 8). Never counted as passing.
    not_scored: Tuple[Tuple[str, str], ...] = ()
    #: Set when the run's artifacts do not reproduce its digest: its rows are not the run's.
    skipped: str = ""
    #: The Simulation's ``counts`` entries, whose tolerances the children-per-parent rows take.
    counts: Tuple[CountsEntry, ...] = ()

    @property
    def empty(self) -> bool:
        """Whether there was nothing to check: no rule and no note."""
        return not (self.rules or self.links or self.notes or self.skipped or self.not_scored)

    def failed(self) -> List[Tuple[str, str]]:
        """What fails the stage: each rule off its band and each link not whole, as ``(exposeId, line)``."""
        found = [
            (
                share.expose_id,
                f"rule {share.name}: {share.holds.text()} of the rows it covers hold it, "
                f"outside what the rule allows",
            )
            for share in self.rules
            if share.met is False
        ]
        found.extend(
            (
                link.child,
                f"integrity {link.child}.{link.name} -> {link.parent}: "
                + "; ".join(
                    f"{CHECK_WORDING.get(name, name)} {holds.text()}"
                    for name, holds in link.checks
                    if not holds.whole
                ),
            )
            for link in self.links
            if not link.whole
        )
        return found

    def failures(self) -> List[str]:
        """A rule whose share is not what it promises, one line each: each fails the stage."""
        return [line for _, line in self.failed()]

    def lines(self) -> List[str]:
        """What a person reads under the legs: each emit's rules with their shares."""
        if self.skipped:
            return [f"  rules and integrity: not checked -- {self.skipped}"]
        out: List[str] = []
        for expose_id in sorted({share.expose_id for share in self.rules}):
            parts = []
            for share in (s for s in self.rules if s.expose_id == expose_id):
                state = (
                    ""
                    if share.met is True
                    else (
                        " (outside its band)"
                        if share.met is False and share.band is not None
                        else (
                            " (outside its target)"
                            if share.met is False and share.target is not None
                            else " (violated)" if share.met is False else " (not enforced)"
                        )
                    )
                )
                parts.append(f"{share.rule_id} {share.holds.text()}{state}")
            out.append(f"  rules {expose_id}: " + "; ".join(parts))
        out.extend(f"  integrity {link.line()}" for link in self.links)
        out.extend(f"  facts: not scored -- {name}: {reason}" for name, reason in self.not_scored)
        out.extend(f"  checks: not run -- {note}" for note in self.notes)
        return out

    def scorecard_rows(self, profile: Optional[Profile]) -> Mapping[str, Sequence["PropertyScore"]]:
        """The fact and relationship rows these checks make for the scorecard, by ``exposeId``.

        Scored against *profile*, the pinned profile (format 1-2-0 holds the facts' shares).
        """
        from flux_engine.concordance.module_scores import module_rows

        return module_rows(
            [share for share in self.rules if share.form != REFERENCE_FORM],
            self.links,
            self.facts,
            profile,
            self.counts,
        )

    def artifact(self) -> Dict[str, Any]:
        """The ``rules`` artifact key of ``verify``'s result, or nothing when there was nothing to check."""
        if self.empty:
            return {}
        emits: Dict[str, List[Dict[str, Any]]] = {}
        for share in self.rules:
            emits.setdefault(share.expose_id, []).append(share.to_document())
        document: Dict[str, Any] = {
            "ok": not any(share.met is False for share in self.rules),
            "emits": emits,
        }
        if self.notes:
            document["notChecked"] = list(self.notes)
        if self.not_scored:
            document["notScored"] = [
                {"fact": name, "reason": reason} for name, reason in self.not_scored
            ]
        if self.skipped:
            document["skipped"] = self.skipped
        found: Dict[str, Any] = (
            {"rules": document}
            if (self.rules or self.notes or self.skipped or self.not_scored)
            else {}
        )
        if self.links:
            found["integrity"] = {
                "ok": all(link.whole for link in self.links),
                "relationships": [link.to_document() for link in self.links],
            }
        return found


def _read_rows(layout: RunLayout, expose_id: str) -> Tuple[List[str], List[List[Any]]]:
    """An emit's column names and raw rows, as the run recorded them (the row path)."""
    attestation = json.loads(read_text_nofollow(layout.seam_attestation_path(expose_id)))
    names = [str(name) for name in attestation["columns"]]
    rows: List[List[Any]] = []
    for line in json_lines(read_text_nofollow(layout.emit_path(expose_id))):
        if line:
            rows.append(json.loads(line))
    return names, rows


@dataclass(frozen=True)
class RuleJob:
    """What one emit's rules need: its contract and extension, and the exposes they read."""

    contract_document: Mapping[str, Any]
    extension: Optional[Mapping[str, Any]]
    generated: bool
    needed: Tuple[str, ...]
    copies: Tuple[Tuple[str, str, str, Optional[str]], ...] = ()
    reported: Tuple[Tuple[str, str, str, Optional[str]], ...] = ()
    #: The relationships the run laid out with a ``counts`` entry (kernel 0.2.0), which a generated aggregate reads.
    counted: FrozenSet[Tuple[str, str]] = frozenset()
    #: The emit's ``counts`` entries, whose minimum and condition are lines of its rules.
    counts: Tuple[CountsEntry, ...] = ()
    #: The facts over dates the emit's lifetimes and ``within`` entries make (ADR 0024 section 8).
    dates: Tuple[DateLine, ...] = ()
    #: The Simulation's ``spec.timeBounds``, which lays the periods of a periodic emit.
    time_bounds: Optional[Mapping[str, Any]] = None
    #: The history of each emit that declares one, which an as-of link reads as versions.
    histories: Tuple[HistoryEntry, ...] = ()


@dataclass(frozen=True)
class ModuleJob:
    """One emit's share of the module checks: its rules, and the key columns other emits are joined on."""

    expose_id: str
    rules: Optional[RuleJob] = None
    keys: Tuple[Tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class EmitModule:
    """What one emit's pass found: its rule shares, its key columns, and what it could not do."""

    rules: Tuple[RuleShare, ...] = ()
    #: Each key (a tuple of column names) as decoded columns, or ``None`` when the row path must read it.
    keys: Mapping[Tuple[str, ...], Optional[KeyColumns]] = field(default_factory=dict)
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedLink:
    """A declared relationship both of whose tables the run emits, and whether the parent repeats keys."""

    link: Any
    parent_versioned: bool


@dataclass(frozen=True)
class ModulePlan:
    """What the module checks need of the run, read once: the bundle's facts, and each emit's job."""

    jobs: Mapping[str, ModuleJob] = field(default_factory=dict)
    links: Tuple[PlannedLink, ...] = ()
    facts: Tuple[Fact, ...] = ()
    notes: Tuple[str, ...] = ()
    skipped: str = ""
    #: The facts over dates that read a hidden column or table, each ``(name, reason)``.
    not_scored: Tuple[Tuple[str, str], ...] = ()
    #: Each reference expose's output compared with its published rows, read once with the bundle.
    reference_rules: Tuple[RuleShare, ...] = ()
    #: The Simulation's ``counts`` entries.
    counts: Tuple[CountsEntry, ...] = ()


def _reference_share(emit: Any, layout: RunLayout, plan_document: Mapping[str, Any]) -> RuleShare:
    """A reference expose's output against the rows its plan records: how many lines are equal.

    The published rows are read as the plan read them (typed by the contract's columns) and framed
    as the emit frames its rows, so a line holds when it is the line the published row makes. A
    row the run lacks or adds is a row that does not hold.
    """
    from flux_engine.stages.plan import REFERENCES_PLAN_KEY

    expose_id = emit.expose_id
    recorded = (plan_document.get(REFERENCES_PLAN_KEY) or {}).get(expose_id)
    table = ReferenceRows.from_document(recorded, where=f"the plan's rows of {expose_id!r}")
    expose = emit.compile()
    expected = [
        line.decode("utf-8").rstrip("\n")
        for line in frame_rows(list(reference_rows(expose, table, where=expose_id)), expose)
    ]
    text = read_text_nofollow(layout.emit_path(expose_id))
    written = [line for line in text.split("\n") if line]
    same = sum(1 for want, have in zip(expected, written, strict=False) if want == have)
    return RuleShare(
        expose_id=expose_id,
        rule_id=REFERENCE_RULE_ID,
        form=REFERENCE_FORM,
        origin=REFERENCE_FORM,
        holds=Holds(same, max(len(expected), len(written))),
        enforced=True,
    )


def _plan_reference_share(
    emit: Any,
    emitted: Mapping[str, Any],
    layout: RunLayout,
    plan_document: Mapping[str, Any],
    notes: List[str],
    reference_rules: List[RuleShare],
) -> None:
    """Add a reference expose's published-rows share to *reference_rules*, or a note saying why not."""
    expose_id = emit.expose_id
    if expose_id not in emitted or not is_regular_file(layout.emit_path(expose_id)):
        notes.append(f"{expose_id}: the run holds no rows for it")
        return
    try:
        reference_rules.append(_reference_share(emit, layout, plan_document))
    except (FluxError, *_UNREADABLE) as error:
        notes.append(f"{expose_id}: its published rows cannot be compared ({type(error).__name__})")


def _has_module_rules(
    emit: Any,
    entry: Any,
    made: Sequence[Any],
    unmade: Sequence[Any],
    counts: Sequence[Any] = (),
    dates: Sequence[Any] = (),
) -> bool:
    """Whether an expose declares anything the module checks read: rules, copies, links, bridges, counts or dates."""
    expose_id = emit.expose_id
    return bool(
        dates
        or contract_rule_ids(entry)
        or rule_ids_of(entry, emit.extension)
        or made
        or unmade
        or counts
        or history_entries_of(emit.extension, expose_id)
        or asof_entries_of(emit.extension, expose_id)
        or projection_entry_of(emit.extension, expose_id) is not None
        or link_entries_of(emit.extension, expose_id)
        or bridge_entries_of(emit.extension, expose_id)
    )


def _usable_copies(
    emit: Any,
    emitted: Mapping[str, Any],
    copies: Sequence[Tuple[str, str, str, Optional[str]]],
    needed: Set[str],
    notes: List[str],
    histories: Sequence[str] = (),
) -> Tuple[Tuple[str, str, str, Optional[str]], ...]:
    """The copies whose tables this run emits; each adds its tables to *needed*, each other has a note."""
    expose_id = emit.expose_id
    usable = []
    for copy in copies:
        more = tables_needed(
            emit.contract_document, expose_id, emit.extension, (copy,), histories=histories
        )
        missing = sorted(name for name in more if name not in emitted)
        if missing:
            notes.append(
                f"{expose_id}: the copy {copy[0]} cannot be checked, "
                f"it reads {missing}, which this run does not emit"
            )
        else:
            usable.append(copy)
            needed |= more
    return tuple(usable)


def _note_unwritten_history(
    emit: Any, emitted: Mapping[str, Any], layout: RunLayout, needed: Set[str], notes: List[str]
) -> None:
    """Leave out of *needed* a projected history the run wrote no rows for, and say it is not scored."""
    entry = projection_entry_of(emit.extension, emit.expose_id)
    if entry is None or entry.of not in needed:
        return
    if entry.of in emitted and is_regular_file(layout.emit_path(entry.of)):
        return
    needed.discard(entry.of)
    notes.append(
        f"{emit.expose_id}: its projection reads {entry.of}, whose rows this run did not write "
        "(the emit is hidden), so the projection is not scored"
    )


def _plan_rule_job(
    emit: Any,
    emitted: Mapping[str, Any],
    layout: RunLayout,
    copy_groups: Tuple[Any, Any],
    rule_jobs: Dict[str, RuleJob],
    notes: List[str],
    counted: FrozenSet[Tuple[str, str]] = frozenset(),
    counts: Tuple[CountsEntry, ...] = (),
    dates: Tuple[DateLine, ...] = (),
    time_bounds: Optional[Mapping[str, Any]] = None,
    histories: Tuple[HistoryEntry, ...] = (),
) -> None:
    """Plan one expose's rule job into *rule_jobs*, or add the note saying what keeps it from running."""
    expose_id = emit.expose_id
    if expose_id not in emitted or not is_regular_file(layout.emit_path(expose_id)):
        notes.append(f"{expose_id}: the run holds no rows for it")
        return
    try:
        needed = tables_needed(
            emit.contract_document,
            expose_id,
            emit.extension,
            counted=counted,
            counts=counts,
            histories=[found.emit for found in histories],
        )
        for line in dates:
            needed |= set(line.tables)
        _note_unwritten_history(emit, emitted, layout, needed, notes)
        absent = sorted(name for name in needed if name not in emitted)
        if absent:
            notes.append(f"{expose_id}: its rules read {absent}, which this run does not emit")
            return
        made, unmade = (
            _usable_copies(emit, emitted, group, needed, notes, [found.emit for found in histories])
            for group in copy_groups
        )
        rule_jobs[expose_id] = RuleJob(
            emit.contract_document,
            emit.extension,
            bool(emitted[expose_id].get("declaresFidelity")),
            tuple(sorted(needed)),
            made,
            unmade,
            counted,
            counts,
            dates,
            time_bounds,
            histories,
        )
    except (FluxError, Expr2Error) as error:
        notes.append(f"{expose_id}: {type(error).__name__} {getattr(error, 'code', '')}")


def _plan_links(
    emit: Any,
    emitted: Mapping[str, Any],
    notes: List[str],
    links: List[PlannedLink],
    key_columns_of: Dict[str, List[Tuple[str, ...]]],
) -> None:
    """Plan one expose's relationship checks and the key columns each side must be read as."""
    expose_id = emit.expose_id
    try:
        keys = compile_keys(emit.contract_document)
    except FluxError:
        return
    if keys is None or expose_id not in emitted:
        return
    for link in keys.expose(expose_id).relationships:
        if link.target not in emitted:
            notes.append(
                f"{expose_id}.{link.name}: its parent {link.target} is not emitted by this run"
            )
            continue
        links.append(PlannedLink(link, keys.expose(link.target).versioned))
        key_columns_of.setdefault(expose_id, []).append(tuple(link.from_columns))
        key_columns_of.setdefault(link.target, []).append(tuple(link.to_columns))


def _hidden_reason(
    line: DateLine, hidden_tables: Set[str], hidden_columns: Mapping[str, Set[str]]
) -> str:
    """Why a fact over dates cannot be scored on the run's streams, or ``""`` when it can."""
    tables = sorted({name for name in line.tables if name in hidden_tables})
    columns = sorted(
        {
            f"{expose}.{column}"
            for expose, column in line.reads
            if column in hidden_columns.get(expose, ())
        }
    )
    parts = []
    if columns:
        parts.append("the hidden column " + ", ".join(columns))
    if tables:
        parts.append("the hidden table " + ", ".join(tables))
    if not parts:
        return ""
    return (
        "it reads " + " and ".join(parts) + "; the run's streams hold no hidden column and "
        "no hidden table"
    )


def _plan_date_lines(
    resolved: Sequence[Any], facts: Sequence[Fact]
) -> Tuple[Dict[str, Tuple[DateLine, ...]], Tuple[Tuple[str, str], ...]]:
    """The facts over dates the bundle's lifetimes and ``within`` entries make (ADR 0024 section 8).

    Returns the lines each emit can score, with the tolerance a ``facts`` entry gives them, and the
    ``(name, reason)`` of each line that reads a hidden column or a hidden table, which the run's
    streams do not hold. A bundle that declares neither key makes none.
    """
    lifetimes = [
        entry for emit in resolved for entry in lifetime_entries_of(emit.extension, emit.expose_id)
    ]
    within = [
        entry for emit in resolved for entry in within_entries_of(emit.extension, emit.expose_id)
    ]
    if not (lifetimes or within):
        return {}, ()
    contracts = {emit.expose_id: emit.contract_document for emit in resolved}
    steps = {id(document): contract_step(document) for document in contracts.values()}

    def step(expose_id: str, relationship: str) -> Optional[str]:
        document = contracts.get(expose_id)
        return None if document is None else steps[id(document)](expose_id, relationship)

    hidden_tables = {emit.expose_id for emit in resolved if emit.is_hidden_table}
    hidden_columns = {
        emit.expose_id: {column.name for column in emit.hidden_columns} for emit in resolved
    }
    tolerances = {(fact.emit, fact.rule): fact.tolerance for fact in facts}
    scorable: Dict[str, List[DateLine]] = {}
    unscored: List[Tuple[str, str]] = []
    for line in date_lines(lifetimes, within, step):
        reason = _hidden_reason(line, hidden_tables, hidden_columns)
        if reason:
            unscored.append((line.name, reason))
            continue
        tolerance = tolerances.get((line.emit, line.rule_id))
        scorable.setdefault(line.emit, []).append(replace(line, tolerance=tolerance))
    return (
        {expose_id: tuple(lines) for expose_id, lines in scorable.items()},
        tuple(sorted(unscored)),
    )


def _copy_texts(
    emit: Any, related: Any, generates: bool
) -> Tuple[Tuple[Any, ...], Tuple[Any, ...]]:
    """The shared copy texts the emit generates, and those it does not, when it asks for copies."""
    expose_id = emit.expose_id
    # Only a Simulation that declares ``copies: true`` has copies to score or to report; any
    # other keeps the checks it always had (ADR 0022 section 4).
    if not copies_of(emit.extension):
        return (), ()
    made = shared_copy_texts(emit.contract_document, expose_id, related) if generates else ()
    unmade = shared_copy_texts_not_generated(
        emit.contract_document, expose_id, related, generates=generates
    )
    return made, unmade


def plan_module_checks(
    bundle_directory: Union[str, Path],
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    *,
    artifacts_ok: bool,
) -> ModulePlan:
    """Read the bundle once and say what every emit's module checks need.

    The bundle must be the run's: the same ``inputDigest`` the run recorded, under the kernel its plan
    names, since the facts' tolerances and the rules are read from it. An emit whose rules cannot be read
    adds a note and costs the other emits nothing.
    """
    from flux_engine.seam.resolve import (
        declared_counts,
        declared_facts,
        load_bundle,
        resolve_all_emits,
    )
    from flux_engine.stages.apply import plan_kernel
    from flux_engine.stages.pipeline import input_digest_of

    if not artifacts_ok:
        return ModulePlan(
            skipped="the run's artifacts do not reproduce run-digest.json (FLX-2110), so its "
            "rows are not the run's; re-run apply"
        )
    emitted = receipt.get("emits")
    emitted = emitted if isinstance(emitted, Mapping) else {}
    simulation_id = str(receipt.get("simulationId") or plan_document.get("simulationId"))
    try:
        loaded = load_bundle(Path(bundle_directory))
        if input_digest_of(loaded, kernel=plan_kernel(plan_document)).ref.value != str(
            receipt.get("inputDigest")
        ):
            return ModulePlan(
                skipped="the bundle is not the one this run was produced from (its inputDigest "
                "differs), so its rules and tolerances are not the run's"
            )
        resolved = resolve_all_emits(loaded, simulation_id=simulation_id)
        facts = declared_facts(loaded.simulation(simulation_id), where=simulation_id)
        entries = declared_counts(loaded.simulation(simulation_id), where=simulation_id)
        spec = loaded.simulation(simulation_id).get("spec")
        time_bounds = spec.get("timeBounds") if isinstance(spec, Mapping) else None
        declared = frozenset((entry.emit, entry.relationship) for entry in entries)
    except FluxError as error:
        return ModulePlan(notes=(f"the bundle cannot be read ({error.code.code})",))
    counter_based = plan_kernel(plan_document).seed_tree_version == kernel2.SEED_TREE_VERSION
    counted = declared if counter_based else frozenset()
    counted_entries = entries if counter_based else ()
    scored_dates, not_scored = _plan_date_lines(resolved, facts)
    rule_jobs: Dict[str, RuleJob] = {}
    key_columns_of: Dict[str, List[Tuple[str, ...]]] = {}
    notes: List[str] = []
    links: List[PlannedLink] = []
    reference_rules: List[RuleShare] = []
    histories = tuple(
        found for emit in resolved for found in history_entries_of(emit.extension, emit.expose_id)
    )
    for emit in resolved:
        expose_id = emit.expose_id
        if emit.is_hidden_table:
            continue  # no stream holds its rows: what reads it is not scored (ADR 0024 section 3)
        if emit.is_reference:
            _plan_reference_share(emit, emitted, layout, plan_document, notes, reference_rules)
        entry = declared_exposes(emit.contract_document).get(expose_id)
        related = related_columns(emit.keys)
        generates = counter_based and emit.fidelity is not None
        # Only a Simulation that declares ``copies: true`` has copies to score or to report; any
        # other keeps the checks it always had (ADR 0022 section 4).
        made, unmade = _copy_texts(emit, related, generates)
        counts = tuple(item for item in counted_entries if item.emit == expose_id)
        dates = scored_dates.get(expose_id, ())
        if entry is not None and _has_module_rules(emit, entry, made, unmade, counts, dates):
            _plan_rule_job(
                emit,
                emitted,
                layout,
                (made, unmade),
                rule_jobs,
                notes,
                counted,
                counts,
                dates,
                time_bounds,
                histories,
            )
        _plan_links(emit, emitted, notes, links, key_columns_of)
    jobs = {
        expose_id: ModuleJob(
            expose_id,
            rule_jobs.get(expose_id),
            tuple(dict.fromkeys(key_columns_of.get(expose_id, ()))),
        )
        for expose_id in sorted(set(rule_jobs) | set(key_columns_of))
    }
    return ModulePlan(
        jobs=jobs,
        links=tuple(links),
        facts=facts,
        notes=tuple(notes),
        not_scored=not_scored,
        reference_rules=tuple(reference_rules),
        counts=tuple(entries),
    )


def _types_of(layout: RunLayout, expose_id: str) -> Dict[str, str]:
    attestation = json.loads(read_text_nofollow(layout.seam_attestation_path(expose_id)))
    return dict(zip(attestation["columns"], attestation["types"], strict=True))


def module_pass(reader: Any, expose_id: str, job: ModuleJob) -> EmitModule:
    """One emit's module work, on the columns *reader* (a ``RunRows``) decoded once for every leg.

    The emit's rules are evaluated on its decoded cells, and each key column set other emits are joined on
    is taken as codes. Nothing is parsed again. What the decoded reader does not take, a key of another
    type or an emit it refused, is left to the row path with the same refusal (``None`` for the key).
    """
    notes: List[str] = []
    found: Tuple[RuleShare, ...] = ()
    if job.rules is not None:
        try:
            model = model_from_contract(job.rules.contract_document)
            tables = {}
            for name in job.rules.needed:
                table, problem = reader.table(name)
                if problem is not None:
                    raise ValueError("rows unreadable")
                tables[name] = stored_rows_of(model, name, table)
            found = rule_shares(
                job.rules.contract_document,
                expose_id,
                tables,
                RuleSource(
                    extension=job.rules.extension,
                    generated=job.rules.generated,
                    copies=job.rules.copies,
                    reported=job.rules.reported,
                    counted=job.rules.counted,
                    counts=job.rules.counts,
                    time_bounds=job.rules.time_bounds,
                    histories=job.rules.histories,
                ),
            )
            if job.rules.dates:
                found += date_shares(
                    job.rules.dates,
                    Rows(
                        model,
                        {name: rows for name, rows in tables.items() if name in model.exposes},
                    ),
                    generated=job.rules.generated,
                )
        except (FluxError, Expr2Error) as error:
            notes.append(f"{expose_id}: {type(error).__name__} {getattr(error, 'code', '')}")
        except _UNREADABLE as error:
            notes.append(
                f"{expose_id}: its rows cannot be read as the contract types them "
                f"({type(error).__name__})"
            )
    keys: Dict[Tuple[str, ...], Optional[KeyColumns]] = {}
    if job.keys:
        table, problem = reader.table(expose_id)
        try:
            types = _types_of(reader.layout, expose_id) if problem is None else {}
        except _UNREADABLE:
            types = {}
        for columns in job.keys:
            keys[columns] = (
                key_columns(table, columns, types) if problem is None and types else None
            )
    return EmitModule(found, keys, tuple(notes))


def finish_module_checks(
    plan: ModulePlan, results: Mapping[str, EmitModule], layout: RunLayout
) -> ModuleChecks:
    """Join the emits' answers, in the order of the plan's emits, into the run's :class:`ModuleChecks`."""
    if (
        plan.skipped
        or not plan.jobs
        and not plan.notes
        and not plan.reference_rules
        and not plan.not_scored
    ):
        return ModuleChecks(
            skipped=plan.skipped, notes=plan.notes, facts=plan.facts, counts=plan.counts
        )
    notes = list(plan.notes)
    rules: List[RuleShare] = list(plan.reference_rules)
    for expose_id in sorted(results):
        rules.extend(results[expose_id].rules)
        notes.extend(results[expose_id].notes)
    rows: Dict[str, Tuple[List[str], List[List[Any]]]] = {}

    def row_path(expose_id: str) -> Tuple[List[str], List[List[Any]]]:
        if expose_id not in rows:
            rows[expose_id] = _read_rows(layout, expose_id)
        return rows[expose_id]

    links: List[LinkIntegrity] = []
    for planned in plan.links:
        link = planned.link
        child = results.get(link.source)
        parent = results.get(link.target)
        child_keys = child.keys.get(tuple(link.from_columns)) if child is not None else None
        parent_keys = parent.keys.get(tuple(link.to_columns)) if parent is not None else None
        found = (
            link_integrity_columns(
                link, child_keys, parent_keys, parent_versioned=planned.parent_versioned
            )
            if child_keys is not None and parent_keys is not None
            else None
        )
        if found is None:
            try:
                child_names, child_rows = row_path(link.source)
                parent_names, parent_rows = row_path(link.target)
                found = link_integrity(
                    link,
                    child_names,
                    child_rows,
                    parent_names,
                    parent_rows,
                    parent_versioned=planned.parent_versioned,
                )
            except _UNREADABLE as error:
                notes.append(
                    f"{link.source}.{link.name}: its rows cannot be read ({type(error).__name__})"
                )
                continue
        given = _given_of(plan.counts, link)
        if given is not None and not planned.parent_versioned:
            # A relationship laid out given a column of its parent is scored per class of it
            # (ADR 0037 decision 7), so its parents are counted apart by that column's value.
            try:
                child_names, child_rows = row_path(link.source)
                parent_names, parent_rows = row_path(link.target)
                found = replace(
                    found,
                    parents_by_group=grouped_spread(
                        link,
                        child_names,
                        child_rows,
                        parent_names,
                        parent_rows,
                        given,
                        _types_of(layout, link.target).get(given, ""),
                    ),
                )
            except _UNREADABLE as error:
                notes.append(
                    f"{link.source}.{link.name}: its rows cannot be grouped by {given} "
                    f"({type(error).__name__})"
                )
        links.append(found)
    return ModuleChecks(
        rules=tuple(rules),
        links=tuple(links),
        facts=plan.facts,
        notes=tuple(notes),
        counts=plan.counts,
        not_scored=plan.not_scored,
    )


def _given_of(counts: Sequence[CountsEntry], link: Relationship) -> Optional[str]:
    """The column of the parent a ``counts`` entry lays the relationship out given, or ``None``."""
    for entry in counts:
        if (entry.emit, entry.relationship) == (link.source, link.name):
            return entry.given
    return None


def module_checks(
    bundle_directory: Union[str, Path],
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    *,
    artifacts_ok: bool,
) -> ModuleChecks:
    """Plan, run and join the module checks in this process: what ``ship`` calls."""
    from flux_engine.stages.verify import RunRows

    plan = plan_module_checks(
        bundle_directory, layout, receipt, plan_document, artifacts_ok=artifacts_ok
    )
    reader = RunRows(layout)
    results = {
        expose_id: module_pass(reader, expose_id, job) for expose_id, job in plan.jobs.items()
    }
    return finish_module_checks(plan, results, layout)
