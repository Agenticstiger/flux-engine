# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A plan's execution, staged under its run directory, and committed by ``apply``.

A plan *is* an execution of the whole pipeline (:mod:`flux_engine.stages.plan`).
Before this module, ``apply --run-id`` ran it a second time to write the run
directory: ``plan`` then ``apply`` cost two generations of every row. ``plan`` now
writes what ``apply`` would write, under ``<run>/.stage/``, and ``apply`` checks
the stage and commits it, so the two cost one generation (ADR 0015).

**The key.** The run directory is named by the plan's digest
(:func:`flux_engine.stages.layout.run_id_for`), and the stage lives inside it, so
a stage can only be found by the plan it was made for. ``stage.json`` repeats the
plan digest, the input digest and the kernel, and ``apply`` refuses a stage that
names another plan, and one whose bundle bytes are not the ones the plan hashed.

**What is written.** Every file ``apply`` writes, by the very function ``apply``
writes them with (:func:`flux_engine.stages.apply.write_run`), over a staging
layout. ``stage.json`` is written last, so a plan killed part-way leaves no
manifest and ``apply`` generates as it did before. It lists every staged file
with its digest and size; the receipt, the run's commit marker, is last.

**What is trusted.** Nothing that is read back unchecked. Staged files are written
and read through the no-follow helpers of :mod:`flux_engine.safeio`; a path in the
manifest that is not a plain name under the run directory is refused; each file's
bytes are digested and compared with the manifest before they are written to the
run directory, and the bytes written are the bytes that were checked. Each emit
stream is also digested the way ``outputDigest`` is, and must equal the digest the
staged receipt reports, so a stream cannot be swapped without the receipt.

Staging is off for the plan of ``fluxsim run`` and for an ``apply`` that takes a
fresh plan, which hold the execution in memory and write it once.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.safeio import (
    read_bytes_nofollow,
    refuse_links_within,
    require_real_directory,
    write_bytes_atomic,
    write_text_atomic,
)
from flux_engine.seam.emit import output_digest
from flux_engine.stages.layout import RunLayout

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from flux_engine.stages.pipeline import PipelineResult

__all__ = [
    "FLX_2120",
    "STAGE_DIRNAME",
    "STAGE_VERSION",
    "StagedRun",
    "commit_stage",
    "discard_stage",
    "has_stage",
    "read_stage",
    "stage_execution",
]

FLX_2120: ErrorCode = register(
    "FLX-2120",
    "the staged execution of a plan does not match its record",
    "`fluxsim plan` stages the rows it generated under `.stage/` in the run directory, "
    "with a record (`stage.json`) of every staged file's digest, and `fluxsim apply` "
    "commits those files instead of generating them again. It does so only for a stage "
    "that names this plan and this bundle, whose every file is a plain file under the "
    "run directory whose bytes hash to the record, and whose streams hash to the "
    "`outputDigest` the staged receipt reports. A stage that fails any of these was "
    "edited, truncated or assembled from another run, and nothing of it is written to "
    "the run. The refusal names the file. Delete the `.stage` directory and apply again "
    "to generate the run from the bundle, or run `fluxsim plan` again.",
)

#: The staging directory inside a run directory.
STAGE_DIRNAME = ".stage"

#: The manifest's version; a reader refuses one it does not know.
STAGE_VERSION = "flux-stage/1"

_MANIFEST = "stage.json"
_RECEIPT = "receipt.json"
_MOST_FILES = 4096


def scenario_scale_note(source: str, scale: Any) -> str:
    """*source* as the summary line words it: a scenario emit says ``--scale`` does not size it.

    A table built from the run's own events has the rows the events make; ``--scale`` sizes the
    tables drawn from a profile and not these, and without the note a scaled run prints the same
    count as an unscaled one and looks as though the flag did nothing.
    """
    if source == "scenario" and scale is not None and scale != 1:
        return "scenario; --scale leaves a table built from events as it is"
    return source


@dataclass(frozen=True)
class StagedRun:
    """A stage that has been read and checked: the bytes to commit, and what to report."""

    files: Tuple[Tuple[str, bytes], ...]
    receipt: Mapping[str, Any]
    run_digest: str
    emits: Tuple[Mapping[str, Any], ...]

    def file(self, relative: str) -> Optional[bytes]:
        """The checked bytes of one staged file, or ``None`` when the stage holds none."""
        return dict(self.files).get(relative)

    def summary_line(self, scale: Any = None) -> str:
        """The line ``apply`` prints for the emits, as it prints it for a run it generated.

        *scale* is the plan's scale (``None`` for one); a table built from the run's events says
        that the scale leaves it as it is (:func:`scenario_scale_note`).
        """
        if not self.emits:
            return "no emits declared"
        parts = []
        for emit in self.emits:
            source = scenario_scale_note(str(emit["source"]), scale)
            if int(emit.get("processes", 1)) > 1:
                source = f"{source}, {emit['processes']} processes"
            parts.append(f"{emit['exposeId']}: {emit['rows']} rows ({source})")
        return "emitted " + "; ".join(parts)


def stage_root(layout: RunLayout) -> Path:
    """The staging directory of *layout*'s run."""
    return layout.run_dir / STAGE_DIRNAME


def stage_layout(layout: RunLayout) -> RunLayout:
    """The layout the staged files are written under: the run's own, one level down."""
    return RunLayout(state_dir=stage_root(layout), run_id=layout.run_id)


def has_stage(layout: RunLayout) -> bool:
    """Whether a staging directory exists in the run directory, whatever it holds."""
    root = stage_root(layout)
    return root.is_symlink() or root.exists()


def discard_stage(layout: RunLayout) -> None:
    """Remove the staging directory; a link at its name is removed, not followed."""
    root = stage_root(layout)
    if root.is_symlink():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)


def _digest(data: bytes) -> str:
    return digest_of_bytes(data, subject="stagedFile").value


def _listed(staged: RunLayout) -> List[Tuple[str, Path]]:
    """Every file the staging layout holds, as (run-relative path, path), the receipt last."""
    run_dir = staged.run_dir
    found: List[Tuple[str, Path]] = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_symlink() or path.is_dir():
            continue
        found.append((path.relative_to(run_dir).as_posix(), path))
    found.sort(key=lambda item: (item[0] == _RECEIPT, item[0]))
    return found


def stage_execution(
    layout: RunLayout,
    directory: Path,
    plan_document: Mapping[str, Any],
    result: "PipelineResult",
    provenance_text: str,
    register_text: Optional[str],
    *,
    simulation_id: Optional[str],
    zone: Optional[str],
) -> Path:
    """Write what ``apply`` would write under the run's stage, then the manifest. Returns its path."""
    from flux_engine.stages.apply import write_run

    discard_stage(layout)
    staged = stage_layout(layout)
    staged.ensure()
    run_digest_doc, _ = write_run(
        staged, directory, plan_document, result, provenance_text, register_text
    )
    files: List[Dict[str, Any]] = []
    for relative, path in _listed(staged):
        data = read_bytes_nofollow(path)
        files.append({"path": relative, "sha256": _digest(data), "bytes": len(data)})
    manifest = {
        "version": STAGE_VERSION,
        "runId": layout.run_id,
        "planDigest": plan_document["planDigest"],
        "inputDigest": plan_document["inputDigest"],
        "kernelVersion": plan_document["kernelVersion"],
        "simulationId": simulation_id,
        "zone": zone,
        "runDigest": run_digest_doc.ref.value,
        "emits": [
            {
                "exposeId": outcome.resolved.expose_id,
                "rows": outcome.result.rows,
                "source": outcome.source,
                "processes": outcome.processes,
                "header": dict(outcome.result.header),
                "outputDigest": outcome.result.output_digest.value,
            }
            for outcome in result.emits
        ],
        "files": files,
    }
    return write_text_atomic(
        stage_root(layout) / _MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


#: What every refusal of a stage tells the caller to do, inline, so the message stands alone.
_REMEDY = (
    "delete the run's .stage directory and run `fluxsim apply` again, or run `fluxsim plan` again"
)


def _refuse(path: object, why: str) -> FluxError:
    return FluxError(FLX_2120, f"{path}: {why}; {_REMEDY}")


def _safe_relative(text: object) -> str:
    """A manifest path that names one plain file of a run directory, or the refusal."""
    if not isinstance(text, str) or not text or "\\" in text or "\x00" in text:
        raise _refuse(_MANIFEST, f"a staged path is not a name: {text!r}")
    parts = PurePosixPath(text).parts
    if (
        PurePosixPath(text).is_absolute()
        or not 1 <= len(parts) <= 2
        or any(part in ("", ".", "..") or part.startswith(".") for part in parts)
        or (len(parts) == 2 and parts[0] != "emits")
    ):
        raise _refuse(_MANIFEST, f"a staged path leaves the run directory: {text!r}")
    return "/".join(parts)


def read_stage(
    layout: RunLayout,
    plan_document: Mapping[str, Any],
    *,
    simulation_id: Optional[str] = None,
    zone: Optional[str] = None,
) -> Optional[StagedRun]:
    """The stage of *layout*'s run, checked, or ``None`` when there is none to use.

    *simulation_id* and *zone* are what the apply names (``None`` when it names none); a stage
    made with other values is not used.

    ``None`` means ``apply`` generates the run as it did before staging: no
    manifest (a plan made with ``--no-stage``, or killed before it finished), a
    manifest that names another run, plan, input or kernel than this plan's (the
    key does not fit, so nothing of it is read), or a stage made for another
    ``--simulation-id`` or ``--zone`` than this call names. A stage that names this
    plan and is wrong in what it holds is ``FLX-2120``, never ``None``.
    """
    root = stage_root(layout)
    if not has_stage(layout):
        return None
    require_real_directory(root)
    refuse_links_within(root)
    manifest_path = root / _MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        manifest = json.loads(read_bytes_nofollow(manifest_path))
    except (OSError, ValueError) as error:
        raise _refuse(manifest_path, "the record cannot be read") from error
    if not isinstance(manifest, dict) or manifest.get("version") != STAGE_VERSION:
        raise _refuse(manifest_path, "the record is not a flux-stage/1 document")
    for key in ("runId", "planDigest", "inputDigest", "kernelVersion"):
        expected = layout.run_id if key == "runId" else plan_document.get(key)
        if manifest.get(key) != expected:
            return None  # a stage of another plan is not this run's: nothing of it is used
    # The stage is used only when it was made with what this apply would generate with: the
    # simulation id and the zone as the call names them, absent meaning absent on both sides.
    if manifest.get("simulationId") != simulation_id or manifest.get("zone") != zone:
        return None
    listed = manifest.get("files")
    if not isinstance(listed, list) or not 0 < len(listed) <= _MOST_FILES:
        raise _refuse(manifest_path, "the record lists no files, or too many")
    staged = stage_layout(layout)
    run_dir = staged.run_dir
    require_real_directory(run_dir)
    files: List[Tuple[str, bytes]] = []
    seen = set()
    for entry in listed:
        if not isinstance(entry, dict):
            raise _refuse(manifest_path, "a staged file is not an entry")
        relative = _safe_relative(entry.get("path"))
        if relative in seen:
            raise _refuse(manifest_path, f"{relative} is listed twice")
        seen.add(relative)
        path = run_dir / relative
        try:
            data = read_bytes_nofollow(path)
        except OSError as error:  # missing, a link, a directory, a pipe
            raise _refuse(path, "the staged file is missing or is not a plain file") from error
        if len(data) != entry.get("bytes") or _digest(data) != entry.get("sha256"):
            raise _refuse(path, "the staged bytes are not the bytes recorded")
        files.append((relative, data))
    if files[-1][0] != _RECEIPT:
        raise _refuse(manifest_path, "the receipt is not the last staged file")
    by_path = dict(files)
    receipt = _read_receipt(by_path[_RECEIPT], layout, plan_document, manifest_path)
    emits = _checked_emits(manifest, receipt, by_path, manifest_path)
    run_digest = str(receipt.get("runDigest"))
    if manifest.get("runDigest") != run_digest:
        raise _refuse(manifest_path, "the receipt and the record name different run digests")
    return StagedRun(files=tuple(files), receipt=receipt, run_digest=run_digest, emits=emits)


def _read_receipt(
    data: bytes, layout: RunLayout, plan_document: Mapping[str, Any], manifest_path: Path
) -> Mapping[str, Any]:
    try:
        receipt = json.loads(data)
    except ValueError as error:
        raise _refuse(manifest_path, "the staged receipt is not JSON") from error
    if (
        not isinstance(receipt, dict)
        or receipt.get("runId") != layout.run_id
        or receipt.get("planDigest") != plan_document.get("planDigest")
        or receipt.get("inputDigest") != plan_document.get("inputDigest")
    ):
        raise _refuse(manifest_path, "the staged receipt is not the receipt of this plan")
    return receipt


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_summary_fields(emit: Mapping[str, Any], manifest_path: Path) -> None:
    """The fields of a recorded emit that ``apply`` prints: refused here, before anything is written.

    :meth:`StagedRun.summary_line` reads ``source``, ``rows`` and ``processes`` of each entry
    once the stage is committed, so a manifest whose entry holds another kind of value would stop
    ``apply`` after the run had been written.
    """
    expose_id = emit["exposeId"]
    if not isinstance(emit.get("source"), str):
        raise _refuse(manifest_path, f"the record of {expose_id!r} names no source")
    if not _whole(emit.get("rows")):
        raise _refuse(manifest_path, f"the record of {expose_id!r} has no whole row count")
    processes = emit.get("processes", 1)
    if not _whole(processes) or processes < 1:
        raise _refuse(manifest_path, f"the record of {expose_id!r} has no whole process count")


def _checked_emits(
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
    by_path: Mapping[str, bytes],
    manifest_path: Path,
) -> Tuple[Mapping[str, Any], ...]:
    """Every staged stream digested as ``outputDigest`` is, against what the receipt reports."""
    recorded = manifest.get("emits")
    reported = receipt.get("emits")
    if not isinstance(recorded, list) or not isinstance(reported, dict):
        raise _refuse(manifest_path, "the record carries no emits")
    if sorted(str(e.get("exposeId")) for e in recorded if isinstance(e, dict)) != sorted(reported):
        raise _refuse(manifest_path, "the record and the receipt name different emits")
    if not all(isinstance(e, dict) and isinstance(e.get("exposeId"), str) for e in recorded):
        raise _refuse(manifest_path, "an emit of the record is not an entry with an exposeId")
    for emit in recorded:
        expose_id = str(emit["exposeId"])
        _require_summary_fields(emit, manifest_path)
        stream = by_path.get(_safe_relative(f"emits/{expose_id}.ndjson"))
        header = emit.get("header")
        if stream is None or not isinstance(header, dict):
            raise _refuse(manifest_path, f"the stream of {expose_id!r} is not staged")
        digest = output_digest(header, [stream], subject=f"outputDigest:{expose_id}").value
        claimed = reported[expose_id].get("outputDigest")
        if digest != claimed or digest != emit.get("outputDigest"):
            raise _refuse(
                f"emits/{expose_id}.ndjson",
                "the stream does not hash to the receipt's outputDigest",
            )
        if stream.count(b"\n") != reported[expose_id].get("rows"):
            raise _refuse(f"emits/{expose_id}.ndjson", "the stream's rows are not the receipt's")
    return tuple(recorded)


def commit_stage(layout: RunLayout, staged: StagedRun) -> None:
    """Write the checked bytes of *staged* into the run directory, the receipt last.

    The bytes written are the bytes :func:`read_stage` digested; the stage is not
    read again. The stage is removed once the receipt is down.
    """
    layout.ensure()
    for relative, data in staged.files:
        write_bytes_atomic(layout.run_dir / relative, data)
    discard_stage(layout)
