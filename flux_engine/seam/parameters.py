# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The parameter register: where each number a run declares came from.

docs/spec/PARAMETER-REGISTER-SPEC.md. A Simulation can say, for the numbers its
run reads, what each one rests on: a statistic of the profile it pins, a place
in the FLUID contract it emits from (register format 1-1-0), an assumption with
its reason, or a default. It says so in one list under the engine's namespace,
beside the pin those citations depend on, and it may list the statistics of the
pinned profile that no number rests on, each with its reason (``unused``,
register format 1-2-0)::

    extensions:
      com.agentics.flux/1:
        profile:
          digest: sha256:...
        parameters:
          - document: q3-retention-world
            pointer: /spec/population/size
            source: profile
            statistic: { pointer: /tables/subscriber/rowCount }
          - document: jr-payment-recovery
            pointer: /spec/personaReactions/persona-pragmatic-family/successProbability
            source: assumption
            reason: The campaign team's estimate from the pilot.

**Every number is counted, named or not.** The register's denominator is not
its list of entries. It is every number the run reads (:func:`declared_numbers`):
every JSON number under ``spec`` in each document of the run's cast and in each
Playback bound to its World, and every number at a position the engine's own
namespace reads as one. A number no entry names is a ``default``, decided by the
engine rather than by an entry, as an SDMX observation with no status reads as
normal. So the shares always add up to every number the run reads, and a
register that names one number cannot report that number as all of them.

Seeds and the sweep identify runs rather than describe the world, and the
fidelity block's ``encoding`` and ``budget`` set how the check computes, so
they are not counted (:data:`NOT_PARAMETERS`, :data:`ENGINE_BLOCK_NUMBERS`).

**Addresses are RFC 6901 JSON Pointers**, into a document of the run named by
its id. A pointer to a mapping or a list names every number under it, and the
most specific entry decides a number. A statistic is cited by a pointer into the
pinned profile and the registered definition it lands in; ``plan`` checks each
citation against the profile it was handed (:func:`check_profile_citations`),
and nothing of the profile's values is copied anywhere.

**The register is a function of the bundle.** It reads the documents and the
pin, never the profile, so ``apply`` and ``replay`` rebuild exactly the record
the plan's digest names without being handed the profile.

Prior art, as patterns: the ODD protocol's rule that every parameter value
states its basis (Grimm et al. 2020); W3C PROV-O's ``wasDerivedFrom`` and
``hadPrimarySource`` for a measured number, ``wasAttributedTo`` for an
assumption; SDMX ``OBS_STATUS``, a status per observation that reads as
normal when absent; RFC 6901 for the addresses.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import canonicalise, nfc
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.seam.coverage import printable
from flux_stats import registry as _stats_registry

__all__ = [
    "ENGINE_BLOCK_NUMBERS",
    "FLX_3113",
    "NOT_PARAMETERS",
    "PARAMETERS_KEY",
    "SOURCES_1_0",
    "UNUSED_KEY",
    "Citation",
    "ContractCitation",
    "DeclaredNumber",
    "ParameterRegister",
    "RegisterEntry",
    "RegisteredNumber",
    "Source",
    "UnusedStatistic",
    "build_parameter_register",
    "check_contract_citations",
    "check_profile_citations",
    "declared_numbers",
    "parse_pointer",
    "read_parameters_block",
    "read_unused_block",
    "render_pointer",
]

FLX_3113 = register(
    "FLX-3113",
    "a parameter cites a statistic the pinned profile does not publish",
    "docs/spec/PARAMETER-REGISTER-SPEC.md: an entry of "
    "extensions['com.agentics.flux/1'].parameters whose source is `profile` cites a "
    "statistic by a JSON Pointer into the profile the Simulation pins, and `plan` reads "
    "that pointer in the profile it is handed. The pointer has to reach a statistic or a "
    "metric (or a part of one) whose `definition` is the one the entry names, or a "
    "table's `rowCount`, and nothing on the way may be withheld. A statistic listed in "
    "extensions['com.agentics.flux/1'].unused is read the same way. The message says which "
    "entry and what is wrong, and never quotes a value. Point the entry at a statistic "
    "the profile publishes, or give the number another source: an assumption, with its "
    "reason. Nothing was written.",
)

#: The key under the engine's namespace that holds the register.
PARAMETERS_KEY = "parameters"

#: The key under the engine's namespace that lists the statistics of the pinned
#: profile no number of the run rests on, each with its reason (register format 1-2-0).
UNUSED_KEY = "unused"


class Source(str, Enum):
    """What a number rests on. The order is the order every report lists them in.

    ``contract`` came with register format 1-1-0. A register no entry of which
    names it is a format 1-0-0 register, and lists the three sources that format
    knows (:meth:`ParameterRegister.sources`).
    """

    PROFILE = "profile"
    CONTRACT = "contract"
    ASSUMPTION = "assumption"
    DEFAULT = "default"


#: The sources of register format 1-0-0, in report order.
SOURCES_1_0: Tuple["Source", ...] = (Source.PROFILE, Source.ASSUMPTION, Source.DEFAULT)

#: Every key one register entry may carry. Closed, as the namespace is.
_ENTRY_KEYS: FrozenSet[str] = frozenset(
    {"document", "pointer", "source", "statistic", "contract", "reason"}
)
_CITATION_KEYS: FrozenSet[str] = frozenset({"pointer", "definition"})
#: Every key one ``unused`` item may carry.
_UNUSED_ITEM_KEYS: FrozenSet[str] = frozenset({"statistic", "reason"})
_CONTRACT_CITATION_KEYS: FrozenSet[str] = frozenset({"productRef", "pointer"})

#: The longest reason an entry may give. A reason is a sentence for a reader of
#: the evidence, not a document of its own.
MAX_REASON_LENGTH = 1000

#: Numbers under ``spec`` that are not parameters, by kind: the seeds and the
#: sweep say which runs are made, not what the world is like. A position
#: names the number itself or a mapping every number under which is left out.
NOT_PARAMETERS: FrozenSet[Tuple[str, Tuple[str, ...]]] = frozenset(
    {
        ("World", ("spec", "seed")),
        ("Simulation", ("spec", "seed")),
        ("Simulation", ("spec", "sweep")),
        ("Experiment", ("spec", "assignment", "seed")),
    }
)

#: Where the engine's namespace holds a number, relative to the namespace.
#: ``*`` is any key of a mapping and ``#`` any index of a list. The fidelity
#: block spells most of its numbers as decimal strings, so the positions are
#: named rather than found by type, and a test keeps this list in step with
#: the block's closed key sets. Left out on purpose: ``encoding`` (the grid the
#: check computes on), ``budget`` (when the check runs), and the namespace's
#: ``profile``, ``strictCoverage`` and ``parameters``, which hold no parameter.
ENGINE_BLOCK_NUMBERS: Tuple[Tuple[str, ...], ...] = (
    ("emits", "*", "rows"),
    ("emits", "*", "fidelity", "records", "min"),
    ("emits", "*", "fidelity", "records", "max"),
    ("emits", "*", "fidelity", "columns", "*", "range", "min"),
    ("emits", "*", "fidelity", "columns", "*", "range", "max"),
    ("emits", "*", "fidelity", "columns", "*", "maxCardinality"),
    ("emits", "*", "fidelity", "columns", "*", "bins", "#"),
    ("emits", "*", "fidelity", "columns", "*", "binWeights", "#"),
    ("emits", "*", "fidelity", "columns", "*", "binWeightsGiven", "*", "#"),
    ("emits", "*", "fidelity", "columns", "*", "levels", "*"),
    ("emits", "*", "fidelity", "columns", "*", "levelWeightsGiven", "*", "*"),
    ("emits", "*", "fidelity", "columns", "*", "mean"),
    ("emits", "*", "fidelity", "columns", "*", "stddev"),
    ("emits", "*", "fidelity", "columns", "*", "rate"),
    ("emits", "*", "fidelity", "columns", "*", "nullFraction"),
    ("emits", "*", "fidelity", "columns", "*", "identifierLength"),
    ("emits", "*", "fidelity", "properties", "#", "tolerance", "value"),
    ("emits", "*", "fidelity", "properties", "#", "tolerance", "max"),
    ("emits", "*", "fidelity", "properties", "#", "bins", "#"),
    ("emits", "*", "fidelity", "properties", "#", "level"),
    ("emits", "*", "fidelity", "properties", "#", "minVariance"),
    ("emits", "*", "rules", "#", "mustBe"),
    ("emits", "*", "rules", "#", "mustBeLessThan"),
    ("emits", "*", "rules", "#", "mustBeLessOrEqualTo"),
    ("emits", "*", "rules", "#", "mustBeGreaterThan"),
    ("emits", "*", "rules", "#", "mustBeGreaterOrEqualTo"),
    ("emits", "*", "rules", "#", "mustBeBetween", "#"),
    ("scorecard", "thresholds", "overall", "*"),
    ("scorecard", "thresholds", "dimensions", "*", "*"),
    ("scorecard", "thresholds", "tables", "*", "*"),
)

_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
_BAD_ESCAPE = re.compile(r"~(?![01])")

#: The status of a value a profile published (SDMX ``CL_CONF_STATUS``).
_FREE = "F"


# ---------------------------------------------------------------------------
# JSON Pointers (RFC 6901)
# ---------------------------------------------------------------------------


def parse_pointer(text: str) -> Tuple[str, ...]:
    """The reference tokens of the RFC 6901 JSON Pointer *text*.

    ``""`` is the whole document. Otherwise the pointer starts with ``/``, and in
    each token ``~1`` stands for ``/`` and ``~0`` for ``~`` (decoded in that
    order, as section 4 says). A ``~`` followed by anything else is not a
    pointer. Raises ``ValueError`` saying why.
    """
    if text == "":
        return ()
    if not text.startswith("/"):
        raise ValueError("a JSON Pointer is empty or starts with '/'")
    tokens = []
    for raw in text[1:].split("/"):
        if _BAD_ESCAPE.search(raw):
            raise ValueError("in a JSON Pointer, '~' is followed by '0' or '1'")
        tokens.append(nfc(raw.replace("~1", "/").replace("~0", "~")))
    return tuple(tokens)


def render_pointer(tokens: Sequence[str]) -> str:
    """The RFC 6901 spelling of *tokens*: the inverse of :func:`parse_pointer`."""
    return "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)


def _child(node: Any, token: str) -> Any:
    """The member of *node* that *token* names; ``LookupError`` when there is none."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            if nfc(str(key)) == token:
                return value
        raise LookupError(token)
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        # An index with more digits than the list's length has is past its end,
        # and is not handed to int(), which refuses a string of 4301 digits.
        if (
            _ARRAY_INDEX.fullmatch(token)
            and len(token) <= len(str(len(node)))
            and int(token) < len(node)
        ):
            return node[int(token)]
        raise LookupError(token)
    raise LookupError(token)


def _resolve(document: Any, tokens: Sequence[str]) -> Any:
    """The value *tokens* reach in *document*; ``LookupError`` when they reach nothing."""
    node = document
    for token in tokens:
        node = _child(node, token)
    return node


def _resolved_depth(document: Any, tokens: Sequence[str]) -> Tuple[int, Any]:
    """How many of *tokens* resolve in *document*, and the node the last of them reaches."""
    node = document
    for depth, token in enumerate(tokens):
        try:
            node = _child(node, token)
        except LookupError:
            return depth, node
    return len(tokens), node


def _where_it_stops(document: Any, tokens: Sequence[str]) -> str:
    """Where a pointer that reaches nothing stops, and what is there instead.

    For a bundle document only: its keys are the author's own names, so a
    message may list them. A profile's are never listed (see
    :func:`check_profile_citations`).
    """
    depth, node = _resolved_depth(document, tokens)
    at = printable(render_pointer(tokens[:depth])) or "the document's root"
    missing = printable(tokens[depth]) if depth < len(tokens) else ""
    if len(missing) > 40:  # a message names a token, it does not reprint a long one
        missing = missing[:40] + "..."
    if isinstance(node, Mapping):
        keys = sorted(printable(nfc(str(key))) for key in node)
        listed = ", ".join(keys[:8]) + (", ..." if len(keys) > 8 else "")
        return f"{at} has no key {missing!r} (it has {listed or 'none'})"
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return f"{at} has no item {missing!r} (it has {len(node)}, from 0)"
    return f"{at} is a value, not a mapping or a list"


# ---------------------------------------------------------------------------
# The numbers a run declares
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredNumber:
    """One number the run reads: where it is, and its exact value as written."""

    document: str
    kind: str
    tokens: Tuple[str, ...]
    value: str

    @property
    def pointer(self) -> str:
        return render_pointer(self.tokens)


def _number_text(value: Any, *, strings: bool) -> Optional[str]:
    """*value*'s exact decimal text, or ``None`` when it is not a number.

    A YAML number arrives as an ``int`` or an exact ``Decimal`` (the bundle's
    loader makes no float), and is spelled as the canonical form spells it, so
    the record shows the number the author wrote. With *strings*, a decimal
    written out as text counts too, which is how the fidelity block writes one.
    A boolean is not a number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return str(canonicalise(value))
    if strings and isinstance(value, str):
        return _decimal_text(value)
    return None


def _decimal_text(value: str) -> Optional[str]:
    """A string the fidelity block's readers read as a number, as canonical decimal text.

    The readers (:func:`flux_stats.fidelity._decimal_of`,
    :func:`flux_engine.generator.statistics.exact_decimal`) take whatever
    :class:`~decimal.Decimal` reads, ``"1e4"``, ``".5"`` and ``"+5"`` among
    them, so the register counts the same strings. Text that is no number, such
    as a timestamp bound, is not counted. A number too long to write out is
    refused by those readers' own rule (``FLX-5030``) before it is spelled. The
    record shows the canonical spelling (KERNEL-SPEC §2.1): no exponent, no
    sign on zero, and the declared scale kept.
    """
    from flux_engine.concordance.predicate import require_number_length

    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return str(canonicalise(require_number_length(number, "a number the register counts")))


#: How :func:`_leaves` marks a step into a list. Not a string, so no mapping
#: key, ``"#"`` included, can be taken for one.
_LIST_STEP = None


def _leaves(
    node: Any, path: Tuple[str, ...], marks: Tuple[Optional[str], ...]
) -> List[Tuple[Tuple[str, ...], Tuple[Optional[str], ...], Any]]:
    """Every scalar under *node* in a fixed order, with its path and its steps.

    A step into a mapping is marked by the key, a step into a list by
    :data:`_LIST_STEP`. Mapping keys are visited in NFC code-point order and
    lists by index, so the order is a function of the document and nothing else.
    """
    found: List[Tuple[Tuple[str, ...], Tuple[Optional[str], ...], Any]] = []
    if isinstance(node, Mapping):
        for key in sorted(node, key=lambda item: nfc(str(item))):
            name = nfc(str(key))
            found.extend(_leaves(node[key], path + (name,), marks + (name,)))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, item in enumerate(node):
            found.extend(_leaves(item, path + (str(index),), marks + (_LIST_STEP,)))
    else:
        found.append((path, marks, node))
    return found


def _matches(marks: Tuple[Optional[str], ...], pattern: Tuple[str, ...]) -> bool:
    """Whether a leaf's steps fit *pattern*: ``#`` is a list step, ``*`` any mapping key."""
    if len(marks) != len(pattern):
        return False
    for mark, part in zip(marks, pattern, strict=True):
        if part == "#":
            if mark is not _LIST_STEP:
                return False
        elif part == "*":
            if mark is _LIST_STEP:
                return False
        elif mark != part:
            return False
    return True


def _excluded(kind: str, path: Tuple[str, ...]) -> bool:
    return any(
        kind == excluded_kind and path[: len(prefix)] == prefix
        for excluded_kind, prefix in NOT_PARAMETERS
    )


def _spec_numbers(doc_id: str, kind: str, spec: Any) -> List[DeclaredNumber]:
    """Every JSON number under a document's ``spec``, except :data:`NOT_PARAMETERS`."""
    numbers: List[DeclaredNumber] = []
    for path, _marks, value in _leaves(spec, ("spec",), ("spec",)) if spec is not None else ():
        text = _number_text(value, strings=False)
        if text is not None and not _excluded(kind, path):
            numbers.append(DeclaredNumber(doc_id, kind, path, text))
    return numbers


def _engine_block_numbers(
    doc_id: str, kind: str, document: Mapping[str, Any], namespace: str
) -> List[DeclaredNumber]:
    """The numbers at :data:`ENGINE_BLOCK_NUMBERS` in the Simulation's own *namespace* block."""
    extensions = document.get("extensions")
    block = extensions.get(namespace) if isinstance(extensions, Mapping) else None
    if not isinstance(block, Mapping):
        return []
    numbers: List[DeclaredNumber] = []
    for path, marks, value in _leaves(block, ("extensions", namespace), ()):
        if not any(_matches(marks, pattern) for pattern in ENGINE_BLOCK_NUMBERS):
            continue
        text = _number_text(value, strings=True)
        if text is not None:
            numbers.append(DeclaredNumber(doc_id, kind, path, text))
    return numbers


def declared_numbers(
    documents: Mapping[str, Mapping[str, Any]], *, simulation_id: str, namespace: str
) -> Tuple[DeclaredNumber, ...]:
    """Every number the register counts, in the order every record lists them.

    *documents* are the run's documents by id: its cast and the Playbacks bound
    to its World. Documents are taken in NFC order of their ids. Under each
    ``spec``, every JSON number counts except :data:`NOT_PARAMETERS`; in the
    Simulation's own *namespace*, the numbers at :data:`ENGINE_BLOCK_NUMBERS`.
    """
    numbers: List[DeclaredNumber] = []
    for doc_id in sorted(documents, key=nfc):
        document = documents[doc_id]
        kind = str(document.get("kind", ""))
        numbers.extend(_spec_numbers(doc_id, kind, document.get("spec")))
        if doc_id == simulation_id:
            numbers.extend(_engine_block_numbers(doc_id, kind, document, namespace))
    return tuple(numbers)


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """A statistic of the pinned profile: a pointer into it, and the definition it lands in."""

    pointer: str
    tokens: Tuple[str, ...]
    definition: Optional[str] = None

    def as_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {"pointer": self.pointer}
        if self.definition is not None:
            document["definition"] = self.definition
        return document


@dataclass(frozen=True)
class ContractCitation:
    """A position in a FLUID contract the run emits from: the contract's id and a pointer into it."""

    product_ref: str
    pointer: str
    tokens: Tuple[str, ...]

    def as_document(self) -> Dict[str, Any]:
        return {"productRef": self.product_ref, "pointer": self.pointer}


@dataclass(frozen=True)
class RegisterEntry:
    """One entry of the register, as declared: the numbers it names and their source."""

    index: int
    document: str
    pointer: str
    tokens: Tuple[str, ...]
    source: Source
    citation: Optional[Citation] = None
    reason: Optional[str] = None
    #: How a message names the entry: the block's place in the Simulation and
    #: the entry's in the list, as the block's own refusals name it.
    label: str = ""
    #: A ``contract`` entry's citation: where in the contract its numbers come from.
    contract: Optional[ContractCitation] = None


@dataclass(frozen=True)
class UnusedStatistic:
    """A statistic of the pinned profile no number of the run rests on, and why."""

    index: int
    citation: Citation
    reason: str
    #: How a message names the item: the block's place in the Simulation and its index.
    label: str = ""

    def as_document(self) -> Dict[str, Any]:
        return {"statistic": self.citation.as_document(), "reason": self.reason}


def _registered_identifiers() -> FrozenSet[str]:
    """Every statistic and metric definition a profile can hold."""
    return frozenset(entry.identifier for entry in _stats_registry.definitions()) | frozenset(
        metric.identifier for metric in _stats_registry.metrics()
    )


def _reason(value: Any, where: str, code: ErrorCode) -> str:
    """A reason: one line of text, of at most :data:`MAX_REASON_LENGTH` characters."""
    if not isinstance(value, str) or not value.strip():
        raise FluxError(code, f"{where} is a non-empty string")
    text = nfc(value.strip())
    if len(text) > MAX_REASON_LENGTH:
        raise FluxError(code, f"{where} has at most {MAX_REASON_LENGTH} characters")
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise FluxError(code, f"{where} is one line of text, with no control character")
    return text


def _pointer(value: Any, where: str, code: ErrorCode) -> Tuple[str, Tuple[str, ...]]:
    if not isinstance(value, str):
        raise FluxError(code, f"{where} is a JSON Pointer (RFC 6901), a string")
    try:
        return value, parse_pointer(value)
    except ValueError as error:
        raise FluxError(code, f"{where} is not a JSON Pointer: {error}") from None


def _citation(value: Any, where: str, code: ErrorCode) -> Citation:
    if not isinstance(value, Mapping):
        raise FluxError(code, f"{where} is a mapping holding 'pointer' and, mostly, 'definition'")
    unknown = sorted(str(key) for key in value if nfc(str(key)) not in _CITATION_KEYS)
    if unknown:
        raise FluxError(code, f"{where} declares {unknown}; it carries {sorted(_CITATION_KEYS)}")
    if "pointer" not in value:
        raise FluxError(code, f"{where} names the statistic with 'pointer'")
    text, tokens = _pointer(value["pointer"], f"{where}.pointer", code)
    if not tokens:
        raise FluxError(code, f"{where}.pointer names a part of the profile, not all of it")
    definition = value.get("definition")
    if definition is not None:
        if not isinstance(definition, str) or nfc(definition) not in _registered_identifiers():
            raise FluxError(
                code,
                f"{where}.definition is a registered statistic or metric, one of "
                f"{sorted(_registered_identifiers())}",
            )
        definition = nfc(definition)
    return Citation(pointer=text, tokens=tokens, definition=definition)


def _contract_citation(value: Any, where: str, code: ErrorCode) -> ContractCitation:
    """A ``contract`` entry's citation: ``productRef`` and a ``pointer`` into that contract."""
    if not isinstance(value, Mapping):
        raise FluxError(code, f"{where} is a mapping holding 'productRef' and 'pointer'")
    unknown = sorted(str(key) for key in value if nfc(str(key)) not in _CONTRACT_CITATION_KEYS)
    if unknown:
        raise FluxError(
            code, f"{where} declares {unknown}; it carries {sorted(_CONTRACT_CITATION_KEYS)}"
        )
    product_ref = value.get("productRef")
    if not isinstance(product_ref, str) or not product_ref:
        raise FluxError(
            code, f"{where}.productRef is the id of a contract the run emits from, a string"
        )
    if "pointer" not in value:
        raise FluxError(code, f"{where} names the position in the contract with 'pointer'")
    text, tokens = _pointer(value["pointer"], f"{where}.pointer", code)
    if not tokens:
        raise FluxError(code, f"{where}.pointer names a part of the contract, not all of it")
    return ContractCitation(product_ref=nfc(product_ref), pointer=text, tokens=tokens)


def read_parameters_block(
    block: Any, *, pinned: bool, where: str, code: ErrorCode
) -> Tuple[RegisterEntry, ...]:
    """Read ``extensions["com.agentics.flux/1"].parameters``, or refuse it with *code*.

    The block is a list of entries, each a mapping with ``document``, ``pointer``
    and ``source``, and nothing else but what its source takes:

    * ``profile``: ``statistic``, a mapping with ``pointer`` (into the pinned
      profile) and ``definition`` (the registered definition it lands in; only a
      table's ``rowCount`` has none). A Simulation that pins no profile cannot
      cite one, so *pinned* must hold.
    * ``contract``: ``contract``, a mapping with ``productRef`` (a contract the
      run emits from) and ``pointer`` (into that contract). Whether the pointer
      reaches anything is :func:`check_contract_citations`' question.
    * ``assumption``: ``reason``, one line of text.
    * ``default``: optionally a ``reason``.

    Two entries that name the same number by the same pointer are refused: the
    second would decide nothing, or decide it differently. Whether a pointer
    reaches anything is :func:`build_parameter_register`'s question, which needs
    the run's documents.
    """
    if isinstance(block, (str, bytes, Mapping)) or not isinstance(block, Sequence):
        raise FluxError(code, f"{where} is a list of entries")
    entries: List[RegisterEntry] = []
    seen: Dict[Tuple[str, Tuple[str, ...]], int] = {}
    for index, raw in enumerate(block):
        entry = _read_entry(raw, index, at=f"{where}[{index}]", pinned=pinned, code=code)
        key = (entry.document, entry.tokens)
        if key in seen:
            raise FluxError(
                code,
                f"{entry.label} names the same numbers as {where}[{seen[key]}], "
                "by the same pointer",
            )
        seen[key] = index
        entries.append(entry)
    return tuple(entries)


def read_unused_block(
    block: Any, *, pinned: bool, where: str, code: ErrorCode
) -> Tuple[UnusedStatistic, ...]:
    """Read ``extensions["com.agentics.flux/1"].unused``, or refuse it with *code*.

    A list of items, each a mapping with ``statistic`` (a citation of the pinned
    profile, as a ``profile`` entry gives one) and ``reason`` (one line of text),
    and nothing else. A Simulation that pins no profile has nothing to list, so
    *pinned* must hold, and two items naming one statistic are refused. Whether a
    pointer reaches a published statistic is :func:`check_profile_citations`'
    question, which needs the profile.
    """
    if isinstance(block, (str, bytes, Mapping)) or not isinstance(block, Sequence):
        raise FluxError(code, f"{where} is a list of statistics, each with its reason")
    if not pinned:
        raise FluxError(
            code,
            f"{where} lists statistics of a profile, and the Simulation pins none under "
            "extensions['com.agentics.flux/1'].profile",
        )
    items: List[UnusedStatistic] = []
    seen: Dict[Tuple[str, ...], int] = {}
    for index, raw in enumerate(block):
        at = f"{where}[{index}]"
        if not isinstance(raw, Mapping):
            raise FluxError(code, f"{at} is a mapping")
        unknown = sorted(str(key) for key in raw if nfc(str(key)) not in _UNUSED_ITEM_KEYS)
        if unknown:
            raise FluxError(
                code, f"{at} declares {unknown}; an item carries {sorted(_UNUSED_ITEM_KEYS)}"
            )
        for required in ("statistic", "reason"):
            if required not in raw:
                raise FluxError(code, f"{at} names its '{required}'")
        citation = _citation(raw["statistic"], f"{at}.statistic", code)
        if citation.tokens in seen:
            raise FluxError(
                code, f"{at} names the same statistic as {where}[{seen[citation.tokens]}]"
            )
        seen[citation.tokens] = index
        items.append(
            UnusedStatistic(
                index=index,
                citation=citation,
                reason=_reason(raw["reason"], f"{at}.reason", code),
                label=at,
            )
        )
    return tuple(items)


def _read_entry(raw: Any, index: int, *, at: str, pinned: bool, code: ErrorCode) -> RegisterEntry:
    """One entry of the block, named *at* in a refusal, with what its source takes."""
    if not isinstance(raw, Mapping):
        raise FluxError(code, f"{at} is a mapping")
    unknown = sorted(str(key) for key in raw if nfc(str(key)) not in _ENTRY_KEYS)
    if unknown:
        raise FluxError(code, f"{at} declares {unknown}; an entry carries {sorted(_ENTRY_KEYS)}")
    for required in ("document", "pointer", "source"):
        if required not in raw:
            raise FluxError(code, f"{at} names its '{required}'")
    document = raw["document"]
    if not isinstance(document, str) or not document:
        raise FluxError(code, f"{at}.document is the id of a document of the run")
    pointer, tokens = _pointer(raw["pointer"], f"{at}.pointer", code)
    try:
        source = Source(nfc(str(raw["source"])))
    except ValueError:
        raise FluxError(
            code, f"{at}.source is one of {[source.value for source in Source]}"
        ) from None
    citation, contract, reason = _source_fields(raw, source, at=at, pinned=pinned, code=code)
    return RegisterEntry(
        index=index,
        document=nfc(document),
        pointer=pointer,
        tokens=tokens,
        source=source,
        citation=citation,
        reason=reason,
        label=at,
        contract=contract,
    )


def _source_fields(
    raw: Mapping[str, Any], source: Source, *, at: str, pinned: bool, code: ErrorCode
) -> Tuple[Optional[Citation], Optional[ContractCitation], Optional[str]]:
    """What the entry's *source* takes: a citation of the profile or the contract, or a reason.

    Each source takes its own key and refuses the others': ``statistic`` for
    ``profile``, ``contract`` for ``contract``, ``reason`` for the other two.
    """
    for key, owner in (("statistic", Source.PROFILE), ("contract", Source.CONTRACT)):
        if key in raw and source is not owner:
            raise FluxError(code, f"{at}: only a number from the {owner.value} names a '{key}'")
    if source is Source.PROFILE:
        return _profile_fields(raw, at=at, pinned=pinned, code=code), None, None
    if source is Source.CONTRACT:
        return None, _contract_fields(raw, at=at, code=code), None
    if source is Source.ASSUMPTION and "reason" not in raw:
        raise FluxError(code, f"{at}: an assumption gives its 'reason'")
    if "reason" in raw:
        return None, None, _reason(raw["reason"], f"{at}.reason", code)
    return None, None, None


def _profile_fields(raw: Mapping[str, Any], *, at: str, pinned: bool, code: ErrorCode) -> Citation:
    """A ``profile`` entry's citation; it gives no reason, and its Simulation pins a profile."""
    if "reason" in raw:
        raise FluxError(code, f"{at}: a number from the profile carries no 'reason'")
    if "statistic" not in raw:
        raise FluxError(code, f"{at}: a number from the profile names its 'statistic'")
    if not pinned:
        raise FluxError(
            code,
            f"{at} cites the profile, and the Simulation pins none under "
            "extensions['com.agentics.flux/1'].profile",
        )
    return _citation(raw["statistic"], f"{at}.statistic", code)


def _contract_fields(raw: Mapping[str, Any], *, at: str, code: ErrorCode) -> ContractCitation:
    """A ``contract`` entry's citation; it gives no reason."""
    if "reason" in raw:
        raise FluxError(code, f"{at}: a number from the contract carries no 'reason'")
    if "contract" not in raw:
        raise FluxError(code, f"{at}: a number from the contract names its place there, 'contract'")
    return _contract_citation(raw["contract"], f"{at}.contract", code)


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegisteredNumber:
    """A number and its source. ``entry`` is ``None`` for a default no entry named."""

    number: DeclaredNumber
    source: Source
    entry: Optional[RegisterEntry] = None


@dataclass(frozen=True)
class ParameterRegister:
    """Every number the run reads, each with its source, and the entries that gave them."""

    simulation_id: str
    profile_digest: Optional[str]
    entries: Tuple[RegisterEntry, ...]
    numbers: Tuple[RegisteredNumber, ...]
    #: The statistics of the pinned profile no number rests on, when the Simulation
    #: lists them (register format 1-2-0); ``None`` when it does not.
    unused: Optional[Tuple[UnusedStatistic, ...]] = None

    @property
    def uses_contract(self) -> bool:
        """Whether an entry names the contract as a source: register format 1-1-0."""
        return any(entry.source is Source.CONTRACT for entry in self.entries)

    @property
    def lists_unused(self) -> bool:
        """Whether the Simulation lists the profile's unused statistics: register format 1-2-0."""
        return self.unused is not None

    def sources(self) -> Tuple[Source, ...]:
        """The sources the register's format knows, in report order.

        ``contract`` only when an entry names it or the register is of format
        1-2-0, so a register that does neither is reported, recorded and counted
        exactly as a format 1-0-0 register was.
        """
        return tuple(Source) if self.uses_contract or self.lists_unused else SOURCES_1_0

    def counts(self) -> Dict[str, int]:
        """How many numbers rest on each source, every source of the format listed, in order."""
        counts = {source.value: 0 for source in self.sources()}
        for registered in self.numbers:
            counts[registered.source.value] += 1
        return counts


def _within(prefix: Tuple[str, ...], path: Tuple[str, ...]) -> bool:
    return path[: len(prefix)] == prefix


def _require_counted(
    entry: RegisterEntry,
    documents: Mapping[str, Mapping[str, Any]],
    numbers: Sequence[DeclaredNumber],
    *,
    at: str,
    code: ErrorCode,
) -> None:
    """Refuse *entry* unless it names a number the register counts.

    Its document has to be one the run reads, its pointer has to reach
    something in it, and one of *numbers*, the counted numbers of that
    document, has to lie under the pointer.
    """
    if entry.document not in documents:
        raise FluxError(
            code,
            f"{at} names document {printable(entry.document)!r}, which this run does not "
            "read (it reads its cast and the Playbacks bound to its World)",
        )
    try:
        _resolve(documents[entry.document], entry.tokens)
    except LookupError:
        raise FluxError(
            code,
            f"{at}.pointer reaches nothing in {printable(entry.document)!r}: "
            f"{_where_it_stops(documents[entry.document], entry.tokens)}",
        ) from None
    if not any(_within(entry.tokens, number.tokens) for number in numbers):
        raise FluxError(
            code,
            f"{at}.pointer reaches no number the register counts in "
            f"{printable(entry.document)!r} (a seed, the sweep, the fidelity block's "
            "encoding and budget, and text are not counted)",
        )


def _deciding_entry(
    number: DeclaredNumber, entries: Sequence[RegisterEntry]
) -> Optional[RegisterEntry]:
    """The entry whose pointer reaches *number* most specifically; ``None`` when none does.

    Of two entries as specific, the first decides.
    """
    deciding: Optional[RegisterEntry] = None
    for entry in entries:
        if entry.document == number.document and _within(entry.tokens, number.tokens):
            if deciding is None or len(entry.tokens) > len(deciding.tokens):
                deciding = entry
    return deciding


def build_parameter_register(
    *,
    simulation_id: str,
    documents: Mapping[str, Mapping[str, Any]],
    namespace: str,
    entries: Sequence[RegisterEntry],
    profile_digest: Optional[str],
    code: ErrorCode,
    unused: Optional[Sequence[UnusedStatistic]] = None,
) -> ParameterRegister:
    """Give every number of the run its source, or refuse an entry that names none.

    Each entry names a document of the run (*documents*: its cast and the
    Playbacks bound to its World) and a pointer that reaches something in it,
    and that something holds at least one number the register counts. An entry
    that fails any of the three is refused with *code*: it would be a source
    stated for nothing, which no share would show. Each number then takes the
    source of the most specific entry whose pointer reaches it, and a number no
    entry reaches is a ``default`` the engine decided.
    """
    numbers = declared_numbers(documents, simulation_id=simulation_id, namespace=namespace)
    by_document: Dict[str, List[DeclaredNumber]] = {}
    for number in numbers:
        by_document.setdefault(number.document, []).append(number)
    for entry in entries:
        at = entry.label or f"{simulation_id}: parameters[{entry.index}]"
        _require_counted(entry, documents, by_document.get(entry.document, ()), at=at, code=code)
    registered = []
    for number in numbers:
        deciding = _deciding_entry(number, entries)
        registered.append(
            RegisteredNumber(
                number=number,
                source=deciding.source if deciding is not None else Source.DEFAULT,
                entry=deciding,
            )
        )
    return ParameterRegister(
        simulation_id=simulation_id,
        profile_digest=profile_digest,
        entries=tuple(entries),
        numbers=tuple(registered),
        unused=tuple(unused) if unused is not None else None,
    )


# ---------------------------------------------------------------------------
# Citations, read against the pinned profile
# ---------------------------------------------------------------------------


def _withheld(node: Mapping[str, Any]) -> bool:
    status = node.get("status")
    return isinstance(status, str) and status != _FREE


def _cited_statistic(
    profile: Mapping[str, Any], tokens: Tuple[str, ...]
) -> Tuple[Optional[str], List[Tuple[Any, Tuple[str, ...]]]]:
    """The definition the pointer lands in, and every node from there to the target.

    Raises ``LookupError`` when the pointer reaches nothing. The definition is
    the one named by the nearest node on the way that is a mapping with a
    registered ``definition``; ``None`` when no node on the way has one.
    """
    chain: List[Tuple[Any, Tuple[str, ...]]] = [(profile, ())]
    node: Any = profile
    for depth, token in enumerate(tokens, start=1):
        node = _child(node, token)
        chain.append((node, tokens[:depth]))
    identifiers = _registered_identifiers()
    for position in range(len(chain) - 1, -1, -1):
        candidate = chain[position][0]
        if isinstance(candidate, Mapping):
            definition = candidate.get("definition")
            if isinstance(definition, str) and nfc(definition) in identifiers:
                return nfc(definition), chain[position:]
    return None, chain


def _is_row_count(profile: Mapping[str, Any], tokens: Tuple[str, ...]) -> bool:
    return (
        len(tokens) == 3
        and tokens[0] == "tables"
        and tokens[2] == "rowCount"
        and isinstance(_resolve(profile, tokens), int)
    )


def _published(chain: Sequence[Tuple[Any, Tuple[str, ...]]]) -> bool:
    """Whether the profile published what the chain from a statistic to the target reaches.

    Nothing on the way may carry a status other than ``F``; a statistic named
    whole needs one published group; a cell of a count table needs its own
    status to be ``F`` when the group lists one.
    """
    return (
        not any(isinstance(node, Mapping) and _withheld(node) for node, _ in chain)
        and _one_group_published(chain)
        and _one_cell_published(chain)
        and _cited_cells_published(chain)
    )


def _one_group_published(chain: Sequence[Tuple[Any, Tuple[str, ...]]]) -> bool:
    """A statistic named whole, or its ``groups`` list named itself, needs one published group."""
    target = chain[-1][0]
    groups = target.get("groups") if isinstance(target, Mapping) else None
    if chain[-1][1][-1:] == ("groups",):
        groups = target  # the statistic's groups, named as a list
    if isinstance(groups, Sequence) and not isinstance(groups, (str, bytes)):
        return any(isinstance(group, Mapping) and not _withheld(group) for group in groups)
    return True


def _one_cell_published(chain: Sequence[Tuple[Any, Tuple[str, ...]]]) -> bool:
    """A count table named whole needs one published cell, as a statistic needs one group."""
    if len(chain) >= 2 and chain[-1][1][-1:] in (("binCounts",), ("levelCounts",)):
        group = chain[-2][0]
        statuses = group.get("cellStatus") if isinstance(group, Mapping) else None
        if isinstance(statuses, Sequence) and not isinstance(statuses, (str, bytes)):
            if statuses and not any(status == _FREE for status in statuses):
                return False
    return True


def _cited_cells_published(chain: Sequence[Tuple[Any, Tuple[str, ...]]]) -> bool:
    """A cell of a count table: the group lists the cells' statuses beside the counts."""
    return all(
        _cell_published(chain[position][0], chain[position + 1][1][-1], chain[position + 2][1][-1])
        for position in range(len(chain) - 2)
    )


def _cell_published(group: Any, counts_key: str, cell: str) -> bool:
    """Whether *cell* of *group*'s *counts_key* is published, when the group lists the statuses."""
    if not isinstance(group, Mapping) or counts_key not in ("binCounts", "levelCounts"):
        return True
    statuses = group.get("cellStatus")
    if not isinstance(statuses, Sequence) or isinstance(statuses, (str, bytes)):
        return True
    if not _ARRAY_INDEX.fullmatch(cell):
        return False
    return not (int(cell) < len(statuses) and statuses[int(cell)] != _FREE)


def check_profile_citations(
    register_: ParameterRegister, profile: Mapping[str, Any], *, where: str
) -> None:
    """Refuse (``FLX-3113``) a citation the pinned *profile* does not publish.

    *profile* is the profile document as it was read, the one whose digest the
    Simulation pins. Each ``profile`` entry's pointer has to reach something in
    it; the nearest node on the way with a registered ``definition`` has to be
    the one the entry names, or the pointer is a table's ``rowCount`` and the
    entry names none; and nothing it reaches may be withheld. A message names
    the entry and the part of its citation that fails, and no value. The
    statistics the register lists as unused are read the same way: each has to be
    a statistic the profile publishes.
    """
    for entry in register_.entries:
        if entry.source is not Source.PROFILE or entry.citation is None:
            continue
        at = f"{entry.label or f'{where}: parameters[{entry.index}]'}.statistic"
        _check_citation(entry.citation, profile, at)
    for item in register_.unused or ():
        _check_citation(
            item.citation, profile, f"{item.label or f'{where}: unused[{item.index}]'}.statistic"
        )


def check_contract_citations(
    register_: ParameterRegister,
    contracts: Mapping[str, Mapping[str, Any]],
    *,
    where: str,
    code: ErrorCode,
) -> None:
    """Refuse (*code*) a ``contract`` citation that reaches nothing in a contract the run emits from.

    *contracts* are the FLUID contracts the run's emits name, by ``productRef``,
    as the bundle holds them. A contract is a document of the bundle, so this
    needs no other input and holds at ``validate`` as at every other command,
    as an entry's own pointer does (:func:`build_parameter_register`). Like a
    profile citation, the pointer is read and no number is compared with the
    value there.
    """
    for entry in register_.entries:
        if entry.source is not Source.CONTRACT or entry.contract is None:
            continue
        at = f"{entry.label or f'{where}: parameters[{entry.index}]'}.contract"
        cited = entry.contract
        contract = contracts.get(cited.product_ref)
        if contract is None:
            named = ", ".join(sorted(printable(ref) for ref in contracts)) or "none"
            raise FluxError(
                code,
                f"{at}.productRef {printable(cited.product_ref)!r} is not a contract this run "
                f"emits from (its emits name: {named})",
            )
        try:
            _resolve(contract, cited.tokens)
        except LookupError:
            raise FluxError(
                code,
                f"{at}.pointer reaches nothing in contract {printable(cited.product_ref)!r}: "
                f"{_where_it_stops(contract, cited.tokens)}",
            ) from None


def _check_citation(citation: Citation, profile: Mapping[str, Any], at: str) -> None:
    """Refuse (``FLX-3113``) a citation, named *at*, that the pinned *profile* does not publish."""
    landing = _landing(citation, profile, at)
    if landing is None:
        return
    found, chain = landing
    if found is None:
        raise FluxError(
            FLX_3113,
            f"{at}.pointer reaches no statistic: it reaches a statistic, a metric, a part "
            "of one, or a table's rowCount",
        )
    if citation.definition is None:
        raise FluxError(
            FLX_3113, f"{at} lands in a {found} statistic; name it with 'definition: {found}'"
        )
    if citation.definition != found:
        raise FluxError(
            FLX_3113,
            f"{at} names {citation.definition}, and the pinned profile holds {found} there",
        )
    if not _published(chain):
        raise FluxError(
            FLX_3113,
            f"{at}.pointer reaches a value the pinned profile withheld; cite a published "
            "one, or state the number as an assumption",
        )


def _landing(
    citation: Citation, profile: Mapping[str, Any], at: str
) -> Optional[Tuple[Optional[str], List[Tuple[Any, Tuple[str, ...]]]]]:
    """Where *citation* lands: :func:`_cited_statistic`'s answer, or ``None`` for a ``rowCount``.

    Refuses (``FLX-3113``) a pointer that reaches nothing, and a table's
    ``rowCount`` cited with a definition.
    """
    try:
        if _is_row_count(profile, citation.tokens):
            if citation.definition is not None:
                raise FluxError(
                    FLX_3113,
                    f"{at} is a table's rowCount, which has no definition; drop 'definition'",
                )
            return None
        return _cited_statistic(profile, citation.tokens)
    except LookupError:
        depth, _ = _resolved_depth(profile, citation.tokens)
        raise FluxError(
            FLX_3113,
            f"{at}.pointer reaches nothing in the pinned profile past "
            f"{printable(render_pointer(citation.tokens[:depth])) or 'its root'}",
        ) from None
