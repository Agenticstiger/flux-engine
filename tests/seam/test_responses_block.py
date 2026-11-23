# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``responses`` and ``forecasts`` blocks a Simulation declares (ADR 0029 sections 2 and 3).

Pinned here: each accepted shape on the ``outcomes`` fixture, each refusal and each bound of the
two blocks, that kernel 0.1.0 refuses them with ``FLX-3105`` naming the kernel, and that
declaring them moves neither ``H`` nor ``G`` of any emit and changes no byte a run writes.
"""

from __future__ import annotations

import shutil
import timeit
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.seam import consumers
from flux_engine.seam.resolve import (
    declared_responses,
    load_bundle,
    resolve_all_emits,
)
from flux_engine.seam.responses import (
    REACTION_DIAL,
    check_responses,
    least_sweep_runs,
    read_forecasts_block,
    read_responses_block,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "outcomes"
BUNDLE = FIXTURE / "bundle"
NAMESPACE = "com.agentics.flux/1"
SIMULATION = "sim-telco.silver.subscriptions"

Edit = Callable[[Dict[str, Any]], None]


def _family(**changes: Any) -> Dict[str, Any]:
    family: Dict[str, Any] = {
        "id": "offer-by-plan",
        "treatment": "tr-retention-offer",
        "experiment": "exp-offer-test",
        "journey": "jr-renewal",
        "segments": {"basic": "seg-basic", "premium": "seg-premium"},
        "estimate": {
            "treated": "renewed-offered",
            "control": "renewed-not-offered",
            "confidence": "0.9",
        },
    }
    family.update(changes)
    return family


def _item(**changes: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "id": "premium-offered",
        "response": "offer-by-plan",
        "arm": "treated",
        "segment": "premium",
    }
    item.update(changes)
    return item


def _bundle(
    tmp_path: Path,
    name: str,
    *,
    simulation: Optional[Edit] = None,
    documents: Optional[Dict[str, Edit]] = None,
) -> Path:
    """A copy of the fixture bundle with *simulation* applied to its namespace and spec."""
    bundle = tmp_path / name
    shutil.copytree(BUNDLE, bundle)
    for filename, edit in {"simulation.flux.yml": simulation, **(documents or {})}.items():
        if edit is None:
            continue
        path = bundle / filename
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        edit(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def _namespace(document: Dict[str, Any]) -> Dict[str, Any]:
    namespace: Dict[str, Any] = document["extensions"][NAMESPACE]
    return namespace


def _resolve(bundle: Path) -> None:
    resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION)


def _refused(
    tmp_path: Path,
    name: str,
    code: str,
    *,
    mention: str,
    simulation: Optional[Edit] = None,
    documents: Optional[Dict[str, Edit]] = None,
) -> None:
    bundle = _bundle(tmp_path, name, simulation=simulation, documents=documents)
    with pytest.raises(FluxError) as caught:
        _resolve(bundle)
    assert caught.value.code.code == code, str(caught.value)
    assert mention in str(caught.value)


def _responses(*families: Dict[str, Any]) -> Edit:
    def edit(document: Dict[str, Any]) -> None:
        _namespace(document)["responses"] = list(families)

    return edit


def _forecasts(block: Any, *, responses: Any = ...) -> Edit:
    def edit(document: Dict[str, Any]) -> None:
        namespace = _namespace(document)
        namespace["forecasts"] = block
        if responses is None:
            del namespace["responses"]
        elif responses is not ...:
            namespace["responses"] = responses

    return edit


def _read_responses(block: Any) -> Any:
    return read_responses_block(block, where="responses")


def _read_forecasts(block: Any) -> Any:
    return read_forecasts_block(block, where="forecasts")


def _shape_refused(read: Callable[[Any], Any], block: Any, code: str, mention: str = "") -> None:
    with pytest.raises(FluxError) as caught:
        read(block)
    assert caught.value.code.code == code, str(caught.value)
    assert mention in str(caught.value)


# ------------------------------------------------------------------ accepted shapes


def test_the_fixture_blocks_read_in_full() -> None:
    simulation = yaml.safe_load((BUNDLE / "simulation.flux.yml").read_text(encoding="utf-8"))
    families, forecasts = declared_responses(simulation, where="sim")
    assert [family.id for family in families] == ["offer-by-plan"]
    family = families[0]
    assert (family.treatment, family.experiment, family.journey) == (
        "tr-retention-offer",
        "exp-offer-test",
        "jr-renewal",
    )
    assert family.segments == (("basic", "seg-basic"), ("premium", "seg-premium"))
    assert family.estimate.treated == "renewed-offered"
    assert family.estimate.control == "renewed-not-offered"
    assert family.estimate.confidence == Fraction(9, 10)
    assert forecasts is not None
    assert forecasts.playback == "pb-renewal"
    assert forecasts.confidence == Fraction(9, 10)
    assert [(i.id, i.arm, i.segment, i.basis) for i in forecasts.items] == [
        ("premium-offered", "treated", "premium", "historical-holdout"),
        ("basic-control", "control", "basic", "historical-holdout"),
    ]


def test_a_simulation_that_declares_neither_key_reads_none() -> None:
    assert declared_responses({"spec": {}}, where="sim") == ((), None)
    assert declared_responses({"extensions": {NAMESPACE: {"responses": None}}}) == ((), None)


def test_the_fixture_resolves_under_the_namespace(tmp_path: Path) -> None:
    _resolve(_bundle(tmp_path, "ok"))


def test_defaults_are_the_cautious_ones() -> None:
    forecasts = _read_forecasts({"items": [_item()]})
    assert forecasts.playback is None
    assert (forecasts.confidence, forecasts.declared_confidence) == (Fraction(9, 10), "0.9")
    assert forecasts.items[0].basis == "historical-holdout"
    live = _read_forecasts({"items": [_item(basis="live-ab")], "confidence": "0.95"})
    assert live.items[0].basis == "live-ab" and live.confidence == Fraction(19, 20)


def test_a_forecasts_block_without_a_playback_resolves(tmp_path: Path) -> None:
    def edit(document: Dict[str, Any]) -> None:
        del _namespace(document)["forecasts"]["playback"]

    _resolve(_bundle(tmp_path, "no-playback", simulation=edit))


def test_a_sweep_of_exactly_the_least_runs_is_enough() -> None:
    assert least_sweep_runs(Fraction(9, 10)) == 20
    assert least_sweep_runs(Fraction(95, 100)) == 40
    assert least_sweep_runs(Fraction(1, 2)) == 4
    assert least_sweep_runs(Fraction(99, 100)) == 200


def _least_by_loop(confidence: Fraction) -> int:
    """The definition, searched one run at a time: the oracle for the closed form."""
    runs = 2
    while (Fraction(runs) * (1 - confidence) / 2) // 1 < 1:
        runs += 1
    return runs


def test_the_closed_form_agrees_with_the_search_for_every_confidence_in_steps() -> None:
    for step in range(100, 200):  # 0.5, 0.505, ..., 0.995
        confidence = Fraction(step * 5, 1000)
        assert least_sweep_runs(confidence) == _least_by_loop(confidence), confidence
    for confidence in (Fraction(1, 3), Fraction(2, 3), Fraction(1, 7), Fraction(999, 1000)):
        assert least_sweep_runs(confidence) == _least_by_loop(confidence), confidence


def test_twenty_runs_are_still_the_least_at_nine_tenths() -> None:
    assert least_sweep_runs(Fraction(9, 10)) == 20
    assert _least_by_loop(Fraction(9, 10)) == 20
    assert Fraction(19) * (1 - Fraction(9, 10)) / 2 < 1 <= Fraction(20) * (1 - Fraction(9, 10)) / 2


def test_a_confidence_that_needs_more_than_100000_runs_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        least_sweep_runs(Fraction("0.999999999999"), at="sim: forecasts.confidence")
    assert caught.value.code.code == "FLX-3674"
    assert "sim: forecasts.confidence" in str(caught.value)
    assert least_sweep_runs(Fraction("0.99998")) == 100_000
    with pytest.raises(FluxError):
        least_sweep_runs(Fraction("0.999981"))


def test_the_least_runs_do_not_loop_over_the_run_count() -> None:
    # The loop this replaced is about 5,000 times slower at the most accepted confidence.
    most, few = Fraction("0.99998"), Fraction(9, 10)
    slow = min(timeit.repeat(lambda: least_sweep_runs(most), repeat=5, number=200))
    fast = min(timeit.repeat(lambda: least_sweep_runs(few), repeat=5, number=200))
    assert least_sweep_runs(most) == 100_000 and least_sweep_runs(few) == 20
    assert slow < 20 * fast, (slow, fast)


def test_no_number_of_runs_gives_a_confidence_of_one_or_more() -> None:
    for confidence in (Fraction(1), Fraction(3, 2)):
        with pytest.raises(FluxError) as caught:
            least_sweep_runs(confidence)
        assert caught.value.code.code == "FLX-3674"


def test_the_forecasts_block_refuses_a_confidence_that_needs_too_many_runs_when_it_is_read() -> (
    None
):
    with pytest.raises(FluxError) as caught:
        read_forecasts_block({"confidence": "0.9999999", "items": []}, where="sim: forecasts")
    assert caught.value.code.code == "FLX-3674"
    assert "sim: forecasts.confidence" in str(caught.value)


def test_the_dial_name_and_the_id_grammar_are_the_runtime_and_registry_ones() -> None:
    from flux_engine.runtime.journey import REACTION_DIAL as RUNTIME_DIAL
    from flux_engine.seam import responses

    assert REACTION_DIAL == RUNTIME_DIAL
    assert responses._ID.pattern == consumers._ID.pattern


def test_a_temperature_dial_other_than_the_reaction_dial_is_allowed(
    tmp_path: Path,
) -> None:
    def edit(document: Dict[str, Any]) -> None:
        document["spec"]["temperature"] = {"engagement": 0.5}

    _resolve(_bundle(tmp_path, "engagement", documents={"world.flux.yml": edit}))


# ------------------------------------------------------------------ responses: shape


def test_the_responses_block_is_a_list_of_one_to_256_families() -> None:
    _shape_refused(_read_responses, {}, "FLX-3660")
    _shape_refused(_read_responses, "offer-by-plan", "FLX-3660")
    _shape_refused(_read_responses, [], "FLX-3660", "1 to 256")
    _read_responses([_family(id=f"f{index}") for index in range(256)])
    _shape_refused(
        _read_responses, [_family(id=f"f{index}") for index in range(257)], "FLX-3660", "256"
    )


def test_a_family_carries_its_keys_and_nothing_else() -> None:
    _shape_refused(_read_responses, [_family(speed="fast")], "FLX-3660", "speed")
    _shape_refused(_read_responses, ["offer-by-plan"], "FLX-3660")
    for key in ("treatment", "experiment", "journey"):
        family = _family()
        del family[key]
        _shape_refused(_read_responses, [family], "FLX-3660", key)
        _shape_refused(_read_responses, [_family(**{key: ""})], "FLX-3660", key)


def test_family_ids_follow_the_registry_grammar_and_are_unique() -> None:
    for bad in ("Offer", "1offer", "offer by plan", "", None, 7, "a" * 129):
        _shape_refused(_read_responses, [_family(id=bad)], "FLX-3661")
    _read_responses([_family(id="a" * 128)])
    _shape_refused(_read_responses, [_family(), _family()], "FLX-3661", "'offer-by-plan' twice")


def test_a_family_declares_one_to_1024_segments_keyed_by_level_text() -> None:
    _shape_refused(_read_responses, [_family(segments={})], "FLX-3663", "offer-by-plan")
    _shape_refused(_read_responses, [_family(segments=["seg-basic"])], "FLX-3663")
    _shape_refused(_read_responses, [_family(segments={1: "seg-basic"})], "FLX-3663", "1")
    _shape_refused(_read_responses, [_family(segments={"": "seg-basic"})], "FLX-3663")
    _shape_refused(_read_responses, [_family(segments={"basic": ""})], "FLX-3663", "basic")
    _shape_refused(_read_responses, [_family(segments={"basic": 4})], "FLX-3663", "basic")
    many = {f"level-{index}": "seg-basic" for index in range(1024)}
    assert len(_read_responses([_family(segments=many)])[0].segments) == 1024
    many["one-more"] = "seg-basic"
    _shape_refused(_read_responses, [_family(segments=many)], "FLX-3663", "1024")


def test_an_estimate_names_two_queries_and_an_exact_confidence_strictly_inside_0_and_1() -> None:
    def estimate(**changes: Any) -> Dict[str, Any]:
        return {**_family()["estimate"], **changes}

    for bad in ("0", "1", "0.0", "1.0", "-0.5", "1.5", 0.9, True, 0, 1, None, "ninety", "9e-1"):
        _shape_refused(
            _read_responses,
            [_family(estimate=estimate(confidence=bad))],
            "FLX-3664",
            "offer-by-plan",
        )
    _shape_refused(_read_responses, [_family(estimate={})], "FLX-3664")
    _shape_refused(_read_responses, [_family(estimate=estimate(extra=1))], "FLX-3664", "extra")
    _shape_refused(_read_responses, [_family(estimate=estimate(treated=""))], "FLX-3664", "treated")
    read = _read_responses([_family(estimate=estimate(confidence="0.999"))])
    assert read[0].estimate.confidence == Fraction(999, 1000)


# ------------------------------------------------------------------ responses: the bundle


def test_a_family_names_a_treatment_of_the_bundle(tmp_path: Path) -> None:
    _refused(
        tmp_path,
        "missing",
        "FLX-3662",
        mention="offer-by-plan",
        simulation=_responses(_family(treatment="tr-nothing")),
    )
    _refused(
        tmp_path,
        "wrong-kind",
        "FLX-3662",
        mention="not a Treatment",
        simulation=_responses(_family(treatment="jr-renewal")),
    )


def test_a_family_names_an_experiment_with_a_variant_of_its_treatment(tmp_path: Path) -> None:
    _refused(
        tmp_path,
        "no-experiment",
        "FLX-3662",
        mention="not a Experiment",
        simulation=_responses(_family(experiment="tr-no-offer")),
    )

    def rename(document: Dict[str, Any]) -> None:
        for variant in document["spec"]["variants"]:
            if variant["treatmentRef"] == "tr-retention-offer":
                variant["treatmentRef"] = "tr-no-offer"

    _refused(
        tmp_path,
        "no-variant",
        "FLX-3662",
        mention="no variant whose treatmentRef is 'tr-retention-offer'",
        documents={"experiment.flux.yml": rename},
    )


def test_a_family_names_a_journey_and_segments_of_the_bundle(tmp_path: Path) -> None:
    _refused(
        tmp_path,
        "journey",
        "FLX-3662",
        mention="not a Journey",
        simulation=_responses(_family(journey="seg-basic")),
    )
    _refused(
        tmp_path,
        "segment",
        "FLX-3662",
        mention="segments[premium]",
        simulation=_responses(_family(segments={"basic": "seg-basic", "premium": "seg-gone"})),
    )
    _refused(
        tmp_path,
        "segment-kind",
        "FLX-3662",
        mention="not a Segment",
        simulation=_responses(_family(segments={"basic": "persona-basic"})),
    )


def _queries(*extra: Dict[str, Any]) -> Edit:
    def edit(document: Dict[str, Any]) -> None:
        _namespace(document)["consumers"]["queries"].extend(extra)

    return edit


def _both(first: Edit, second: Edit) -> Edit:
    def edit(document: Dict[str, Any]) -> None:
        first(document)
        second(document)

    return edit


def test_an_estimate_names_grouped_calibration_queries_of_the_registry(tmp_path: Path) -> None:
    def estimate(**changes: Any) -> Edit:
        return _responses(_family(estimate={**_family()["estimate"], **changes}))

    _refused(
        tmp_path,
        "unknown",
        "FLX-3664",
        mention="'renewed-gone'",
        simulation=estimate(treated="renewed-gone"),
    )
    held_out = {
        "id": "renewed-held",
        "sql": "SELECT plan FROM offered WHERE renewed",
        "unit": "offered",
        "groupBy": "plan",
        "heldOut": True,
    }
    _refused(
        tmp_path,
        "held-out",
        "FLX-3664",
        mention="heldOut",
        simulation=_both(_queries(held_out), estimate(control="renewed-held")),
    )
    ungrouped = {
        "id": "renewed-all",
        "sql": "SELECT * FROM subscriptions WHERE renewed",
        "unit": "subscriptions",
    }
    _refused(
        tmp_path,
        "ungrouped",
        "FLX-3664",
        mention="no groupBy",
        simulation=_both(_queries(ungrouped), estimate(treated="renewed-all")),
    )
    other_column = {
        "id": "renewed-by-region",
        "sql": "SELECT region FROM not_offered WHERE renewed",
        "unit": "not_offered",
        "groupBy": "region",
    }
    _refused(
        tmp_path,
        "columns",
        "FLX-3664",
        mention="'plan' and 'region'",
        simulation=_both(_queries(other_column), estimate(control="renewed-by-region")),
    )


def test_a_family_without_a_consumers_registry_has_no_query_to_name(tmp_path: Path) -> None:
    def drop(document: Dict[str, Any]) -> None:
        del _namespace(document)["consumers"]

    _refused(
        tmp_path, "no-registry", "FLX-3664", mention="not a query of the consumers", simulation=drop
    )


def test_a_family_on_a_journey_the_world_dials_is_refused(tmp_path: Path) -> None:
    for name, value in (("half", 0.5), ("zero", 0)):

        def dial(document: Dict[str, Any], value: Any = value) -> None:
            document["spec"]["temperature"] = {REACTION_DIAL: value}

        _refused(
            tmp_path,
            name,
            "FLX-3665",
            mention="offer-by-plan",
            documents={"world.flux.yml": dial},
        )


def test_the_responses_check_needs_no_forecasts_and_forecasts_need_it(tmp_path: Path) -> None:
    def drop(document: Dict[str, Any]) -> None:
        del _namespace(document)["forecasts"]

    _resolve(_bundle(tmp_path, "responses-only", simulation=drop))
    _refused(
        tmp_path,
        "forecasts-only",
        "FLX-3666",
        mention="declares no family",
        simulation=_forecasts({"items": [_item()]}, responses=None),
    )


# ------------------------------------------------------------------ forecasts: shape


def test_the_forecasts_block_is_a_mapping_of_its_keys() -> None:
    _shape_refused(_read_forecasts, [], "FLX-3666")
    _shape_refused(_read_forecasts, {"items": [], "horizon": 3}, "FLX-3666", "horizon")
    _shape_refused(_read_forecasts, {}, "FLX-3666", "items")
    _shape_refused(_read_forecasts, {"items": {}}, "FLX-3666", "items")
    _read_forecasts({"items": [_item(id=f"i{index}") for index in range(1024)]})
    _shape_refused(
        _read_forecasts,
        {"items": [_item(id=f"i{index}") for index in range(1025)]},
        "FLX-3666",
        "1024",
    )


def test_item_ids_follow_the_grammar_and_are_unique() -> None:
    for bad in ("Premium", "", None, "x y"):
        _shape_refused(_read_forecasts, {"items": [_item(id=bad)]}, "FLX-3667")
    _shape_refused(
        _read_forecasts, {"items": [_item(), _item()]}, "FLX-3667", "'premium-offered' twice"
    )


def test_an_item_has_an_arm_a_basis_and_names() -> None:
    for bad in ("treat", "Treated", "", None, 1):
        _shape_refused(_read_forecasts, {"items": [_item(arm=bad)]}, "FLX-3668", "premium-offered")
    for bad in ("holdout", "none", "", None, True):
        _shape_refused(
            _read_forecasts, {"items": [_item(basis=bad)]}, "FLX-3668", "premium-offered"
        )
    _shape_refused(_read_forecasts, {"items": [_item(extra=1)]}, "FLX-3668", "extra")
    _shape_refused(_read_forecasts, {"items": ["premium-offered"]}, "FLX-3668")
    for key in ("response", "segment"):
        _shape_refused(_read_forecasts, {"items": [_item(**{key: ""})]}, "FLX-3668", key)


def test_forecasts_confidence_and_playback_are_usable() -> None:
    for bad in ("0", "1", "1.5", 0.9, 1, "", None, True):
        _shape_refused(_read_forecasts, {"items": [], "confidence": bad}, "FLX-3669")
    for bad in ("", None, 3):
        _shape_refused(_read_forecasts, {"items": [], "playback": bad}, "FLX-3669")


# ------------------------------------------------------------------ forecasts: the bundle


def test_an_item_names_a_family_and_one_of_its_segments(tmp_path: Path) -> None:
    _refused(
        tmp_path,
        "response",
        "FLX-3668",
        mention="premium-offered",
        simulation=_forecasts({"items": [_item(response="nothing")]}),
    )
    _refused(
        tmp_path,
        "segment",
        "FLX-3668",
        mention="['basic', 'premium']",
        simulation=_forecasts({"items": [_item(segment="standard")]}),
    )


def test_a_playback_it_names_is_a_playback_of_the_bundle(tmp_path: Path) -> None:
    _refused(
        tmp_path,
        "gone",
        "FLX-3669",
        mention="'pb-gone'",
        simulation=_forecasts({"playback": "pb-gone", "items": [_item()]}),
    )
    _refused(
        tmp_path,
        "kind",
        "FLX-3669",
        mention="not a Playback",
        simulation=_forecasts({"playback": "exp-offer-test", "items": [_item()]}),
    )


def test_the_sweep_is_long_enough_for_the_interval(tmp_path: Path) -> None:
    def runs(count: Optional[int], confidence: Optional[str] = None) -> Edit:
        def edit(document: Dict[str, Any]) -> None:
            if count is None:
                del document["spec"]["sweep"]
            else:
                document["spec"]["sweep"]["runs"] = count
            if confidence is not None:
                _namespace(document)["forecasts"]["confidence"] = confidence

        return edit

    _resolve(_bundle(tmp_path, "twenty", simulation=runs(20)))
    _resolve(_bundle(tmp_path, "many", simulation=runs(500)))
    _refused(tmp_path, "nineteen", "FLX-3670", mention="at least 20 runs", simulation=runs(19))
    _refused(tmp_path, "none", "FLX-3670", mention="spec.sweep.runs is None", simulation=runs(None))
    _refused(
        tmp_path, "tighter", "FLX-3670", mention="at least 40 runs", simulation=runs(20, "0.95")
    )
    _resolve(_bundle(tmp_path, "tighter-ok", simulation=runs(40, "0.95")))
    _resolve(_bundle(tmp_path, "looser", simulation=runs(4, "0.5")))


def test_a_confidence_near_one_is_refused_when_the_bundle_is_resolved(
    tmp_path: Path,
) -> None:
    def edit(document: Dict[str, Any]) -> None:
        _namespace(document)["forecasts"]["confidence"] = "0.9999999"

    _refused(tmp_path, "near-one", "FLX-3674", mention="forecasts.confidence", simulation=edit)


def test_check_responses_reads_the_documents_it_is_given() -> None:
    loaded = load_bundle(BUNDLE)
    simulation = loaded.simulation(SIMULATION)
    families, forecasts = declared_responses(simulation, where="t")
    registry = consumers.read_consumers_block(
        _namespace(dict(simulation))["consumers"], emits=frozenset({"subscriptions"}), where="t"
    )
    check_responses(
        families,
        forecasts,
        documents=loaded.flux_documents,
        registry=registry,
        simulation=simulation,
        where="t",
    )
    with pytest.raises(FluxError) as caught:
        check_responses(
            families,
            forecasts,
            documents={},
            registry=registry,
            simulation=simulation,
            where="t",
        )
    assert caught.value.code.code == "FLX-3662"


# ------------------------------------------------------------------ kernel 0.1.0


def test_kernel_0_1_0_refuses_either_key_and_names_the_kernel() -> None:
    from flux_engine.stages.pipeline import check_responses_declared

    simulation = yaml.safe_load((BUNDLE / "simulation.flux.yml").read_text(encoding="utf-8"))
    check_responses_declared(simulation, where="sim", counter_based=True)
    for key in ("responses", "forecasts"):
        only = yaml.safe_load((BUNDLE / "simulation.flux.yml").read_text(encoding="utf-8"))
        for other in {"responses", "forecasts"} - {key}:
            del _namespace(only)[other]
        with pytest.raises(FluxError) as caught:
            check_responses_declared(only, where="sim", counter_based=False)
        assert caught.value.code.code == "FLX-3105"
        assert key in str(caught.value) and "kernel 0.1.0" in str(caught.value)
    plain = yaml.safe_load((BUNDLE / "simulation.flux.yml").read_text(encoding="utf-8"))
    for key in ("responses", "forecasts"):
        del _namespace(plain)[key]
    check_responses_declared(plain, where="sim", counter_based=False)


@pytest.mark.integration
def test_plan_on_kernel_0_1_0_refuses_the_blocks(tmp_path: Path) -> None:
    from flux_engine.stages.plan import plan

    profile = FIXTURE / "subscriptions.profile.json"
    refused = plan(
        _bundle(tmp_path, "old"), state_dir=tmp_path / "s1", profile=profile, kernel="0.1.0"
    )
    assert not refused.success
    assert "FLX-3105" in refused.stderr and "kernel 0.1.0" in refused.stderr
    assert "responses" in refused.stderr
    assert plan(_bundle(tmp_path, "new"), state_dir=tmp_path / "s2", profile=profile).success


# ------------------------------------------------------------------ digests


def _without_keys(document: Dict[str, Any]) -> None:
    for key in ("responses", "forecasts"):
        del _namespace(document)[key]


def _digests(bundle: Path) -> Dict[str, Any]:
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


def test_declaring_the_keys_moves_neither_h_nor_g(tmp_path: Path) -> None:
    with_keys = _digests(_bundle(tmp_path, "with"))
    without = _digests(_bundle(tmp_path, "without", simulation=_without_keys))
    assert with_keys == without and set(with_keys) == {"subscriptions"}


@pytest.mark.integration
def test_the_keys_enter_the_input_digest_and_change_no_row(tmp_path: Path) -> None:
    from flux_engine.stages.apply import apply
    from flux_engine.stages.layout import RunLayout
    from flux_engine.stages.plan import plan

    profile = FIXTURE / "subscriptions.profile.json"
    written = {}
    input_digests = {}
    for name, edit in (("with", None), ("without", _without_keys)):
        bundle = _bundle(tmp_path, name, simulation=edit)
        state = tmp_path / f"{name}-state"
        planned = plan(bundle, state_dir=state, profile=profile)
        assert planned.success, planned.stderr
        run_id = planned.artifacts["run_id"]
        applied = apply(bundle, run_id=run_id, state_dir=state, auto_plan=False)
        assert applied.success, applied.stderr
        written[name] = (
            RunLayout.for_state_dir(state, run_id).emit_path("subscriptions").read_bytes()
        )
        input_digests[name] = load_bundle(bundle).digest_inputs().documents["simulation.flux.yml"]
    assert written["with"] and written["with"] == written["without"]
    assert input_digests["with"] != input_digests["without"]
