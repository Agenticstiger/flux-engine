#!/usr/bin/env python3

# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Generate THIRD_PARTY_LICENSES.md.

Uses `pip-licenses` when it's importable (``pip install pip-licenses``);
falls back to a documented manual list otherwise, so this script never
hard-requires a tool outside [dev].

The manual list is *derived*, not transcribed: :func:`declared_requirements`
reads ``pyproject.toml``'s ``[project.dependencies]`` and
``[project.optional-dependencies]`` and the table is built from that, so
the two cannot drift. What this file holds per distribution is only what
pyproject does not carry -- its licence and the one line saying why it is
here. A dependency with no entry in :data:`LICENSE_NOTES` is a hard error
(:func:`missing_entries`, gated by ``tests/test_third_party_licenses.py``)
rather than a row that quietly goes missing: ``NOTICE`` points at the
generated file as the authoritative inventory, and ``cryptography`` sat
outside it for exactly as long as the list was hand-maintained.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
OUTPUT = REPO_ROOT / "THIRD_PARTY_LICENSES.md"

#: Per distribution: (display name, SPDX licence expression, why it is here).
#: Keyed by the PEP 503 normalized name so "PyYAML"/"pyyaml" and
#: "import-linter"/"import_linter" are one entry, not two.
LICENSE_NOTES: Dict[str, Tuple[str, str, str]] = {
    "numpy": ("numpy", "BSD-3-Clause", "core: seed-tree bit source"),
    "pydantic": ("pydantic", "MIT", "core: typed models/Protocols"),
    "pyyaml": ("PyYAML", "MIT", "core: FLUX/FLUID document parsing"),
    "jsonschema": ("jsonschema", "MIT", "core: FLUX/FLUID schema validation"),
    "rfc8785": ("rfc8785", "MIT", "core: JSON Canonicalization Scheme"),
    "cloudevents": ("cloudevents", "Apache-2.0", "core: event envelope"),
    "cryptography": (
        "cryptography",
        "Apache-2.0 OR BSD-3-Clause",
        "core: ed25519 signature over the fidelity record",
    ),
    "flux-spec": (
        "flux-spec",
        "Apache-2.0",
        "core: FLUX validator, RFC-04 enforcement gate, RFC-09 bundler",
    ),
    "openlineage-python": ("openlineage-python", "Apache-2.0", "[lineage]: RunEvent client"),
    "pyarrow": (
        "pyarrow",
        "Apache-2.0",
        "[arrow]: Arrow batches, Parquet and Arrow IPC files for the output sinks",
    ),
    "adbc-driver-manager": (
        "adbc-driver-manager",
        "Apache-2.0",
        "[adbc]: loads the ADBC driver the database sink writes through",
    ),
    "adbc-driver-sqlite": (
        "adbc-driver-sqlite",
        "Apache-2.0",
        "[dev]: the database sink's tests, against SQLite",
    ),
    "duckdb": ("duckdb", "MIT", "[dev]: the database sink's tests, against DuckDB"),
    "pytest": ("pytest", "MIT", "[dev]"),
    "pytest-cov": ("pytest-cov", "MIT", "[dev]"),
    "hypothesis": ("hypothesis", "MPL-2.0", "[dev]: property-based testing"),
    "mypy": ("mypy", "MIT", "[dev]"),
    "ruff": ("ruff", "MIT", "[dev]"),
    "black": ("black", "MIT", "[dev]"),
    "import-linter": ("import-linter", "BSD-2-Clause", "[dev]: architecture contracts"),
    "pytest-xdist": ("pytest-xdist", "MIT", "[dev]: parallel test execution"),
    "pytest-timeout": ("pytest-timeout", "MIT", "[dev]: per-test hang backstop"),
    "sybil": ("sybil", "MIT", "[dev]: runs the examples in the documentation as tests"),
    "reuse": (
        "reuse",
        "Apache-2.0 AND CC0-1.0 AND CC-BY-SA-4.0 AND GPL-3.0-or-later",
        "[dev]: licence-header check, run as a tool and not distributed",
    ),
}

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize(name: str) -> str:
    """PEP 503 normalization: lower-case, runs of ``-``/``_``/``.`` collapsed to ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str:
    """The distribution name from a PEP 508 requirement string.

    Handles the plain (``numpy>=1.26,<3``), extras (``foo[bar]>=1``) and
    direct-reference (``flux-spec @ git+https://...@<sha>``) forms alike --
    every one of them starts with the distribution name.
    """
    match = _NAME.match(requirement)
    if match is None:  # pragma: no cover - defensive; no such requirement today
        raise ValueError(f"cannot read a distribution name from {requirement!r}")
    return normalize(match.group(1))


def declared_requirements() -> List[str]:
    """Every distribution pyproject declares, core first then each extra, deduplicated."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]
    names: List[str] = [
        requirement_name(requirement) for requirement in project.get("dependencies", [])
    ]
    for _extra, requirements in sorted(project.get("optional-dependencies", {}).items()):
        names.extend(requirement_name(requirement) for requirement in requirements)
    seen: Dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def missing_entries() -> List[str]:
    """Declared dependencies with no row in :data:`LICENSE_NOTES`."""
    return [name for name in declared_requirements() if name not in LICENSE_NOTES]


def stale_entries() -> List[str]:
    """Rows in :data:`LICENSE_NOTES` naming something pyproject no longer declares."""
    declared = set(declared_requirements())
    return sorted(name for name in LICENSE_NOTES if name not in declared)


def _pip_licenses_table() -> Optional[str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "piplicenses", "--format=markdown", "--with-urls"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout


def _manual_table() -> str:
    missing = missing_entries()
    if missing:
        raise SystemExit(
            "third_party_licenses: pyproject declares "
            f"{', '.join(missing)} with no entry in LICENSE_NOTES -- "
            "add the licence and the reason, then regenerate."
        )
    lines = ["| Package | License | Used for |", "|---|---|---|"]
    for name in declared_requirements():
        display, license_name, note = LICENSE_NOTES[name]
        lines.append(f"| {display} | {license_name} | {note} |")
    return "\n".join(lines)


def main() -> int:
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    table = _pip_licenses_table()
    source_note = (
        "pip-licenses"
        if table
        else "documented list derived from pyproject.toml (pip-licenses not installed)"
    )
    if not table:
        table = _manual_table()

    body = (
        "# Third-party licenses\n\n"
        f"Generated {generated_at} by `scripts/third_party_licenses.py` ({source_note}).\n"
        "Every distribution `pyproject.toml` declares appears below; the list is\n"
        "derived from those declarations rather than maintained by hand, and\n"
        "`tests/test_third_party_licenses.py` fails when one has no entry.\n"
        "Also see `flux_engine/_vendor/flux_spec/NOTICE` for the vendored\n"
        "Apache-2.0 `flux_spec` fallback copy, which carries the same license as\n"
        "the `flux-spec` distribution listed below.\n\n"
        f"{table}\n"
    )
    OUTPUT.write_text(body)
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)} ({source_note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
