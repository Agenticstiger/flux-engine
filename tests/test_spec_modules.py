# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke coverage for the flux_engine.spec.{validate,bundle} shim submodules.

test_spec_shim.py already exercises flux_engine.spec.enforce end to end
(all 34 conformance vectors). These tests do the equivalent minimal smoke
check for the other two re-exported modules -- validate.py's offline
reference-validator surface and bundle.py's evidence-bundle surface --
without needing a full FLUX bundle fixture, which is out of scope here.
"""

from __future__ import annotations

import pytest

from flux_engine.spec.bundle import merkle_root, pack, verify
from flux_engine.spec.validate import Bundle, canonical_digest, load_yaml_docs, semver_satisfies


@pytest.mark.unit
def test_canonical_digest_is_stable() -> None:
    assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})


@pytest.mark.unit
def test_semver_satisfies_caret_and_tilde_ranges() -> None:
    assert semver_satisfies("1.2.3", "^1.0.0")
    assert not semver_satisfies("2.0.0", "^1.0.0")
    assert semver_satisfies("1.2.9", "~1.2.0")
    assert not semver_satisfies("1.3.0", "~1.2.0")


@pytest.mark.unit
def test_bundle_class_is_importable() -> None:
    assert isinstance(Bundle, type)


@pytest.mark.unit
def test_load_yaml_docs_reads_multi_document_yaml(tmp_path) -> None:
    yaml_path = tmp_path / "docs.flux.yml"
    yaml_path.write_text("kind: World\nid: w1\n---\nkind: Persona\nid: p1\n")

    docs, error = load_yaml_docs(yaml_path)

    assert error is None
    assert docs == [{"kind": "World", "id": "w1"}, {"kind": "Persona", "id": "p1"}]


@pytest.mark.unit
def test_load_yaml_docs_reports_an_error_for_a_missing_file(tmp_path) -> None:
    docs, error = load_yaml_docs(tmp_path / "does-not-exist.flux.yml")
    assert docs == []
    assert error is not None


@pytest.mark.unit
def test_merkle_root_is_deterministic() -> None:
    empty_root = merkle_root({})
    assert empty_root.startswith("sha256:")
    assert merkle_root({}) == empty_root

    digest = "sha256:" + "00" * 32
    one_file_root = merkle_root({"a.json": digest})
    assert one_file_root.startswith("sha256:")
    assert merkle_root({"a.json": digest}) == one_file_root
    assert one_file_root != empty_root


@pytest.mark.unit
def test_pack_and_verify_are_callable() -> None:
    assert callable(pack)
    assert callable(verify)
