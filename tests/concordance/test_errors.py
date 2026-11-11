# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The codes this subsystem owns, and the register-discipline vocabulary.

Two things are pinned here. Every code the fidelity check can raise is in
the one catalog, so ``fluxsim explain FLX-5010`` answers. And no docstring,
error message or log line in the package uses a word from the register
list -- the module *names* may say ``concordance``; the prose may not.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from flux_engine.concordance.errors import OWNED_CODES
from flux_engine.core import errors as core_errors

pytestmark = pytest.mark.unit

PACKAGE_ROOT = Path(core_errors.__file__).resolve().parents[1] / "concordance"

#: Copied from ``scripts/register_lint.py`` -- the same list, applied to
#: this package's prose. Kept as a literal rather than imported so that
#: relaxing the lint script cannot quietly relax this test.
BANNED_WORDS = [
    "concordance",
    "zero-knowledge",
    "tolerance predicate",
    "clear-statistic",
    "practise",
    "mathematically",
    "proves",
    "legally admissible",
    "certified",
    "FLUX Simulator",
]
_BANNED = re.compile("|".join(re.escape(word) for word in BANNED_WORDS), re.IGNORECASE)

#: The one place ``concordance`` is allowed to appear in this package's
#: source: an import statement or a dotted module path. Those are names,
#: not prose.
_MODULE_PATH = re.compile(r"flux_engine[./]concordance")


def _source_files() -> List[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_the_package_has_source_files_to_scan() -> None:
    """A scan over an empty file list would pass and mean nothing."""
    assert len(_source_files()) >= 8


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def test_every_owned_code_is_registered_in_the_one_catalog() -> None:
    for entry in OWNED_CODES:
        assert core_errors.is_registered(entry.code)
        assert core_errors.get(entry.code) is entry


def test_the_owned_codes_are_sorted_and_unique() -> None:
    codes = [entry.code for entry in OWNED_CODES]
    assert codes == sorted(codes)
    assert len(set(codes)) == len(codes)


def test_every_owned_code_sits_in_a_band_the_kernel_spec_names() -> None:
    for entry in OWNED_CODES:
        assert entry.band in core_errors.CATEGORIES
        assert entry.category in ("numerics", "evidence/pack")


def test_the_three_codes_the_spec_names_are_owned() -> None:
    """``FLX-5010`` (§4), ``FLX-5011`` (§8) and ``FLX-8001`` (§7)."""
    owned = {entry.code for entry in OWNED_CODES}
    assert {"FLX-5010", "FLX-5011", "FLX-8001"} <= owned


def test_every_owned_code_explains_itself() -> None:
    for entry in OWNED_CODES:
        rendered = core_errors.explain(entry.code)
        assert entry.title in rendered
        assert entry.explanation in rendered
        assert len(entry.explanation) > 80, f"{entry.code} explains too little"


def test_every_code_raised_in_the_package_is_one_the_package_owns() -> None:
    """A raise of a code from another module would explain the wrong thing."""
    owned = {entry.code.replace("-", "_") for entry in OWNED_CODES}
    imported = {
        "FLX_5001",
        "FLX_5002",
        "FLX_5005",
        "FLX_5022",
    }
    used: set = set()
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id.startswith("FLX_"):
                used.add(node.id)
    assert used <= owned | imported, f"unexpected codes: {sorted(used - owned - imported)}"
    assert owned <= used | {"FLX_8001"}, "every owned code has a raise site or a decision"


# ---------------------------------------------------------------------------
# The register-discipline vocabulary
# ---------------------------------------------------------------------------


def _prose_of(path: Path) -> Iterator[Tuple[int, str]]:
    """Yield every docstring and string literal line of *path*.

    Comments and identifiers are excluded: what the rule governs is the
    text a reader is shown -- a docstring, an error message, a log line.
    Import statements naming the module are excluded by the caller.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for offset, line in enumerate(node.value.splitlines()):
                yield (node.lineno + offset, line)


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_no_prose_in_the_package_uses_a_word_from_the_register_list(path: Path) -> None:
    hits = []
    for lineno, line in _prose_of(path):
        if _MODULE_PATH.search(line):
            continue
        match = _BANNED.search(line)
        if match:
            hits.append(f"{path.name}:{lineno}: {match.group(0)!r} in {line.strip()!r}")
    assert hits == []


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_no_comment_in_the_package_uses_a_word_from_the_register_list(path: Path) -> None:
    """Comments too: they are read by anyone who opens the file."""
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        if _MODULE_PATH.search(stripped):
            continue
        match = _BANNED.search(stripped)
        if match:
            hits.append(f"{path.name}:{lineno}: {match.group(0)!r}")
    assert hits == []


def test_the_vocabulary_scan_is_not_vacuous(tmp_path) -> None:
    """A file that does use a banned word is caught, so the scan has teeth."""
    offender = tmp_path / "offender.py"
    offender.write_text('"""This module proves a thing."""\n', encoding="utf-8")
    hits = [line for _, line in _prose_of(offender) if _BANNED.search(line)]
    assert hits


def test_the_package_uses_the_words_the_register_prescribes() -> None:
    """ "fidelity check" and "fidelity record", not the reserved spellings."""
    text = "\n".join(path.read_text(encoding="utf-8") for path in _source_files())
    assert "fidelity check" in text
    assert "fidelity record" in text


def test_no_owned_catalog_entry_uses_a_word_from_the_register_list() -> None:
    for entry in OWNED_CODES:
        for field in (entry.title, entry.explanation):
            assert _BANNED.search(field) is None, f"{entry.code}: {field}"
