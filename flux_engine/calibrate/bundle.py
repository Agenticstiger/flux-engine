# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A calibrated bundle: the input bundle with a calibration patch applied, and its register saying so (ADR 0028 section 8).

:func:`write_calibrated_bundle` writes a new bundle at ``out``. It never touches the input bundle, and it
writes the way ``fluxsim init`` does: every file is made in a scratch directory beside ``out``, the bundle
is checked there as ``validate`` and ``plan`` would read it, and only then is the directory moved into place.
A bundle that fails a check is not written, and the scratch directory is removed.

* **The numbers.** Each operation's value is written into the text of its document and every other byte
  stays (:func:`flux_engine.calibrate.patch.splice`). The Simulation's file is the one that is written
  again as a whole, because its register changes: it is read exactly (no float, every number as written),
  edited, and written as ``init`` writes a document (``yaml.safe_dump``, keys in order, width 100) under the
  comment header it had.
* **The register** (format 1-3-0). Each moved value's entry in the Simulation's ``parameters`` has the
  source ``calibrated`` and a ``calibration`` member: the queries it was fitted to, the value before and
  after, the patch's digest, the digest of the profile that held the targets, the held-out profile's digest
  with its largest gap and tolerance, and the cma, sqlglot and DuckDB versions. An entry that named exactly
  that number is replaced, and when only a broader entry named it a new one is added, which decides it
  because the most specific entry does.

This module imports the standard library, PyYAML and the engine's bundle readers, and no SQL library.
"""

from __future__ import annotations

import io
import shutil
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import yaml

from flux_engine.calibrate.errors import FLX_3710, FLX_3711
from flux_engine.calibrate.parameters import FitParameter, exact_text
from flux_engine.calibrate.patch import Located, Operation, locate, splice
from flux_engine.core.errors import FluxError
from flux_engine.seam.coverage import printable
from flux_engine.seam.parameters import Source, check_profile_citations, parse_pointer

__all__ = ["CalibratedBundle", "HeldOutEvidence", "register_entry", "write_calibrated_bundle"]


@dataclass(frozen=True)
class HeldOutEvidence:
    """What the held-out check found, as the register records it: exact decimal texts and a digest."""

    #: The held-out profile's ``sha256:`` digest.
    profile: str
    #: The largest absolute gap of any held-out query with a target.
    largest_gap: str
    #: The tolerance that query was held to.
    tolerance: str


@dataclass(frozen=True)
class CalibratedBundle:
    """The bundle written: where, and the register entries that say what moved."""

    path: Path
    entries: Tuple[Mapping[str, Any], ...]


class _Dumper(yaml.SafeDumper):
    """``init``'s dump, with an exact decimal written as the number it was read from."""


_Dumper.add_representer(
    Decimal,
    lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:float", format(value, "f")),
)


def _dump(document: Mapping[str, Any]) -> str:
    return yaml.dump(dict(document), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


def _header(text: str) -> str:
    """The leading comment block of *text* (``init`` writes one), or nothing."""
    lines: List[str] = []
    for line in io.StringIO(text):
        if not line.startswith("#"):
            break
        lines.append(line)
    return "".join(lines)


def register_entry(
    parameter: FitParameter,
    operation: Operation,
    *,
    queries: Sequence[str],
    patch: str,
    targets: str,
    held_out: HeldOutEvidence,
    versions: Mapping[str, str],
) -> Dict[str, Any]:
    """The ``calibrated`` entry of the register for one moved value (format 1-3-0)."""
    return {
        "document": parameter.doc_id,
        "pointer": parameter.pointer,
        "source": Source.CALIBRATED.value,
        "calibration": {
            "queries": list(queries),
            "before": exact_text(parameter.before),
            "after": operation.value,
            "patch": patch,
            "targets": targets,
            "heldOut": {
                "profile": held_out.profile,
                "largestGap": held_out.largest_gap,
                "tolerance": held_out.tolerance,
            },
            "versions": {key: versions[key] for key in ("cma", "sqlglot", "duckdb")},
        },
    }


def _typed(existing: Any, text: str, where: str) -> Any:
    """*text* as the kind of value *existing* is: a string stays text, a number stays a number."""
    if isinstance(existing, str):
        return text
    if isinstance(existing, bool) or not isinstance(existing, (int, Decimal)):
        raise FluxError(FLX_3711, f"{where}: the target is not a number; nothing was written")
    value = Decimal(text)
    return int(value) if isinstance(existing, int) and value == value.to_integral_value() else value


def _set(document: Any, tokens: Sequence[str], text: str, where: str) -> None:
    node = document
    for token in tokens[:-1]:
        node = node[int(token)] if isinstance(node, list) else node[token]
    last = tokens[-1]
    if isinstance(node, list):
        node[int(last)] = _typed(node[int(last)], text, where)
    else:
        node[last] = _typed(node[last], text, where)


def _with_entries(
    entries: Sequence[Mapping[str, Any]], added: Sequence[Mapping[str, Any]]
) -> List[Mapping[str, Any]]:
    """*entries* with each *added* entry in the place of the one that named exactly its number, or last."""
    out = list(entries)
    for entry in added:
        place = tuple(parse_pointer(str(entry["pointer"])))
        for index, old in enumerate(out):
            if (
                old.get("document") == entry["document"]
                and tuple(parse_pointer(str(old.get("pointer")))) == place
            ):
                out[index] = entry
                break
        else:
            out.append(entry)
    return out


def _exact_documents(text: str, name: str) -> Any:
    from flux_stats.exact_yaml import load_documents

    return load_documents(text, name=name)


def write_calibrated_bundle(
    bundle: Path,
    out: Path,
    *,
    parameters: Sequence[FitParameter],
    operations: Sequence[Operation],
    queries: Sequence[str],
    patch: str,
    targets: str,
    held_out: HeldOutEvidence,
    versions: Mapping[str, str],
    profile_document: Mapping[str, Any],
) -> CalibratedBundle:
    """Write *bundle* with *operations* applied, and each moved value recorded in its register, at *out*.

    *parameters* are the values the operations moved (:class:`~flux_engine.calibrate.parameters.FitParameter`,
    which know their register document and their value before), *queries* the calibration queries the
    fit was made to, *patch* the digest of the patch, *targets* the digest of the profile that held the
    targets, and *profile_document* that profile (the register's citations are read in it). ``FLX-3710``
    when *out* exists; ``FLX-3711`` for an operation the text cannot take or a Simulation file that holds
    more than one document; ``FLX-3105`` and the engine's other refusals for a bundle that does not
    validate or plan.
    """
    from flux_engine.seam.resolve import LOCK_FILE_NAME, load_bundle

    if out.exists() or out.is_symlink():
        raise FluxError(FLX_3710, f"{out} already exists; nothing was written")
    if not out.parent.is_dir():
        raise FluxError(FLX_3710, f"{out.parent} is not a directory; nothing was written")
    loaded = load_bundle(bundle)
    by_key = {parameter.key: parameter for parameter in parameters}
    for op in operations:
        if f"{op.document}#{op.path}" not in by_key:
            raise FluxError(
                FLX_3711,
                f"the operation on {printable(op.document)} {printable(op.path)} names no "
                "declared parameter; nothing was written",
            )
    ops = [(by_key[f"{op.document}#{op.path}"], op) for op in operations]
    simulation_file = _simulation_file(loaded)
    files: Dict[str, bytes] = {
        name: loaded.file_bytes[name]
        for name in (*loaded.flux_file_names, *loaded.fluid_file_names, LOCK_FILE_NAME)
        if name in loaded.file_bytes
    }
    added = [
        register_entry(
            parameter,
            op,
            queries=queries,
            patch=patch,
            targets=targets,
            held_out=held_out,
            versions=versions,
        )
        for parameter, op in ops
    ]
    for name in sorted({parameter.document for parameter, _ in ops} - {simulation_file}):
        files[name] = _spliced(files[name], name, [(p, o) for p, o in ops if p.document == name])
    if ops:
        files[simulation_file] = _simulation(
            files[simulation_file],
            simulation_file,
            [(p, o) for p, o in ops if p.document == simulation_file],
            added,
        )
    return _move_into_place(files, out, bundle, added, profile_document)


def _simulation_file(loaded: Any) -> str:
    """The file that holds the bundle's one Simulation."""
    found = []
    for name in loaded.flux_file_names:
        for document in _exact_documents(loaded.file_bytes[name].decode("utf-8"), name):
            if document.get("kind") == "Simulation":
                found.append(name)
    if len(found) != 1:
        raise FluxError(
            FLX_3711, f"the bundle has {len(found)} Simulation files, not one; nothing was written"
        )
    return found[0]


def _spliced(data: bytes, name: str, ops: Sequence[Tuple[FitParameter, Operation]]) -> bytes:
    text = data.decode("utf-8")
    found: Dict[str, Located] = locate(text, name, [(p.pointer, p.tokens) for p, _ in ops])
    return splice(text, [(found[p.pointer], op.value) for p, op in ops]).encode("utf-8")


def _simulation(
    data: bytes,
    name: str,
    ops: Sequence[Tuple[FitParameter, Operation]],
    added: Sequence[Mapping[str, Any]],
) -> bytes:
    """The Simulation's file with the values written and the register's entries added, as ``init`` dumps it."""
    text = data.decode("utf-8")
    documents = _exact_documents(text, name)
    if len(documents) != 1:
        raise FluxError(
            FLX_3711,
            f"{printable(name)} holds {len(documents)} documents and is written as a whole; "
            "nothing was written",
        )
    document: Dict[str, Any] = documents[0]
    for parameter, op in ops:
        # Positions are checked exactly as for any other file: an alias or a block scalar is refused.
        locate(text, name, [(parameter.pointer, parameter.tokens)])
        _set(
            document,
            parameter.tokens,
            op.value,
            f"{printable(name)} {printable(parameter.pointer)}",
        )
    namespace = document.setdefault("extensions", {}).setdefault("com.agentics.flux/1", {})
    if added:
        namespace["parameters"] = _with_entries(namespace.get("parameters", []), added)
    written = _header(text) + _dump(document)
    if list(_exact_documents(written, name)) != [document]:
        raise FluxError(
            FLX_3711, f"{printable(name)} does not read back as written; nothing was written"
        )
    return written.encode("utf-8")


def _move_into_place(
    files: Mapping[str, bytes],
    out: Path,
    bundle: Path,
    added: Sequence[Mapping[str, Any]],
    profile_document: Mapping[str, Any],
) -> CalibratedBundle:
    from flux_engine.starter import StarterFile, write_project

    scratch = Path(tempfile.mkdtemp(prefix=f".{out.name}.calibrating-", dir=out.parent))
    try:
        write_project(
            scratch / "bundle",
            lambda: [StarterFile(name, "", data) for name, data in files.items()],
        )
        _check(scratch / "bundle", profile_document, {str(e["pointer"]) for e in added})
        if out.exists() or out.is_symlink():
            raise FluxError(FLX_3710, f"{out} appeared while the fit ran; nothing was written")
        (scratch / "bundle").rename(out)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return CalibratedBundle(path=out, entries=tuple(added))


def _check(directory: Path, profile_document: Mapping[str, Any], moved: set[str]) -> None:
    """The bundle as ``validate`` and ``plan`` read it, before it is moved into place.

    It validates, resolves its emits, builds its register and reads that register's citations in the
    pinned profile, and each moved value has to be a ``calibrated`` number there.
    """
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits
    from flux_engine.stages.pipeline import parameter_register_of
    from flux_engine.stages.validate import validate

    result = validate(directory)
    if result.exit_code != 0:
        raise FluxError(
            FLX_3711,
            "the calibrated bundle does not validate: "
            + "; ".join(
                str(problem) for problem in (result.artifacts.get("problems") or [result.stderr])
            ),
        )
    loaded = load_bundle(directory)
    cast = resolve_cast(loaded.flux_documents)
    resolve_all_emits(loaded, simulation_id=cast.simulation_id)
    register = parameter_register_of(loaded, cast)
    if register is None:
        raise FluxError(FLX_3711, "the calibrated bundle has no parameter register")
    check_profile_citations(register, profile_document, where=cast.simulation_id)
    calibrated = {
        registered.number.pointer
        for registered in register.numbers
        if registered.source is Source.CALIBRATED
    }
    if not moved <= calibrated:
        raise FluxError(
            FLX_3711,
            "a moved value is not a calibrated number of the written register: "
            f"{sorted(printable(pointer) for pointer in moved - calibrated)}",
        )
