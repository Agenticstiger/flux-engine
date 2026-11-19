# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Read a FLUX bundle's YAML exactly, and within bounds.

Needs PyYAML, which the ``yaml`` extra installs (``pip install
'flux-stats[yaml]'``), to read anything. The module itself imports the
standard library alone, so ``import flux_stats`` imports it and knows
``FLX-3105``: PyYAML is imported when a document is first read, or when
:class:`ExactLoader` is first named.

:class:`ExactLoader` builds no ``float`` and no ``datetime``: a float scalar
is the :class:`~decimal.Decimal` of its source text, a timestamp stays the
string written, and an integer too long to form is refused. That is the read
a FLUX digest is taken over (KERNEL-SPEC §2.1), and :func:`load_documents` is
the engine's read of a bundle's documents. It moved here from the engine's
``flux_engine.seam.resolve`` so that a reader without the engine, such as
forge-cli's profiler, reads a Simulation exactly as the engine does. It reads
text: which files a caller reads, and how, stays the caller's, so this module
opens nothing.

The text may be untrusted, so the read is bounded (:class:`ReadLimits`), and
each bound refuses with ``FLX-3105`` naming the file and the line, never a
value:

* a document may write out at most ``values`` values and ``characters``
  characters once its aliases and merge keys are written out, and nest at most
  ``depth`` levels; a document that holds itself through an alias is refused.
  YAML reads an alias as a shared reference, so a kilobyte of aliases can stand
  for billions of values, and a merge key (``<<``) copies its mappings' pairs
  while the document is built. A string is one value however long it is, so
  the characters are counted too: a long string held by many aliases costs its
  length each time, and so is a number, which is spelled in full: ``1.e+999`` is
  seven characters of text and a thousand written out. Each document's node graph
  is measured before anything is constructed, each node once, so the measure costs
  what the text costs;
* the merge keys of a document may name at most ``merged`` mappings in all,
  each counted for every mapping that holds the key: naming an empty mapping
  writes nothing out, yet building the document visits it once per holder;
* the pairs merge keys copy are counted against ``merge_pairs`` for the whole
  read: share one :class:`ReadBudget` across a bundle's files, and many files
  that each stay under the bound cannot pass it together;
* a number may need at most ``digits`` digits written out, the bound a
  fidelity block's numbers have (:data:`flux_stats.spelling.MOST_DIGITS`),
  checked at the scalar because writing ``1E+300000000`` out takes gigabytes.
  A zero counts the digits its scale spells, since the canonical form keeps the
  scale: ``0.e-300000000`` is as long as ``1E-300000000``;
* a document nested too deeply for the parser is refused instead of raising
  :class:`RecursionError`.

Prior art for the bounds: go-yaml v3's alias budget (``decode.go``, its
refusal of "excessive aliasing") and ruamel.yaml's ``max_depth``. PyYAML's
SafeLoader bounds none of these.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Generator, List, Optional, Set, Tuple, cast

from flux_stats.canonical import FLX_1003
from flux_stats.errors import FluxError, register
from flux_stats.reading import digits_written_out
from flux_stats.spelling import MOST_DIGITS

__all__ = [
    "FLX_3105",
    "ExactLoader",  # noqa: F822 - served by the module __getattr__, on first use
    "ReadBudget",
    "ReadLimits",
    "check_documents",
    "load_documents",
]

FLX_3105 = register(
    "FLX-3105",
    "bundle does not validate, so its seam cannot be resolved",
    "Resolution delegates to the FLUX offline reference validator: every "
    "reference resolves, every id is unique, every weight sums, and every "
    "in-bundle FLUID document is valid for its declared version. A bundle "
    "that fails those checks has no seam to resolve, and repairing it here "
    "would mean the engine ran against something the validator refused.",
)


@dataclass(frozen=True)
class ReadLimits:
    """How much one read of a bundle may write out; past a bound, ``FLX-3105``."""

    #: Values one document may write out, its aliases and merge keys written out.
    #: A document may be a whole FLUID contract, so the bound is generous: it stops
    #: a document that stands for far more than its text, not a large one.
    values: int = 1_000_000
    #: Levels one document may nest, counted through its aliases.
    depth: int = 64
    #: Digits a number may need written out (:func:`flux_stats.reading.digits_written_out`).
    digits: int = MOST_DIGITS
    #: Pairs the merge keys of every document in the read may copy, together.
    merge_pairs: int = 100_000
    #: Mappings the merge keys of one document may name, each counted for every
    #: mapping that holds the key. A mapping that names a non-empty one adds at least
    #: two values, so a document inside ``values`` reaches this only by naming empty
    #: mappings, which add none.
    merged: int = 1_000_000
    #: Characters one document may write out in the keys and values it holds, its
    #: aliases and merge keys written out: about 64 for each of the ``values`` values.
    #: A string is one value however long it is, and a number counts as the characters
    #: its decimal is spelled in when that is more than its text (``1.e+999`` is a
    #: thousand), as many as ``digits`` allow it. A document that uses no alias and no
    #: such number is counted at no more characters than its text holds.
    characters: int = 64 * 1024 * 1024


class ReadBudget:
    """One read's :class:`ReadLimits`, and the merge-key pairs it may still copy.

    Share one across every file of a bundle, so the merge-key bound holds for
    the whole read.
    """

    def __init__(self, limits: Optional[ReadLimits] = None) -> None:
        self.limits = limits if limits is not None else ReadLimits()
        self.merge_pairs_left = self.limits.merge_pairs


#: The most digits an integer in a bundle may have: CPython's own limit on
#: converting between an ``int`` and its decimal text
#: (``sys.int_info.default_max_str_digits``, 4300, from CVE-2020-10735).
#: PyYAML builds every integer scalar with ``int()``, so a longer decimal one
#: raised a bare ``ValueError`` from inside either read, before any reader
#: could refuse it, and a longer value spelled in hex loaded, then broke the
#: first message that printed it. An integer past 2^53 - 1 is outside the
#: JSON safe-integer domain and is refused when its digest is taken
#: (``FLX-1003``); this refuses the ones too long to get that far, with the
#: same code. Prior art: PyYAML issue yaml/pyyaml#1000 asks for this refusal
#: at the scalar, with its position, and jackson-core's
#: ``StreamReadConstraints`` bounds a number's length before converting it.
#: Python 3.10.0 to 3.10.6 predate the limit and have no such attribute, so
#: the read takes CPython's default there and refuses what later Pythons do.
_MOST_INTEGER_DIGITS: int = getattr(sys.int_info, "default_max_str_digits", 4300)

#: The smallest magnitude with more than :data:`_MOST_INTEGER_DIGITS` digits.
_TOO_LONG_INTEGER: int = 10**_MOST_INTEGER_DIGITS

#: The tags YAML resolves an integer, a float, a boolean, a timestamp and a
#: merge key to.
_INTEGER_TAG = "tag:yaml.org,2002:int"
_FLOAT_TAG = "tag:yaml.org,2002:float"
_BOOL_TAG = "tag:yaml.org,2002:bool"
_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"
_MERGE_TAG = "tag:yaml.org,2002:merge"


def _yaml() -> Any:
    """PyYAML, or an :class:`ImportError` that names the extra which installs it."""
    try:
        import yaml
    except ImportError as error:  # pragma: no cover - the extra is not installed
        raise ImportError(
            "flux_stats.exact_yaml reads YAML with PyYAML, which is not installed; "
            "install it with: pip install 'flux-stats[yaml]'"
        ) from error
    return yaml


@lru_cache(maxsize=1)
def _build_loader() -> Any:
    """:class:`ExactLoader`, built once, on first use, so PyYAML stays off the import."""
    yaml = _yaml()

    class ExactLoader(yaml.SafeLoader):  # type: ignore[name-defined]
        """A YAML loader that produces no ``float`` and no ``datetime``, within bounds.

        A bundle is read twice, for two incompatible consumers.

        * **The JSON Schema validator** needs real JSON types: ``unitInterval``
          is ``{"type": "number"}``, and a ``Decimal`` is not a number to
          ``jsonschema``. That read is the reference validator's own, and
          :func:`check_documents` holds its text to this loader's bounds first.
        * **The digests** must contain no float at all (KERNEL-SPEC §2.1), and
          the shipped telco example declares ``minCredibility: 0.7`` -- a YAML
          float in the very ``emits[]`` entry ``H`` is taken over. This loader
          is that read: it constructs a float scalar as
          ``Decimal(<the source text>)``, so the number that gets hashed is the
          number the author wrote, exactly, and no binary float is ever
          created.

        Reading the source text rather than converting a parsed float is the
        whole point: ``Decimal(0.7)`` is 0.69999999999999995559..., while
        ``Decimal("0.7")`` is 0.7. Only the second is what the contract says.

        Timestamps stay strings, matching the reference validator's loader and
        the RFC 3339 grammar the schema declares. An integer is refused when it
        is too long for CPython to form or print (:func:`_construct_exact_int`).
        Each document is held to *budget* before it is constructed (the module
        docstring says how); a loader made without one gets its own.
        """

        def __init__(self, stream: Any, budget: Optional[ReadBudget] = None) -> None:
            super().__init__(stream)
            self.budget = budget if budget is not None else ReadBudget()

        def construct_document(self, node: Any) -> Any:
            _check_bounds(node, self.budget)
            return super().construct_document(node)

    ExactLoader.add_constructor(_FLOAT_TAG, _construct_exact_float)
    ExactLoader.add_constructor(_INTEGER_TAG, _construct_exact_int)
    ExactLoader.add_constructor(_TIMESTAMP_TAG, lambda loader, node: loader.construct_scalar(node))
    ExactLoader.__module__ = __name__
    return ExactLoader


def __getattr__(name: str) -> Any:
    """:class:`ExactLoader` on first use; anything else is not here.

    Without PyYAML, naming :class:`ExactLoader` raises the :class:`ImportError`
    that names the extra, not an :class:`AttributeError`, so ``hasattr`` raises
    too. That is deliberate: ``from flux_stats.exact_yaml import ExactLoader``
    turns an :class:`AttributeError` into a bare "cannot import name", and what
    the reader needs to know is which extra is missing.
    """
    if name == "ExactLoader":
        return _build_loader()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _where(node: Any) -> Tuple[str, str]:
    """The file name a node was read from, and its ``(line L, column C)``."""
    mark = node.start_mark
    name = str(mark.name).replace("\\", "/").rsplit("/", 1)[-1]
    return name, f"(line {mark.line + 1}, column {mark.column + 1})"


def _refuse(node: Any, why: str) -> FluxError:
    name, where = _where(node)
    return FluxError(FLX_3105, f"{name}: {why} {where}")


def _construct_exact_float(loader: Any, node: Any) -> Decimal:
    """Construct a YAML float scalar as an exact :class:`~decimal.Decimal`.

    Each refusal names the file and the position, never the value.
    """
    text = str(loader.construct_scalar(node)).replace("_", "")
    if ":" in text:
        raise _refuse(
            node,
            "a YAML sexagesimal number has no canonical decimal spelling; write "
            "it in plain decimal notation",
        )
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise _refuse(node, "a scalar read as a float is not a decimal number") from exc
    if not value.is_finite():
        # YAML 1.1 spells an infinity and a not-a-number with a leading dot
        # (``.inf``, ``.nan``), which Decimal refuses above; a scalar tagged
        # ``!!float`` arrives as ``NaN`` or ``Infinity``, which it forms.
        raise _refuse(node, "an infinite or not-a-number value has no digest and no ordering")
    most = loader.budget.limits.digits
    if digits_written_out(value) > most:
        if value:
            raise _refuse(
                node, f"a number that needs more than {most} digits written out is too long"
            )
        raise _refuse(
            node, f"a zero whose scale needs more than {most} digits written out is too long"
        )
    return value


def _construct_exact_int(loader: Any, node: Any) -> int:
    """Construct a YAML integer scalar, or refuse one no reader can form.

    The digits are counted before ``int()`` is called, so a decimal integer
    too long for CPython is refused instead of raising from inside it; the
    value is bounded afterwards, for the spellings ``int()`` forms at any
    length (hex, octal, binary). A spelling PyYAML cannot build at all, such
    as ``0x_`` or ``!!int ''``, raised a bare ``ValueError`` or
    ``IndexError``: it is unreadable YAML (``FLX-3105``). Each refusal names
    the file and the position, never the value.
    """
    name, where = _where(node)
    too_long = f"{name}: an integer of more than {_MOST_INTEGER_DIGITS} digits is too long to read"
    digits = sum(character.isdigit() for character in str(loader.construct_scalar(node)))
    if digits > _MOST_INTEGER_DIGITS:
        raise FluxError(FLX_1003, f"{too_long} {where}")
    try:
        value = loader.construct_yaml_int(node)
    except (ValueError, IndexError) as exc:
        raise FluxError(
            FLX_3105, f"{name}: a scalar read as an integer has no integer value {where}"
        ) from exc
    if not -_TOO_LONG_INTEGER < value < _TOO_LONG_INTEGER:
        raise FluxError(FLX_1003, f"{too_long} {where}")
    return int(value)


def _merged_parts(value_node: Any) -> List[Any]:
    """The mappings a merge key names: one mapping, or a sequence of them.

    Anything else is left to the constructor, which refuses it as YAML.
    """
    yaml = _yaml()
    if isinstance(value_node, yaml.MappingNode):
        return [value_node]
    if isinstance(value_node, yaml.SequenceNode):
        return [part for part in value_node.value if isinstance(part, yaml.MappingNode)]
    return []


#: What a node writes out: its values, its levels and its characters.
_Size = Tuple[int, int, int]

#: A walk of the nodes: it yields ``(node, level)`` for each collection it needs
#: measured, is sent that node's :data:`_Size`, and returns its own.
_Walk = Generator[Tuple[Any, int], _Size, _Size]


def _spelled_length(number: Decimal) -> int:
    """The characters :func:`flux_stats.canonical.render_decimal` spells the finite *number* in.

    It is worked out from the decimal's digits and exponent, so no spelling is formed to count
    one: ``0E-300000000`` is three hundred million characters, and is not formed here.
    """
    sign, digits, written = number.as_tuple()
    exponent = cast(int, written)  # only a NaN or an infinity has no integer exponent
    if exponent >= 0:
        length = len(digits) + exponent if number else 1
    elif len(digits) > -exponent:
        length = len(digits) + 1
    else:
        length = 2 - exponent
    return length + 1 if sign and number else length


class _Measure:
    """The values, levels and characters each node writes out, measured once per node.

    A scalar is one value and as many characters as it writes out, a collection is one
    value plus what its items (a mapping's keys and values) write out, and an alias
    stands for all of its node wherever it is written. A merge key copies the pairs
    of the mappings it names into the mapping that holds it, at that mapping's level,
    so it adds what they write out, less the mappings themselves. What a merge key's
    value adds is worked out once however many mappings hold the key, so the measure
    costs what the text costs.

    It walks the document with a stack of generators, as PyYAML's own constructor
    does, so how deep a document is, which its text sets, does not use the
    interpreter's stack: every document PyYAML can compose is measured.
    """

    def __init__(self, budget: ReadBudget) -> None:
        self.yaml = _yaml()
        self.budget = budget
        self.collections = (self.yaml.MappingNode, self.yaml.SequenceNode)
        self.measured: Dict[int, _Size] = {}
        #: The pairs each mapping holds once its own merge keys are flattened.
        self.pairs: Dict[int, int] = {}
        #: What the value of a merge key adds: its values, levels and characters, the pairs
        #: it copies and the mappings it names.
        self.merges: Dict[int, Tuple[int, int, int, int, int]] = {}
        #: The characters a float is counted for, once for each node.
        self.spelled: Dict[int, int] = {}
        #: The mappings the merge keys met so far in this document name.
        self.named = 0
        self.on_path: Set[int] = set()

    def node(self, root: Any, level: int) -> _Size:
        """The values, levels and characters *root* writes out, seen at *level*."""
        stack = [self._visit(root, level)]
        result: Any = None
        while stack:
            try:
                call = stack[-1].send(result)
            except StopIteration as done:
                stack.pop()
                result = done.value
            else:
                stack.append(self._visit(*call))
                result = None
        return cast(_Size, result)

    def _scalar(self, node: Any, level: int) -> _Size:
        if level > self.budget.limits.depth:
            raise _refuse(node, f"nests deeper than {self.budget.limits.depth} levels")
        return 1, 1, self._characters(node)

    def _characters(self, node: Any) -> int:
        """The characters the scalar *node* writes out: its text, or, for a float, the characters
        its decimal is spelled in when that is more, since a number is spelled in full: ``1.e+999``
        is seven characters of text and a thousand written out. A float written with an exponent
        is spelled much longer than its text; one written without is spelled in at most a character
        more (``.5`` is ``0.5``), which is not counted. A number that needs more than ``digits``
        digits is refused where it is built, so it counts as no more than that.

        What an alias of the float costs is a lookup, and a text as long as that count is not
        parsed at all, so the cost of the count is the text's however often it is held.
        """
        text = node.value
        if node.tag != _FLOAT_TAG:
            return len(text)
        known = self.spelled.get(id(node))
        if known is None:
            known = self.spelled[id(node)] = self._spelled(text)
        return known

    def _spelled(self, text: str) -> int:
        """The characters the float written *text* counts for: see :meth:`_characters`."""
        if "e" not in text and "E" not in text:
            return len(text)
        most = self.budget.limits.digits + 3  # its digits, and a sign, a point and a leading zero
        if len(text) >= most:
            return len(text)  # it counts for no more than *most*, so it is not parsed to find out
        try:
            number = Decimal(text.replace("_", ""))
        except InvalidOperation:
            return len(text)
        if not number.is_finite():
            return len(text)
        return max(len(text), min(_spelled_length(number), most))

    def _visit(self, node: Any, level: int) -> _Walk:
        """Measure *node*, seen at *level*, as a walk the caller drives."""
        if not isinstance(node, self.collections):
            return self._scalar(node, level)
        limits = self.budget.limits
        if level > limits.depth:
            raise _refuse(node, f"nests deeper than {limits.depth} levels")
        key = id(node)
        known = self.measured.get(key)
        if known is not None:
            if level + known[1] - 1 > limits.depth:
                raise _refuse(node, f"nests deeper than {limits.depth} levels")
            return known
        if key in self.on_path:
            raise _refuse(node, "holds itself through an alias")
        self.on_path.add(key)
        if isinstance(node, self.yaml.MappingNode):
            values, levels, characters = yield from self._mapping(node, level)
        else:
            values, levels, characters = 1, 1, 0
            for item in node.value:
                if isinstance(item, self.collections):
                    item_values, item_levels, item_characters = yield item, level + 1
                else:
                    item_values, item_levels, item_characters = self._scalar(item, level + 1)
                values, levels = values + item_values, max(levels, item_levels + 1)
                characters += item_characters
        self.on_path.discard(key)
        if values > limits.values:
            raise _refuse(
                node,
                f"writes out more than {limits.values} values once its aliases and merge keys "
                "are written out",
            )
        if characters > limits.characters:
            raise _refuse(
                node,
                f"writes out more than {limits.characters} characters once its aliases and "
                "merge keys are written out",
            )
        self.measured[key] = (values, levels, characters)
        return values, levels, characters

    def _mapping(self, node: Any, level: int) -> _Walk:
        values, levels, characters, pairs = 1, 1, 0, 0
        for key_node, value_node in node.value:
            if key_node.tag == _MERGE_TAG:
                added, merged_levels, added_characters, copied = yield from self._merge(
                    node, value_node, level
                )
                values, levels = values + added, max(levels, merged_levels)
                characters, pairs = characters + added_characters, pairs + copied
                continue
            for part in (key_node, value_node):
                if isinstance(part, self.collections):
                    part_values, part_levels, part_characters = yield part, level + 1
                else:
                    part_values, part_levels, part_characters = self._scalar(part, level + 1)
                values, levels = values + part_values, max(levels, part_levels + 1)
                characters += part_characters
            pairs += 1
        self.pairs[id(node)] = pairs
        return values, levels, characters

    def _merge(
        self, host: Any, value_node: Any, level: int
    ) -> Generator[Tuple[Any, int], _Size, Tuple[int, int, int, int]]:
        """What the mappings a merge key's value names add to *host*, seen at *level*.

        Its values, levels and characters, and the pairs it copies. The mappings' pairs
        become the host's own, and building them copies them: the cost the merge-pair
        bound counts. Naming a mapping costs a visit however empty it is: the cost the
        bound on mappings named counts.
        """
        limits = self.budget.limits
        key = id(value_node)
        known = self.merges.get(key)
        if known is None or level + known[1] - 1 > limits.depth:
            # The first time, or a mapping too deep from here: measuring them one by one
            # refuses it.
            parts = _merged_parts(value_node)
            added, levels, characters, copied = 0, 0, 0, 0
            for merged in parts:
                merged_values, merged_levels, merged_characters = yield merged, level
                added, levels = added + merged_values - 1, max(levels, merged_levels)
                characters += merged_characters
                copied += self.pairs[id(merged)]
            known = self.merges[key] = (added, levels, characters, copied, len(parts))
        self.named += known[4]
        if self.named > limits.merged:
            raise _refuse(host, f"names more than {limits.merged} mappings in its merge keys")
        self.budget.merge_pairs_left -= known[3]
        if self.budget.merge_pairs_left < 0:
            raise _refuse(
                host,
                f"merge keys (<<) copy more than {limits.merge_pairs} pairs in one read",
            )
        return known[0], known[1], known[2], known[3]


def _check_bounds(document: Any, budget: ReadBudget) -> None:
    """Refuse *document* (a composed node) when it writes out past *budget*."""
    _Measure(budget).node(document, 1)


def _construct_as_the_safe_loader_does(loader: Any, node: Any) -> None:
    """Construct a float or a boolean scalar as PyYAML's SafeLoader does, or refuse it.

    SafeLoader builds them with ``float()`` and a lookup, which raise a bare
    ``ValueError``, ``IndexError`` or ``KeyError`` for a scalar whose explicit
    tag cannot build it (``!!float ZZZ``, ``!!float ''``, ``!!bool maybe``).
    A scalar YAML resolves by itself always builds. The refusal names the
    position, never the value.
    """
    kind = "a float" if node.tag == _FLOAT_TAG else "a boolean"
    build = loader.construct_yaml_float if node.tag == _FLOAT_TAG else loader.construct_yaml_bool
    try:
        build(node)
    except (ValueError, IndexError, KeyError) as exc:
        raise _refuse(node, f"a scalar read as {kind} has no {kind[2:]} value") from exc


def _construct_every_typed_scalar(loader: Any, document: Any) -> None:
    """Construct each integer, float and boolean scalar in *document*, in document order.

    An integer is constructed as :class:`ExactLoader` constructs one, so one
    no read can form is refused; a float and a boolean as a plain SafeLoader
    constructs them (:func:`_construct_as_the_safe_loader_does`). Depth
    first, iterative, and visiting each node once: an alias is the node it
    names, so a recursive document is walked once rather than forever.
    """
    yaml = _yaml()
    seen: Set[int] = set()
    pending: List[Any] = [document]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            if node.tag == _INTEGER_TAG:
                _construct_exact_int(loader, node)
            elif node.tag in (_FLOAT_TAG, _BOOL_TAG):
                _construct_as_the_safe_loader_does(loader, node)
        elif isinstance(node, yaml.SequenceNode):
            pending.extend(reversed(node.value))
        elif isinstance(node, yaml.MappingNode):
            for key, value in reversed(node.value):
                pending.extend((value, key))


def _loader(text: str, name: str, budget: ReadBudget) -> Any:
    loader = _build_loader()(text, budget)
    loader.name = name  # every mark names the file, as a stream opened from it would
    return loader


#: A line break as YAML counts one (YAML 1.1, 5.4), for a position PyYAML
#: gives as an offset into the text rather than a mark.
_LINE_BREAK = re.compile("\r\n|[\n\r\x85\u2028\u2029]")


def _line_and_column(text: str, position: int) -> Tuple[int, int]:
    """The line and column, from 1, of the character at offset *position* in *text*."""
    line, start = 1, 0
    for line_break in _LINE_BREAK.finditer(text, 0, position):
        line, start = line + 1, line_break.end()
    return line, position - start + 1


#: The most characters of PyYAML's account of a problem that a refusal keeps.
_MOST_PROBLEM = 200

#: What PyYAML quotes in a message: a token it expected or found, or the text's
#: own anchor, alias, tag or character.
_QUOTED = re.compile("'([^']*)'")


@lru_cache(maxsize=1)
def _token_names() -> FrozenSet[str]:
    """PyYAML's names for its tokens (``','``, ``'<block end>'``): a message may quote them."""
    tokens = _yaml().tokens
    names = (
        getattr(value, "id", None) for value in vars(tokens).values() if isinstance(value, type)
    )
    return frozenset(name for name in names if isinstance(name, str))


def _without_values(account: str) -> str:
    """PyYAML's *account* of a problem, with every quoted word of the text left out.

    PyYAML quotes what it expected or found. Its own token names say nothing of
    the document and are kept. An anchor, an alias, a tag or a character is the
    document's own text, which a refusal never repeats, so each becomes ``'...'``.
    The account is cut to ``_MOST_PROBLEM`` characters.
    """
    names = _token_names()
    kept = _QUOTED.sub(
        lambda match: match.group(0) if match.group(1) in names else "'...'", account
    )
    return kept if len(kept) <= _MOST_PROBLEM else kept[:_MOST_PROBLEM] + "..."


def _unreadable(error: BaseException, text: str, name: str) -> FluxError:
    """``FLX-3105`` for *text* PyYAML cannot read: what it says is wrong, and where.

    The problem is PyYAML's own (``expected ',' or ']', but got ':'``) rather
    than its context (``while parsing a flow sequence``), except where the
    context is the problem, as with a duplicate anchor. It is placed at the line
    and column of PyYAML's mark, or of the offset a :class:`yaml.ReaderError`
    gives. No word of the text itself is repeated (:func:`_without_values`).
    """
    problem = getattr(error, "problem", None)
    context = getattr(error, "context", None)
    if isinstance(context, str) and context.startswith("found duplicate anchor"):
        account = context.split(";", 1)[0]
    elif isinstance(problem, str) and problem:
        account = problem
    elif isinstance(context, str) and context:
        account = context
    else:
        account = str(error).splitlines()[0] if str(error) else type(error).__name__
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    position = getattr(error, "position", None)
    if isinstance(line, int) and isinstance(column, int):
        where = f" (line {line + 1}, column {column + 1})"
    elif isinstance(position, int) and 0 <= position <= len(text):
        line, column = _line_and_column(text, position)
        where = f" (line {line}, column {column})"
    else:
        where = ""
    return FluxError(FLX_3105, f"{name}: unreadable YAML: {_without_values(account)}{where}")


def check_documents(text: str, *, name: str, budget: Optional[ReadBudget] = None) -> None:
    """Refuse what no read of *text* can form or should write out, before another reader.

    For a reader that bounds nothing, such as the FLUX reference validator's
    (a plain SafeLoader): each document is composed (parsed, nothing
    constructed) and held to *budget*, and every integer scalar is
    constructed as :class:`ExactLoader` constructs one and every float and
    boolean scalar as a SafeLoader does, so one that no read can build is
    refused by its position, in document order, before the next document is
    parsed: the order that reader meets them in. Text that does not parse is
    left to that reader,
    which meets it the same way. Text that holds a character YAML does not
    allow is refused here, with the message that reader gives: PyYAML refuses
    such text before it reads a document, while a reader of a file meets the
    character only when it reaches it, after building every document before
    it. *name* is the file the refusals name.
    """
    yaml = _yaml()
    budget = budget if budget is not None else ReadBudget()
    try:
        loader = _loader(text, name, budget)
    except yaml.YAMLError as exc:
        raise _unreadable(exc, text, name) from None
    try:
        while loader.check_node():
            document = loader.get_node()
            _check_bounds(document, budget)
            _construct_every_typed_scalar(loader, document)
    except RecursionError:
        raise FluxError(FLX_3105, f"{name}: nests too deeply to read") from None
    except yaml.YAMLError:
        return
    finally:
        loader.dispose()


def load_documents(
    text: str, *, name: str, budget: Optional[ReadBudget] = None
) -> Tuple[Dict[str, Any], ...]:
    """Every mapping document in *text*, read with no float and no datetime, within *budget*.

    The read the digests are taken over. Raises ``FLX-3105`` on unreadable
    YAML, so a caller never has to tell "no documents" from "a broken
    file". *name* is the file the refusals name; pass one *budget* for every
    file of a bundle.
    """
    yaml = _yaml()
    budget = budget if budget is not None else ReadBudget()
    documents: List[Any] = []
    loader = None
    try:
        # Made inside the try: PyYAML refuses a character YAML does not allow
        # when a loader is made from text, not when it reads.
        loader = _loader(text, name, budget)
        while loader.check_data():
            documents.append(loader.get_data())
    except RecursionError:
        raise FluxError(FLX_3105, f"{name}: nests too deeply to read") from None
    except yaml.YAMLError as exc:
        raise _unreadable(exc, text, name) from exc
    except (KeyError, ValueError) as exc:
        # What PyYAML's constructors raise, with no mark, for a scalar its
        # explicit tag cannot build, such as ``!!bool maybe``. The check builds
        # every typed scalar as this read does and refuses that one by position.
        # A fresh budget with the caller's own limits: the read already spent this one.
        check_documents(text, name=name, budget=ReadBudget(budget.limits))
        raise FluxError(FLX_3105, f"{name}: unreadable YAML ({type(exc).__name__})") from exc
    finally:
        if loader is not None:
            loader.dispose()
    return tuple(document for document in documents if isinstance(document, dict))
