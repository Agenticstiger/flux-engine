# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Product Guardrail profile — FLUX Simulation Engine.

Hand-written and deliberately NOT synced: this is where the repo-specific
facts live so that canon_payload.py and check.py can stay byte-identical
across every repo.

COMPLEMENTS scripts/register_lint.py, does not replace it. That lint is the
NEGATIVE side — a fixed list of words reserved until an external event
authorises them — and it scans rendered `fluxsim --help` text by walking argparse,
which is a better technique than reading source for that surface. This is the
POSITIVE side: the approved names, their casing, and a coverage report. The two
overlap on exactly one term ("FLUX Simulator"), and both should keep it.

This repo is public, so its leak surface is the same as the other product
repos'. The naming discipline matters MORE here, not less: the
canon records that the FLUX Simulation Engine is spec-only, and this repo is
where a present-tense capability claim would first be written. The README
already handles that well and says so explicitly.
"""

# Consistent with the other product repos. The three existing uses of the
# British spelling are all module docstrings and a pyproject comment — that is
# developer prose, where the locale rule does not apply at all, so nothing
# needs renaming. The rule stays symmetric either way.
LOCALE = "en-US"

# Mirrors register_lint.py's own boundary exactly: docs/spec/ holds the
# normative specifications, which name engine modules by design. Everything
# else under docs/ is a public-facing surface.
# docs/spec/ is still SCANNED — it just lands in developer prose, where naming
# advises rather than blocks.
SURFACE_TIER = (
    "README.md",
    "docs/adr/",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_LICENSES.md",
)

EXCLUDED = (
    # Holds "FLUX Simulator" as a banned-word literal — scanning it is scanning
    # the rule list. Same reason the guardrail excludes its own files.
    "scripts/register_lint.py",
    # Vendored third-party source. Not our prose to police, and rewriting a
    # vendored file breaks its provenance.
    "flux_engine/_vendor/",
    "CHANGELOG.md",
    ".venv",
    "dist",
    "build",
    "htmlcov",
    "*.egg-info",
    "node_modules",
    "tools/product_guardrail/",
)
SCAN_ENTITY_FILES = True

# Files that ARE the copy: every string literal is user-facing.
PY_ALL_STRINGS = ()

# Floors: a scan that collapses must fail loudly rather than pass vacuously.
# Set below today's real numbers so ordinary growth does not trip them.
# Measured, not guessed: this repo scans 46 files / ~84 KB / 96 spans today.
# The floors sit at roughly two thirds of that — low enough that an ordinary
# deletion does not trip them, high enough that a real collapse does. An
# earlier draft quoted numbers that were wrong by 8 files and ~3x, and set the
# span floor ABOVE the actual, at which the gate correctly refused to certify.
MIN_FILES_SCANNED = 31
MIN_BYTES_READ = 55_000
MIN_SPANS_EXTRACTED = 63
MIN_TEXT_EXTRACTED = 25_000

GRACE = []
