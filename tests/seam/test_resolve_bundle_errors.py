# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a bundle that cannot be loaded says to the person who typed the path.

Two failures, both of which used to be reported as ``FLX-3105`` ("bundle
does not validate, so its seam cannot be resolved"), whose catalogue entry
is about reference resolution and id uniqueness:

* a mistyped path, which never reached the validator at all -- and which
  was printed twice, because the joined validator errors were already
  path-prefixed;
* one unreadable or schema-invalid document, whose failure was first in a
  semicolon-joined run-on line and was followed by a *derived* error for
  every reference into it, each asserting that a document sitting on disk
  is "no such document in bundle".
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.resolve import load_bundle

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.unit


@pytest.fixture()
def bundle_copy(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, target)
    return target


def test_a_path_that_does_not_exist_is_its_own_code(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as raised:
        load_bundle(tmp_path / "nope")
    error = raised.value
    assert error.code.code == "FLX-3107"
    assert "is not a directory" in error.detail
    # Named once, not twice: the old message prefixed the path and then
    # printed a validator error that already began with the same path.
    assert error.detail.count(str(tmp_path / "nope")) == 1


def test_a_file_inside_the_bundle_is_not_the_bundle(bundle_copy: Path) -> None:
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy / "simulation.flux.yml")
    assert raised.value.code.code == "FLX-3107"


def test_the_new_code_explains_itself() -> None:
    from flux_engine.core.errors import explain, is_registered

    assert is_registered("FLX-3107")
    text = explain("FLX-3107")
    assert "directory" in text


def test_the_closed_set_code_explains_itself() -> None:
    """`fluxsim explain FLX-3108` has to be real, not a KeyError."""
    from flux_engine.core.errors import explain, is_registered

    assert is_registered("FLX-3108")
    text = explain("FLX-3108")
    assert "flux-manifest.json" in text
    assert "*.flux.yml" in text


def test_an_unparseable_document_reports_file_line_and_column(bundle_copy: Path) -> None:
    with (bundle_copy / "persona.flux.yml").open("a", encoding="utf-8") as handle:
        handle.write("\n  : broken\n")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    detail = raised.value.detail
    assert "persona.flux.yml" in detail
    assert "line " in detail and "column " in detail


def test_a_broken_document_suppresses_the_errors_derived_from_it(bundle_copy: Path) -> None:
    """The reference checks read what loaded, so they cannot be trusted here."""
    with (bundle_copy / "persona.flux.yml").open("a", encoding="utf-8") as handle:
        handle.write("\n  : broken\n")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    detail = raised.value.detail
    assert "no such document in bundle" not in detail
    assert "dangling reference" not in detail
    assert "skipped" in detail


def test_a_schema_failure_does_the_same(bundle_copy: Path) -> None:
    text = (bundle_copy / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "fidelity: deterministic" in text
    (bundle_copy / "simulation.flux.yml").write_text(
        text.replace("fidelity: deterministic", "fidelity: banana"), encoding="utf-8"
    )
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    detail = raised.value.detail
    assert "schema:" in detail
    assert "no such document in bundle" not in detail


def test_errors_are_listed_one_per_line(bundle_copy: Path) -> None:
    """A semicolon-joined run-on is not a list a person can read."""
    (bundle_copy / "extra.flux.yml").write_text("\n  : broken\n", encoding="utf-8")
    with (bundle_copy / "persona.flux.yml").open("a", encoding="utf-8") as handle:
        handle.write("\n  : broken\n")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    lines = [line for line in raised.value.detail.splitlines() if line.strip()]
    assert len(lines) >= 2
    assert all(line.startswith("  ") for line in lines)


def test_a_healthy_bundle_still_loads(bundle_copy: Path) -> None:
    loaded = load_bundle(bundle_copy)
    assert loaded.flux_documents
