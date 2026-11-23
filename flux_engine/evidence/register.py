# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``parameter-register.json``, and what ``verify``, ``ship`` and ``EVIDENCE.md`` say of it.

docs/spec/PARAMETER-REGISTER-SPEC.md. A Simulation that declares a
parameter register gets a record of every number its run reads, each with what
it rests on: a statistic of the pinned profile, a place in the contract (format
1-1-0), an assumption and its reason, or a default
(:mod:`flux_engine.seam.parameters` decides which), and, in format 1-2-0, the
statistics of the pinned profile no number rests on, each with its reason.
``apply`` writes the record next to the run, ``ship`` packs it, and the reports
state the shares and name the unused statistics.

**The plan holds its digest,** as it holds ``column-provenance.json``'s: ``plan``
builds the record from its own execution and writes ``parameterRegisterDigest``,
the ``raw-bytes`` digest of the file's exact bytes, into ``plan.json``. ``verify``
and ``ship`` read the record only through that digest, and a record that is
missing, moved or unreadable is ``FLX-2116``. Unlike the column record, the key
is there only when the Simulation declares a register, so a bundle that declares
none plans to the bytes, and the ``planDigest``, it had.

**The envelope is OpenLineage's** (``_producer``, ``_schemaURL``), as the
profile's and the scorecard's are, so the record can ride on a lineage event
as a custom run facet. Nothing in it comes from the profile's values: it names
the pinned digest and the pointers the bundle declares, and those are all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.digest import DigestRef, digest_of_bytes
from flux_engine.evidence.provenance import quoted_name
from flux_engine.lineage.emitter import OPENLINEAGE_PRODUCER
from flux_engine.seam.parameters import (
    RESPONSE_CONTROL,
    RESPONSE_EFFECT,
    SOURCES_1_0,
    SOURCES_1_2,
    SOURCES_1_3,
    ParameterRegister,
    RegisterEntry,
    Source,
)
from flux_stats.integers import is_int_at_least

__all__ = [
    "REGISTER_DIGEST_KEY",
    "REGISTER_FILE",
    "REGISTER_SCHEMA_URL",
    "REGISTER_SCHEMA_URL_1_1",
    "REGISTER_SCHEMA_URL_1_2",
    "REGISTER_SCHEMA_URL_1_3",
    "Assumption",
    "BoundRegister",
    "RegisterSummary",
    "UnusedNote",
    "build_register_document",
    "parse_register",
    "register_digest",
    "render_register",
    "render_register_line",
    "render_register_note",
]

#: The key under which ``plan.json`` records the digest of the record.
REGISTER_DIGEST_KEY = "parameterRegisterDigest"

#: The record's file name in the run directory, and so under ``run/`` in a pack.
REGISTER_FILE = "parameter-register.json"

#: The record schema's ``$id`` for format 1-0-0. The format version is part of it.
REGISTER_SCHEMA_URL = (
    "https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-0-0/"
    "FluxParameterRegister.json"
)

#: Format 1-1-0, which adds the ``contract`` source. A record names it only when an
#: entry uses that source, so every other record keeps the 1-0-0 bytes it had.
REGISTER_SCHEMA_URL_1_1 = (
    "https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-1-0/"
    "FluxParameterRegister.json"
)

#: Format 1-2-0, which adds ``unused``: the statistics of the pinned profile no number
#: rests on. A record names it only when the Simulation lists them, so every other
#: record keeps the bytes it had.
REGISTER_SCHEMA_URL_1_2 = (
    "https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-2-0/"
    "FluxParameterRegister.json"
)

#: Format 1-3-0, which adds a calibrated value (``source: calibrated`` and its ``calibration``
#: member), the response estimates derived at plan time, and the overlay additions
#: (``source: overlay`` with ``overlay`` and ``statistic`` on its entries, and ``replacedBy``;
#: ADR 0030 section 8). A record names it only when the register holds one of them, so every
#: other record keeps the bytes it had.
REGISTER_SCHEMA_URL_1_3 = (
    "https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-3-0/"
    "FluxParameterRegister.json"
)

#: The sources each format this build reads lists, in report order.
_FORMAT_SOURCES: Mapping[str, Tuple[str, ...]] = {
    REGISTER_SCHEMA_URL: tuple(source.value for source in SOURCES_1_0),
    REGISTER_SCHEMA_URL_1_1: tuple(source.value for source in SOURCES_1_2),
    REGISTER_SCHEMA_URL_1_2: tuple(source.value for source in SOURCES_1_2),
    REGISTER_SCHEMA_URL_1_3: tuple(source.value for source in SOURCES_1_3),
}

#: A pinned profile's digest, as the record names it (the pin's own spelling).
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _entry_document(entry: RegisterEntry) -> Dict[str, Any]:
    """One entry as the record writes it: the keys the entry's source takes, as declared."""
    document: Dict[str, Any] = {
        "index": entry.index,
        "document": entry.document,
        "pointer": entry.pointer,
        "source": entry.source.value,
    }
    if entry.citation is not None:
        document["statistic"] = entry.citation.as_document()
    if entry.contract is not None:
        document["contract"] = entry.contract.as_document()
    if entry.overlay is not None:
        document["overlay"] = entry.overlay
    if entry.replaced_by is not None:
        document["replacedBy"] = entry.replaced_by.as_document()
    if entry.reason is not None:
        document["reason"] = entry.reason
    if entry.calibration is not None:
        document["calibration"] = entry.calibration.as_document()
    return document


def _schema_url(register: ParameterRegister) -> str:
    """The schema URL of the format *register* needs: the latest of its additions."""
    if register.uses_1_3_additions:
        return REGISTER_SCHEMA_URL_1_3
    if register.lists_unused:
        return REGISTER_SCHEMA_URL_1_2
    return REGISTER_SCHEMA_URL_1_1 if register.uses_contract else REGISTER_SCHEMA_URL


def build_register_document(register: ParameterRegister) -> Dict[str, Any]:
    """The record ``apply`` writes: the entries as read, then every number with its source.

    A number's ``entry`` is the index of the entry that decided it, or ``null``
    for a default no entry names. ``counts`` holds every source of the record's
    format, so a share is a count over ``declared`` with nothing to infer. The
    format is 1-3-0 when the register holds an overlay, a calibrated value, a response estimate
    or a ``replacedBy``, 1-2-0 when the Simulation lists the profile's unused statistics, 1-1-0
    when an entry names the contract, and 1-0-0 otherwise.
    """
    entries = [_entry_document(entry) for entry in register.entries]
    entries.extend(derived.as_document() for derived in register.derived)
    numbers = [
        {
            "document": registered.number.document,
            "kind": registered.number.kind,
            "pointer": registered.number.pointer,
            "value": registered.number.value,
            "source": registered.source.value,
            "entry": registered.entry.index if registered.entry is not None else None,
        }
        for registered in register.numbers
    ]
    url = _schema_url(register)
    record: Dict[str, Any] = {
        "_producer": OPENLINEAGE_PRODUCER,
        "_schemaURL": url,
        "simulationId": register.simulation_id,
        "profile": (
            {"digest": register.profile_digest} if register.profile_digest is not None else None
        ),
        "declared": len(numbers),
        "counts": register.counts(),
        "entries": entries,
        "parameters": numbers,
    }
    if register.unused is not None:
        record["unused"] = [item.as_document() for item in register.unused]
    return record


def render_register(register: ParameterRegister) -> str:
    """The exact text of the record: sorted keys, two-space indent, a final newline."""
    return json.dumps(build_register_document(register), indent=2, sort_keys=True) + "\n"


def register_digest(data: bytes) -> DigestRef:
    """The ``raw-bytes`` digest of a record, from its bytes exactly as on disk.

    No parsing, so a damaged or reformatted file digests to something else and
    the plan's digest does not match, as for ``column-provenance.json``.
    """
    return digest_of_bytes(data, subject=REGISTER_DIGEST_KEY)


@dataclass(frozen=True)
class Assumption:
    """One assumption entry: what it names, its reason, and how many numbers it decided."""

    document: str
    pointer: str
    reason: str
    numbers: int
    #: The statistic a profile of the contract would publish for it (format 1-3-0): its pointer
    #: and definition, or ``None`` when the entry names none.
    replaced_by: Optional[Tuple[str, Optional[str]]] = None


@dataclass(frozen=True)
class UnusedNote:
    """One statistic of the pinned profile no number rests on: its pointer, definition, reason."""

    pointer: str
    definition: Optional[str]
    reason: str


@dataclass(frozen=True)
class RegisterSummary:
    """What the reports say of a record: its shares, and the assumptions behind them."""

    simulation_id: str
    profile_digest: Optional[str]
    declared: int
    counts: Mapping[str, int]
    assumptions: Tuple[Assumption, ...]
    #: Defaults an entry states, as against defaults no entry names.
    stated_defaults: int
    #: The statistics of the pinned profile no number rests on, when the record lists
    #: them (formats 1-2-0 and 1-3-0); ``None`` for a record that does not.
    unused: Optional[Tuple[UnusedNote, ...]] = None
    #: The overlays the record's entries cite (format 1-3-0), by name; empty for any other record.
    overlays: Tuple[str, ...] = ()

    def as_document(self) -> Dict[str, Any]:
        """The shares as a stage's ``--json`` envelope carries them."""
        document: Dict[str, Any] = {"declared": self.declared, "counts": dict(self.counts)}
        if self.unused is not None:
            document["unused"] = len(self.unused)
        return document


@dataclass(frozen=True)
class BoundRegister:
    """A record the plan vouches for: what it says, and the exact bytes that hashed to the plan's digest."""

    summary: RegisterSummary
    data: bytes


def _count(value: Any, where: str) -> int:
    if not is_int_at_least(value, 0):
        raise ValueError(f"{where} is a count")
    return value


def parse_register(text: str) -> RegisterSummary:
    """Read a record back for the reports; ``ValueError`` when it is not one this module wrote.

    The checks are the record's own arithmetic: ``counts`` names the sources of
    the record's format, they add up to ``declared``, which is the number of
    rows, and each row's source is the source of the entry it names (``default``
    for none). Nothing is repaired or guessed.
    """
    document = json.loads(text)
    known, shares, declared, entries, rows = _totals(document)
    sources = _entry_sources(entries, known)
    _check_format_1_3(document, entries)
    tally, decided = _row_sources(rows, sources, known)
    if tally != shares:
        raise ValueError("the record's counts are not the sources of its parameters")
    digest = _profile_digest(document.get("profile"))
    replacements = _replacements(entries, sources, document.get("_schemaURL"))
    assumptions = tuple(
        Assumption(
            document=str(entry.get("document", "")),
            pointer=str(entry.get("pointer", "")),
            reason=str(entry.get("reason", "")),
            numbers=decided.get(position, 0),
            replaced_by=replacements.get(position),
        )
        for position, entry in enumerate(entries)
        if sources[position] == Source.ASSUMPTION.value
    )
    stated = sum(
        decided.get(position, 0)
        for position in sources
        if sources[position] == Source.DEFAULT.value
    )
    return RegisterSummary(
        simulation_id=str(document.get("simulationId", "")),
        profile_digest=digest,
        declared=declared,
        counts=shares,
        assumptions=assumptions,
        stated_defaults=stated,
        unused=_unused_notes(document),
        overlays=_overlay_names(entries, sources),
    )


def _replacements(
    entries: List[Any], sources: Mapping[int, str], url: Any
) -> Dict[int, Tuple[str, Optional[str]]]:
    """Each ``replacedBy`` of the entries, by the entry's index: format 1-3-0, assumptions and defaults."""
    found: Dict[int, Tuple[str, Optional[str]]] = {}
    for position, entry in enumerate(entries):
        cited = entry.get("replacedBy") if isinstance(entry, Mapping) else None
        if cited is not None:
            found[position] = _replacement_of(cited, sources[position], url)
    return found


def _replacement_of(cited: Any, source: str, url: Any) -> Tuple[str, Optional[str]]:
    """One entry's ``replacedBy`` as a pointer and a definition; ``ValueError`` when it is not one."""
    if url != REGISTER_SCHEMA_URL_1_3 or source not in (
        Source.ASSUMPTION.value,
        Source.DEFAULT.value,
    ):
        raise ValueError("only an assumption or a default of a format 1-3-0 record is replaced")
    pointer = cited.get("pointer") if isinstance(cited, Mapping) else None
    definition = cited.get("definition") if isinstance(cited, Mapping) else None
    if not isinstance(pointer, str) or not (definition is None or isinstance(definition, str)):
        raise ValueError("an entry's replacedBy names a pointer and a definition")
    return pointer, definition


def _overlay_names(entries: List[Any], sources: Mapping[int, str]) -> Tuple[str, ...]:
    """The names of the overlays the entries cite, sorted: format 1-3-0's ``overlay`` entries."""
    names = set()
    for position, entry in enumerate(entries):
        if sources[position] != Source.OVERLAY.value:
            continue
        name = entry.get("overlay") if isinstance(entry, Mapping) else None
        if not isinstance(name, str) or not name:
            raise ValueError("an overlay entry names its overlay")
        names.add(name)
    return tuple(sorted(names))


def _check_estimate_entry(entry: Any, kind: Any) -> None:
    """An entry with a ``kind`` is a response estimate: a ``profile`` entry, well formed."""
    if kind not in (RESPONSE_CONTROL, RESPONSE_EFFECT):
        raise ValueError("an entry's kind is a response estimate's")
    if entry.get("source") != Source.PROFILE.value:
        raise ValueError("a response estimate rests on the profile")
    wanted = 1 if kind == RESPONSE_CONTROL else 2
    cells = entry.get("cells")
    if not isinstance(cells, list) or len(cells) != wanted:
        raise ValueError("a response estimate names the cells it was read from")
    if ("interval" in entry) != (kind == RESPONSE_EFFECT) or not isinstance(
        entry.get("value"), str
    ):
        raise ValueError("a response effect has an interval and a control rate has none")


def _check_format_1_3(document: Mapping[str, Any], entries: List[Any]) -> None:
    """The additions of format 1-3-0 sit only in a record of that format, and are well formed.

    A ``calibrated`` entry has its ``calibration`` member and no other has one; an
    entry with a ``kind`` is a response estimate: a ``profile`` entry with its
    ``value`` and ``cells``, and an ``interval`` for an effect only.
    """
    later = document.get("_schemaURL") == REGISTER_SCHEMA_URL_1_3
    for entry in entries:
        calibrated = entry.get("source") == Source.CALIBRATED.value
        if calibrated != ("calibration" in entry):
            raise ValueError("only a calibrated entry has a calibration, and each one does")
        kind = entry.get("kind")
        if kind is not None:
            _check_estimate_entry(entry, kind)
    if not later and any("calibration" in entry or "kind" in entry for entry in entries):
        raise ValueError("only a record of format 1-3-0 holds a calibration or an estimate")


def _unused_note(item: Any) -> UnusedNote:
    """One ``unused`` item of a record: its statistic's pointer and definition, and the reason."""
    statistic = item.get("statistic") if isinstance(item, Mapping) else None
    pointer = statistic.get("pointer") if isinstance(statistic, Mapping) else None
    reason = item.get("reason") if isinstance(item, Mapping) else None
    if not isinstance(pointer, str) or not isinstance(reason, str):
        raise ValueError("an unused statistic names its pointer and its reason")
    definition = statistic.get("definition") if isinstance(statistic, Mapping) else None
    return UnusedNote(
        pointer=pointer,
        definition=definition if isinstance(definition, str) else None,
        reason=reason,
    )


def _unused_notes(document: Mapping[str, Any]) -> Optional[Tuple[UnusedNote, ...]]:
    """The record's ``unused`` list, which format 1-2-0 holds and no earlier one does."""
    listed = document.get("unused")
    url = document.get("_schemaURL")
    if url not in (REGISTER_SCHEMA_URL_1_2, REGISTER_SCHEMA_URL_1_3):
        if listed is not None:
            raise ValueError("only a record of format 1-2-0 or later lists unused statistics")
        return None
    if listed is None and url == REGISTER_SCHEMA_URL_1_3:
        return None  # format 1-3-0 lists them only when the Simulation does
    if not isinstance(listed, list):
        raise ValueError("a record of this format lists its unused statistics")
    notes = [_unused_note(item) for item in listed]
    return tuple(notes)


def _totals(
    document: Any,
) -> Tuple[Tuple[str, ...], Dict[str, int], int, List[Any], List[Any]]:
    """The format's sources, the shares, the declared count, the entries and the rows.

    They have to add up.
    """
    url = document.get("_schemaURL") if isinstance(document, Mapping) else None
    known = _FORMAT_SOURCES.get(url) if isinstance(url, str) else None
    if known is None:
        raise ValueError("not a parameter register this build reads")
    counts = document.get("counts")
    if not isinstance(counts, Mapping) or sorted(counts) != sorted(known):
        raise ValueError("the record's counts name the sources of its format")
    shares = {source: _count(counts[source], f"counts.{source}") for source in known}
    declared = _count(document.get("declared"), "declared")
    entries = document.get("entries")
    rows = document.get("parameters")
    if not isinstance(entries, list) or not isinstance(rows, list):
        raise ValueError("the record's entries and parameters are lists")
    if sum(shares.values()) != declared or len(rows) != declared:
        raise ValueError("the record's counts do not add up to its parameters")
    return known, shares, declared, entries, rows


def _entry_sources(entries: List[Any], known: Tuple[str, ...]) -> Dict[int, str]:
    """Each entry's source, by the entry's index, which has to be its place in the list."""
    sources: Dict[int, str] = {}
    for position, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or type(entry.get("index")) is not int:
            raise ValueError("the record's entries are listed by index")
        if entry["index"] != position:
            raise ValueError("the record's entries are listed by index")
        if entry.get("source") not in known:
            raise ValueError("an entry's source is one its format knows")
        sources[position] = str(entry["source"])
    return sources


def _row_sources(
    rows: List[Any], sources: Mapping[int, str], known: Tuple[str, ...]
) -> Tuple[Dict[str, int], Dict[int, int]]:
    """How many rows rest on each source, and how many each entry decides.

    Each row's source has to be the source of the entry it names, or
    ``default`` when it names none.
    """
    decided: Dict[int, int] = {}
    tally = {source: 0 for source in known}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("a parameter is a mapping")
        index = row.get("entry")
        if index is not None and type(index) is not int:
            raise ValueError("a parameter's entry is an index or null")
        expected = Source.DEFAULT.value if index is None else sources.get(index)
        if expected is None or row.get("source") != expected:
            raise ValueError("a parameter's source is not its entry's")
        tally[expected] += 1
        if index is not None:
            decided[index] = decided.get(index, 0) + 1
    return tally, decided


def _profile_digest(profile: Any) -> Optional[str]:
    """The pinned profile's digest, or ``None`` when the record names no profile."""
    digest = profile.get("digest") if isinstance(profile, Mapping) else None
    if profile is not None and (not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        raise ValueError("the record's profile is null or a sha256 digest")
    return str(digest) if isinstance(digest, str) else None


def _percent(count: int, total: int) -> str:
    """*count* over *total* as a percentage: exact when whole, else to one place, half-even.

    Never 0 or 100 when it is not exactly so: a share that rounds to either end
    is written as below 0.1 % or above 99.9 %, so one assumption among
    thousands of defaults is not reported as none.
    """
    share = Fraction(count * 100, total)
    if share.denominator == 1:
        return f"{share.numerator} %"
    rounded = (Decimal(share.numerator) / Decimal(share.denominator)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_EVEN
    )
    if rounded == 0:
        return "below 0.1 %"
    if rounded == 100:
        return "above 99.9 %"
    return f"{rounded} %"


def _from_profile(summary: RegisterSummary) -> str:
    return "the pinned profile" if summary.profile_digest is not None else "a profile"


def render_register_line(summary: RegisterSummary) -> str:
    """One line for ``plan``, ``verify`` and ``ship``: how many numbers, and what they rest on."""
    counts = summary.counts
    if not summary.declared:
        return "parameters: the run reads no number the register counts" + _unused_clause(summary)
    return (
        f"parameters: {summary.declared} declared -- "
        f"{counts[Source.PROFILE.value]} from {_from_profile(summary)}, "
        + (
            f"{counts[Source.CONTRACT.value]} from the contract, "
            if Source.CONTRACT.value in counts
            else ""
        )
        + (
            f"{counts[Source.OVERLAY.value]} from the overlay, "
            if counts.get(Source.OVERLAY.value)
            else ""
        )
        + (
            f"{counts[Source.CALIBRATED.value]} calibrated, "
            if counts.get(Source.CALIBRATED.value)
            else ""
        )
        + f"{counts[Source.ASSUMPTION.value]} assumed, "
        f"{counts[Source.DEFAULT.value]} default" + _unused_clause(summary)
    )


def _unused_clause(summary: RegisterSummary) -> str:
    """How many statistics of the profile no number rests on, when the record lists them."""
    if summary.unused is None:
        return ""
    count = len(summary.unused)
    noun = "statistic" if count == 1 else "statistics"
    return f"; {count} {noun} of the profile unused"


def _prose(reason: str) -> str:
    """An author's reason, as the report can carry it: a code span, or withheld.

    In a code span nothing is markup: no link, no emphasis, no HTML, and no
    renderer turns a URL into a link (GitHub's autolinks do not reach inside
    one). So the reason is printed as :func:`~flux_engine.evidence.provenance.quoted_name`
    prints a column name, and withheld when it could not be: a reserved word
    (the whole report would be refused, ``FLX-8030``), a character that is not
    plain text, or a backtick. The record keeps the reason as it was given.
    """
    return quoted_name(reason, "a reason this report does not print (it is in the register file)")


def _share(count: int, total: int) -> str:
    return f"{count} ({_percent(count, total)})"


def _defaults_detail(defaults: int, stated: int) -> str:
    """Which of the defaults an entry states, and which no entry names."""
    if not defaults:
        return ""
    if not stated:
        return " (no entry names it)" if defaults == 1 else " (no entry names them)"
    unnamed = defaults - stated
    return (
        f" ({stated} stated as {'a default' if stated == 1 else 'defaults'} by an entry, "
        f"{unnamed} named by no entry)"
    )


def _calibrated_clause(counts: Mapping[str, int], total: int) -> str:
    """The note's words on calibrated values; none for a register of a format that has none.

    A register of format 1-3-0 counts them even when there are none, and the words it has always
    carried then stay as they were. A register that holds one says the bundle declares it: the
    engine reads the record and the digests it names and does not re-run the fit.
    """
    if Source.CALIBRATED.value not in counts:
        return ""
    found = counts[Source.CALIBRATED.value]
    share = f"{_share(found, total)} are calibrated values, "
    if not found:
        return share + "fitted to targets of the pinned profile, "
    return (
        share + "which the bundle declares were fitted to targets of the pinned profile and "
        "passed a held-out check, with the digests it names (the engine did not re-run the fit), "
    )


def render_register_note(summary: RegisterSummary) -> str:
    """What ``EVIDENCE.md`` adds under "What Was Not Proven" for a bound register.

    One paragraph: the shares, which defaults an entry states, and what the
    register does not show (it records what each number rests on, and ``ship``
    checked it is the record the plan names; it checks no number against the
    statistic it cites). Then, when there are assumptions, the list of them with
    their reasons, which the What Was Not Proven list calls for in this section.
    """
    total = summary.declared
    if not total:
        return (
            "Parameter register: this run reads no number the register counts, so it states "
            "no share."
        ) + _render_unused(summary)
    counts = summary.counts
    profile = counts[Source.PROFILE.value]
    assumed = counts[Source.ASSUMPTION.value]
    defaults = counts[Source.DEFAULT.value]
    if summary.profile_digest is not None:
        measured = f"come from statistics of the pinned profile `{summary.profile_digest}`"
    else:
        measured = "come from a profile (the Simulation pins none)"
    contract = (
        f"{_share(counts[Source.CONTRACT.value], total)} come from the contract the run "
        "emits from, "
        if Source.CONTRACT.value in counts
        else ""
    )
    overlay = (
        f"{_share(counts[Source.OVERLAY.value], total)} come from "
        f"{_overlay_phrase(summary.overlays)}, a scenario the maintainers wrote and not a "
        "measurement, "
        if counts.get(Source.OVERLAY.value)
        else ""
    )
    calibrated = _calibrated_clause(counts, total)
    paragraph = (
        f"Parameter register: the register counts {total} "
        f"number{'s' if total != 1 else ''} of this run: the numbers under `spec` in the "
        "documents the run reads, and the numbers the Simulation's `com.agentics.flux/1` "
        "namespace holds at the positions the specification names (the fidelity block, the "
        "rows and the rules). It does not count seeds, a sweep, the fidelity block's "
        "`encoding` and `budget`, or the namespace's own declarations of how rows relate and "
        "fall in time (such as `links`, `history` and `within`), so the run reads numbers this "
        f"report does not show. {_share(profile, total)} {measured}, "
        f"{contract}{overlay}{calibrated}"
        f"{_share(assumed, total)} are assumptions and {_share(defaults, total)} are "
        "defaults, which no measurement or assumption supports"
        f"{_defaults_detail(defaults, summary.stated_defaults)}. The register "
        "(`run/parameter-register.json`) records what each number rests on. It does not "
        "show that any number is right, and `ship` compared no number with the statistic it "
        "cites."
    )
    listed: List[str] = []
    for assumption in summary.assumptions:
        where = (
            f"{quoted_name(assumption.document, 'a document')} "
            f"{quoted_name(assumption.pointer, 'at a pointer this report does not print')}"
        )
        noun = "number" if assumption.numbers == 1 else "numbers"
        line = f"- {where} ({assumption.numbers} {noun}): {_prose(assumption.reason)}"
        if assumption.replaced_by is not None:
            pointer, definition = assumption.replaced_by
            named = quoted_name(pointer, "a statistic at a pointer this report does not print")
            kind = f" ({quoted_name(definition, 'a definition')})" if definition else ""
            line += f" A profile statistic, {named}{kind}, would replace it."
        listed.append(line)
    text = paragraph
    if listed:
        text += "\n\nThe assumptions:\n\n" + "\n".join(listed)
    return text + _render_unused(summary)


def _overlay_phrase(names: Sequence[str]) -> str:
    """The overlays a record cites, named as a code span each: ``the overlay `a```."""
    if not names:
        return "an overlay"
    shown = [
        quoted_name(name, "an overlay whose name this report does not print") for name in names
    ]
    return ("the overlay " if len(shown) == 1 else "the overlays ") + ", ".join(shown)


def _render_unused(summary: RegisterSummary) -> str:
    """The statistics of the pinned profile no number rests on, as the report lists them."""
    if summary.unused is None:
        return ""
    if not summary.unused:
        return (
            "\n\nEvery statistic of the pinned profile is used: the register lists none that "
            "no number rests on."
        )
    count = len(summary.unused)
    lines = [
        f"\n\nThe pinned profile publishes {count} "
        f"statistic{'s' if count != 1 else ''} no number of this run rests on, so the run "
        "shows nothing about " + ("it" if count == 1 else "them") + ":\n"
    ]
    for item in summary.unused:
        name = quoted_name(item.pointer, "a statistic at a pointer this report does not print")
        definition = f" ({quoted_name(item.definition, 'a definition')})" if item.definition else ""
        lines.append(f"- {name}{definition}: {_prose(item.reason)}")
    return "\n".join(lines)
