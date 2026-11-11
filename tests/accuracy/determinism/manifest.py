# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Build a determinism manifest: digests of what the engine computes under a given seed.

Run as ``python -m tests.accuracy.determinism.manifest --seed <int> --out <path>``.

The manifest is a canonical JSON document (sorted keys, no floats: only digests and
integers) with two parts:

* ``digests`` -- sha256 digests over the draws of every kernel sampler, the rows of a
  fidelity generator, the seed tree's entropies and one small end-to-end run. Every seed is
  derived from ``--seed`` and a label, so a nightly manifest reproduces from its seed. Two
  machines that disagree about any value disagree about a digest.
* ``environment`` -- python, numpy and platform, for the reader. The comparison
  (:mod:`tests.accuracy.determinism.compare`) ignores it.

Standard library, the engine and numpy only: the determinism cells install ``.[dev]`` plus
one pinned numpy and nothing else, so this module must not import scipy or any other
member of the ``accuracy`` dependency group.
"""

from __future__ import annotations

import argparse
import contextlib
import decimal
import hashlib
import io
import json
import platform
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy

from flux_engine.core.rng import (
    DrawStream,
    bernoulli,
    bounded_uint,
    categorical,
    exponential,
    normal_cdf,
    normal_ppf,
    permutation,
    poisson,
    truncated_normal,
    uniform53,
)
from flux_engine.core.seedtree import (
    agentic_path,
    campaign_path,
    derive_identifier,
    derive_root,
    experiment_assignment_path,
    journey_path,
    market_path,
    signal_path,
    stream_entropy,
    subject_products_path,
    subject_trait_path,
    sweep_path,
    temperature_path,
    world_cohorts_path,
)

__all__ = [
    "MANIFEST_SCHEMA",
    "REPO_ROOT",
    "SMALL_BUNDLE",
    "apply_ambient_decimal_context",
    "build_manifest",
    "canonical_json",
    "small_fixture_plan",
    "derive_seed",
    "main",
]

MANIFEST_SCHEMA = "flux-accuracy-determinism/1"
REPO_ROOT = Path(__file__).resolve().parents[3]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
SIMULATION_ID = "sim-payment-recovery"
EXPOSE_ID = "payment_recovery_moment"

#: Draws per kernel sampler case. ``truncated_normal`` evaluates the normal CDF and its
#: inverse in decimal arithmetic (about 0.2 ms a draw), so its cases draw fewer.
DRAWS = 4000
SLOW_DRAWS = 1000
#: Rows per generator digest, and the number of fresh roots they are drawn under.
GENERATOR_ROWS = 400
GENERATOR_ROOTS = 3
#: Streams whose first raw words are digested.
RAW_STREAMS = 500

D = Decimal


def derive_seed(base_seed: int, *labels: object) -> int:
    """A non-negative 63-bit seed that is a pure function of *base_seed* and *labels*."""
    material = "\x1f".join([str(base_seed), *(str(label) for label in labels)])
    digest = hashlib.sha256(b"flux-accuracy/determinism\x00" + material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (1 << 63)


def canonical_json(document: Any) -> str:
    """The manifest's one spelling: sorted keys, no spaces, ASCII, a trailing newline."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


# ---------------------------------------------------------------------------
# Rendering values to text, so a digest never depends on a repr that may change
# ---------------------------------------------------------------------------


def render(value: Any) -> str:
    """A type-tagged, platform-independent spelling of one engine value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool:" + ("1" if value else "0")
    if isinstance(value, int):
        return f"int:{value}"
    if isinstance(value, Decimal):
        return "dec:" + format(value, "f")
    if isinstance(value, str):
        return "str:" + value
    if isinstance(value, datetime):
        return "ts:" + value.isoformat()
    if isinstance(value, date):
        return "date:" + value.isoformat()
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(render(item) for item in value) + "]"
    if isinstance(value, float):  # the engine promises none; fail loudly rather than hash one
        raise TypeError(f"a float reached the manifest: {value!r}")
    raise TypeError(f"no rendering for {type(value).__name__}: {value!r}")


def digest_of(items: Iterable[str]) -> str:
    """sha256 over *items*, one per line."""
    hasher = hashlib.sha256()
    for item in items:
        hasher.update(item.encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Kernel samplers
# ---------------------------------------------------------------------------

Sampler = Callable[[DrawStream], Any]


def _kernel_cases(divisor: int = 1) -> List[Tuple[str, int, Sampler]]:
    draws, slow_draws = DRAWS // divisor, SLOW_DRAWS // divisor
    levels = {"a": D("0.5"), "b": D("0.3"), "c": D("0.2")}
    return [
        ("uniform53", draws, uniform53),
        ("bernoulli.p0.3", draws, lambda s: bernoulli(s, D("0.3"))),
        ("bernoulli.p0.999", draws, lambda s: bernoulli(s, D("0.999"))),
        ("categorical.3", draws, lambda s: categorical(s, levels)),
        ("bounded_uint.7", draws, lambda s: bounded_uint(s, 7)),
        ("bounded_uint.1000003", draws, lambda s: bounded_uint(s, 1_000_003)),
        ("bounded_uint.2^62+1", draws, lambda s: bounded_uint(s, (1 << 62) + 1)),
        ("permutation.8", draws // 4, lambda s: permutation(s, 8)),
        (
            "truncated_normal.centre",
            slow_draws,
            lambda s: truncated_normal(s, mean=D(0), stddev=D(1), lo=D(-3), hi=D(3)),
        ),
        (
            "truncated_normal.mirrored",
            slow_draws,
            lambda s: truncated_normal(s, mean=D(0), stddev=D(1), lo=D(2), hi=D(6), scale=8),
        ),
        (
            "truncated_normal.far_tail",
            slow_draws,
            lambda s: truncated_normal(s, mean=D(0), stddev=D(1), lo=D(7), hi=D(9), scale=10),
        ),
        (
            "truncated_normal.wide",
            slow_draws,
            lambda s: truncated_normal(s, mean=D(50), stddev=D(15), lo=D(1), hi=D(10000), scale=2),
        ),
        ("exponential.rate2", draws, lambda s: exponential(s, rate=D(2))),
        ("exponential.rate0.01.scale0", draws, lambda s: exponential(s, rate=D("0.01"), scale=0)),
        ("poisson.mean3", draws, lambda s: poisson(s, mean=D(3))),
        ("poisson.mean40", draws, lambda s: poisson(s, mean=D(40))),
    ]


def _sibling_stream(root: Any, label: str, row: int) -> DrawStream:
    path = ("sim", "determinism", "gen", label, "row", row, "value")
    return DrawStream.from_entropy(stream_entropy(root, path))


def _root(base_seed: int, label: str) -> Any:
    seam = hashlib.sha256(b"flux-accuracy/determinism-root\x00" + label.encode("utf-8")).digest()
    return derive_root(seam, derive_seed(base_seed, "root", label))


def kernel_digests(base_seed: int, divisor: int = 1) -> Dict[str, str]:
    """One digest per sampler case: *n* draws, each on its own path-derived stream."""
    digests: Dict[str, str] = {}
    for name, count, sampler in _kernel_cases(divisor):
        root = _root(base_seed, name)
        draws = (render(sampler(_sibling_stream(root, name, row))) for row in range(count))
        digests[f"kernel.{name}"] = digest_of(draws)
    # The raw words themselves, before any sampler transform.
    root = _root(base_seed, "raw")
    words = (
        render(_sibling_stream(root, "raw", row).next_raw() & ((1 << 64) - 1))
        for row in range(RAW_STREAMS // divisor)
    )
    digests["kernel.raw_first_word"] = digest_of(words)
    # One long stream: PCG64's own sequence, many words deep.
    long_stream = DrawStream.from_entropy(stream_entropy(root, ("sim", "determinism", "long")))
    digests["kernel.raw_long_stream"] = digest_of(
        render(long_stream.next_raw()) for _ in range(20_000 // divisor)
    )
    return digests


def numeric_digests() -> Dict[str, str]:
    """The decimal normal CDF and its inverse on a fixed grid."""
    # Built by scaling integers, never by Decimal arithmetic: that would run in the ambient
    # context, and the manifest must not depend on it even in its own inputs.
    grid = [D(n * 125).scaleb(-3) for n in range(-72, 73)]
    probabilities = [D(n * 5).scaleb(-3) for n in range(1, 200)] + [
        D("1E-12"),
        D("1E-6"),
        D("0.999999"),
        D("0.999999999999"),
    ]
    return {
        "numerics.normal_cdf": digest_of(render(normal_cdf(x)) for x in grid),
        "numerics.normal_ppf": digest_of(render(normal_ppf(p)) for p in probabilities),
    }


# ---------------------------------------------------------------------------
# The seed tree
# ---------------------------------------------------------------------------


def seed_tree_digests(base_seed: int) -> Dict[str, str]:
    """Entropies of one path per canonical shape, under several roots, plus identifiers."""
    digests: Dict[str, str] = {}
    for index in range(3):
        root = _root(base_seed, f"tree-{index}")
        paths = [
            world_cohorts_path("w1"),
            sweep_path(0),
            sweep_path(17),
            experiment_assignment_path("exp-1"),
        ]
        for ordinal in (0, 1, 7, 12345, 2**31):
            paths.append(market_path("w1", ordinal))
            paths.append(subject_trait_path("w1", ordinal, "tenure"))
            paths.append(subject_products_path("w1", ordinal))
            paths.append(signal_path("sim-1", "sig-1", ordinal, 3))
            paths.append(journey_path("sim-1", "jr-1", ordinal, 2))
            paths.append(campaign_path("sim-1", "cam-1", ordinal))
            paths.append(temperature_path("sim-1", "warmth", ordinal, 1))
            paths.append(agentic_path("sim-1", "skill-1", ordinal, 4))
        paths.append(("world", "wé", "market", 1))  # a non-ASCII id, NFC-normalised
        entropies = (render(stream_entropy(root, path)) for path in paths)
        digests[f"seedtree.entropy.root{index}"] = digest_of(entropies)
        digests[f"seedtree.identifier.root{index}"] = digest_of(
            derive_identifier(root, path, length=length)
            for path in paths[:12]
            for length in (16, 32)
        )
        digests[f"seedtree.root.root{index}"] = hashlib.sha256(root.digest).hexdigest()
    return digests


# ---------------------------------------------------------------------------
# Generator rows
# ---------------------------------------------------------------------------


def small_fixture_plan(rows: int) -> Tuple[Any, bytes]:
    """The generator plan for the small fixture's fidelity emit, and its seam bytes (``H_bytes``).

    Shared with the benchmarks, so that what is timed is what is digested here.
    """
    from flux_engine.generator.rows import build_plan
    from flux_engine.seam.emit import seam_digests, seam_entropy_bytes
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits

    loaded = load_bundle(SMALL_BUNDLE)
    resolved = resolve_all_emits(loaded, simulation_id=SIMULATION_ID)[0]
    expose = resolved.compile()
    digests = seam_digests(
        resolved.contract_document, resolved.emit, expose.column_names, resolved.extension
    )
    simulation = loaded.flux_documents[SIMULATION_ID]
    plan = build_plan(
        expose,
        sim_id=SIMULATION_ID,
        seam_digest=digests.h.value,
        rows=rows,
        fidelity=resolved.fidelity,
        time_bounds=simulation.get("spec", {}).get("timeBounds"),
    )
    return plan, seam_entropy_bytes([digests])


def generator_digests(base_seed: int, divisor: int = 1) -> Dict[str, str]:
    """Rows of the small fixture's fidelity emit, under several fresh roots."""
    from flux_engine.generator.rows import generate_rows

    plan, seam = small_fixture_plan(GENERATOR_ROWS // divisor)
    out: Dict[str, str] = {"generator.plan": plan.digest().value.split(":", 1)[1]}
    for index in range(GENERATOR_ROOTS):
        root = derive_root(seam, derive_seed(base_seed, "generator-root", index))
        rows = generate_rows(plan, root=root)
        out[f"generator.rows.root{index}"] = digest_of(render(row) for row in rows)
    return out


# ---------------------------------------------------------------------------
# One small end-to-end run, at its committed seed
# ---------------------------------------------------------------------------


def _run_cli(argv: Sequence[str]) -> Dict[str, Any]:
    from flux_engine.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        code = main(["-j", *argv])
    envelope = json.loads(buffer.getvalue())
    if code != 0 or not envelope.get("ok"):
        raise RuntimeError(f"flux {' '.join(argv)} failed: {envelope.get('error')}")
    return envelope


def run_digests(bundle: Path = SMALL_BUNDLE) -> Dict[str, str]:
    """``plan`` then ``apply`` of the bundle at its committed seed, in a temporary state dir.

    The seed is the one the Simulation commits (``spec.seed``): it is part of the pinned
    digest set, not a fresh seed, so this digest is the same every night.
    """
    with tempfile.TemporaryDirectory(prefix="flux-determinism-") as scratch:
        state = Path(scratch) / "state"
        planned = _run_cli(["plan", str(bundle), "--state-dir", str(state)])
        artifacts = planned["artifacts"]
        run_id = str(artifacts.get("run_id") or artifacts["plan_digest"].split(":", 1)[1][:16])
        applied = _run_cli(["apply", str(bundle), "--state-dir", str(state), "--run-id", run_id])
        run_dir = Path(applied["artifacts"]["run_dir"])
        receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    out = {"run.digest": str(receipt["runDigest"]).split(":", 1)[1]}
    for name, item in sorted(receipt["emits"].items()):
        out[f"run.emit.{name}"] = str(item["outputDigest"]).split(":", 1)[1]
    return out


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def environment() -> Dict[str, str]:
    """What this manifest was built on. Informational: the comparison ignores it."""
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "numpy": numpy.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "system": platform.system(),
    }


def build_manifest(seed: int, *, include_run: bool = True, quick: bool = False) -> Dict[str, Any]:
    """The manifest for *seed*.

    ``include_run=False`` leaves out the end-to-end run. ``quick=True`` draws an eighth as
    many values per case, for the pull-request tier's tests; a quick manifest is only
    comparable with another quick one, and says so in its ``profile``.
    """
    divisor = 8 if quick else 1
    digests: Dict[str, str] = {}
    digests.update(kernel_digests(seed, divisor))
    digests.update(numeric_digests())
    digests.update(seed_tree_digests(seed))
    digests.update(generator_digests(seed, divisor))
    if include_run:
        digests.update(run_digests())
    return {
        "schema": MANIFEST_SCHEMA,
        "profile": "quick" if quick else "full",
        "seed": seed,
        "digests": dict(sorted(digests.items())),
        "environment": environment(),
    }


def apply_ambient_decimal_context(spec: str) -> None:
    """Poison the ambient ``decimal`` context before the engine runs.

    *spec* is ``PREC:ROUNDING``, for example ``6:ROUND_UP``. A low precision and an
    unusual rounding are the settings most likely to move a value if any engine arithmetic
    leaks out of its own pinned context. Used by the determinism tests to show the
    manifest does not depend on the ambient context.
    """
    precision, _, rounding = spec.partition(":")
    context = decimal.getcontext()
    context.prec = int(precision)
    context.rounding = getattr(decimal, rounding or "ROUND_HALF_EVEN")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, required=True, help="the night's base seed")
    parser.add_argument("--out", type=Path, required=True, help="where to write the manifest")
    parser.add_argument(
        "--ambient-decimal",
        metavar="PREC:ROUNDING",
        help="set this ambient decimal context before the engine runs (determinism tests)",
    )
    parser.add_argument(
        "--skip-run", action="store_true", help="leave out the end-to-end run's digests"
    )
    parser.add_argument(
        "--quick", action="store_true", help="an eighth of the draws (pull-request tests only)"
    )
    args = parser.parse_args(argv)
    if args.ambient_decimal:
        apply_ambient_decimal_context(args.ambient_decimal)
    manifest = build_manifest(args.seed, include_run=not args.skip_run, quick=args.quick)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(canonical_json(manifest), encoding="utf-8")
    print(f"wrote {args.out} ({len(manifest['digests'])} digests, seed {args.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
