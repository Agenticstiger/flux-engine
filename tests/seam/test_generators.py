# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation's ``generators`` declaration (ADR 0032 section 2): read, refused, and bound into ``H``.

The bundle is ``tests/fixtures/related-star``, five exposes in one Simulation. A declaration names a
plugin and pins it, grades it ``P4`` or ``P5``, and for ``P4`` states the account and the model
digest. It changes the rows its expose generates, so it moves ``H`` of that expose and no other, and
a bundle that declares none hashes as it always did. Nothing here imports or calls a plugin.
"""

from __future__ import annotations

import dataclasses
import shutil
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern
from flux_engine.seam.generators import (
    GENERATORS_KEY,
    GeneratorDeclaration,
    PrivacyAccount,
    bound_into_h,
    declaration_for,
    read_generators_block,
)
from flux_engine.seam.resolve import (
    FLX_3105,
    declared_generators,
    load_bundle,
    resolve_all_emits,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "related-star"
NAMESPACE = "  com.agentics.flux/1:\n"
SIMULATION = "sim-store-star"
DIGEST = "sha256:" + "ab" * 32

P4 = """\
generators:
  item:
    plugin: learned
    distribution: my-package
    version: "1.2.0"
    grade: P4
    privacy:
      unit: one data subject with all of its records
      epsilon: "3"
      delta: "0.00001"
      accounting: a Renyi accountant over the training steps
    model: {digest: "sha256:%s"}
    reproducibility: environment
    options: {temperature: "1", sizes: ["1", "2"], nested: {enabled: true}}
""" % ("ab" * 32)


def _bundle(tmp_path: Path, block: str = "", name: str = "bundle") -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    if block:
        simulation = target / "simulation.flux.yml"
        text = simulation.read_text("utf-8")
        assert text.count(NAMESPACE) == 1
        lines = "".join(f"    {line}\n" for line in block.splitlines())
        simulation.write_text(text.replace(NAMESPACE, NAMESPACE + lines, 1), "utf-8")
    return target


def _digests(bundle: Path) -> Dict[str, Tuple[str, str]]:
    from flux_engine.seam.emit import seam_digests

    found = {}
    for resolved in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION):
        digests = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[resolved.expose_id] = (digests.h.value, digests.g.value)
    return found


def _entry(**change: object) -> str:
    """The ``item`` entry of ``P4`` as YAML, with the top-level keys of *change* replaced or (None) dropped."""
    import yaml

    document = yaml.safe_load(P4)["generators"]["item"]
    for key, value in change.items():
        if value is None:
            document.pop(key, None)
        else:
            document[key] = value
    return yaml.safe_dump({"generators": {"item": document}}, sort_keys=False)


def _refused(tmp_path: Path, block: str) -> str:
    bundle = _bundle(tmp_path, block, name="bad")
    with pytest.raises(FluxError) as refused:
        resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
    assert refused.value.code == FLX_3105
    return str(refused.value)


def _private(**change: object) -> Dict[str, object]:
    base: Dict[str, object] = {
        "unit": "a subject",
        "epsilon": "3",
        "delta": "0.00001",
        "accounting": "an accountant",
    }
    base.update(change)
    return base


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Tuple[str, str]]:
    return _digests(_bundle(tmp_path_factory.mktemp("plain")))


def test_a_p4_declaration_is_read_as_a_typed_frozen_record(tmp_path: Path) -> None:
    from flux_engine.runtime import resolve_cast

    loaded = load_bundle(_bundle(tmp_path, P4))
    cast = resolve_cast(loaded.flux_documents, simulation_id=SIMULATION)
    (found,) = declared_generators(cast.simulation, where=SIMULATION)
    assert isinstance(found, GeneratorDeclaration)
    assert (found.expose_id, found.plugin, found.distribution, found.version) == (
        "item",
        "learned",
        "my-package",
        "1.2.0",
    )
    assert found.grade is Pattern.P4 and found.reproducibility == "environment"
    assert found.model_digest == DIGEST
    assert found.privacy == PrivacyAccount(
        unit="one data subject with all of its records",
        accounting="a Renyi accountant over the training steps",
        epsilon="3",
        delta="0.00001",
    )
    assert found.privacy is not None and found.privacy.loss == "epsilon = 3, delta = 0.00001"
    assert found.options == {"temperature": "1", "sizes": ["1", "2"], "nested": {"enabled": True}}
    with pytest.raises(dataclasses.FrozenInstanceError):
        found.plugin = "other"  # type: ignore[misc]
    assert (
        declaration_for([found], "item") is found and declaration_for([found], "date_dim") is None
    )


def test_a_p5_declaration_needs_no_account_and_may_hold_a_digest(tmp_path: Path) -> None:
    minimal = (
        "generators:\n  item: {plugin: p, distribution: d, version: '1', grade: P5, "
        "reproducibility: none}\n"
    )
    resolve_all_emits(load_bundle(_bundle(tmp_path, minimal)), simulation_id=SIMULATION)
    with_digest = (
        "generators:\n  item: {plugin: p, distribution: d, version: '1', grade: P5, "
        f"reproducibility: bytes, model: {{digest: '{DIGEST}'}}}}\n"
    )
    resolve_all_emits(
        load_bundle(_bundle(tmp_path, with_digest, "digest")), simulation_id=SIMULATION
    )


def test_a_p4_account_may_state_rho_alone(tmp_path: Path) -> None:
    block = _entry(privacy={"unit": "a subject", "rho": "0.5", "accounting": "zCDP composition"})
    bundle = _bundle(tmp_path, block)
    (resolved,) = [
        entry
        for entry in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)
        if entry.expose_id == "item"
    ]
    assert resolved.extension is not None
    member = resolved.extension[GENERATORS_KEY]
    assert member["privacy"] == {
        "unit": "a subject",
        "accounting": "zCDP composition",
        "rho": "0.5",
    }


@pytest.mark.parametrize("grade", ["P0", "P1", "P3", "P2", "P6", "p4", "", "4", "assumed"])
def test_a_grade_other_than_p4_and_p5_is_refused(tmp_path: Path, grade: str) -> None:
    message = _refused(tmp_path, _entry(grade=grade))
    assert ".grade is P4 or P5" in message


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"privacy": None}, "declares no privacy"),
        ({"model": None}, "declares no model.digest"),
        ({"model": {}}, "declares no model.digest"),
        ({"model": {"digest": "sha256:" + "AB" * 32}}, "64 lowercase hex digits"),
        ({"model": {"digest": "sha256:" + "ab" * 31}}, "64 lowercase hex digits"),
        ({"model": {"digest": "sha512:" + "ab" * 32}}, "64 lowercase hex digits"),
        ({"model": {"digest": "sha256:" + "ab" * 32, "path": "m.bin"}}, "holding only 'digest'"),
        ({"plugin": "my plugin"}, "an entry-point name"),
        ({"plugin": "../learned"}, "an entry-point name"),
        ({"plugin": "pkg.module:factory"}, "an entry-point name"),
        ({"plugin": "1learned"}, "an entry-point name"),
        ({"plugin": "learned\n"}, "an entry-point name"),
        ({"plugin": None}, "does not declare ['plugin']"),
        ({"distribution": None}, "does not declare ['distribution']"),
        ({"version": 1.2}, ".version is a non-empty string"),
        ({"version": None}, "does not declare ['version']"),
        ({"reproducibility": "sometimes"}, ".reproducibility is one of"),
        ({"reproducibility": None}, "does not declare ['reproducibility']"),
        ({"extra": 1}, "declares ['extra']"),
        ({"options": ["a"]}, ".options is a mapping"),
    ],
)
def test_a_broken_declaration_is_refused(
    tmp_path: Path, change: Dict[str, object], problem: str
) -> None:
    assert problem in _refused(tmp_path, _entry(**change))


def test_a_p5_declaration_with_privacy_is_refused(tmp_path: Path) -> None:
    message = _refused(tmp_path, _entry(grade="P5"))
    assert "graded P5" in message and "declares privacy" in message


@pytest.mark.parametrize(
    ("privacy", "problem"),
    [
        (_private(epsilon="1e3"), "no sign and no exponent"),
        (_private(epsilon="-1"), "no sign and no exponent"),
        (_private(epsilon="0"), "epsilon is finite and positive"),
        (_private(epsilon="0.0"), "epsilon is finite and positive"),
        (_private(epsilon="abc"), "no sign and no exponent"),
        (_private(epsilon="NaN"), "no sign and no exponent"),
        (_private(epsilon="inf"), "no sign and no exponent"),
        (_private(epsilon=3), "written as a string"),
        (_private(epsilon=3.5), "written as a string"),
        (_private(epsilon="3" * 1001), "no sign and no exponent"),
        (_private(delta="1"), "at least 0 and below 1"),
        (_private(delta="1.5"), "at least 0 and below 1"),
        (_private(delta="-0.1"), "no sign and no exponent"),
        (_private(delta=None), "epsilon and delta, or as rho alone"),
        (_private(epsilon=None), "epsilon and delta, or as rho alone"),
        (_private(rho="1"), "epsilon and delta, or as rho alone"),
        ({"unit": "u", "accounting": "a"}, "epsilon and delta, or as rho alone"),
        ({"unit": "u", "accounting": "a", "rho": "0"}, "rho is finite and positive"),
        ({"unit": "u", "accounting": "a", "rho": "-1"}, "no sign and no exponent"),
        (_private(unit=""), "unit is a non-empty string"),
        (_private(unit="   "), "unit is a non-empty string"),
        (_private(unit=None), "unit is a non-empty string"),
        (_private(unit=3), "unit is a non-empty string"),
        (_private(unit="u" * 1001), "at most 1000 characters"),
        (_private(accounting=""), "accounting is a non-empty string"),
        (_private(accounting="a" * 1001), "at most 1000 characters"),
        (_private(note="x"), "declares ['note']"),
    ],
)
def test_a_malformed_account_is_refused(
    tmp_path: Path, privacy: Dict[str, object], problem: str
) -> None:
    privacy = {key: value for key, value in privacy.items() if value is not None}
    assert problem in _refused(tmp_path, _entry(privacy=privacy))


def test_a_delta_of_zero_and_an_account_at_the_bounds_are_accepted(tmp_path: Path) -> None:
    block = _entry(privacy=_private(delta="0", unit="u" * 1000, accounting="a" * 1000))
    resolve_all_emits(load_bundle(_bundle(tmp_path, block)), simulation_id=SIMULATION)


@pytest.mark.parametrize(
    "options",
    [
        {"password": "hunter2-not-for-the-message"},
        {"outer": {"api_key": "hunter2-not-for-the-message"}},
        {"outer": [{"deeper": {"accessKey": "hunter2-not-for-the-message"}}]},
        {"Token": "hunter2-not-for-the-message"},
        {"key": "hunter2-not-for-the-message"},
        {"secretaccesskey": "hunter2-not-for-the-message"},
        {"Secret-Access-Key": "hunter2-not-for-the-message"},
        {"authkey": "hunter2-not-for-the-message"},
        {"passphrase": "hunter2-not-for-the-message"},
        {"pass-phrase": "hunter2-not-for-the-message"},
        {"authorization": "hunter2-not-for-the-message"},
        {"outer": {"Bearer": "hunter2-not-for-the-message"}},
        {"a": {"b": {"c": {"client-secret": "hunter2-not-for-the-message"}}}},
    ],
)
def test_a_credential_shaped_key_anywhere_in_options_is_refused_naming_the_key_not_the_value(
    tmp_path: Path, options: Dict[str, object]
) -> None:
    from flux_engine.sinks._secrets import credential_key

    message = _refused(tmp_path, _entry(options=options))

    def keys(node: object) -> List[str]:
        if isinstance(node, dict):
            return [k for k, v in node.items()] + [x for v in node.values() for x in keys(v)]
        if isinstance(node, list):
            return [x for v in node for x in keys(v)]
        return []

    (named,) = [key for key in keys(options) if credential_key(key)]
    assert f"'{named}'" in message and "names a credential" in message
    assert "hunter2" not in message


@pytest.mark.parametrize(
    "value",
    [
        "https://user:hunter2@example.com/path",
        "https://hunter2@example.com/path",
        "https://example.com/p?sig=hunter2",
        "https://example.com/p?X-Amz-Signature=hunter2&X-Amz-Date=1",
        "https://example.com/p?sv=2022-11-02&sas=hunter2",
        "postgresql://db/sim?password=hunter2",
        "AKIAIOSFODNN7EXAMPLE",
        "use AKIAIOSFODNN7EXAMPLE here",
        "-----BEGIN RSA PRIVATE KEY-----\nhunter2\n-----END RSA PRIVATE KEY-----",
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
        "Bearer hunter2tokenvalue",
        "Authorization: Bearer hunter2.tokenvalue-1_2~3+4/5==",
        "ey" + "J" * 20 + ".ey" + "K" * 20 + "." + "hunter2" * 3,
        "host=127.0.0.1 dbname=x password=hunter2",
        "host=127.0.0.1 password='hunter2 two'",
        "Server=db;PWD=hunter2",
        "//user:hunter2@host/db",
        "file:x.db#access_token=hunter2",
        "file:x.db?mode=rwc;sig=hunter2",
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nhunter2\n-----END PGP PRIVATE KEY BLOCK-----",
        "akiaiosfodnn7example",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "AGPAIOSFODNN7EXAMPLE",
        "AIDAIOSFODNN7EXAMPLE",
        "AROAIOSFODNN7EXAMPLE",
        "ANPAIOSFODNN7EXAMPLE",
        "ANVAIOSFODNN7EXAMPLE",
        "AIPAIOSFODNN7EXAMPLE",
        "A3TXIOSFODNN7EXAMPLE",
        "ABIAIOSFODNN7EXAMPLE",
        "ACCAIOSFODNN7EXAMPLE",
    ],
)
def test_a_credential_shaped_value_anywhere_in_options_is_refused_without_repeating_it(
    tmp_path: Path, value: str
) -> None:
    for number, options in enumerate(
        (
            {"endpoint": value},
            {"outer": [{"deeper": {"where": value}}]},
            {"note-env": value},
        )
    ):
        (tmp_path / str(number)).mkdir()
        message = _refused(tmp_path / str(number), _entry(options=options))
        assert "holds a value that is a credential" in message
        assert "hunter2" not in message and "AKIAIOSFODNN7" not in message


@pytest.mark.parametrize(
    "options",
    [
        {"endpoint": "https://example.com/path?page=2&sort=asc"},
        {"endpoint": "https://example.com/p#sig"},
        {"mail": "user@example.com"},
        {"label": "AKIA is a prefix", "note": "a private key is kept elsewhere"},
        {"note": "host=127.0.0.1 dbname=x", "page": "?page=2&sort=asc"},
        {"sort_key": "name", "note": "sort_key=name", "url": "https://h/p?sort_key=name"},
        {"a": "asiapacificsouthern1", "b": "Asiapacificsoutherns", "c": "accaofficialsstatement"},
        {"note": "the Bearer scheme is named in RFC 6750", "label": "bearer"},
        {"token-env": "MY_PLUGIN_TOKEN", "outer": {"api_key-env": "MY_PLUGIN_KEY"}},
    ],
)
def test_ordinary_text_and_a_variable_name_are_accepted_as_options(
    tmp_path: Path, options: Dict[str, object]
) -> None:
    resolve_all_emits(
        load_bundle(_bundle(tmp_path, _entry(options=options))), simulation_id=SIMULATION
    )


@pytest.mark.parametrize(
    "options",
    [
        {"ratio": 0.5},
        {"when": None},
        {"day": date(2026, 1, 1)},
        {"nested": {"deep": object()}},
        {1: "a"},
    ],
)
def test_an_option_that_is_not_plain_data_is_refused(options: Dict[object, object]) -> None:
    with pytest.raises(FluxError) as refused:
        read_generators_block(
            {
                "item": {
                    "plugin": "p",
                    "distribution": "d",
                    "version": "1",
                    "grade": "P5",
                    "reproducibility": "none",
                    "options": options,
                }
            },
            emits=frozenset({"item"}),
            where="w",
            code=FLX_3105,
        )
    assert refused.value.code == FLX_3105


def test_options_nested_past_the_bound_are_refused_and_do_not_overflow_the_stack() -> None:
    deep: Dict[str, object] = {}
    node = deep
    for _ in range(200):
        child: Dict[str, object] = {}
        node["a"] = child
        node = child
    with pytest.raises(FluxError, match="nests more than 64 levels"):
        read_generators_block(
            {
                "item": {
                    "plugin": "p",
                    "distribution": "d",
                    "version": "1",
                    "grade": "P5",
                    "reproducibility": "none",
                    "options": deep,
                }
            },
            emits=frozenset({"item"}),
            where="w",
            code=FLX_3105,
        )


def test_an_expose_id_that_is_not_an_emit_is_refused(tmp_path: Path) -> None:
    block = P4.replace("  item:", "  nowhere:")
    message = _refused(tmp_path, block)
    assert "does not emit" in message and "'nowhere'" in message


@pytest.mark.parametrize("block", ["generators: [a]", "generators: nope", "generators: 3"])
def test_a_block_that_is_not_a_mapping_is_refused(tmp_path: Path, block: str) -> None:
    assert "is a mapping from an emitted exposeId" in _refused(tmp_path, block)


def test_an_entry_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    assert "is a mapping declaring" in _refused(tmp_path, "generators:\n  item: learned")


def test_a_bundle_that_declares_none_keeps_its_digests(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    assert _digests(_bundle(tmp_path, name="again")) == plain
    assert _digests(_bundle(tmp_path, "generators: {}", name="empty")) == plain
    assert _digests(_bundle(tmp_path, "generators:", name="null")) == plain


def test_a_declaration_moves_h_of_its_expose_only(
    tmp_path: Path, plain: Dict[str, Tuple[str, str]]
) -> None:
    moved = _digests(_bundle(tmp_path, P4, "declared"))
    for expose_id, (h, g) in plain.items():
        if expose_id == "item":
            assert moved[expose_id][0] != h
        else:
            assert moved[expose_id] == (h, g), expose_id


def test_every_part_of_the_declaration_is_in_h(tmp_path: Path) -> None:
    base = _digests(_bundle(tmp_path, P4, "base"))["item"][0]
    seen = {base}
    for number, change in enumerate(
        (
            {"plugin": "other"},
            {"version": "1.2.1"},
            {"distribution": "other-package"},
            {"reproducibility": "bytes"},
            {"model": {"digest": "sha256:" + "cd" * 32}},
            {"privacy": _private(epsilon="4")},
            {"options": {"temperature": "2"}},
        )
    ):
        digest = _digests(_bundle(tmp_path, _entry(**change), f"change-{number}"))["item"][0]
        assert digest not in seen, change
        seen.add(digest)


def test_two_spellings_of_one_declaration_are_one_h(tmp_path: Path) -> None:
    reordered = (
        "generators:\n  item:\n    options: {nested: {enabled: true}, sizes: ['1', '2'], temperature: '1'}\n"
        "    reproducibility: environment\n    model: {digest: '" + DIGEST + "'}\n"
        "    privacy: {accounting: a Renyi accountant over the training steps, "
        "delta: '0.00001', epsilon: '3', unit: one data subject with all of its records}\n"
        "    grade: P4\n    version: '1.2.0'\n    distribution: my-package\n    plugin: learned\n"
    )
    assert _digests(_bundle(tmp_path, reordered, "reordered")) == _digests(
        _bundle(tmp_path, P4, "plain-order")
    )


def test_the_member_bound_into_h_is_the_declaration_of_that_expose_only() -> None:
    declared = (
        GeneratorDeclaration(
            expose_id="a",
            plugin="p",
            distribution="d",
            version="1",
            grade=Pattern.P5,
            reproducibility="none",
        ),
    )
    assert bound_into_h(declared, "a") == {
        "plugin": "p",
        "distribution": "d",
        "version": "1",
        "grade": "P5",
        "reproducibility": "none",
    }
    assert bound_into_h(declared, "b") is None
    assert bound_into_h((), "a") is None


def test_the_closed_set_of_namespace_keys_names_generators() -> None:
    from flux_engine.seam.resolve import _NAMESPACE_KEYS

    assert GENERATORS_KEY in _NAMESPACE_KEYS
