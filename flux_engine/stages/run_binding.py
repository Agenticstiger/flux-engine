# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a committed run directory has to hold before ``verify`` or ``ship`` reads it.

Two checks, made after the plan has been read and bound to the directory:

* **The receipt agrees with the plan.** ``receipt.json`` names a simulation and the
  emits of the run, and so does ``plan.json``. ``apply`` writes the receipt from the
  plan, so a receipt that names another simulation id, or another set of emit ids, was
  edited or assembled from another run (``FLX-2122``). ``verify`` and ``ship`` build
  file names from the receipt's ids, so this is checked before either does.
* **The directory holds only files the run wrote.** ``ship`` packs the run directory
  whole, so a file the engine never writes there is carried into the pack as if the
  run had produced it (``FLX-2123``).
"""

from __future__ import annotations

import os
import re
from typing import AbstractSet, Any, List, Mapping, Optional, Tuple, Union

from flux_engine.core.errors import FluxError
from flux_engine.seam.coverage import printable
from flux_engine.stages.errors import FLX_2122, FLX_2123
from flux_engine.stages.layout import COMPONENT_LEDGER, RunLayout

__all__ = ["require_receipt_binds_plan", "require_run_files_named"]

#: The longest stretch of an id or a file name a message repeats.
_SHOWN = 80

#: The directory ``plan`` stages an execution in; ``ship`` leaves it out of a pack.
_STAGE_DIRNAME = ".stage"

#: Files directly in a run directory that carry no expose id: what ``plan``, ``apply``,
#: ``ship``, ``run --sink`` and ``calibrate --write`` each write. The ledgers are the
#: ones :data:`~flux_engine.stages.layout.COMPONENT_LEDGER` names.
_FIXED_FILES = frozenset(
    {
        "plan.json",
        "seed-paths.ndjson",
        "decisions.ndjson",
        "decisions.meta.ndjson",
        "suppressions.ndjson",
        "lineage.ndjson",
        "run-digest.json",
        "receipt.json",
        "column-provenance.json",
        "parameter-register.json",
        "sink-receipts.json",
        "scorecard.json",
        "forecast-accuracy.json",
        "EVIDENCE.md",
        "calibration-provenance.json",
        *COMPONENT_LEDGER.values(),
    }
)

#: The share record ``apply --shard I/N`` writes.
_SHARD_MANIFEST = re.compile(r"shard-[0-9]+-of-[0-9]+\.json")
#: Per-emit files directly in a run directory, and the expose id each one names.
_PER_EMIT = re.compile(r"(?:seam-attestation|fidelity-record|fidelity-input)-(?P<id>.+)\.json")
#: An emit stream, or one share of it, in ``emits/``.
_STREAM = re.compile(r"(?P<id>.+?)(?:\.shard-[0-9]+-of-[0-9]+)?\.ndjson")


def _shown(value: object) -> str:
    text = printable(str(value))
    return text if len(text) <= _SHOWN else text[:_SHOWN] + "..."


def _names(ids: AbstractSet[str]) -> str:
    return ", ".join(_shown(name) for name in sorted(ids))


def require_receipt_binds_plan(
    layout: RunLayout, plan_document: Mapping[str, Any], receipt: Mapping[str, Any]
) -> None:
    """Refuse (``FLX-2122``) a ``receipt.json`` that names another simulation or other emits than ``plan.json``."""
    recorded = receipt.get("simulationId")
    planned = plan_document.get("simulationId")
    if recorded != planned:
        raise FluxError(
            FLX_2122,
            f"receipt.json in {layout.run_dir} records simulationId {_shown(recorded)}, "
            f"but the plan there names {_shown(planned)}",
        )
    receipt_emits = receipt.get("emits")
    plan_emits = plan_document.get("emits")
    held = set(receipt_emits) if isinstance(receipt_emits, Mapping) else set()
    # A hidden table is in the plan and in no stream of the run (ADR 0024 section 3).
    declared = (
        {
            name
            for name, entry in plan_emits.items()
            if not (isinstance(entry, Mapping) and entry.get("hidden") is True)
        }
        if isinstance(plan_emits, Mapping)
        else set()
    )
    if held != declared:
        parts: List[str] = []
        if declared - held:
            parts.append(f"lacks the plan's emits {_names(declared - held)}")
        if held - declared:
            parts.append(f"names emits the plan does not declare ({_names(held - declared)})")
        raise FluxError(
            FLX_2122,
            f"receipt.json in {layout.run_dir} differs from the plan there in its emits: "
            f"it {' and '.join(parts)}",
        )


def _is_leftover(name: str) -> bool:
    """A ``.<name>-XXXX.tmp`` file a killed atomic write left, or a file manager's ``.DS_Store``.

    ``ship`` never packs either one.
    """
    return (name.startswith(".") and name.endswith(".tmp")) or name == ".DS_Store"


def _name_is_known(name: str, exposes: AbstractSet[str]) -> bool:
    if name in _FIXED_FILES or _SHARD_MANIFEST.fullmatch(name) or _is_leftover(name):
        return True
    per_emit = _PER_EMIT.fullmatch(name)
    return per_emit is not None and per_emit.group("id") in exposes


def _stream_is_known(name: str, exposes: AbstractSet[str]) -> bool:
    if _is_leftover(name):
        return True
    stream = _STREAM.fullmatch(name)
    return stream is not None and stream.group("id") in exposes


def _entries(path: Union[str, "os.PathLike[str]"]) -> List[Tuple[str, bool]]:
    """The names directly in *path*, each with whether it is a real directory; ``[]`` when unreadable."""
    try:
        with os.scandir(path) as scan:
            return sorted((entry.name, entry.is_dir(follow_symlinks=False)) for entry in scan)
    except OSError:
        return []


def _first_stray(layout: RunLayout, exposes: AbstractSet[str]) -> Optional[str]:
    for name, is_dir in _entries(layout.run_dir):
        if not is_dir:
            if not _name_is_known(name, exposes):
                return name
        elif name == "emits":
            for inner, inner_dir in _entries(layout.emits_dir):
                if inner_dir:
                    return f"emits/{inner}/"
                if not _stream_is_known(inner, exposes):
                    return f"emits/{inner}"
        elif name != _STAGE_DIRNAME:
            return f"{name}/"
    return None


def require_run_files_named(layout: RunLayout, receipt: Mapping[str, Any]) -> None:
    """Refuse (``FLX-2123``) a run directory that holds a file the run does not account for.

    ``ship`` copies the run directory whole (apart from the staging directory), so every
    file in it is either one the engine wrote, or is packed as if it had been. The names
    are the ones ``RunLayout`` and the writers that use it produce; a per-emit file counts
    only for an emit the receipt names. Nothing is read: the names alone are compared.
    """
    receipt_emits = receipt.get("emits")
    exposes = frozenset(receipt_emits) if isinstance(receipt_emits, Mapping) else frozenset()
    stray = _first_stray(layout, exposes)
    if stray is not None:
        folder = stray[:-1]
        if stray.endswith("/") and "/" not in folder and _name_is_known(folder, exposes):
            raise FluxError(
                FLX_2123,
                f"{layout.run_dir} holds {_shown(stray)}, a directory where the run writes a "
                "file; nothing was packed or verified from this run; remove the directory, "
                "or move it out of the run directory",
            )
        raise FluxError(
            FLX_2123,
            f"{layout.run_dir} holds {_shown(stray)}, which the run's receipt does not name "
            "and `apply` did not write; nothing was packed or verified from this run; remove "
            "the file, or move it out of the run directory",
        )
