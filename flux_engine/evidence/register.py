# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``parameter-register.json``, and what ``verify``, ``ship`` and ``EVIDENCE.md`` say of it.

docs/spec/PARAMETER-REGISTER-SPEC.md. A Simulation that declares a
parameter register gets a record of every number its run reads, each with what
it rests on: a statistic of the pinned profile, a place in the contract (format
1-1-0), an assumption and its reason, or a default
(:mod:`flux_engine.seam.parameters` decides which). ``apply`` writes the
record next to the run, ``ship`` packs it, and the reports state the shares.

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
from typing import Any, Dict, List, Mapping, Optional, Tuple

from flux_engine.core.digest import DigestRef, digest_of_bytes
from flux_engine.evidence.provenance import quoted_name
from flux_engine.lineage.emitter import OPENLINEAGE_PRODUCER
from flux_engine.seam.parameters import SOURCES_1_0, ParameterRegister, Source
from flux_stats.integers import is_int_at_least

__all__ = [
    "REGISTER_DIGEST_KEY",
    "REGISTER_FILE",
    "REGISTER_SCHEMA_URL",
    "REGISTER_SCHEMA_URL_1_1",
    "Assumption",
    "BoundRegister",
    "RegisterSummary",
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

#: The sources each format this build reads lists, in report order.
_FORMAT_SOURCES: Mapping[str, Tuple[str, ...]] = {
    REGISTER_SCHEMA_URL: tuple(source.value for source in SOURCES_1_0),
    REGISTER_SCHEMA_URL_1_1: tuple(source.value for source in Source),
}

#: A pinned profile's digest, as the record names it (the pin's own spelling).
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def build_register_document(register: ParameterRegister) -> Dict[str, Any]:
    """The record ``apply`` writes: the entries as read, then every number with its source.

    A number's ``entry`` is the index of the entry that decided it, or ``null``
    for a default no entry names. ``counts`` holds every source of the record's
    format, so a share is a count over ``declared`` with nothing to infer. The
    format is 1-1-0 when an entry names the contract, and 1-0-0 otherwise.
    """
    entries = []
    for entry in register.entries:
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
        if entry.reason is not None:
            document["reason"] = entry.reason
        entries.append(document)
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
    return {
        "_producer": OPENLINEAGE_PRODUCER,
        "_schemaURL": REGISTER_SCHEMA_URL_1_1 if register.uses_contract else REGISTER_SCHEMA_URL,
        "simulationId": register.simulation_id,
        "profile": (
            {"digest": register.profile_digest} if register.profile_digest is not None else None
        ),
        "declared": len(numbers),
        "counts": register.counts(),
        "entries": entries,
        "parameters": numbers,
    }


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

    def as_document(self) -> Dict[str, Any]:
        """The shares as a stage's ``--json`` envelope carries them."""
        return {"declared": self.declared, "counts": dict(self.counts)}


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
    tally, decided = _row_sources(rows, sources, known)
    if tally != shares:
        raise ValueError("the record's counts are not the sources of its parameters")
    digest = _profile_digest(document.get("profile"))
    assumptions = tuple(
        Assumption(
            document=str(entry.get("document", "")),
            pointer=str(entry.get("pointer", "")),
            reason=str(entry.get("reason", "")),
            numbers=decided.get(position, 0),
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
    )


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
        return "parameters: the run reads no number the register counts"
    return (
        f"parameters: {summary.declared} declared -- "
        f"{counts[Source.PROFILE.value]} from {_from_profile(summary)}, "
        + (
            f"{counts[Source.CONTRACT.value]} from the contract, "
            if Source.CONTRACT.value in counts
            else ""
        )
        + f"{counts[Source.ASSUMPTION.value]} assumed, "
        f"{counts[Source.DEFAULT.value]} default"
    )


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
        )
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
    paragraph = (
        f"Parameter register: this run reads {total} declared "
        f"number{'s' if total != 1 else ''}. {_share(profile, total)} {measured}, "
        f"{contract}"
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
        listed.append(f"- {where} ({assumption.numbers} {noun}): {_prose(assumption.reason)}")
    if not listed:
        return paragraph
    return paragraph + "\n\nThe assumptions:\n\n" + "\n".join(listed)
