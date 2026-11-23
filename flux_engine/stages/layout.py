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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple, Union

from flux_engine.api.accuracy import IDENTIFIER_PATTERN
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.safeio import read_text_nofollow, refuse_links_within, require_real_directory
from flux_engine.safeio import write_text_atomic as atomic_write_text
from flux_engine.seam.coverage import printable

__all__ = [
    "COMPONENT_LEDGER",
    "DEFAULT_STATE_DIRNAME",
    "EVENTS_LEDGER_COMPONENTS",
    "FLX_2112",
    "FLX_2113",
    "POPULATION_LEDGER_COMPONENTS",
    "RUN_ID_PATTERN",
    "RunLayout",
    "RUN_FILE_REMEDY",
    "atomic_write_text",
    "read_json_object",
    "require_identifier",
    "require_run_id",
    "run_id_for",
]

FLX_2112: ErrorCode = register(
    "FLX-2112",
    "a run id, simulation id or expose id is not a safe identifier",
    "A run id is not a name a caller chooses: it is the first 16 hex "
    "characters of the plan's own planDigest, so the only well-formed value "
    "is 16 lowercase hex digits ([0-9a-f]{16}). Anything else -- a path, a "
    "word, an uppercase spelling, a longer or shorter string -- names no run "
    "this engine could have written, and `<state_dir>/runs/<run id>` is a "
    "path component, so the check runs before the join. `fluxsim plan` prints "
    "the value; pass that to --run-id. The same code refuses a run "
    "directory whose receipt names a simulation id or an expose id that is "
    "not a FLUX identifier (a letter, digit or underscore at each end, dots "
    "and hyphens only inside): `verify` and `ship` read both ids back from "
    "the receipt, and each becomes part of a file name under the run "
    "directory or the golden directory, so an id holding a separator, a "
    "leading dot or nothing at all is refused before any path is built. A "
    "run `apply` wrote never holds one, because the bundle's schemas pin "
    "both ids to the identifier grammar; one that does was edited after "
    "`apply`, so run `fluxsim plan` and `fluxsim apply` again.",
)

FLX_2113: ErrorCode = register(
    "FLX-2113",
    "the run directory does not belong to the plan it holds",
    "A run directory is named by the run id derived from its plan's own "
    "planDigest, so the directory name and the plan inside it are two "
    "spellings of one fact, and `run-digest.json` records that planDigest "
    "as a third: the runtime statement `ship` writes names the run digest. "
    "When they disagree, the directory was copied, renamed or assembled from "
    "another run's artifacts, and every later binding check would be "
    "comparing files that all came from the same copy. Re-run `fluxsim plan` "
    "and apply into the directory it names. When only `run-digest.json` "
    "names another plan, `fluxsim apply` with this run id writes it again "
    "from the plan beside it.",
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
        raise FluxError(
            FLX_2112,
            f"--run-id is 16 lowercase hex characters, as `fluxsim plan` prints (not {value!r})",
        )
    return value


#: The longest stretch of a refused id a message repeats: the id is read from a file the
#: caller may have edited, so the message shows a short form of it and no more.
_SHOWN_ID_LIMIT = 40


def require_identifier(value: object, *, field: str) -> str:
    """Return *value* when it is a FLUX identifier, else raise ``FLX-2112`` naming *field*.

    A simulation id or an expose id becomes part of a file name under the run directory
    (``emits/<expose>.ndjson``, ``seam-attestation-<expose>.json``) or the golden directory
    (``golden/<simulation>.json``). The bundle's schemas pin both to the identifier grammar,
    so a run ``apply`` wrote never holds anything else; ``verify`` and ``ship`` read them back
    from ``receipt.json``, which anyone with the run directory can edit, so the grammar is
    checked again before the join. Reuses :data:`~flux_engine.api.accuracy.IDENTIFIER_PATTERN`,
    which holds no separator, no ``.`` or ``..``, no leading or trailing dot and no empty value.
    """
    if isinstance(value, str) and IDENTIFIER_PATTERN.fullmatch(value) is not None:
        return value
    text = value if isinstance(value, str) else f"a {type(value).__name__}"
    if len(text) > _SHOWN_ID_LIMIT:
        text = text[:_SHOWN_ID_LIMIT] + "..."
    shown = printable(text)
    if shown == text and isinstance(value, str):
        shown = f"'{text}'"
    raise FluxError(
        FLX_2112,
        f"{field} must be an identifier (letters, digits and underscores, with dots or hyphens "
        f"only between them), not {shown}; a run directory that names it was not written by "
        f"`fluxsim apply`: {RUN_FILE_REMEDY}",
    )


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
        return self.emits_dir / f"{require_identifier(expose_id, field='exposeId')}.ndjson"

    def shard_emit_path(self, expose_id: str, index: int, count: int) -> Path:
        """One share of an emit's stream (``apply --shard I/N``): rows the shares join into."""
        expose = require_identifier(expose_id, field="exposeId")
        return self.emits_dir / f"{expose}.shard-{index}-of-{count}.ndjson"

    def shard_manifest_path(self, index: int, count: int) -> Path:
        """What share ``I`` of ``N`` of a run holds, per emit (``apply --shard I/N``)."""
        return self.run_dir / f"shard-{index}-of-{count}.json"

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
        return (
            self.run_dir
            / f"seam-attestation-{require_identifier(expose_id, field='exposeId')}.json"
        )

    def fidelity_record_path(self, expose_id: str) -> Path:
        return (
            self.run_dir / f"fidelity-record-{require_identifier(expose_id, field='exposeId')}.json"
        )

    def fidelity_input_path(self, expose_id: str) -> Path:
        """Where the fidelity check's scoped, canonicalised input is recorded.

        Written once by ``apply`` (only for an emit that declares a
        ``fidelity`` block), and read back, unmodified, by both ``verify``'s
        fidelity leg and ``ship``'s gate -- the single source for a path three
        stages previously each spelled out by hand.
        """
        return (
            self.run_dir / f"fidelity-input-{require_identifier(expose_id, field='exposeId')}.json"
        )

    @property
    def run_digest_path(self) -> Path:
        return self.run_dir / "run-digest.json"

    @property
    def receipt_path(self) -> Path:
        return self.run_dir / "receipt.json"

    @property
    def column_provenance_path(self) -> Path:
        """``column-provenance.json``: how each column of each emit was produced.

        Written by ``apply`` for every run, read by ``ship`` and ``verify``
        only through the plan's ``columnProvenanceDigest`` (``FLX-2115``), and
        packed with the rest of the run directory. It is a report of how the
        columns were produced, not an input to the run: the plan holds a digest
        of it, and no component of the run digest does.
        """
        return self.run_dir / "column-provenance.json"

    @property
    def parameter_register_path(self) -> Path:
        """``parameter-register.json``: what each number the run reads rests on.

        Written by ``apply`` only for a Simulation that declares a parameter
        register, read by ``ship`` and ``verify`` only through the plan's
        ``parameterRegisterDigest`` (``FLX-2116``), and packed with the rest of the
        run directory. Like the column provenance record it is a report, not an
        input to the run: the plan holds a digest of it, and no component of the
        run digest does.
        """
        return self.run_dir / "parameter-register.json"

    @property
    def sink_receipts_path(self) -> Path:
        """Where ``fluxsim run --sink`` records what each sink did with each stream."""
        return self.run_dir / "sink-receipts.json"

    @property
    def golden_dir(self) -> Path:
        return self.state_dir / "golden"

    def golden_path(self, simulation_id: str) -> Path:
        return self.golden_dir / f"{require_identifier(simulation_id, field='simulationId')}.json"

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
        # The resolve above lets a linked directory through as long as it lands
        # under runs/. Each directory the engine writes under has to be a
        # directory itself (FLX-2118), and the run directory may not already
        # hold a link: every file written below goes to the name it was given.
        require_real_directory(self.runs_dir, create=True)
        require_real_directory(self.run_dir, create=True)
        require_real_directory(self.emits_dir, create=True)
        refuse_links_within(self.run_dir)
        return self

    def require_real_dirs(self) -> "RunLayout":
        """``FLX-2118`` when ``runs/``, the run directory or ``emits/`` is a link. Creates nothing.

        For the stages that write into a run directory ``apply`` made (``ship``,
        ``verify``) rather than make one, so they refuse a directory swapped for
        a link in between before they write a file under it.
        """
        for directory in (self.runs_dir, self.run_dir, self.emits_dir):
            require_real_directory(directory)
        return self

    def ensure_golden_dir(self) -> Path:
        """Create ``golden_dir``, refusing one that is a link or holds a link (``FLX-2118``)."""
        require_real_directory(self.golden_dir, create=True)
        refuse_links_within(self.golden_dir)
        return self.golden_dir


#: What a refusal of an unreadable ``receipt.json`` or ``run-digest.json`` tells the caller to do.
RUN_FILE_REMEDY = "delete the run directory and run `fluxsim plan` and `fluxsim apply` again"


def read_json_object(path: Path, *, code: ErrorCode, remedy: str) -> Dict[str, Any]:
    """The JSON object in a run file, or a refusal of *code* that names the file and the remedy.

    ``plan.json``, ``receipt.json`` and ``run-digest.json`` are each read back by a stage that
    expects the document it wrote. One that was truncated, is not UTF-8, is not JSON, or holds
    anything but an object is not that document: the refusal says which file, what is wrong
    with it and what to do, in place of the ``JSONDecodeError`` or ``AttributeError`` the first
    use of it would raise. A file that is a link or not a regular file stays ``FLX-2117``.
    """
    try:
        document = json.loads(read_text_nofollow(path))
    except (ValueError, RecursionError) as error:  # JSONDecodeError and UnicodeDecodeError
        problem = f"is not readable JSON ({type(error).__name__})"
    else:
        if isinstance(document, dict):
            return document
        problem = f"holds {type(document).__name__}, not a JSON object"
    raise FluxError(code, f"{path} {problem}; {remedy}")
