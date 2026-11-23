# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A plugin whose import or factory raises is a coded refusal that names the plugin and the cause.

ADR 0032 section 3, step 7: the entry point is loaded and its factory called. Whatever the plugin's
module or factory raises, the command ends with ``FLX-3124`` naming the expose, the plugin, the
phase and the exception's type and first line, and prints no traceback and no option value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flux_engine.cli import main

from .conftest import PACKAGE, make_distribution, plugin_bundle

pytestmark = pytest.mark.integration

_FACTORY = "\ndef factory(options):\n    return ('plugin', dict(options))\n"


def _refusal(site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str) -> str:
    make_distribution(site, plugin_source=source)
    bundle = str(plugin_bundle(tmp_path))
    code = main(
        ["plan", bundle, "--state-dir", str(tmp_path / "state"), "--allow-generator", "learned"]
    )
    err = capsys.readouterr().err
    assert code == 1, err
    assert "Traceback" not in err and "report" not in err.lower(), err
    return err


def test_a_module_that_raises_syntax_error_on_import_is_refused(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _refusal(site, tmp_path, capsys, "def broken(:\n    pass\n")
    assert "FLX-3124" in err
    assert "importing the module" in err and "SyntaxError" in err
    assert "learned" in err


def test_a_module_that_raises_runtime_error_on_import_is_refused(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _refusal(
        site, tmp_path, capsys, "raise RuntimeError('boom on import\\nsecond line')" + _FACTORY
    )
    assert "FLX-3124" in err
    assert "importing the module" in err and "RuntimeError: boom on import" in err
    assert "second line" not in err and "learned" in err


def test_a_factory_that_raises_is_refused_without_the_options(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _refusal(
        site, tmp_path, capsys, "def factory(options):\n    raise ValueError('bad factory')\n"
    )
    assert "FLX-3124" in err
    assert "calling the factory" in err and "ValueError: bad factory" in err
    assert "temperature" not in err


def test_a_missing_module_of_the_plugin_is_named_as_its_own(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_distribution(site, value=f"{PACKAGE}.absent:factory")
    bundle = str(plugin_bundle(tmp_path))
    code = main(
        ["plan", bundle, "--state-dir", str(tmp_path / "state"), "--allow-generator", "learned"]
    )
    err = capsys.readouterr().err
    assert code == 1 and "FLX-3124" in err, err
    assert f"{PACKAGE}.absent" in err and "the plugin's own module" in err


def test_a_missing_module_the_plugin_imports_is_named_as_another(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _refusal(site, tmp_path, capsys, "import fxgen_no_such_dependency" + _FACTORY)
    assert "FLX-3124" in err
    assert "fxgen_no_such_dependency" in err and "a module it imports" in err
