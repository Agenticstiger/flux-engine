# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux_engine.spec -- the shim over the flux_spec distribution."""

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
from tests.rfc04 import TRIPWIRE, VECTOR_COUNT


@pytest.mark.unit
def test_the_source_is_the_published_distribution() -> None:
    # The distribution is the only backend. describe --self reports this
    # value as spec_source, so it stays, and it does not vary.
    assert SOURCE == "flux_spec"


@pytest.mark.unit
def test_a_missing_flux_spec_is_refused_with_the_command_that_installs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the distribution the shim names the fix and runs on nothing else.

    It used to fall back to a vendored copy of the distribution, which is
    retired (ADR 0005, amended 2026-11-17).
    """
    import flux_engine.spec as shim

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
        if name == "flux_spec":
            return None
        return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setitem(sys.modules, "flux_spec", None)
    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    try:
        with pytest.raises(ModuleNotFoundError) as raised:
            importlib.reload(shim)
        assert raised.value.name == "flux_spec"
        assert f"pip install 'flux-spec{SUPPORTED_SPEC_VERSION_RANGE}'" in str(raised.value)
    finally:
        monkeypatch.undo()
        importlib.reload(shim)


@pytest.mark.unit
def test_the_data_files_come_from_the_same_backend_as_the_code() -> None:
    """Code from one copy and schema/vectors from another is the bug.

    The probes this replaced looked for ``flux_spec/vectors/`` and
    ``flux_spec/schema/``; the published distribution keeps its data under
    ``flux_spec/data/``, so with flux_spec installed the engine reported
    ``spec_source: flux_spec`` while reading the *vendored* schema and
    vectors -- silently, because a missed ``exists()`` fell through to the
    fallback. Assert the invariant instead of a path spelling: the
    distribution that supplies the code supplies both files.
    """
    import flux_spec

    vectors = default_vectors_path()
    schema = flux_schema_path()
    assert vectors.is_file(), vectors
    assert schema.is_file(), schema
    package_root = Path(flux_spec.__file__).resolve().parent
    for path in (vectors, schema):
        assert path.resolve().is_relative_to(
            package_root
        ), f"{path} is not inside the flux_spec distribution at {package_root}"


@pytest.mark.unit
def test_source_version_reports_the_installed_distribution() -> None:
    import flux_spec

    assert SOURCE_VERSION == flux_spec.__version__


@pytest.mark.unit
@pytest.mark.parametrize(
    "version, parts",
    [
        ("0.5.4", (0, 5, 4)),
        ("0.5", (0, 5, 0)),
        ("0.6.0rc1", (0, 6, 0)),
        ("0.5.10.dev0", (0, 5, 10)),
    ],
)
def test_a_flux_spec_version_is_read_to_its_patch(version: str, parts: tuple) -> None:
    from flux_engine.spec import _version_tuple

    assert _version_tuple(version) == parts


@pytest.mark.unit
@pytest.mark.parametrize("version", ["9.9.0", "0.6.0", "0.5.3"])
def test_an_unsupported_flux_spec_is_refused_rather_than_used(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """An out-of-range flux_spec must fail loudly, not answer quietly.

    flux_spec supplies the RFC-04 enforcement decision; substituting a
    different one for the pinned one is exactly the silent behaviour
    change this shim exists to prevent.
    """
    import flux_engine.spec as shim

    fake = types.ModuleType("flux_spec")
    # 0.5.3 is below the floor: it reads a bundle's files in the locale's encoding.
    fake.__version__ = version  # type: ignore[attr-defined]
    # find_spec() reads __spec__ off an already-imported module and raises
    # ValueError when it is None, so the stand-in needs one.
    fake.__spec__ = importlib.machinery.ModuleSpec("flux_spec", loader=None)
    monkeypatch.setitem(sys.modules, "flux_spec", fake)
    try:
        with pytest.raises(RuntimeError) as raised:
            importlib.reload(shim)
        assert version in str(raised.value)
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
def test_every_vector_passes_through_the_shim() -> None:
    vectors_path = default_vectors_path()
    data = json.loads(Path(vectors_path).read_text())
    total = len(data["vectors"])
    assert total == VECTOR_COUNT, TRIPWIRE

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
