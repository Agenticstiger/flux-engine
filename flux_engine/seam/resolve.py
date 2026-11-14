# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Resolving the FLUID seam: which contract, which expose, which bytes.

Implements the resolution half of KERNEL-SPEC §8: "``contractDigest`` is
recomputed from the in-bundle ``.fluid.yml`` (``jcs-document``) and
compared with the declared ``provenance.contractDigest`` (``FLX-3101`` on
mismatch)."

**The validator is borrowed, not rebuilt.** Every cross-document rule the
seam depends on -- ``productRef`` resolves to a ``.fluid.yml`` in the
bundle, ``exposeId`` is a member of its ``exposes[]``, the pinned
``fluidVersion`` is satisfied, the emitted contract is at least as strict
as every consent profile gating the simulation -- is already implemented
in the FLUX reference validator, and this module delegates to it
(``flux_engine.spec.validate.Bundle``) rather than restating it. What this
module adds is the part the offline validator cannot do: it hands back the
resolved documents, the file bytes, and a recomputed digest in the
engine's own canonical domain, so the run has one object to bind to.

**The two digests are computed by two implementations, on purpose.** The
validator's own ``canonical_digest`` and the engine's
:func:`flux_engine.core.digest.digest_of_document` both claim to be "sha256
over the RFC 8785 canonicalisation", and a bundle that satisfies one and
not the other would be a bundle whose seam means two things.
:func:`resolve_emit` recomputes with the engine's canonicaliser --
the one every other digest in the run goes through -- and
``test_resolve.py`` pins the two against each other on the shipped
example.

**The seam's own declarations live in the extension namespace.** FLUX
0.5.0 closes ``Simulation.spec.emits[]`` (``additionalProperties: false``)
and opens exactly one door for a dialect: RFC-02's document-level
``extensions`` map, keyed by ``reverse-dns/major``. So the fidelity block
(SEAM-FIDELITY-SPEC §2), the declared row count (§1) and the column
mapping live at::

    extensions:
      com.agentics.flux/1:
        emits:
          <exposeId>:
            rows: 64
            mapping: { <column>: <source path> }
            fidelity: { ... }

:data:`EXTENSION_NAMESPACE` is that key. Every bundle stays valid against
the unmodified published schema -- the public ``scripts/validate.py`` and
``scripts/bundle.py verify`` accept it with no tolerance, no stripping and
no engine-specific relaxation -- and :func:`resolve_emit` hands the block
back beside the emit entry so ``H`` can quote both (KERNEL-SPEC §8,
SEAM-FIDELITY-SPEC §3).

:data:`ADDITIVE_SEAM_FIELDS` is consequently **empty**. The enumeration
stays because it is the one place a future additive ``emits[]`` field
would have to be named, and because a test can prove it is empty; with
nothing in it the tolerance :class:`SeamBundle` applies is a no-op and the
schema sees every document exactly as authored.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import AbstractSet, Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from flux_engine.core.canonical import FLX_1003, nfc
from flux_engine.core.digest import DigestRef, digest_of_bytes, digest_of_document
from flux_engine.core.errors import FluxError, register
from flux_engine.seam.contract import CompiledExpose, compile_expose, find_expose
from flux_engine.spec.bundle import MANIFEST_NAME
from flux_engine.spec.validate import Bundle, canonical_digest, load_yaml_docs, semver_satisfies

__all__ = [
    "ADDITIVE_SEAM_FIELDS",
    "BUNDLE_PACK_PRODUCTS",
    "BundleDigestInputs",
    "EXTENSION_NAMESPACE",
    "ExactLoader",
    "FLUID_DOCUMENT_GLOBS",
    "FLUX_DOCUMENT_GLOBS",
    "LOCK_FILE_NAME",
    "load_documents_exact",
    "FLX_3101",
    "FLX_3102",
    "FLX_3103",
    "FLX_3104",
    "FLX_3105",
    "FLX_3107",
    "FLX_3108",
    "LoadedBundle",
    "ResolvedEmit",
    "SeamBundle",
    "extension_block",
    "flux_schema_validator",
    "load_bundle",
    "resolve_all_emits",
    "resolve_emit",
    "strip_additive_seam_fields",
]

FLX_3101 = register(
    "FLX-3101",
    "declared contractDigest does not match the in-bundle contract",
    "KERNEL-SPEC §8 recomputes the referenced FLUID document's digest from "
    "the bytes actually in the bundle and compares it with the digest the "
    "emit declares. A mismatch means the contract that was reviewed is not "
    "the contract that would ship, which is the one thing the seam exists "
    "to make impossible. Re-run plan to refresh the declared digest, or "
    "restore the contract the digest was taken over.",
)

FLX_3102 = register(
    "FLX-3102",
    "emit productRef resolves to no FLUID document in the bundle",
    "An emit names the FLUID data product it fulfils by id, and that "
    "document travels in the bundle so the proven bytes are the shipped "
    "bytes. No document in the bundle declares this id.",
)

FLX_3103 = register(
    "FLX-3103",
    "emit exposeId is not an expose of the referenced product",
    "The emit names which interface of the data product this stream "
    "fulfils. The referenced product declares no expose with that id, so "
    "there is no column set to emit against.",
)

FLX_3104 = register(
    "FLX-3104",
    "referenced contract does not satisfy the pinned fluidVersion",
    "The emit pins the FLUID version it was written against, exactly or as "
    "a semver range. The referenced document declares a version outside "
    "that pin, so the schema the emit was checked under is not the schema "
    "the contract is written in.",
)

FLX_3105 = register(
    "FLX-3105",
    "bundle does not validate, so its seam cannot be resolved",
    "Resolution delegates to the FLUX offline reference validator: every "
    "reference resolves, every id is unique, every weight sums, and every "
    "in-bundle FLUID document is valid for its declared version. A bundle "
    "that fails those checks has no seam to resolve, and repairing it here "
    "would mean the engine ran against something the validator refused.",
)

FLX_3107 = register(
    "FLX-3107",
    "bundle directory not found",
    "Every command that takes a bundle takes the path of a *directory* "
    "holding the bundle's `.flux.yml` documents (and the `.fluid.yml` "
    "contract they reference). Nothing at the path given is a directory -- "
    "a typo, a path relative to a different working directory, or the path "
    "of one document inside the bundle rather than the bundle itself. "
    "Check the path and re-run the same command; nothing was read and "
    "nothing was written.",
)

FLX_3108 = register(
    "FLX-3108",
    "the bundle directory holds a file this engine does not recognise",
    "A bundle directory holds two classes of file and no third. Its "
    "inputs are the documents the loader reads: `*.flux.yml`, "
    "`*.flux.yaml`, `*.fluid.yml`, `*.fluid.yaml` and `flux.lock`. Its "
    "products are the files a pack writes beside those documents, "
    "`flux-manifest.json` and its detached signature, which are "
    "recognised and left out of `inputDigest` because their content is a "
    "function of the inputs. Any other regular file ends the command "
    "here, naming the file, before a digest is taken. KERNEL-SPEC "
    "section 11 makes `inputDigest` the value `apply` and `verify` bind a "
    "run to, so a file that arrives beside the documents must neither "
    "move that value quietly nor be hashed as an input without anyone "
    "deciding it is one. Delete the file, or keep it outside the bundle "
    "directory. If it is a document kind a newer FLUX version defines, "
    "this engine is too old to read the bundle and the fix is to upgrade "
    "the engine rather than to delete the document.",
)

#: The filename patterns a bundle's documents are named by, and the one
#: exact name its lockfile carries. The loader globs the directory with
#: these and nothing else, and every later decision about which files an
#: `inputDigest` covers reads the loader's result rather than matching the
#: directory a second time: two matchers that agree today are how the
#: original defect was written.
FLUX_DOCUMENT_GLOBS: Tuple[str, ...] = ("*.flux.yml", "*.flux.yaml")
FLUID_DOCUMENT_GLOBS: Tuple[str, ...] = ("*.fluid.yml", "*.fluid.yaml")
LOCK_FILE_NAME = "flux.lock"

#: What packing a bundle writes into the directory it packs. The manifest
#: carries the digests of the very files beside it, so hashing it into
#: `inputDigest` would fold a bundle's digest back into itself, and the
#: FLUX bundle format already excludes the manifest from its own file map
#: and Merkle root for that reason. The signature is the detached
#: signature `ssh-keygen -Y sign` writes next to the manifest it signs.
#:
#: Two exact names, never a suffix class: a wildcard over `*.sig` would
#: let a file called `evil.sig` sit beside the documents uncovered by any
#: digest, which is the case the bundle format's own verifier refuses.
#: The names are built from the bundler this engine links against, so an
#: upstream rename carries here with no edit.
BUNDLE_PACK_PRODUCTS: FrozenSet[str] = frozenset({MANIFEST_NAME, f"{MANIFEST_NAME}.sig"})

#: This engine's RFC-02 extension namespace. ``reverse-dns/major``, the
#: shape the published schema's ``propertyNames`` pattern requires, so a
#: bundle carrying it validates unmodified.
EXTENSION_NAMESPACE = "com.agentics.flux/1"

#: The key under :data:`EXTENSION_NAMESPACE` that holds the per-expose
#: blocks, and the three keys one block may carry.
_EMITS_KEY = "emits"
_ROWS_KEY = "rows"
_MAPPING_KEY = "mapping"

#: The one key SEAM-FIDELITY-SPEC §2 declares the block under. There is
#: no second spelling: two spellings of one declaration would be two
#: digests of one contract (§2's own reason for refusing a bare support
#: list), and a name only this engine's internals use has no business in
#: a document an author writes.
_FIDELITY_BLOCK_KEY: str = "fidelity"

#: Every key an extension block for one expose may carry. Closed: an
#: unknown key is a declaration this engine would silently ignore, and the
#: whole point of moving the block into ``extensions`` was to stop the
#: seam from tolerating declarations nothing reads.
_BLOCK_KEYS: FrozenSet[str] = frozenset({_ROWS_KEY, _MAPPING_KEY, _FIDELITY_BLOCK_KEY})

#: Additive FLUX RFC fields on an ``emits[]`` entry that the published
#: schema does not carry. **Empty**: everything this engine declares about
#: an emit now lives under :data:`EXTENSION_NAMESPACE` (see the module
#: docstring), so there is nothing left to tolerate and the schema sees
#: every document exactly as authored. The enumeration survives as the one
#: place a future additive field would be named.
ADDITIVE_SEAM_FIELDS: FrozenSet[str] = frozenset()


class ExactLoader(yaml.SafeLoader):
    """A YAML loader that produces no ``float`` and no ``datetime``.

    A bundle is read twice, for two incompatible consumers.

    * **The JSON Schema validator** needs real JSON types: ``unitInterval``
      is ``{"type": "number"}``, and a ``Decimal`` is not a number to
      ``jsonschema``. That read is the reference validator's own, through
      :func:`flux_engine.spec.validate.load_yaml_docs`.
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
    """


def _construct_exact_float(loader: yaml.SafeLoader, node: yaml.Node) -> Decimal:
    """Construct a YAML float scalar as an exact :class:`~decimal.Decimal`."""
    text = str(loader.construct_scalar(node)).replace("_", "")
    if ":" in text:
        raise FluxError(
            FLX_3105,
            "YAML sexagesimal numbers have no canonical decimal spelling; write "
            "the number in plain decimal notation",
        )
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise FluxError(FLX_3105, f"{text!r} is not a decimal number") from exc
    if not value.is_finite():  # pragma: no cover - see below
        # Defence in depth, and today unreachable: YAML 1.1 spells every
        # infinity and not-a-number with a leading dot (``.inf``, ``.nan``),
        # and Decimal refuses all of those at the constructor above. The
        # guard stays because the loader that decides which scalars reach
        # here is the one thing about this function most likely to change,
        # and a NaN in a digest pre-image is not a failure mode worth
        # discovering downstream.
        raise FluxError(
            FLX_3105,
            "an infinite or not-a-number value has no digest and no ordering",
        )
    return value


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
_MOST_INTEGER_DIGITS: int = sys.int_info.default_max_str_digits

#: The smallest magnitude with more than :data:`_MOST_INTEGER_DIGITS` digits.
_TOO_LONG_INTEGER: int = 10**_MOST_INTEGER_DIGITS

#: The tag YAML resolves an integer scalar to.
_INTEGER_TAG = "tag:yaml.org,2002:int"


def _construct_exact_int(loader: yaml.SafeLoader, node: yaml.Node) -> int:
    """Construct a YAML integer scalar, or refuse one no reader can form.

    The digits are counted before ``int()`` is called, so a decimal integer
    too long for CPython is refused instead of raising from inside it; the
    value is bounded afterwards, for the spellings ``int()`` forms at any
    length (hex, octal, binary). A spelling PyYAML cannot build at all, such
    as ``0x_`` or ``!!int ''``, raised a bare ``ValueError`` or
    ``IndexError``: it is unreadable YAML (``FLX-3105``). Each refusal names
    the file and the position, never the value.
    """
    mark = node.start_mark
    where = f"(line {mark.line + 1}, column {mark.column + 1})"
    name = Path(mark.name).name
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
    return value


ExactLoader.add_constructor("tag:yaml.org,2002:float", _construct_exact_float)
ExactLoader.add_constructor(_INTEGER_TAG, _construct_exact_int)
ExactLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    lambda loader, node: loader.construct_scalar(node),
)


def _refuse_integers_no_read_can_form(path: Path) -> None:
    """Refuse an integer in *path* that no read can form, before the validator reads it.

    The validator's own read is borrowed (:func:`load_yaml_docs`), and it
    catches YAML errors only, so each integer :func:`_construct_exact_int`
    refuses escaped it as a bare ``ValueError``: from PyYAML's ``int()``, or
    from the first schema message that printed a hex value too long to print.
    Each document is composed here (parsed, nothing constructed) and every
    scalar resolved as an integer is constructed as :class:`ExactLoader`
    constructs one, in document order, before the next document is parsed:
    the order the validator's read meets them in. A file that does not
    parse, or does not exist, is left to that read, which reports it.
    """
    try:
        with Path(path).open(encoding="utf-8") as handle:
            loader = ExactLoader(handle)
            try:
                while loader.check_node():
                    _construct_every_integer(loader, loader.get_node())
            finally:
                loader.dispose()
    except (yaml.YAMLError, OSError):
        return


def _construct_every_integer(loader: ExactLoader, document: yaml.Node) -> None:
    """Construct each integer scalar in *document*, depth first, in document order.

    Iterative, and visiting each node once: an alias is the node it names,
    so a recursive document is walked once rather than forever.
    """
    seen: Set[int] = set()
    pending: List[yaml.Node] = [document]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            if node.tag == _INTEGER_TAG:
                _construct_exact_int(loader, node)
        elif isinstance(node, yaml.SequenceNode):
            pending.extend(reversed(node.value))
        elif isinstance(node, yaml.MappingNode):
            for key, value in reversed(node.value):
                pending.extend((value, key))


def _with_yaml_mark(path: Path, message: str) -> str:
    """Append ``(line L, column C)`` to a YAML load error when the parser knows it.

    The reader this engine loads bundles through reports only the first
    line of the parser's message, which is the sentence *without* the
    position -- so a broken document said what was wrong and never where.
    The position is re-derived by parsing the file again here, on the
    failure path only, and the message is returned unchanged when the
    second parse finds nothing to report.
    """
    try:
        with Path(path).open(encoding="utf-8") as handle:
            list(yaml.compose_all(handle))
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        if mark is not None:
            return f"{message} (line {mark.line + 1}, column {mark.column + 1})"
    except (yaml.YAMLError, OSError):
        return message
    return message


def load_documents_exact(path: Path) -> Tuple[Dict[str, Any], ...]:
    """Load every YAML document in *path* with no float and no datetime.

    The read the digests are taken over. Raises ``FLX-3105`` on unreadable
    YAML, so a caller never has to tell "no documents" from "a broken
    file".
    """
    try:
        with Path(path).open(encoding="utf-8") as handle:
            documents = list(yaml.load_all(handle, Loader=ExactLoader))
    except (yaml.YAMLError, OSError) as exc:
        raise FluxError(
            FLX_3105, f"{Path(path).name}: unreadable YAML ({type(exc).__name__})"
        ) from exc
    return tuple(document for document in documents if isinstance(document, dict))


def flux_schema_validator() -> Draft202012Validator:
    """Build the FLUX schema validator :class:`Bundle` expects.

    The schema ships with whichever ``flux_spec`` provided :class:`Bundle`
    -- the published distribution when it is installed, the vendored
    fallback otherwise -- so it is read from that module rather than from
    a path this repository would have to keep in step.
    """
    provider = importlib.import_module(Bundle.__module__)
    schema_path = Path(provider.FLUX_SCHEMA_PATH)
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def strip_additive_seam_fields(
    document: Mapping[str, Any], *, fields: FrozenSet[str] = ADDITIVE_SEAM_FIELDS
) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """Return a copy of *document* with the additive emit fields removed.

    :data:`ADDITIVE_SEAM_FIELDS` is empty (see the module docstring), so
    the default call returns the document unchanged and reports nothing
    removed. The function keeps its shape because it is where a future
    additive field would be handled, and because passing an explicit
    *fields* set is how a test exercises the removal.

    Only ``spec.emits[]`` entries are touched: the additive fields are
    declared there and nowhere else, so a key of the same name anywhere
    else in the document is left exactly where the author put it. The
    second element names what was removed, as ``spec.emits[i].<field>``,
    so a caller can report the tolerance rather than merely benefit from
    it.
    """
    removed: List[str] = []
    copy: Dict[str, Any] = dict(document)
    spec = copy.get("spec")
    if not isinstance(spec, Mapping):
        return copy, ()
    emits = spec.get("emits")
    if not isinstance(emits, Sequence) or isinstance(emits, (str, bytes)):
        return copy, ()

    new_emits: List[Any] = []
    for index, entry in enumerate(emits):
        if not isinstance(entry, Mapping):
            new_emits.append(entry)
            continue
        trimmed = {key: value for key, value in entry.items() if key not in fields}
        removed.extend(f"spec.emits[{index}].{key}" for key in sorted(entry) if key in fields)
        new_emits.append(trimmed)
    if not removed:
        return copy, ()
    new_spec = dict(spec)
    new_spec["emits"] = new_emits
    copy["spec"] = new_spec
    return copy, tuple(removed)


class SeamBundle(Bundle):
    """A :class:`Bundle` that validates documents modulo the additive fields.

    Overrides exactly one method. :meth:`Bundle.load` reads the directory,
    validates each document against the FLUX schema and keeps only the
    schema-valid ones; this subclass performs the same walk but hands the
    schema a copy with :data:`ADDITIVE_SEAM_FIELDS` removed, while storing
    the document **as authored**. Every cross-document check the base class
    runs afterwards therefore sees the real document, and only the schema's
    ``additionalProperties: false`` is relaxed, only on ``emits[]``, only
    for the enumerated fields.

    :attr:`tolerated` records what was removed from which document, so the
    relaxation appears in the resolution result instead of vanishing --
    and with :data:`ADDITIVE_SEAM_FIELDS` empty it is always empty, which
    is the observable form of "this engine validates against the published
    schema with nothing relaxed".
    """

    def __init__(
        self,
        directory: Path,
        flux_validator: Draft202012Validator,
        *,
        tolerate_additive: bool = True,
    ) -> None:
        super().__init__(directory, flux_validator)
        self.tolerate_additive = tolerate_additive
        self.tolerated: Dict[str, Tuple[str, ...]] = {}
        #: Every error raised while *reading and schema-checking* the
        #: documents, as opposed to the cross-document checks that run
        #: afterwards over whatever loaded. Empty until :meth:`load` runs.
        self.load_errors: Tuple[str, ...] = ()

    def load(self) -> None:
        """Load and schema-check every document in the bundle directory.

        Records where the load phase ended (:attr:`load_errors`) so the
        caller can tell a document that could not be read or did not
        validate from a cross-document check made afterwards. Those later
        checks read ``flux_docs``, which a failed document is not in, so
        every reference to it is reported as dangling -- a chain of derived
        errors asserting that a document sitting on disk is missing.
        """
        for path in self.flux_paths():
            _refuse_integers_no_read_can_form(path)
            docs, load_error = load_yaml_docs(path)
            if load_error:
                self.err(path.name, _with_yaml_mark(path, load_error))
                continue
            for doc in docs:
                self._load_flux_document(path, doc)
        for path in self.fluid_paths():
            _refuse_integers_no_read_can_form(path)
            docs, load_error = load_yaml_docs(path)
            if load_error:
                self.err(path.name, _with_yaml_mark(path, load_error))
                continue
            for doc in docs:
                self._load_fluid_document(path, doc)
        # RFC-03's supply-chain check reads the lockfile with the validator's
        # loader later in this run, so its integers are refused here first.
        _refuse_integers_no_read_can_form(self.dir / LOCK_FILE_NAME)
        self.load_errors = tuple(self.errors)

    def flux_paths(self) -> List[Path]:
        return [path for pattern in FLUX_DOCUMENT_GLOBS for path in sorted(self.dir.glob(pattern))]

    def fluid_paths(self) -> List[Path]:
        return [path for pattern in FLUID_DOCUMENT_GLOBS for path in sorted(self.dir.glob(pattern))]

    def _load_flux_document(self, path: Path, doc: Any) -> None:
        if not isinstance(doc, dict):
            self.err(path.name, f"document is not a mapping ({type(doc).__name__})")
            return
        candidate: Mapping[str, Any] = doc
        if self.tolerate_additive:
            candidate, removed = strip_additive_seam_fields(doc)
            if removed:
                self.tolerated[str(doc.get("id", path.name))] = removed
        errors = sorted(self.flux_validator.iter_errors(candidate), key=lambda e: e.json_path)
        for error in errors:
            self.err(path.name, f"schema: {error.json_path}: {error.message}")
        if errors:
            return
        doc_id = doc.get("id")
        if not isinstance(doc_id, str):  # pragma: no cover - the schema requires an id
            self.err(path.name, "document has no string 'id'")
        elif doc_id in self.flux_docs:
            self.err(path.name, f"duplicate document id '{doc_id}'")
        else:
            self.flux_docs[doc_id] = doc

    def _load_fluid_document(self, path: Path, doc: Any) -> None:
        if not isinstance(doc, dict):
            self.err(path.name, f"document is not a mapping ({type(doc).__name__})")
            return
        doc_id = doc.get("id")
        if not isinstance(doc_id, str):
            self.err(path.name, "FLUID document has no string 'id'")
        elif doc_id in self.fluid_docs:
            self.err(path.name, f"duplicate FLUID document id '{doc_id}'")
        elif doc_id in self.flux_docs:
            self.err(path.name, f"FLUID document id '{doc_id}' collides with a FLUX document id")
        else:
            self.fluid_docs[doc_id] = (path, doc)


@dataclass(frozen=True)
class BundleDigestInputs:
    """The three maps KERNEL-SPEC §11 builds ``inputDigest`` from, plus what it left out.

    ``documents``, ``fluid_documents`` and ``lock`` are the bundle's
    inputs, keyed by filename and holding the bytes on disk.
    ``not_hashed`` names every remaining file the loader read, so a stage
    can report what it found beside the documents and did not cover. For a
    bundle :func:`load_bundle` returned, those are the pack products:
    :func:`load_bundle` refuses any other name.
    """

    documents: Mapping[str, bytes]
    fluid_documents: Mapping[str, bytes]
    lock: Optional[bytes]
    not_hashed: Tuple[str, ...]


@dataclass(frozen=True)
class LoadedBundle:
    """A validated bundle, with the file bytes the digests are taken over.

    ``flux_documents`` and ``fluid_documents`` hold the documents **as
    authored** -- additive fields included, every number exact -- because
    that is what ``H`` and ``contractDigest`` are computed over. They come
    from :class:`ExactLoader`, not from the validator's own read, so no
    binary float ever reaches a digest. ``file_bytes`` maps a
    bundle-relative filename to the bytes on disk, which is what
    KERNEL-SPEC §11's ``inputDigest`` and the shipped-contract copy of
    SEAM-FIDELITY-SPEC §7 need.

    ``flux_file_names`` and ``fluid_file_names`` are the filenames the
    loader's own globs matched, in the order it read them. They are what
    :meth:`digest_inputs` partitions ``file_bytes`` by, so the files a
    bundle was validated against and the files its ``inputDigest`` covers
    are one selection rather than two that have to be kept equal. They
    carry no default: a caller assembling this object in process is
    stating which of its files are documents, and a default of ``()``
    would silently digest none of them.
    """

    directory: Path
    flux_documents: Mapping[str, Mapping[str, Any]]
    fluid_documents: Mapping[str, Tuple[Path, Mapping[str, Any]]]
    file_bytes: Mapping[str, bytes]
    flux_file_names: Tuple[str, ...]
    fluid_file_names: Tuple[str, ...]
    tolerated_additive_fields: Mapping[str, Tuple[str, ...]]

    def digest_inputs(self) -> BundleDigestInputs:
        """Partition :attr:`file_bytes` into KERNEL-SPEC §11's input maps.

        A file is a FLUX document, a FLUID document or the lockfile
        because the loader matched it as one, never because this method
        matched its name a second time. Everything else is reported in
        :attr:`~BundleDigestInputs.not_hashed` rather than folded into
        ``documents``, which is what keeps a pack product from moving a
        bundle's ``inputDigest``.
        """
        flux_names = set(self.flux_file_names)
        fluid_names = set(self.fluid_file_names)
        documents: Dict[str, bytes] = {}
        fluid_documents: Dict[str, bytes] = {}
        not_hashed: List[str] = []
        for name, data in self.file_bytes.items():
            if name in flux_names:
                documents[name] = data
            elif name in fluid_names:
                fluid_documents[name] = data
            elif name != LOCK_FILE_NAME:
                not_hashed.append(name)
        return BundleDigestInputs(
            documents=documents,
            fluid_documents=fluid_documents,
            lock=self.file_bytes.get(LOCK_FILE_NAME),
            not_hashed=tuple(sorted(not_hashed)),
        )

    def simulation(self, simulation_id: str) -> Mapping[str, Any]:
        """Return the ``Simulation`` document called *simulation_id*."""
        document = self.flux_documents.get(nfc(simulation_id))
        if document is None or document.get("kind") != "Simulation":
            raise FluxError(
                FLX_3105,
                f"bundle declares no Simulation {simulation_id!r} "
                f"(has: {sorted(self.flux_documents)})",
            )
        return document

    def simulation_ids(self) -> Tuple[str, ...]:
        """Every ``Simulation`` id in the bundle, sorted."""
        return tuple(
            sorted(
                doc_id
                for doc_id, doc in self.flux_documents.items()
                if doc.get("kind") == "Simulation"
            )
        )


def load_bundle(directory: Path, *, tolerate_additive: bool = True) -> LoadedBundle:
    """Validate the bundle at *directory* and return its resolved documents.

    Two reads, for the two consumers :class:`ExactLoader` describes: the
    validator's own read decides whether the bundle is legal, and the exact
    read supplies the documents every digest is taken over. The document id
    sets of the two reads are compared, so a loader that drifted -- one
    that dropped a document the other kept -- is a failure here rather than
    a digest over a document nothing validated.

    Raises ``FLX-3107`` when the path is not a directory at all,
    ``FLX-3108`` when the directory holds a regular file that is neither
    a document nor a pack product, ``FLX-1003`` for an integer too long
    to read, and ``FLX-3105`` carrying the validator errors when the
    bundle does not validate. Nothing is written.

    The file-set refusal comes before validation on purpose: it is a
    statement about which files the validator is even going to consider,
    and a reader who is told "this bundle does not validate" for a
    directory holding an unread file is being sent to the wrong place.
    """
    root = Path(directory)
    # Before the validator, and with its own code: a mistyped path is the
    # most common first-run failure, and reporting it as "this bundle does
    # not validate" sent the reader to an explanation about references and
    # ids for a directory that was never opened.
    if not root.is_dir():
        raise FluxError(
            FLX_3107,
            f"{root} is not a directory; pass the bundle directory that holds the "
            "`.flux.yml` documents",
        )
    bundle = SeamBundle(root, flux_schema_validator(), tolerate_additive=tolerate_additive)
    # One level, regular files only. A subdirectory is not part of the
    # bundle: a run state directory such as `.flux/`, and the `run/`
    # directory an extracted evidence archive carries, both sit beside the
    # documents and neither is read here or digested later.
    files = [path for path in sorted(root.iterdir()) if path.is_file()]
    flux_file_names = tuple(path.name for path in bundle.flux_paths())
    fluid_file_names = tuple(path.name for path in bundle.fluid_paths())
    _require_closed_file_set(
        root,
        names=[path.name for path in files],
        document_names=set(flux_file_names) | set(fluid_file_names),
    )

    errors = bundle.run()
    if errors:
        raise FluxError(FLX_3105, _validation_detail(errors, bundle.load_errors))

    flux_documents: Dict[str, Mapping[str, Any]] = {}
    for path in bundle.flux_paths():
        for document in load_documents_exact(path):
            doc_id = document.get("id")
            if isinstance(doc_id, str) and doc_id in bundle.flux_docs:
                flux_documents[doc_id] = document
    fluid_documents: Dict[str, Tuple[Path, Mapping[str, Any]]] = {}
    for path in bundle.fluid_paths():
        for document in load_documents_exact(path):
            doc_id = document.get("id")
            if isinstance(doc_id, str) and doc_id in bundle.fluid_docs:
                fluid_documents[doc_id] = (path, document)

    _require_same_document_set(bundle, flux_documents, fluid_documents)

    file_bytes = {path.name: path.read_bytes() for path in files}
    return LoadedBundle(
        directory=root,
        flux_documents=flux_documents,
        fluid_documents=fluid_documents,
        file_bytes=file_bytes,
        flux_file_names=flux_file_names,
        fluid_file_names=fluid_file_names,
        tolerated_additive_fields=dict(bundle.tolerated),
    )


#: How many errors one refusal prints before it stops enumerating.
_MAX_REPORTED_ERRORS = 10


def _validation_detail(errors: Sequence[str], load_errors: Sequence[str]) -> str:
    """Render a bundle's validation errors: one per line, causes only.

    When any document failed to load or failed schema, only those errors
    are shown. Every check after the load phase reads the documents that
    loaded, so a reference into a document that did not is reported as
    dangling -- and the reader is sent hunting for a file that is right
    there. Those derived lines are replaced by a count of what was skipped,
    which is a true statement about why they are not listed.
    """
    reported = list(load_errors) if load_errors else list(errors)
    suppressed = len(errors) - len(reported)
    lines = [f"  {error}" for error in reported[:_MAX_REPORTED_ERRORS]]
    if len(reported) > _MAX_REPORTED_ERRORS:
        lines.append(f"  ... and {len(reported) - _MAX_REPORTED_ERRORS} more")
    if suppressed:
        lines.append(
            f"  {suppressed} further check(s) skipped: a document above did not "
            "load or did not validate, so every check that reads it would report "
            "it as missing"
        )
    return "\n" + "\n".join(lines)


def _require_closed_file_set(
    root: Path,
    *,
    names: Sequence[str],
    document_names: AbstractSet[str],
) -> None:
    """Refuse a bundle directory holding a file that is neither input nor product.

    The sibling of :func:`_require_same_document_set` one level down.
    That function refuses a bundle whose two reads disagree about which
    documents exist; this one refuses a bundle whose files and whose
    documents disagree.

    *document_names* is the loader's own glob result, not a second match
    over *names*, so this refusal and the digest's file selection cannot
    hold two opinions about which file is a document. Everything left
    over after the documents, the lockfile and
    :data:`BUNDLE_PACK_PRODUCTS` is named in one refusal.
    """
    unknown = sorted(
        {
            name
            for name in names
            if name not in document_names
            and name != LOCK_FILE_NAME
            and name not in BUNDLE_PACK_PRODUCTS
        }
    )
    if not unknown:
        return
    listed = unknown[:_MAX_REPORTED_ERRORS]
    lines = [f"  {name}" for name in listed]
    if len(unknown) > len(listed):
        lines.append(f"  ... and {len(unknown) - len(listed)} more")
    recognised = ", ".join([*FLUX_DOCUMENT_GLOBS, *FLUID_DOCUMENT_GLOBS, LOCK_FILE_NAME])
    raise FluxError(
        FLX_3108,
        f"{root} holds {len(unknown)} file(s) this engine does not recognise:\n"
        + "\n".join(lines)
        + f"\n  this engine reads {recognised}, and recognises "
        + f"{', '.join(sorted(BUNDLE_PACK_PRODUCTS))} as written by a pack rather than "
        "read by a run",
    )


def _require_same_document_set(
    bundle: SeamBundle,
    flux_documents: Mapping[str, Mapping[str, Any]],
    fluid_documents: Mapping[str, Tuple[Path, Mapping[str, Any]]],
) -> None:
    """Refuse a bundle whose two reads disagree about which documents exist."""
    for label, validated, exact in (
        ("FLUX", set(bundle.flux_docs), set(flux_documents)),
        ("FLUID", set(bundle.fluid_docs), set(fluid_documents)),
    ):
        if validated != exact:
            raise FluxError(
                FLX_3105,
                f"the validating and exact reads disagree about the {label} documents: "
                f"{sorted(validated ^ exact)}",
            )


@dataclass(frozen=True)
class ResolvedEmit:
    """One ``emits[]`` entry, fully resolved against the bundle.

    Attributes
    ----------
    simulation_id, emit_index
        Where the entry was declared.
    emit
        The entry **as authored**. ``H`` (SEAM-FIDELITY-SPEC §3) is taken
        over this mapping, so the object here and the object hashed there
        are the same object.
    extension
        This emit's entry in the Simulation's
        ``extensions["com.agentics.flux/1"].emits`` map, or ``None`` when
        the Simulation declares none. ``H`` quotes it beside the emit
        entry; a bundle with no entry hashes exactly as it did before the
        block existed.
    product_ref, expose_id, fluid_version
        The three fields that identify the seam crossing.
    contract_document
        The parsed ``.fluid.yml``.
    contract_path, contract_bytes
        The file as it sits in the bundle. SEAM-FIDELITY-SPEC §7 ships
        these bytes verbatim, so they travel with the resolution rather
        than being re-read later from a directory that may have moved on.
    contract_digest
        The recomputed ``jcs-document`` digest of ``contract_document``.
    contract_file_digest
        The ``raw-bytes`` digest of ``contract_bytes``: what the shipped
        copy is checked against.
    declared_contract_digest
        ``provenance.contractDigest`` as the emit declared it, or ``None``
        when the emit declares no provenance block.
    expose
        The referenced ``exposes[]`` entry.
    """

    simulation_id: str
    emit_index: int
    emit: Mapping[str, Any]
    product_ref: str
    expose_id: str
    fluid_version: str
    contract_document: Mapping[str, Any]
    contract_path: Path
    contract_bytes: bytes
    contract_digest: DigestRef
    contract_file_digest: DigestRef
    declared_contract_digest: Optional[str]
    expose: Mapping[str, Any]
    extension: Optional[Mapping[str, Any]] = None

    def compile(self) -> CompiledExpose:
        """Compile the referenced expose into its ordered column set."""
        return compile_expose(self.expose)

    @property
    def declared_row_count(self) -> Optional[int]:
        """The extension block's ``rows`` when it declares one (§1).

        ``None`` when it does not: §1's fallback -- "the population size of
        the referenced Segment when the emit is journey-driven" -- is a
        property of the world, not of the seam, so it is the runtime that
        supplies it and this property does not guess.
        """
        rows = (self.extension or {}).get(_ROWS_KEY)
        if isinstance(rows, bool) or not isinstance(rows, int):
            return None
        return rows

    @property
    def fidelity(self) -> Any:
        """The declared fidelity block, or ``None`` when the emit declares none.

        ``None`` is the scenario-driven answer, not an error: an emit
        without a block draws its rows from the run's own moments
        (SEAM-FIDELITY-SPEC §1). A YAML null declares none, as a null
        namespace, ``emits`` map or entry does in :func:`extension_block`.

        Any other value is returned as declared, and not checked here. One
        that is not a mapping is a declaration nobody can read, not the
        absence of one: read as ``None`` it was no block at all, so
        ``validate`` passed it and ``verify`` reported ``not declared``. The
        block's one parser
        (:func:`flux_engine.concordance.predicate.parse_fidelity_block`)
        refuses it (``FLX-5030``) in every command that reads the block.
        """
        return (self.extension or {}).get(_FIDELITY_BLOCK_KEY)

    @property
    def column_mapping(self) -> Optional[Mapping[str, Any]]:
        """The extension block's ``mapping`` overrides, or ``None``.

        Read by :func:`flux_engine.seam.mapping.resolve_column_mapping` as
        step 1 -- the author's own word about which runtime attribute fills
        which declared column. Returned unvalidated: the resolver refuses a
        malformed block by name (``FLX-3302``), because it is the module
        that knows what a legal source path is.
        """
        overrides = (self.extension or {}).get(_MAPPING_KEY)
        return overrides if isinstance(overrides, Mapping) else None


def extension_block(
    simulation: Mapping[str, Any], expose_id: str, *, where: str = ""
) -> Optional[Mapping[str, Any]]:
    """This engine's extension entry for *expose_id*, or ``None``.

    Reads ``extensions["com.agentics.flux/1"].emits[<exposeId>]`` off a
    Simulation document. RFC-02 leaves the contents of a namespace to its
    owner, so every shape check below is this engine's own, and each one
    fails rather than passes: a namespace whose value is not a mapping, an
    ``emits`` that is not a mapping, a block that is not a mapping, and a
    block carrying a key outside :data:`_BLOCK_KEYS` are all ``FLX-3105``.
    An unreadable declaration is not a weaker declaration -- it is a
    declaration the engine would silently drop, which is exactly how a
    fidelity block goes missing without anyone noticing.
    """
    extensions = simulation.get("extensions")
    if not isinstance(extensions, Mapping):
        return None
    namespace = extensions.get(EXTENSION_NAMESPACE)
    if namespace is None:
        return None
    prefix = f"{where}: " if where else ""
    if not isinstance(namespace, Mapping):
        raise FluxError(
            FLX_3105,
            f"{prefix}extensions[{EXTENSION_NAMESPACE!r}] is "
            f"{type(namespace).__name__}, not a mapping",
        )
    emits = namespace.get(_EMITS_KEY)
    if emits is None:
        return None
    if not isinstance(emits, Mapping):
        raise FluxError(
            FLX_3105,
            f"{prefix}extensions[{EXTENSION_NAMESPACE!r}].{_EMITS_KEY} is "
            f"{type(emits).__name__}, not a mapping keyed by exposeId",
        )
    wanted = nfc(expose_id)
    block = next((emits[key] for key in emits if nfc(str(key)) == wanted), None)
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise FluxError(
            FLX_3105,
            f"{prefix}extensions[{EXTENSION_NAMESPACE!r}].{_EMITS_KEY}[{wanted!r}] is "
            f"{type(block).__name__}, not a mapping",
        )
    unknown = sorted(str(key) for key in block if nfc(str(key)) not in _BLOCK_KEYS)
    if unknown:
        raise FluxError(
            FLX_3105,
            f"{prefix}extensions[{EXTENSION_NAMESPACE!r}].{_EMITS_KEY}[{wanted!r}] "
            f"declares {unknown}; this namespace carries {sorted(_BLOCK_KEYS)}",
        )
    return block


def _require_extension_emits_resolve(simulation: Mapping[str, Any], simulation_id: str) -> None:
    """Refuse an extension block keyed by an expose the Simulation never emits.

    A typo'd ``exposeId`` under the namespace would otherwise be a fidelity
    block, a row count and a column mapping that no emit ever reads --
    declared, valid against the published schema, and completely inert.
    """
    extensions = simulation.get("extensions")
    namespace = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
    emits = namespace.get(_EMITS_KEY) if isinstance(namespace, Mapping) else None
    if not isinstance(emits, Mapping):
        return
    declared = simulation.get("spec", {}).get("emits")
    known = (
        {nfc(str(entry.get("exposeId", ""))) for entry in declared if isinstance(entry, Mapping)}
        if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes))
        else set()
    )
    stray = sorted(nfc(str(key)) for key in emits if nfc(str(key)) not in known)
    if stray:
        raise FluxError(
            FLX_3105,
            f"{simulation_id}: extensions[{EXTENSION_NAMESPACE!r}].{_EMITS_KEY} declares "
            f"{stray}, which this Simulation does not emit (it emits {sorted(known)})",
        )


def resolve_emit(loaded: LoadedBundle, *, simulation_id: str, emit_index: int = 0) -> ResolvedEmit:
    """Resolve one ``emits[]`` entry of a Simulation (KERNEL-SPEC §8).

    Checks, in order: the entry exists and is a mapping; ``productRef``
    resolves to a FLUID document in the bundle (``FLX-3102``); the
    referenced document's declared ``fluidVersion`` satisfies the emit's
    pin or range (``FLX-3104``); ``exposeId`` is a member of its
    ``exposes[]`` (``FLX-3103``); and, when the emit declares one, the
    recomputed contract digest equals ``provenance.contractDigest``
    (``FLX-3101``).

    The version check uses the reference validator's own
    :func:`semver_satisfies`, so a pin means here exactly what it means to
    ``fluxsim validate``.
    """
    simulation = loaded.simulation(simulation_id)
    _require_extension_emits_resolve(simulation, simulation_id)
    emits = simulation.get("spec", {}).get("emits")
    if not isinstance(emits, Sequence) or isinstance(emits, (str, bytes)):
        raise FluxError(FLX_3105, f"Simulation {simulation_id!r} declares no emits[]")
    if not 0 <= emit_index < len(emits):
        raise FluxError(
            FLX_3105,
            f"Simulation {simulation_id!r} declares {len(emits)} emits; "
            f"there is no emits[{emit_index}]",
        )
    emit = emits[emit_index]
    if not isinstance(emit, Mapping):
        raise FluxError(
            FLX_3105,
            f"Simulation {simulation_id!r} emits[{emit_index}] is "
            f"{type(emit).__name__}, not a mapping",
        )
    where = f"{simulation_id} emits[{emit_index}]"

    product_ref = str(emit.get("productRef", ""))
    entry = loaded.fluid_documents.get(product_ref)
    if entry is None:
        raise FluxError(
            FLX_3102,
            f"{where}: productRef {product_ref!r} (bundle has: "
            f"{sorted(loaded.fluid_documents)})",
        )
    contract_path, contract_document = entry

    fluid_version = str(emit.get("fluidVersion", ""))
    declared_version = contract_document.get("fluidVersion")
    if not isinstance(declared_version, str) or not semver_satisfies(
        declared_version, fluid_version
    ):
        raise FluxError(
            FLX_3104,
            f"{where}: pin {fluid_version!r}; {contract_path.name} declares "
            f"{declared_version!r}",
        )

    expose_id = str(emit.get("exposeId", ""))
    try:
        expose = find_expose(contract_document, expose_id)
    except FluxError as exc:
        raise FluxError(
            FLX_3103,
            f"{where}: exposeId {expose_id!r} ({exc.detail or exc.code.title})",
        ) from exc

    contract_bytes = loaded.file_bytes.get(contract_path.name, contract_path.read_bytes())
    contract_digest = digest_of_document(contract_document, subject=f"contractDigest:{product_ref}")
    contract_file_digest = digest_of_bytes(contract_bytes, subject=contract_path.name)

    provenance = emit.get("provenance")
    declared_digest = provenance.get("contractDigest") if isinstance(provenance, Mapping) else None
    if isinstance(declared_digest, str) and declared_digest != contract_digest.value:
        raise FluxError(
            FLX_3101,
            f"{where}: declared {declared_digest}, in-bundle contract is "
            f"{contract_digest.value}{_canonicalisation_note(contract_path, declared_digest)}",
        )

    return ResolvedEmit(
        simulation_id=nfc(simulation_id),
        emit_index=emit_index,
        emit=emit,
        product_ref=product_ref,
        expose_id=nfc(expose_id),
        fluid_version=fluid_version,
        contract_document=contract_document,
        contract_path=contract_path,
        contract_bytes=contract_bytes,
        contract_digest=contract_digest,
        contract_file_digest=contract_file_digest,
        declared_contract_digest=declared_digest if isinstance(declared_digest, str) else None,
        expose=expose,
        extension=extension_block(simulation, expose_id, where=where),
    )


def _canonicalisation_note(contract_path: Path, declared_digest: str) -> str:
    """Say so when the declared digest is the reference validator's, not ours.

    The two canonicalisers agree on every document that carries no YAML
    float, and disagree on every document that does: KERNEL-SPEC §2.1
    serialises an exact decimal as a JSON **string** at its declared scale,
    while the reference validator hands the float straight to RFC 8785.
    When a mismatch is that divergence rather than a changed contract, the
    diagnostic says which, because the two have completely different
    remedies.
    """
    documents, load_error = load_yaml_docs(contract_path)
    if load_error:  # pragma: no cover - the bundle already loaded
        return ""
    for document in documents:
        if isinstance(document, dict) and "sha256:" + canonical_digest(document) == declared_digest:
            return (
                f" -- the declared digest is the reference validator's canonicalisation of "
                f"{contract_path.name}; the two differ only where a document carries a YAML "
                "float, which KERNEL-SPEC §2.1 spells as an exact decimal string"
            )
    return ""


def resolve_all_emits(loaded: LoadedBundle, *, simulation_id: str) -> Tuple[ResolvedEmit, ...]:
    """Resolve every ``emits[]`` entry of a Simulation, in declared order.

    Declared order is load-bearing: KERNEL-SPEC §3 forms ``H_bytes`` for a
    multi-emit simulation as "the sha256 of the concatenation of the
    per-emit digests in ``emits[]`` order", so the sequence returned here
    is the sequence that seeds the run.
    """
    simulation = loaded.simulation(simulation_id)
    emits = simulation.get("spec", {}).get("emits")
    count = len(emits) if isinstance(emits, Sequence) and not isinstance(emits, (str, bytes)) else 0
    return tuple(
        resolve_emit(loaded, simulation_id=simulation_id, emit_index=index)
        for index in range(count)
    )
