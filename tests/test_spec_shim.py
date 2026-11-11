# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux_engine.spec -- the flux_spec shim, vendored-fallback-aware."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from flux_engine.governance.gate import run_conformance_vectors
from flux_engine.spec import (
    SOURCE,
    SOURCE_VERSION,
    SUPPORTED_SPEC_VERSION_RANGE,
    default_vectors_path,
    flux_schema_path,
)


@pytest.mark.unit
def test_source_reflects_the_environment() -> None:
    # SOURCE is derived from whether the public ``flux_spec`` distribution
    # is importable -- never a constant. Assert the derivation, not a
    # particular environment.
    expected = "flux_spec" if importlib.util.find_spec("flux_spec") is not None else "vendored"
    assert SOURCE == expected


@pytest.mark.unit
def test_source_is_vendored_when_flux_spec_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate an environment without the public distribution: block the
    # import and make find_spec report it missing, then reload the shim.
    import flux_engine.spec as shim

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "flux_spec":
            return None
        return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setitem(sys.modules, "flux_spec", None)
    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    try:
        reloaded = importlib.reload(shim)
        assert reloaded.SOURCE == "vendored"
        assert reloaded.SOURCE_VERSION is None
        # Both data files come from the vendored copy, and both are real.
        for path in (reloaded.default_vectors_path(), reloaded.flux_schema_path()):
            assert "_vendor" in str(path), path
            assert Path(path).is_file(), path
    finally:
        monkeypatch.undo()
        importlib.reload(shim)


@pytest.mark.unit
def test_the_data_files_come_from_the_same_backend_as_the_code() -> None:
    """Code from one backend and schema/vectors from the other is the bug.

    The probes this replaced looked for ``flux_spec/vectors/`` and
    ``flux_spec/schema/``; the published distribution keeps its data under
    ``flux_spec/data/``, so with flux_spec installed the engine reported
    ``spec_source: flux_spec`` while reading the *vendored* schema and
    vectors -- silently, because a missed ``exists()`` fell through to the
    fallback. Assert the invariant instead of a path spelling: whichever
    backend is active supplies both files.
    """
    vectors = default_vectors_path()
    schema = flux_schema_path()
    assert vectors.is_file(), vectors
    assert schema.is_file(), schema
    if SOURCE == "flux_spec":
        import flux_spec

        package_root = Path(flux_spec.__file__).resolve().parent
        for path in (vectors, schema):
            assert path.resolve().is_relative_to(
                package_root
            ), f"{path} is not inside the active flux_spec backend at {package_root}"
            assert "_vendor" not in path.parts, path
    else:
        for path in (vectors, schema):
            assert "_vendor" in str(path), path


@pytest.mark.unit
def test_source_version_reports_the_installed_distribution() -> None:
    if SOURCE == "flux_spec":
        import flux_spec

        assert SOURCE_VERSION == flux_spec.__version__
    else:
        assert SOURCE_VERSION is None


@pytest.mark.unit
def test_an_unsupported_flux_spec_is_refused_rather_than_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-range flux_spec must fail loudly, not answer quietly.

    flux_spec supplies the RFC-04 enforcement decision; substituting a
    different one for the pinned one is exactly the silent behaviour
    change this shim exists to prevent.
    """
    import flux_engine.spec as shim

    fake = types.ModuleType("flux_spec")
    fake.__version__ = "9.9.0"  # type: ignore[attr-defined]
    # find_spec() reads __spec__ off an already-imported module and raises
    # ValueError when it is None, so the stand-in needs one.
    fake.__spec__ = importlib.machinery.ModuleSpec("flux_spec", loader=None)
    monkeypatch.setitem(sys.modules, "flux_spec", fake)
    try:
        with pytest.raises(RuntimeError) as raised:
            importlib.reload(shim)
        assert "9.9.0" in str(raised.value)
        assert SUPPORTED_SPEC_VERSION_RANGE in str(raised.value)
    finally:
        monkeypatch.undo()
        importlib.reload(shim)


@pytest.mark.unit
def test_default_vectors_path_resolves_to_a_real_file() -> None:
    path = default_vectors_path()
    assert path.is_file()
    assert path.name == "enforcement-vectors.json"


@pytest.mark.unit
def test_all_34_vectors_pass_through_the_shim() -> None:
    vectors_path = default_vectors_path()
    data = json.loads(Path(vectors_path).read_text())
    total = len(data["vectors"])
    assert total == 34, "the RFC-04 conformance vector count changed upstream"

    failures, messages = run_conformance_vectors(vectors_path)
    assert failures == 0, messages


@pytest.mark.unit
def test_a_fail_open_gate_fails_the_conformance_vectors() -> None:
    """A deliberately fail-open gate (always allows) must FAIL the vectors.

    Regression guard in the spirit of the "six deliberately broken
    kernels" idea, applied here to the enforcement gate: a gate that
    always allows would pass no vector expecting a denial, proving the
    vectors are a real oracle and not a rubber stamp that passes anything.
    """

    def _fail_open_gate(
        policy: Dict[str, Any],
        request: Dict[str, Any],
        skill: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {"allow": True, "reasonCode": "ALLOWED"}

    vectors_path = default_vectors_path()
    failures, messages = run_conformance_vectors(vectors_path, gate=_fail_open_gate)
    assert failures > 0, "a fail-open gate must not pass every RFC-04 vector"
    assert messages
