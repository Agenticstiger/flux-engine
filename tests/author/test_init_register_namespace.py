# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init``'s register gives every number it writes beside ``emits`` a source.

docs/spec/PARAMETER-REGISTER-SPEC.md section 2 counts the numbers of ``counts``, ``history``,
``lifetimes``, ``within``, ``rates`` and the like, and of the fidelity block's ``copula``. Pinned
here, through ``fluxsim init`` and the register a plan reads back:

* no such number is left to ``default``: each cites the statistic of the profile it was read from,
  or is assumed, for the reason init prints;
* the values of a copula cite the rank correlation of their pair, and a binned clique's numbers cite
  ``cliqueMarginal/2``;
* the bundle plans: every citation holds against the profile it was written from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from jsonschema import Draft202012Validator

from flux_engine.cli._init import run_init
from flux_engine.evidence.register import REGISTER_SCHEMA_URL_1_4, build_register_document
from flux_engine.runtime import resolve_cast
from flux_engine.seam.parameters import Source
from flux_engine.seam.resolve import load_bundle
from flux_engine.stages.pipeline import parameter_register_of
from flux_engine.stages.plan import plan
from tests.author import rank_profile as rp
from tests.author.test_history_init import CONTRACT as HISTORY_CONTRACT
from tests.author.test_history_init import _profile as history_profile
from tests.author.test_init_cliques_binned import _document as clique_document

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "init-verifies"
BESIDE_EMITS = (
    "counts",
    "history",
    "lifetimes",
    "within",
    "rates",
    "snapshots",
    "events",
    "periods",
    "conditions",
)


def _is_format_1_4_0(register: Any) -> None:
    """The record counts a new position and cites a new definition, so it is format 1-4-0."""
    document = build_register_document(register)
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL_1_4
    schema = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "spec"
            / "parameter-register-1-4.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(document)


def _register(bundle: Path) -> Any:
    loaded = load_bundle(bundle)
    cast = resolve_cast(loaded.flux_documents)
    register = parameter_register_of(loaded, cast)
    assert register is not None
    return register


def _beside_emits(register: Any) -> List[Any]:
    """The counted numbers of the Simulation under a key beside ``emits``."""
    return [
        number
        for number in register.numbers
        if number.number.document == register.simulation_id
        and len(number.number.tokens) > 2
        and number.number.tokens[2] in BESIDE_EMITS
    ]


def _write(tmp_path: Path, contract_text: str, profile: Dict[str, Any]) -> Path:
    (tmp_path / "c.fluid.yaml").write_text(contract_text, encoding="utf-8")
    (tmp_path / "p.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yaml"),
        profile=str(tmp_path / "p.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return tmp_path / "bundle"


def _planned(bundle: Path, profile: Path, state: Path) -> None:
    planned = plan(str(bundle), state_dir=str(state), profile=str(profile))
    assert planned.exit_code == 0, planned.stderr


def _by_key(register: Any) -> Dict[str, List[Tuple[str, str]]]:
    found: Dict[str, List[Tuple[str, str]]] = {}
    for number in _beside_emits(register):
        found.setdefault(number.number.tokens[2], []).append(
            (number.number.pointer, number.source.value)
        )
    return found


def test_a_history_s_numbers_cite_the_history_statistics_they_come_from(tmp_path: Path) -> None:
    bundle = _write(tmp_path, HISTORY_CONTRACT.read_text("utf-8"), history_profile())
    register = _register(bundle)
    history = _by_key(register)["history"]
    assert history
    assert {source for _, source in history} == {"profile"}
    cited = {
        entry.pointer.rsplit("/", 1)[-1]: entry.citation.pointer
        for entry in register.entries
        if "/history/0/" in entry.pointer and entry.citation is not None
    }
    assert cited["entities"] == "/tables/plan_version/history/versionsPerEntity"
    assert cited["versions"] == "/tables/plan_version/history/versionsPerEntity"
    assert cited["start"] == "/tables/plan_version/history/firstStart"
    assert cited["length"] == "/tables/plan_version/history/versionLength"
    assert cited["share"] == "/tables/plan_version/history/lastVersion"
    assert cited["initial"] == "/tables/plan_version/history/columns/tier/initial"
    assert cited["transitions"] == "/tables/plan_version/history/columns/tier/transitions"
    profile = tmp_path / "p.profile.json"
    _planned(bundle, profile, tmp_path / "state")


@pytest.mark.parametrize("name", ["jc1", "journey-d"])
def test_no_number_beside_emits_is_left_to_a_default(name: str, tmp_path: Path) -> None:
    contract = FIXTURES / name / "contract.fluid.yaml"
    profile = FIXTURES / name / "profile.json"
    result = run_init(str(tmp_path / "bundle"), contract=str(contract), profile=str(profile))
    assert result.exit_code == 0, result.stderr
    register = _register(tmp_path / "bundle")
    found = _by_key(register)
    assert found, "the fixture declares keys beside emits"
    defaults = [
        pointer for pairs in found.values() for pointer, source in pairs if source == "default"
    ]
    assert defaults == []
    # Each of the date rules' entries names the statistic its numbers were read from.
    for key in ("lifetimes", "within", "rates", "counts"):
        for pointer, source in found.get(key, []):
            assert source in ("profile", "assumption"), pointer
    _planned(tmp_path / "bundle", profile, tmp_path / "state")


def test_the_values_of_a_copula_cite_the_rank_correlation_of_their_pair(tmp_path: Path) -> None:
    data = rp.rows(6000)
    profile = rp.profile(data, None, rows=20000)
    bundle = _write(tmp_path, rp.CONTRACT, profile)
    register = _register(bundle)
    values = [
        entry
        for entry in register.entries
        if "/copula/rankCorrelations/pairs/" in entry.pointer and entry.pointer.endswith("/value")
    ]
    assert values
    for entry in values:
        assert entry.source is Source.PROFILE
        assert entry.citation is not None
        assert entry.citation.definition in ("kendallTau/1", "spearmanRho/1")
    counted = {
        number.number.pointer: number.source
        for number in register.numbers
        if "/copula/" in number.number.pointer
    }
    assert counted and set(counted.values()) == {Source.PROFILE}
    _is_format_1_4_0(register)
    _planned(bundle, tmp_path / "p.profile.json", tmp_path / "state")


def test_the_numbers_a_binned_clique_gives_cite_cliquemarginal_2(tmp_path: Path) -> None:
    data = rp.rows(6000)
    document = clique_document(data, ["balance", "region"])
    bundle = _write(tmp_path, rp.CONTRACT, document)
    register = _register(bundle)
    cited = [
        entry
        for entry in register.entries
        if entry.citation is not None and entry.citation.definition == "cliqueMarginal/2"
    ]
    assert cited
    assert all(entry.source is Source.PROFILE for entry in cited)
    _is_format_1_4_0(register)
    _planned(bundle, tmp_path / "p.profile.json", tmp_path / "state")
