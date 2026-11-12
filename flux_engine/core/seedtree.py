# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The seed tree -- KERNEL-SPEC §3 (``flux-seed/1``).

Every stochastic value in a run comes from a stream addressed by a **path**, and every path
hangs off one root derived from the seam contract digest and the effective seed::

    effective_seed = Simulation.spec.seed if present else World.spec.seed   # both int64
    root_label     = b"flux-seed/1" + b"\\x00" + H_bytes + b"\\x00"
                     + effective_seed.to_bytes(8, "big", signed=True)
    root           = int.from_bytes(sha256(root_label).digest(), "big")

    label(path)    = b"\\x1f".join(NFC(str(part)).encode("utf-8") for part in path)
    stream(path)   = Generator(PCG64(SeedSequence(
                         int.from_bytes(sha256(b"flux-seed/1\\x00" + root_bytes + b"\\x00"
                                               + label).digest(), "big"))))

Two consequences the rest of the engine leans on:

* **Changing the contract changes every stream.** ``H`` is the seam's contract digest, so a
  run against an edited contract is a different run, not the same run with different
  columns.
* **Derivation is by path, never by spawn order.** A stream's values depend on its address
  and nothing else -- not on how many streams were created before it, not on the order the
  scheduler happened to reach it. This is what makes the "insertion stability" property
  true: adding an unreferenced document to the bundle, or a new consumer on a new path,
  changes no existing stream. A ``SeedSequence.spawn``-based tree would be reproducible
  only for a fixed traversal order, which is a weaker promise wearing the same word.

The canonical path table below is **closed**: adding a row is a ``kernelVersion`` bump.
Every path a run materialises is recorded in a :class:`SeedPathLedger` and written out as
``seed-paths.ndjson`` beside the plan -- one canonical path per line, sorted -- whose
raw-bytes digest is the ``seedTreeDigest`` that ``plan.json`` carries. The plan itself keeps
only that digest and a per-shape count table, so it stays small enough to read and to pin as
a golden while the addresses themselves stay verifiable line by line. A run that reaches a
path the plan did not anticipate fails with ``FLX-2201``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union

from flux_engine.core.canonical import canonical_bytes, nfc, sha256_hex
from flux_engine.core.errors import FluxError, register
from flux_engine.core.rng import DrawStream

__all__ = [
    "CANONICAL_PATH_SHAPES",
    "FLX_2201",
    "FLX_2202",
    "PathKind",
    "PathShape",
    "SEED_TREE_VERSION",
    "SeedPath",
    "SeedPathLedger",
    "SeedTree",
    "SeedTreeRoot",
    "agentic_path",
    "campaign_path",
    "derive_identifier",
    "derive_root",
    "effective_seed",
    "experiment_assignment_path",
    "journey_path",
    "market_path",
    "match_shape",
    "parse_seed_path",
    "parse_seed_paths",
    "path_label",
    "render_seed_path",
    "seam_bytes",
    "seed_paths_bytes",
    "seed_paths_digest",
    "seed_tree_digest",
    "shape_counts",
    "signal_path",
    "stream_entropy",
    "stream_entropy_of_label",
    "stream_of_label",
    "subject_products_path",
    "subject_trait_path",
    "sweep_effective_seed",
    "sweep_path",
    "temperature_path",
    "world_cohorts_path",
]

FLX_2201 = register(
    "FLX-2201",
    "run touched a seed-tree path the plan did not anticipate",
    "Every stream a run draws from is addressed by a path from the closed table in "
    "KERNEL-SPEC §3, and the set of paths the run will touch is recorded beside the "
    "plan in seed-paths.ndjson, whose digest plan.json carries as seedTreeDigest. A "
    "path outside that table, or outside the planned set, means "
    "the run is not the run that was planned -- so it stops rather than emitting rows "
    "whose provenance the plan cannot account for.",
)

FLX_2202 = register(
    "FLX-2202",
    "seed-tree input outside its declared domain",
    "The root of the seed tree is derived from the seam contract digest and an int64 "
    "effective seed (KERNEL-SPEC §3). A seed outside the int64 range, a malformed or "
    "non-sha256 digest reference, or an empty emit set has no defined derivation, and "
    "guessing one would silently produce a run nobody can reproduce.",
)

#: KERNEL-SPEC §1: the version of the derivation, carried in the receipt and plan.json.
SEED_TREE_VERSION = "flux-seed/1"

_DOMAIN = SEED_TREE_VERSION.encode("utf-8")
_NUL = b"\x00"

#: §3's path separator: ASCII UNIT SEPARATOR between the parts of one path. Chosen by the
#: spec because it cannot occur in a document id, a trait name or a decimal ordinal, so no
#: two distinct paths can share a label through a separator collision.
_PART_SEPARATOR = b"\x1f"

#: One rank up from the part separator (ASCII RECORD SEPARATOR). Refused inside a path part
#: for the same reason ``\x1f`` is: it is the separator a caller reaching for a flat
#: encoding of a whole path set would pick, so a part that carries one is a part that could
#: forge a record boundary in someone else's reading of the set.
_LABEL_SEPARATOR = b"\x1e"

_PART_SEPARATOR_TEXT = _PART_SEPARATOR.decode("ascii")
_LABEL_SEPARATOR_TEXT = _LABEL_SEPARATOR.decode("ascii")

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

_DIGEST_PREFIX = "sha256:"
_DIGEST_HEX_LENGTH = 64

#: One part of a seed-tree path. Ints appear as ordinals, occurrences, steps and run
#: indices; strings as the fixed segment names and document ids.
SeedPathPart = Union[str, int]

#: A seed-tree path: the address of one stream.
SeedPath = Tuple[SeedPathPart, ...]


class PathKind(Enum):
    """The kind of a variable segment in a canonical path shape."""

    #: A document id, trait name, dial name or skill name.
    NAME = "name"
    #: A non-negative integer: a subject ordinal, an occurrence, a step, a run index.
    ORDINAL = "ordinal"


@dataclass(frozen=True)
class PathShape:
    """One row of KERNEL-SPEC §3's canonical path table.

    ``pattern`` mixes literal segment names with :class:`PathKind` placeholders, so a path
    can be matched against the closed table without a regular expression over its label.
    ``rooted_at`` records §3's one exception: the experiment assignment stream hangs off
    ``Experiment.spec.assignment.seed``, **not** off the run's effective seed, so that an
    A/B assignment is stable across the runs of a sweep.
    """

    name: str
    consumer: str
    pattern: Tuple[Union[str, PathKind], ...]
    rooted_at: str = "root"


#: KERNEL-SPEC §3's canonical path table, transcribed row for row. **Closed**: adding a row
#: is a ``kernelVersion`` bump with a ``GOLDEN-CHANGELOG.md`` entry.
CANONICAL_PATH_SHAPES: Tuple[PathShape, ...] = (
    PathShape(
        name="world.cohorts",
        consumer="LRM tie-free, so this stream is reserved and currently unused",
        pattern=("world", PathKind.NAME, "cohorts"),
    ),
    PathShape(
        name="world.market",
        consumer="market assignment of subject <ordinal>",
        pattern=("world", PathKind.NAME, "market", PathKind.ORDINAL),
    ),
    PathShape(
        name="world.subject.trait",
        consumer="trait draw",
        pattern=("world", PathKind.NAME, "subject", PathKind.ORDINAL, "trait", PathKind.NAME),
    ),
    PathShape(
        name="world.subject.products",
        consumer="vertical-pack derived attributes",
        pattern=("world", PathKind.NAME, "subject", PathKind.ORDINAL, "products"),
    ),
    PathShape(
        name="sim.signal",
        consumer="event arrival and payload",
        pattern=(
            "sim",
            PathKind.NAME,
            "signal",
            PathKind.NAME,
            "subject",
            PathKind.ORDINAL,
            PathKind.ORDINAL,
        ),
    ),
    PathShape(
        name="sim.journey",
        consumer="reaction draw",
        pattern=(
            "sim",
            PathKind.NAME,
            "journey",
            PathKind.NAME,
            "subject",
            PathKind.ORDINAL,
            PathKind.ORDINAL,
        ),
    ),
    PathShape(
        name="sim.campaign",
        consumer="delivery-side randomness",
        pattern=("sim", PathKind.NAME, "campaign", PathKind.NAME, "subject", PathKind.ORDINAL),
    ),
    PathShape(
        name="sim.temperature",
        consumer="stochastic share of a dial",
        pattern=(
            "sim",
            PathKind.NAME,
            "temperature",
            PathKind.NAME,
            "subject",
            PathKind.ORDINAL,
            PathKind.ORDINAL,
        ),
    ),
    PathShape(
        name="experiment.assignment",
        consumer="variant assignment",
        pattern=("experiment", PathKind.NAME, "assignment"),
        rooted_at="Experiment.spec.assignment.seed",
    ),
    PathShape(
        name="sweep.run",
        consumer="stochastic sweeps: run k uses effective_seed = baseSeed + k",
        pattern=("sweep", PathKind.ORDINAL),
        rooted_at="baseSeed + run_index",
    ),
    PathShape(
        name="sim.agentic",
        consumer="v0.3 agentic skill call",
        pattern=(
            "sim",
            PathKind.NAME,
            "agentic",
            PathKind.NAME,
            "subject",
            PathKind.ORDINAL,
            PathKind.ORDINAL,
        ),
    ),
)


# ---------------------------------------------------------------------------
# Labels and roots
# ---------------------------------------------------------------------------


def path_label(path: Sequence[SeedPathPart]) -> bytes:
    """Return §3's label for *path*.

    ``b"\\x1f".join(NFC(str(part)).encode("utf-8") for part in path)``. The NFC pass is
    what makes the composed and decomposed spellings of a trait name address one stream
    rather than two.

    A part that itself contains one of the two separator characters is refused
    (``FLX-2202``) rather than escaped. §3 chose ``\\x1f`` because it cannot occur in a
    document id, a trait name or a decimal ordinal, and the whole of path addressing rests
    on the label being unique to the path: without this check
    ``("world", "a\\x1fb", "cohorts")`` and ``("world", "a", "b", "cohorts")`` would share
    a label, and therefore share a stream. Escaping instead would change the label bytes
    for every path and move every digest, so the assumption is enforced rather than worked
    around.
    """
    if not path:
        raise FluxError(FLX_2202, "a seed-tree path must have at least one part")
    parts = []
    for index, part in enumerate(path):
        text = nfc(str(part))
        if _PART_SEPARATOR_TEXT in text or _LABEL_SEPARATOR_TEXT in text:
            raise FluxError(
                FLX_2202,
                f"path part {index} contains a separator character (U+001F or U+001E), "
                "which would make its label ambiguous",
            )
        parts.append(text.encode("utf-8"))
    return _PART_SEPARATOR.join(parts)


def _sha256_bytes(data: bytes) -> bytes:
    """The raw 32 digest bytes, through the engine's one hashing primitive.

    KERNEL-SPEC §2 makes :mod:`flux_engine.core.canonical` the only module that touches
    ``hashlib``; §3's derivation needs the digest as bytes rather than as hex, and the
    round trip through the hex spelling is exact.
    """
    return bytes.fromhex(sha256_hex(data))


def digest_bytes(digest_ref: str) -> bytes:
    """Return the raw 32 bytes of a ``"sha256:<hex>"`` digest reference.

    Strict by design: a reference without the domain prefix, or with the wrong hex length,
    is refused rather than coerced. A digest is the one value in the engine whose spelling
    must not be guessed at.
    """
    if not digest_ref.startswith(_DIGEST_PREFIX):
        raise FluxError(FLX_2202, f"digest reference must start with {_DIGEST_PREFIX!r}")
    hex_part = digest_ref[len(_DIGEST_PREFIX) :]
    if len(hex_part) != _DIGEST_HEX_LENGTH:
        raise FluxError(
            FLX_2202,
            f"digest reference carries {len(hex_part)} hex characters, "
            f"expected {_DIGEST_HEX_LENGTH}",
        )
    try:
        return bytes.fromhex(hex_part)
    except ValueError as exc:
        raise FluxError(FLX_2202, "digest reference is not hexadecimal") from exc


def seam_bytes(emit_digests: Sequence[str]) -> bytes:
    """Return §3's ``H_bytes`` for a Simulation's ``emits[]``.

    §3: "``H`` is the contract digest of the seam ... When a Simulation has several
    ``emits[]``, ``H_bytes`` is the sha256 of the concatenation of the per-emit digests in
    ``emits[]`` order."

    Read literally, that gives two rules, and this function implements both: with one emit,
    ``H_bytes`` is that emit's own 32 digest bytes; with several, it is the sha256 of their
    concatenation in declaration order. The one-emit case is deliberately *not* folded
    through a second sha256 -- §3 introduces the folding only for the several-emit case, and
    the single-emit root is the one pinned by the vector file.
    """
    if not emit_digests:
        raise FluxError(FLX_2202, "a Simulation must declare at least one emit to derive H")
    raw = [digest_bytes(ref) for ref in emit_digests]
    if len(raw) == 1:
        return raw[0]
    return _sha256_bytes(b"".join(raw))


def effective_seed(simulation_seed: Optional[int], world_seed: Optional[int]) -> int:
    """§3's first line: the Simulation's seed if present, else the World's.

    Both are int64. A seed outside that range is refused (``FLX-2202``) rather than
    truncated, because ``to_bytes(8, signed=True)`` would otherwise raise deep inside the
    derivation with a message that names neither document.
    """
    seed = simulation_seed if simulation_seed is not None else world_seed
    if seed is None:
        raise FluxError(FLX_2202, "neither the Simulation nor the World declares a seed")
    return _checked_int64(seed, "effective seed")


def _checked_int64(seed: int, what: str) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise FluxError(FLX_2202, f"{what} must be an integer, got {type(seed).__name__}")
    if not _INT64_MIN <= seed <= _INT64_MAX:
        raise FluxError(FLX_2202, f"{what} {seed} is outside the int64 range")
    return seed


def sweep_effective_seed(base_seed: int, run_index: int) -> int:
    """§3's sweep rule: "run ``k`` uses ``effective_seed = baseSeed + k``".

    The sum is range-checked as an int64 in its own right, so a sweep that would walk off
    the end of the domain stops at the plan rather than at the 2^63rd run.
    """
    _checked_int64(base_seed, "sweep base seed")
    if run_index < 0:
        raise FluxError(FLX_2202, f"sweep run index must be >= 0, got {run_index}")
    return _checked_int64(base_seed + run_index, "sweep effective seed")


@dataclass(frozen=True)
class SeedTreeRoot:
    """The root of one run's seed tree (KERNEL-SPEC §3).

    Carries the seam bytes it was derived from as well as the derived digest, so that the
    two streams §3 roots elsewhere -- an experiment's assignment and a sweep's run -- can be
    re-rooted from the same contract with :meth:`reseed` rather than by re-deriving ``H``.
    """

    seam_bytes: bytes
    effective_seed: int
    digest: bytes

    @property
    def value(self) -> int:
        """The root as §3's integer: ``int.from_bytes(sha256(root_label).digest(), "big")``."""
        return int.from_bytes(self.digest, "big")

    @property
    def hex(self) -> str:
        """The root digest in lowercase hex."""
        return self.digest.hex()

    def reseed(self, seed: int) -> "SeedTreeRoot":
        """The root for the same contract under a different seed.

        Used for §3's two re-rooted streams: an ``Experiment``'s assignment seed, and a
        sweep run's ``baseSeed + k``.
        """
        return derive_root(self.seam_bytes, seed)


def derive_root(seam: bytes, seed: int) -> SeedTreeRoot:
    """Derive the root from ``H_bytes`` and the effective seed, exactly as §3 spells it."""
    if not seam:
        raise FluxError(FLX_2202, "H_bytes must not be empty")
    checked = _checked_int64(seed, "effective seed")
    root_label = _DOMAIN + _NUL + seam + _NUL + checked.to_bytes(8, "big", signed=True)
    return SeedTreeRoot(seam_bytes=seam, effective_seed=checked, digest=_sha256_bytes(root_label))


def stream_entropy(root: SeedTreeRoot, path: Sequence[SeedPathPart]) -> int:
    """The ``SeedSequence`` entropy for *path* under *root* (§3's second formula)."""
    return stream_entropy_of_label(root, path_label(path))


def stream_entropy_of_label(root: SeedTreeRoot, label: bytes) -> int:
    """:func:`stream_entropy` for a path whose :func:`path_label` the caller already holds.

    For a caller that needs the label anyway (to key a stream table, or to build it from
    parts it checked once), so the label is not built a second time. *label* must be the
    label :func:`path_label` returns; this function takes it on trust.
    """
    material = _DOMAIN + _NUL + root.digest + _NUL + label
    return int.from_bytes(_sha256_bytes(material), "big")


def stream_of_label(root: SeedTreeRoot, label: bytes) -> DrawStream:
    """The stream §3 derives for the path whose :func:`path_label` is *label*, at its first word."""
    return DrawStream.from_entropy(
        stream_entropy_of_label(root, label), label=label.decode("utf-8", "replace")
    )


def derive_identifier(root: SeedTreeRoot, path: Sequence[SeedPathPart], *, length: int = 32) -> str:
    """A stable identifier for *path*, in lowercase hex.

    The sanctioned replacement for ``uuid4`` anywhere the engine needs an id: an event id,
    a subject id, the identifier columns SEAM-FIDELITY-SPEC §1 requires to be "derived
    deterministically from ``(H, ordinal)`` -- never random". Two runs of the same plan
    produce the same identifiers, which is what lets a golden compare them at all.
    """
    if not 1 <= length <= 64:
        raise FluxError(FLX_2202, f"identifier length must be in [1, 64], got {length}")
    material = _DOMAIN + _NUL + b"id" + _NUL + root.digest + _NUL + path_label(path)
    return sha256_hex(material)[:length]


# ---------------------------------------------------------------------------
# The canonical path table
# ---------------------------------------------------------------------------


def _is_ordinal(part: SeedPathPart) -> bool:
    return isinstance(part, int) and not isinstance(part, bool) and part >= 0


def match_shape(path: Sequence[SeedPathPart]) -> Optional[PathShape]:
    """Return the canonical shape *path* matches, or ``None`` if it matches none."""
    for shape in CANONICAL_PATH_SHAPES:
        if len(shape.pattern) != len(path):
            continue
        for expected, part in zip(shape.pattern, path, strict=True):
            if isinstance(expected, str):
                if not isinstance(part, str) or nfc(part) != expected:
                    break
            elif expected is PathKind.ORDINAL:
                if not _is_ordinal(part):
                    break
            elif not isinstance(part, str) or not part:
                break
        else:
            return shape
    return None


def _require_name(value: str, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise FluxError(FLX_2202, f"{what} must be a non-empty string")
    return value


def _require_ordinal(value: int, what: str) -> int:
    if not _is_ordinal(value):
        raise FluxError(FLX_2202, f"{what} must be a non-negative integer, got {value!r}")
    return value


def world_cohorts_path(world_id: str) -> SeedPath:
    """``("world", world_id, "cohorts")`` -- reserved; the LRM cohort split is tie-free."""
    return ("world", _require_name(world_id, "world id"), "cohorts")


def market_path(world_id: str, ordinal: int) -> SeedPath:
    """``("world", world_id, "market", ordinal)`` -- market assignment of one subject."""
    return (
        "world",
        _require_name(world_id, "world id"),
        "market",
        _require_ordinal(ordinal, "subject ordinal"),
    )


def subject_trait_path(world_id: str, ordinal: int, trait_name: str) -> SeedPath:
    """``("world", world_id, "subject", ordinal, "trait", trait_name)`` -- one trait draw."""
    return (
        "world",
        _require_name(world_id, "world id"),
        "subject",
        _require_ordinal(ordinal, "subject ordinal"),
        "trait",
        _require_name(trait_name, "trait name"),
    )


def subject_products_path(world_id: str, ordinal: int) -> SeedPath:
    """``("world", world_id, "subject", ordinal, "products")`` -- vertical-pack attributes."""
    return (
        "world",
        _require_name(world_id, "world id"),
        "subject",
        _require_ordinal(ordinal, "subject ordinal"),
        "products",
    )


def signal_path(sim_id: str, signal_id: str, ordinal: int, occurrence: int) -> SeedPath:
    """``("sim", sim_id, "signal", signal_id, "subject", ordinal, occurrence)``."""
    return (
        "sim",
        _require_name(sim_id, "simulation id"),
        "signal",
        _require_name(signal_id, "signal id"),
        "subject",
        _require_ordinal(ordinal, "subject ordinal"),
        _require_ordinal(occurrence, "occurrence"),
    )


def journey_path(sim_id: str, journey_id: str, ordinal: int, step: int) -> SeedPath:
    """``("sim", sim_id, "journey", journey_id, "subject", ordinal, step)``."""
    return (
        "sim",
        _require_name(sim_id, "simulation id"),
        "journey",
        _require_name(journey_id, "journey id"),
        "subject",
        _require_ordinal(ordinal, "subject ordinal"),
        _require_ordinal(step, "journey step"),
    )


def campaign_path(sim_id: str, campaign_id: str, ordinal: int) -> SeedPath:
    """``("sim", sim_id, "campaign", campaign_id, "subject", ordinal)``."""
    return (
        "sim",
        _require_name(sim_id, "simulation id"),
        "campaign",
        _require_name(campaign_id, "campaign id"),
        "subject",
        _require_ordinal(ordinal, "subject ordinal"),
    )


def temperature_path(sim_id: str, dial: str, ordinal: int, occurrence: int) -> SeedPath:
    """``("sim", sim_id, "temperature", dial, "subject", ordinal, occurrence)``.

    Dial names are open (§6), so *dial* is validated as a name and not against a closed set.
    """
    return (
        "sim",
        _require_name(sim_id, "simulation id"),
        "temperature",
        _require_name(dial, "temperature dial"),
        "subject",
        _require_ordinal(ordinal, "subject ordinal"),
        _require_ordinal(occurrence, "occurrence"),
    )


def experiment_assignment_path(experiment_id: str) -> SeedPath:
    """``("experiment", experiment_id, "assignment")``.

    Rooted at ``Experiment.spec.assignment.seed``, not at the run's root: build its stream
    from ``root.reseed(assignment_seed)`` so that variant assignment is stable across the
    runs of a sweep.
    """
    return ("experiment", _require_name(experiment_id, "experiment id"), "assignment")


def sweep_path(run_index: int) -> SeedPath:
    """``("sweep", run_index)`` -- rooted at ``baseSeed + run_index``."""
    return ("sweep", _require_ordinal(run_index, "sweep run index"))


def agentic_path(sim_id: str, skill_name: str, ordinal: int, call_ordinal: int) -> SeedPath:
    """``("sim", sim_id, "agentic", skill_name, "subject", ordinal, call_ordinal)`` (v0.3)."""
    return (
        "sim",
        _require_name(sim_id, "simulation id"),
        "agentic",
        _require_name(skill_name, "skill name"),
        "subject",
        _require_ordinal(ordinal, "subject ordinal"),
        _require_ordinal(call_ordinal, "call ordinal"),
    )


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def render_seed_path(path: Sequence[SeedPathPart]) -> bytes:
    """Return the one ``seed-paths.ndjson`` line for *path*, without its newline.

    The line is the canonical JSON (RFC 8785, KERNEL-SPEC §2.1) of the path as an array,
    so ``("world", "w", "market", 0)`` becomes ``["world","w","market",0]``: the same
    canonicaliser every other hashed document in the engine goes through, which is what
    makes the file's bytes reproducible rather than a spelling this module invented.

    The path is put through :func:`path_label` first, so a part carrying a separator
    character is refused here exactly as it is when a stream is derived -- the file records
    stream addresses, and an address that cannot be labelled is not one.
    """
    normalised: SeedPath = tuple(path)
    path_label(normalised)
    return canonical_bytes(list(normalised))


def parse_seed_path(line: Union[str, bytes]) -> SeedPath:
    """Return the path one ``seed-paths.ndjson`` line records.

    The inverse of :func:`render_seed_path`, and strict in the same way: a line that is not
    a JSON array of strings and non-negative integers is refused (``FLX-2202``) rather than
    coerced, because a mis-parsed path silently widens the planned set that ``FLX-2201``
    checks against.
    """
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    try:
        decoded = json.loads(text)
    except ValueError as exc:
        raise FluxError(FLX_2202, f"seed-path line is not JSON: {text!r}") from exc
    if not isinstance(decoded, list) or not decoded:
        raise FluxError(FLX_2202, "a seed-path line must be a non-empty JSON array")
    parts: List[SeedPathPart] = []
    for index, part in enumerate(decoded):
        if isinstance(part, bool) or not isinstance(part, (str, int)):
            raise FluxError(
                FLX_2202,
                f"seed-path part {index} is {type(part).__name__}, expected a string or an "
                "integer",
            )
        parts.append(part)
    return tuple(parts)


def seed_paths_bytes(paths: Iterable[Sequence[SeedPathPart]]) -> bytes:
    """Return the exact bytes of ``seed-paths.ndjson`` for *paths* (KERNEL-SPEC §3).

    One canonical path per line, sorted, newline-terminated -- and therefore a pure
    function of the *set* of paths: the order they were materialised in, and how many times
    each was reached, cannot reach the bytes. Sorting is over the rendered lines rather than
    over §3's labels, so the file a reader has in front of them is the file that is sorted
    (``sort -c`` passes on it) and the digest below is a plain ``sha256sum`` of it.

    Two paths that share a label -- which §3's ``str(part)`` rule allows in principle,
    ``("w", 1)`` and ``("w", "1")`` addressing one stream -- contribute **one** line, the
    lexicographically first, because the file records one line per stream.
    """
    by_label: Dict[bytes, bytes] = {}
    for path in paths:
        normalised: SeedPath = tuple(path)
        label = path_label(normalised)
        line = canonical_bytes(list(normalised))
        previous = by_label.get(label)
        if previous is None or line < previous:
            by_label[label] = line
    return b"".join(line + b"\n" for line in sorted(by_label.values()))


def parse_seed_paths(data: bytes) -> Tuple[SeedPath, ...]:
    """Return every path recorded in the bytes of a ``seed-paths.ndjson``.

    Blank lines are skipped; every other line must parse (:func:`parse_seed_path`). The
    result is in file order, which for a file this module wrote is sorted order.
    """
    return tuple(
        parse_seed_path(line) for line in data.decode("utf-8").splitlines() if line.strip()
    )


def seed_paths_digest(data: bytes) -> str:
    """§3's ``seedTreeDigest``: the ``raw-bytes`` digest of ``seed-paths.ndjson``.

    A raw-bytes digest (KERNEL-SPEC §2.3) rather than a digest over a structure the engine
    holds in memory: the file beside the plan is the artifact, and anyone -- a reviewer, a
    CI step, ``sha256sum`` -- can recompute this number from it without running the engine.
    """
    return f"{_DIGEST_PREFIX}{sha256_hex(data)}"


def seed_tree_digest(paths: Iterable[Sequence[SeedPathPart]]) -> str:
    """§3's ``seedTreeDigest`` over the set of paths a run materialised.

    "Every path materialised during a run is recorded (sorted) in ``seed-paths.ndjson``
    beside the plan (one canonical path per line; raw-bytes digest = ``seedTreeDigest``)."
    So this is :func:`seed_paths_digest` of :func:`seed_paths_bytes` -- the digest of the
    file, computed without needing the file on disk.
    """
    return seed_paths_digest(seed_paths_bytes(paths))


def shape_counts(paths: Iterable[Sequence[SeedPathPart]]) -> Dict[str, int]:
    """Count *paths* per canonical shape, keyed by :attr:`PathShape.name`.

    The table ``plan.json`` carries in place of the path list: ``{"world.subject.trait":
    75000, ...}``, in sorted-key order. It is the part of the path set a human reads a plan
    for -- how many streams of each kind this run will touch -- while the addresses
    themselves stay in the NDJSON beside it.

    A path matching no row of the closed table is refused (``FLX-2201``) rather than
    counted under some catch-all: the counts are a summary of the plan's seed tree, and a
    summary that quietly absorbs an unanticipated shape would defeat the check the plan
    exists to make.
    """
    counts: Dict[str, int] = {}
    for path in paths:
        normalised: SeedPath = tuple(path)
        shape = match_shape(normalised)
        if shape is None:
            raise FluxError(
                FLX_2201,
                f"path {normalised!r} matches no row of the canonical path table "
                "(KERNEL-SPEC §3)",
            )
        counts[shape.name] = counts.get(shape.name, 0) + 1
    return {name: counts[name] for name in sorted(counts)}


@dataclass
class SeedPathLedger:
    """Every path a run has materialised, recorded once, in canonical order.

    Append-safe in the two senses the plan needs. :meth:`record` is idempotent -- reaching
    one path a second time adds nothing -- so a caller may record as it goes without
    tracking what it has already seen. And the rendered file is a pure function of the set,
    so a ``seed-paths.ndjson`` that was half-written when a run was killed is rewritten
    whole on the next attempt rather than appended to (the same restart-safety
    ``fluxsim apply`` gives every other artifact it writes).

    The sorted view, the file bytes and the digest are memoised and invalidated on
    :meth:`record`, because a plan asks for the digest several times over a set that can
    hold millions of paths, and re-sorting it once per question was measurable.
    """

    _paths: Dict[bytes, SeedPath] = field(default_factory=dict, repr=False)
    _sorted: Optional[Tuple[SeedPath, ...]] = field(default=None, repr=False)
    _bytes: Optional[bytes] = field(default=None, repr=False)

    def record(self, path: Sequence[SeedPathPart]) -> bytes:
        """Record *path* and return its label. Idempotent."""
        normalised: SeedPath = tuple(path)
        label = path_label(normalised)
        if label not in self._paths:
            self._paths[label] = normalised
            self._sorted = None
            self._bytes = None
        return label

    def __contains__(self, label: object) -> bool:
        return label in self._paths

    def __len__(self) -> int:
        return len(self._paths)

    @property
    def paths(self) -> Tuple[SeedPath, ...]:
        """Every recorded path, in ``seed-paths.ndjson`` order."""
        if self._sorted is None:
            self._sorted = tuple(
                sorted(self._paths.values(), key=lambda path: canonical_bytes(list(path)))
            )
        return self._sorted

    def ndjson_bytes(self) -> bytes:
        """The exact bytes of this ledger's ``seed-paths.ndjson``."""
        if self._bytes is None:
            self._bytes = seed_paths_bytes(self._paths.values())
        return self._bytes

    def digest(self) -> str:
        """``seedTreeDigest``: the raw-bytes digest of :meth:`ndjson_bytes`."""
        return seed_paths_digest(self.ndjson_bytes())

    def shape_counts(self) -> Dict[str, int]:
        """:func:`shape_counts` over the recorded paths."""
        return shape_counts(self.paths)


@dataclass
class SeedTree:
    """The path-addressed stream table for one run (KERNEL-SPEC §3).

    ``planned_paths``, when given, is the set the plan anticipated -- the labels of the
    paths in the run's ``seed-paths.ndjson`` -- and a request for any other path raises
    ``FLX-2201`` rather than deriving a stream the plan cannot account for. Leave it
    ``None`` while planning, when the point of the pass is to discover the set.

    Streams are memoised by label, so two requests for one path continue **one** stream
    rather than restarting it -- a caller that asks for the same path twice gets the second
    draw, not the first one again.
    """

    root: SeedTreeRoot
    planned_paths: Optional[FrozenSet[bytes]] = None
    _streams: Dict[bytes, DrawStream] = field(default_factory=dict, repr=False)
    _ledger: SeedPathLedger = field(default_factory=SeedPathLedger, repr=False)

    @classmethod
    def for_plan(
        cls, root: SeedTreeRoot, planned_paths: Iterable[Sequence[SeedPathPart]]
    ) -> "SeedTree":
        """Build a tree restricted to the paths a plan recorded.

        *planned_paths* is what :func:`parse_seed_paths` read back out of the run's
        ``seed-paths.ndjson``; the set is held as labels, so the membership test costs one
        hash per stream rather than a scan of the file.
        """
        return cls(root=root, planned_paths=frozenset(path_label(p) for p in planned_paths))

    @property
    def ledger(self) -> SeedPathLedger:
        """The paths this tree has materialised."""
        return self._ledger

    @property
    def materialised_paths(self) -> Tuple[SeedPath, ...]:
        """Every path this tree has derived a stream for, in ``seed-paths.ndjson`` order."""
        return self._ledger.paths

    def seed_paths_bytes(self) -> bytes:
        """The bytes of the ``seed-paths.ndjson`` this run's paths make up."""
        return self._ledger.ndjson_bytes()

    def digest(self) -> str:
        """``seedTreeDigest`` over the paths materialised so far."""
        return self._ledger.digest()

    def shape_counts(self) -> Dict[str, int]:
        """The per-shape count table ``plan.json`` carries."""
        return self._ledger.shape_counts()

    def entropy(self, path: Sequence[SeedPathPart]) -> int:
        """The ``SeedSequence`` entropy for *path*, without materialising its stream."""
        return stream_entropy(self.root, path)

    def stream(self, path: Sequence[SeedPathPart]) -> DrawStream:
        """Return the stream addressed by *path*, deriving it on first request.

        Refuses a path outside the closed canonical table, and a path outside the planned
        set when one was given -- both ``FLX-2201``.
        """
        normalised: SeedPath = tuple(path)
        shape = match_shape(normalised)
        if shape is None:
            raise FluxError(
                FLX_2201,
                f"path {normalised!r} matches no row of the canonical path table "
                "(KERNEL-SPEC §3)",
            )
        label = path_label(normalised)
        if self.planned_paths is not None and label not in self.planned_paths:
            raise FluxError(
                FLX_2201,
                f"path {normalised!r} ({shape.name}) is not in the planned path set",
            )
        existing = self._streams.get(label)
        if existing is not None:
            return existing
        created = self._new_stream(normalised, label)
        self._streams[label] = created
        self._ledger.record(normalised)
        return created

    def _new_stream(self, path: SeedPath, label: bytes) -> DrawStream:
        """Derive one stream. The single seam a test subclass overrides to break the tree."""
        return stream_of_label(self.root, label)
