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

import itertools
import json
import os
import re
from pathlib import Path
from typing import AbstractSet, Any, List, Mapping, Optional, Tuple, Union

from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import seed_paths_digest
from flux_engine.core.sizing import share_of
from flux_engine.safeio import read_bytes_nofollow
from flux_engine.seam.coverage import printable
from flux_engine.stages.errors import FLX_2122, FLX_2123, SeedPathsMoved
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

#: The share record ``apply --shard I/N`` writes. The numbers are as long as ``--shard`` takes.
_SHARD_MANIFEST = re.compile(r"shard-(?P<index>[0-9]{1,9})-of-(?P<count>[0-9]{1,9})\.json")
#: Per-emit files directly in a run directory, and the expose id each one names.
_PER_EMIT = re.compile(r"(?:seam-attestation|fidelity-record|fidelity-input)-(?P<id>.+)\.json")
#: An emit stream, or one share of it, in ``emits/``.
_STREAM = re.compile(
    r"(?P<id>.+?)(?:\.shard-(?P<index>[0-9]{1,9})-of-(?P<count>[0-9]{1,9}))?\.ndjson"
)


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


def _share_problem(
    layout: RunLayout, receipt: Mapping[str, Any], plan_document: Mapping[str, Any]
) -> Optional[Tuple[str, str]]:
    """The first share file of the run that is not what ``apply --shard`` writes, with why.

    A share record is accepted when its numbers are a share (``1 <= I <= N``), it names this
    plan, and every digest and row range it records matches the share beside it. A share
    stream is accepted when its record is there and lists it, and its bytes are the rows
    :func:`~flux_engine.core.sizing.share_of` gives it of the whole stream, which the receipt
    binds. Both are read here, so a hand-made share cannot ride along into a pack.
    """
    receipt_emits = receipt.get("emits")
    emits: Mapping[str, Any] = receipt_emits if isinstance(receipt_emits, Mapping) else {}
    for name, is_dir in _entries(layout.run_dir):
        found = None if is_dir else _SHARD_MANIFEST.fullmatch(name)
        if found is not None:
            reason = _manifest_problem(
                layout, emits, plan_document, int(found["index"]), int(found["count"])
            )
            if reason is not None:
                return name, reason
    for name, is_dir in _entries(layout.emits_dir):
        stream = None if is_dir else _STREAM.fullmatch(name)
        if stream is None or stream["index"] is None or name[: -len(".ndjson")] in emits:
            continue
        reason = _stream_problem(
            layout, emits, stream["id"], int(stream["index"]), int(stream["count"])
        )
        if reason is not None:
            return f"emits/{name}", reason
    return None


def _expected_range(entry: Mapping[str, Any], index: int, count: int) -> Optional[Tuple[int, int]]:
    """The ``[start, stop)`` rows share ``index`` of ``count`` holds of an emit the receipt records."""
    rows = entry.get("rows")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        return None
    if "mapping" in entry:  # made from the run's own events: it belongs whole to the first share
        return (0, rows) if index == 1 else (0, 0)
    return share_of(rows, (index, count))


def _same_rows(whole: Path, share: Path, start: int, stop: int) -> bool:
    """Whether *share* is exactly lines ``[start, stop)`` of *whole*, byte for byte."""
    try:
        with whole.open("rb") as left, share.open("rb") as right:
            wanted = itertools.islice(left, start, stop)
            return all(a == b for a, b in itertools.zip_longest(wanted, right))
    except OSError:
        return False


def _manifest_problem(
    layout: RunLayout,
    emits: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    index: int,
    count: int,
) -> Optional[str]:
    if count < 1 or not 1 <= index <= count:
        return "is not a share: N is at least 1 and I is 1 to N"
    try:
        document = json.loads(layout.shard_manifest_path(index, count).read_bytes())
    except (OSError, ValueError):
        return "cannot be read as the JSON `apply --shard` writes"
    if not isinstance(document, Mapping):
        return "is not the JSON object `apply --shard` writes"
    if document.get("shard") != {"index": index, "of": count}:
        return "records another share than its name says"
    for key in ("planDigest", "inputDigest"):
        if document.get(key) != plan_document.get(key):
            return f"records another {key} than the plan of this run"
    entries = document.get("emits")
    if not isinstance(entries, list):
        return "lists no emits"
    for entry in entries:
        expose = entry.get("exposeId") if isinstance(entry, Mapping) else None
        if not isinstance(expose, str) or expose not in emits:
            return "lists an emit the receipt does not name"
        held = emits[expose]
        expected = _expected_range(held if isinstance(held, Mapping) else {}, index, count)
        if expected is None:
            return f"cannot be compared for {_shown(expose)}: the receipt records no row count"
        if (entry.get("firstRow"), entry.get("endRow")) != (expected[0], expected[1]) and not (
            "mapping" in held and (entry.get("firstRow"), entry.get("endRow")) == (0, expected[1])
        ):
            return f"records other rows for {_shown(expose)} than its share of the stream"
        share = layout.emits_dir / f"{expose}.shard-{index}-of-{count}.ndjson"
        if share.is_file():
            data = share.read_bytes()
            if (
                entry.get("bytes") != len(data)
                or entry.get("rows") != data.count(b"\n")
                or entry.get("sha256") != digest_of_bytes(data, subject="shardFile").value
            ):
                return f"records another size or digest for {_shown(expose)} than the share has"
    return None


def _stream_problem(
    layout: RunLayout, emits: Mapping[str, Any], expose: str, index: int, count: int
) -> Optional[str]:
    if count < 1 or not 1 <= index <= count:
        return "is not a share: N is at least 1 and I is 1 to N"
    if expose not in emits:
        return "is the share of an emit the receipt does not name"
    manifest = layout.shard_manifest_path(index, count)
    if not manifest.is_file():
        return f"has no {manifest.name} beside it"
    try:
        listed = json.loads(manifest.read_bytes()).get("emits", [])
    except (OSError, ValueError, AttributeError):
        return f"has a {manifest.name} that cannot be read"
    if not any(isinstance(item, Mapping) and item.get("exposeId") == expose for item in listed):
        return f"is not listed in {manifest.name}"
    held = emits[expose]
    expected = _expected_range(held if isinstance(held, Mapping) else {}, index, count)
    if expected is None:
        return "cannot be compared: the receipt records no row count for the emit"
    share = layout.emits_dir / f"{expose}.shard-{index}-of-{count}.ndjson"
    whole = layout.emits_dir / f"{expose}.ndjson"
    if not _same_rows(whole, share, *expected):
        return "is not the share of the run's stream that its number says"
    return None


def _require_seed_paths_as_planned(layout: RunLayout, plan_document: Mapping[str, Any]) -> None:
    """Refuse (``FLX-2108``) a ``seed-paths.ndjson`` whose bytes are not the ones the plan digests."""
    recorded = plan_document.get("seedTreeDigest")
    if not isinstance(recorded, str):
        return
    try:
        data = read_bytes_nofollow(layout.seed_paths_path)
    except OSError:
        return  # not there: nothing was added
    recomputed = seed_paths_digest(data)
    if recomputed != recorded:
        raise SeedPathsMoved(path=layout.seed_paths_path, recorded=recorded, recomputed=recomputed)


def require_run_files_named(
    layout: RunLayout, receipt: Mapping[str, Any], plan_document: Mapping[str, Any]
) -> None:
    """Refuse (``FLX-2123``) a run directory that holds a file the run does not account for.

    ``ship`` copies the run directory whole (apart from the staging directory), so every
    file in it is either one the engine wrote, or is packed as if it had been. The names
    are the ones ``RunLayout`` and the writers that use it produce; a per-emit file counts
    only for an emit the receipt names. A share (``apply --shard``) is also read: its record
    and its stream have to agree with the plan and with the whole stream the receipt binds.
    """
    receipt_emits = receipt.get("emits")
    exposes = frozenset(receipt_emits) if isinstance(receipt_emits, Mapping) else frozenset()
    stray = _first_stray(layout, exposes)
    if stray is None:
        _require_seed_paths_as_planned(layout, plan_document)
        bad = _share_problem(layout, receipt, plan_document)
        if bad is not None:
            raise FluxError(
                FLX_2123,
                f"{layout.run_dir} holds {_shown(bad[0])}, which {bad[1]}; nothing was packed or "
                "verified from this run; remove the file, or move it out of the run directory",
            )
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
