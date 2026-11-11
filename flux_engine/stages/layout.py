# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The run directory layout: ``<state_dir>/runs/<runId>/``.

One directory per run, named by the run's own :data:`RunLayout.run_id` --
derived from ``planDigest`` (see :func:`run_id_for`), never from a
timestamp or a counter, so re-planning identical documents lands in the
same directory instead of accumulating a new one every time. ``apply``
and ``run`` are therefore restart-safe: killed and re-run, they write
into the same directory and a partially-written emit stream is simply
overwritten from row zero, never appended to.

The default state directory is ``./.flux`` under the current working
directory -- the bundle directory itself is never written to (SEAM-
FIDELITY-SPEC's examples and fixtures are frequently read-only checkouts
of a sibling repo), mirroring forge-cli's own ``.fluid/`` convention.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Tuple, Union

from flux_engine.core.errors import ErrorCode, FluxError, register

__all__ = [
    "COMPONENT_LEDGER",
    "DEFAULT_STATE_DIRNAME",
    "EVENTS_LEDGER_COMPONENTS",
    "FLX_2112",
    "FLX_2113",
    "POPULATION_LEDGER_COMPONENTS",
    "RUN_ID_PATTERN",
    "RunLayout",
    "atomic_write_text",
    "require_run_id",
    "run_id_for",
]

FLX_2112: ErrorCode = register(
    "FLX-2112",
    "the run id is not a safe identifier",
    "A run id is not a name a caller chooses: it is the first 16 hex "
    "characters of the plan's own planDigest, so the only well-formed value "
    "is 16 lowercase hex digits ([0-9a-f]{16}). Anything else -- a path, a "
    "word, an uppercase spelling, a longer or shorter string -- names no run "
    "this engine could have written, and `<state_dir>/runs/<run id>` is a "
    "path component, so the check runs before the join. `fluxsim plan` prints "
    "the value; pass that to --run-id.",
)

FLX_2113: ErrorCode = register(
    "FLX-2113",
    "the run directory does not belong to the plan it holds",
    "A run directory is named by the run id derived from its plan's own "
    "planDigest, so the directory name and the plan inside it are two "
    "spellings of one fact. When they disagree, the directory was copied, "
    "renamed or assembled from another run's artifacts, and every later "
    "binding check would be comparing files that all came from the same "
    "copy. Re-run `fluxsim plan` and apply into the directory it names.",
)

#: The shape of every run id this engine writes or reads: the first 16 hex
#: characters of a ``planDigest`` (:func:`run_id_for`). Deliberately
#: narrower than the FLUX ``$defs/identifier`` grammar it used to be
#: checked against -- that grammar admits any dotted/hyphenated word, so
#: ``--run-id nothexatall`` walked past the only gate in front of the path
#: join and the refusal a reader expected ("that is not a run id") never
#: happened. Run ids are derived, never chosen, so the gate can be exactly
#: as narrow as the derivation.
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{16}")


def require_run_id(value: object) -> str:
    """Return *value* when it is a well-formed run id, else raise ``FLX-2112``.

    Called before any path is joined, not after: ``<state_dir>/runs/<run_id>``
    is a path component the caller supplies, and ``"../../.."`` interpolated
    into it walks straight out of the state directory. :meth:`RunLayout.ensure`
    keeps a containment assertion behind this as defence in depth.

    Path safety is now a consequence rather than the whole rule: 16
    lowercase hex digits cannot contain a separator, a dot or a leading
    space, and they also cannot be a plausible-looking id that no plan
    could have produced.
    """
    if not isinstance(value, str) or RUN_ID_PATTERN.fullmatch(value) is None:
        raise FluxError(FLX_2112, f"run_id={value!r}")
    return value


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* through a staged file and one ``os.replace``.

    The receipt is the run directory's commit marker -- ``ship`` and
    ``verify`` read its presence as "this run completed" -- so it must
    never exist in a half-written state. A plain ``write_text`` killed
    part-way leaves a truncated file that is present, parses as nothing,
    and reads to the next stage as a completed run whose JSON is corrupt.
    ``os.replace`` renames within one directory, so the marker either is
    not there or is the whole document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


#: Name of the state directory created under the CWD by default.
DEFAULT_STATE_DIRNAME = ".flux"

#: The components of KERNEL-SPEC §10 whose ops ``population.ndjson`` carries.
POPULATION_LEDGER_COMPONENTS: Tuple[str, ...] = ("population",)

#: The components whose ops ``events.ndjson`` carries -- every traced
#: component that is not the population, ``delivery``'s ``suppress`` ops
#: included. ``suppressions.ndjson`` keeps its own copy of those as the
#: refusal ledger a reader looks at; the copy a digest is taken over is
#: this one, because it is the only file that holds ``deliver`` and
#: ``suppress`` in the single emission order the ``delivery`` component
#: digest was computed in.
EVENTS_LEDGER_COMPONENTS: Tuple[str, ...] = ("events", "journeys", "delivery", "assignment")

#: Which ledger file under a run directory carries each component's ops --
#: the inverse of the two tuples above, and the map ``verify``/``ship``
#: read when they recompute a component digest from what is on disk.
COMPONENT_LEDGER: Mapping[str, str] = MappingProxyType(
    {
        **{name: "population.ndjson" for name in POPULATION_LEDGER_COMPONENTS},
        **{name: "events.ndjson" for name in EVENTS_LEDGER_COMPONENTS},
    }
)


def run_id_for(plan_digest: str) -> str:
    """Derive a run id from a ``planDigest`` value (``"sha256:<hex>"``).

    The first 16 hex characters are enough to make collisions practically
    impossible for the number of runs any one state directory will ever
    hold, and short enough to stay a usable directory name.

    Handed straight to :func:`require_run_id`, so the derivation and the
    gate in front of the path join read one grammar: a digest that cannot
    supply 16 hex digits is ``FLX-2112`` here rather than an invented name
    (``"unplanned"``) that every later check would then have to accept.
    """
    _, _, hexed = plan_digest.partition(":")
    digits = hexed or plan_digest
    return require_run_id(digits[:16])


@dataclass(frozen=True)
class RunLayout:
    """Every path inside one run directory."""

    state_dir: Path
    run_id: str

    def __post_init__(self) -> None:
        require_run_id(self.run_id)

    @classmethod
    def for_state_dir(cls, state_dir: Union[str, Path], run_id: str) -> "RunLayout":
        return cls(state_dir=Path(state_dir), run_id=require_run_id(run_id))

    @property
    def runs_dir(self) -> Path:
        return self.state_dir / "runs"

    @property
    def run_dir(self) -> Path:
        return self.runs_dir / self.run_id

    @property
    def plan_path(self) -> Path:
        return self.run_dir / "plan.json"

    @property
    def seed_paths_path(self) -> Path:
        """``seed-paths.ndjson`` -- every seed-tree path the plan anticipated.

        KERNEL-SPEC §3 keeps the path list beside the plan rather than inside it: one
        canonical path per line, sorted, and the file's raw-bytes digest is the
        ``seedTreeDigest`` ``plan.json`` carries. A 25 000-subject run addresses millions of
        streams, and a plan that inlined them would be tens of megabytes of addresses
        wrapped around a few dozen lines anyone actually reads.
        """
        return self.run_dir / "seed-paths.ndjson"

    @property
    def population_path(self) -> Path:
        return self.run_dir / "population.ndjson"

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.ndjson"

    @property
    def emits_dir(self) -> Path:
        return self.run_dir / "emits"

    def emit_path(self, expose_id: str) -> Path:
        return self.emits_dir / f"{expose_id}.ndjson"

    @property
    def decisions_path(self) -> Path:
        return self.run_dir / "decisions.ndjson"

    @property
    def decisions_meta_path(self) -> Path:
        return self.run_dir / "decisions.meta.ndjson"

    @property
    def suppressions_path(self) -> Path:
        return self.run_dir / "suppressions.ndjson"

    def ledger_path(self, component: str) -> Path:
        """The ledger file carrying *component*'s trace ops (:data:`COMPONENT_LEDGER`).

        Raises ``KeyError`` for a name outside KERNEL-SPEC §10's five trace
        components -- ``emits`` and ``decisions`` are digested from their
        own artifacts, not from a trace ledger, so asking this for either
        is a caller's mistake rather than a missing file.
        """
        return self.run_dir / COMPONENT_LEDGER[component]

    @property
    def lineage_path(self) -> Path:
        return self.run_dir / "lineage.ndjson"

    def seam_attestation_path(self, expose_id: str) -> Path:
        return self.run_dir / f"seam-attestation-{expose_id}.json"

    def fidelity_record_path(self, expose_id: str) -> Path:
        return self.run_dir / f"fidelity-record-{expose_id}.json"

    def fidelity_input_path(self, expose_id: str) -> Path:
        """Where the fidelity check's scoped, canonicalised input is recorded.

        Written once by ``apply`` (only for an emit that declares a
        ``fidelity`` block), and read back, unmodified, by both ``verify``'s
        fidelity leg and ``ship``'s gate -- the single source for a path three
        stages previously each spelled out by hand.
        """
        return self.run_dir / f"fidelity-input-{expose_id}.json"

    @property
    def run_digest_path(self) -> Path:
        return self.run_dir / "run-digest.json"

    @property
    def receipt_path(self) -> Path:
        return self.run_dir / "receipt.json"

    @property
    def sink_receipts_path(self) -> Path:
        """Where ``fluxsim run --sink`` records what each sink did with each stream."""
        return self.run_dir / "sink-receipts.json"

    @property
    def golden_dir(self) -> Path:
        return self.state_dir / "golden"

    def golden_path(self, simulation_id: str) -> Path:
        return self.golden_dir / f"{simulation_id}.json"

    @property
    def golden_changelog_path(self) -> Path:
        return self.golden_dir / "GOLDEN-CHANGELOG.md"

    def ensure(self) -> "RunLayout":
        """Create ``run_dir`` and ``emits_dir``. Idempotent.

        The containment assertion behind :func:`require_run_id`: whatever
        the id was spelled as, the directory this stage is about to create
        has to sit under ``<state_dir>/runs/``.
        """
        runs_dir = self.runs_dir.resolve(strict=False)
        run_dir = self.run_dir.resolve(strict=False)
        if run_dir != runs_dir and runs_dir not in run_dir.parents:
            raise FluxError(FLX_2112, f"run_id={self.run_id!r} resolves outside {runs_dir}")
        self.emits_dir.mkdir(parents=True, exist_ok=True)
        return self
