# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Every subcommand's ``--help`` text passes ``scripts/register_lint.py``'s
banned-vocabulary list -- the same regex the register-discipline lint runs
against README/docs, applied here
per-subcommand so a CI failure names the offending command directly
instead of a repo-wide grep hit with no subcommand attribution.

In-process, not subprocess: this only introspects argparse's own
``format_help()`` output (no engine run, no filesystem bundle), so it
belongs with :mod:`tests.test_cli`'s in-process style rather than
:mod:`tests.cli.test_six_step_loop_cli`'s real-binary one.

Also covers ``_scan_source`` -- the source/docstring pass that closes the
gap where the lint used to see only README/docs/``--help`` and miss
docstrings and error-catalog ``register(...)`` explanations. Exercised
against a synthetic package under ``tmp_path`` (not the real
``flux_engine/`` tree, whose own docstrings and explanations are free to
change as other work lands) so this test's pass/fail never depends on
today's source content -- only on the scanner's own logic: a banned word
in a docstring is caught, one in a ``register()`` explanation is caught,
a dotted ``flux_engine.concordance.*`` reference is not a false positive,
and a clean file produces no hits.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_register_lint():
    """Import ``scripts/register_lint.py`` as a module.

    ``scripts/`` is not a package (no ``__init__.py``, and it sits outside
    ``flux_engine``'s own import-linter contracts by design -- it is a CI
    tool, not runtime code), so it is loaded by path rather than by dotted
    import.
    """
    path = REPO_ROOT / "scripts" / "register_lint.py"
    spec = importlib.util.spec_from_file_location("register_lint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("register_lint", module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_every_subcommand_help_passes_register_lint() -> None:
    register_lint = _load_register_lint()
    hits = register_lint._scan_help()
    assert hits == [], "banned vocabulary leaked into --help:\n" + "\n".join(hits)


@pytest.mark.unit
def test_help_scan_covers_every_registered_subcommand() -> None:
    """A regression guard on the guard itself: if a future subcommand is
    added to the parser but the walk that discovers subparsers breaks (a
    typo in the ``_SubParsersAction`` introspection, say), this fails
    loudly instead of the vocabulary test above silently checking fewer
    commands than exist.
    """
    from flux_engine.cli import _build_parser

    register_lint = _load_register_lint()
    labels = {label for label, _text in register_lint._iter_help_texts(_build_parser(), "fluxsim")}
    expected_top_level = {
        f"fluxsim {name}"
        for name in (
            "validate",
            "plan",
            "apply",
            "run",
            "verify",
            "ship",
            "replay",
            "calibrate",
            "accuracy",
            "gate",
            "expr",
            "describe",
            "plugins",
            "explain",
        )
    }
    assert expected_top_level <= labels


def _write_fake_engine(root: Path, *, module_body: str) -> None:
    package = root / "flux_engine"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text('"""Fake package for register_lint tests."""\n')
    (package / "sample.py").write_text(module_body)


@pytest.mark.unit
def test_scan_source_catches_a_banned_word_in_a_docstring(tmp_path, monkeypatch) -> None:
    register_lint = _load_register_lint()
    _write_fake_engine(
        tmp_path,
        module_body='"""This module proves the scanner works."""\n',
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = register_lint._scan_source()
    assert any("proves" in hit for hit in hits), hits


@pytest.mark.unit
def test_scan_source_catches_a_banned_word_in_a_register_explanation(tmp_path, monkeypatch) -> None:
    register_lint = _load_register_lint()
    _write_fake_engine(
        tmp_path,
        module_body=(
            "from flux_engine.core.errors import register\n\n"
            "register('FLX-9999', 'title', 'this explanation is legally admissible')\n"
        ),
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = register_lint._scan_source()
    assert any("legally admissible" in hit for hit in hits), hits


@pytest.mark.unit
def test_scan_source_does_not_flag_a_dotted_concordance_module_reference(
    tmp_path, monkeypatch
) -> None:
    register_lint = _load_register_lint()
    _write_fake_engine(
        tmp_path,
        module_body=(
            '"""See :mod:`flux_engine.concordance.verifier.ClearStatistic` '
            'for the fidelity check itself."""\n'
        ),
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = register_lint._scan_source()
    assert hits == [], hits


@pytest.mark.unit
def test_scan_source_is_clean_on_a_clean_module(tmp_path, monkeypatch) -> None:
    register_lint = _load_register_lint()
    _write_fake_engine(
        tmp_path,
        module_body=(
            '"""Nothing to see here."""\n'
            "\n"
            "def helper():\n"
            '    """Returns the fidelity check\'s verdict."""\n'
            "    return True\n"
        ),
    )
    monkeypatch.setattr(register_lint, "REPO_ROOT", tmp_path)
    hits = register_lint._scan_source()
    assert hits == [], hits
