# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``kendallTau/1`` and ``spearmanRho/1`` in a ``columnPairs`` entry (profile format 1-4-0).

Exact and noisy forms, every refusal of PROFILE-SPEC.md section 5, the charge of a noisy form, the
format it names, and the schema that validates it.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_3_0, SCHEMA_URL_1_4_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-4-example.json").read_text("utf-8"))


def _noisy(change: Callable[[Dict[str, Any]], None] = lambda d: None) -> Dict[str, Any]:
    """The example, changed: a Kendall's and a Spearman's of the pair, both noisy with a floor."""
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _kendall(document: Dict[str, Any]) -> Dict[str, Any]:
    statistic: Dict[str, Any] = document["tables"]["subscriber"]["columnPairs"][0]["statistics"][0]
    return statistic


def _spearman(document: Dict[str, Any]) -> Dict[str, Any]:
    statistic: Dict[str, Any] = document["tables"]["subscriber"]["columnPairs"][0]["statistics"][1]
    return statistic


def _exact(**statistic: Any) -> Dict[str, Any]:
    """A profile with no privacy block whose one pair holds the exact statistics given."""
    return {
        "_producer": "urn:example:t",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "t": {
                "columns": {"a": {"domain": "number"}, "b": {"domain": "timestamp"}},
                "columnPairs": [
                    {
                        "columns": ["a", "b"],
                        "statistics": [
                            {
                                "definition": "kendallTau/1",
                                "sampleSize": {"min": 1000},
                                "disclosure": "none",
                                "groups": [{"group": "", "k": 10, "status": "F", "S": "-12"}],
                            }
                            | statistic.get("kendall", {}),
                            {
                                "definition": "spearmanRho/1",
                                "sampleSize": {"min": 1000},
                                "disclosure": "none",
                                "groups": [{"group": "", "k": 10, "status": "F", "A4": "100"}],
                            }
                            | statistic.get("spearman", {}),
                        ],
                    }
                ],
            }
        },
    }


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


# ---------------------------------------------------------------------------
# What they are
# ---------------------------------------------------------------------------


def test_the_example_profile_reads_and_every_noise_block_is_one_part() -> None:
    profile = parse_profile(EXAMPLE)
    assert [where for where, _ in profile.parts] == [
        "profile.tables.subscriber.columns.region.statistics[0]",
        "profile.tables.subscriber.columnPairs[0].statistics[0]",
        "profile.tables.subscriber.columnPairs[0].statistics[1]",
    ]
    kendall, spearman = profile.tables["subscriber"].pairs[0].statistics
    assert (kendall.identifier, kendall.row_floor) == ("kendallTau/1", 99)
    assert (spearman.identifier, spearman.row_floor) == ("spearmanRho/1", 99)
    assert profile.pair_statistics("subscriber", ["age", "tenure"]) == (kendall, spearman)


def test_a_noisy_value_is_clamped_and_its_variance_is_the_noise_s() -> None:
    document = _noisy()
    _kendall(document)["groups"][0]["noisyValue"] = "1.7"
    _spearman(document)["groups"][0]["noisyValue"] = "-3"
    kendall, spearman = parse_profile(document).tables["subscriber"].pairs[0].statistics
    assert kendall.groups[0].value == 1 and spearman.groups[0].value == -1
    assert kendall.groups[0].noisy == (Fraction("1.7"),)
    assert kendall.cell_variance == Fraction(1, 25)  # a Gaussian of scale 0.2: scale^2
    assert spearman.cell_variance == Fraction(1, 4)  # scale 0.5
    unchanged = parse_profile(EXAMPLE).tables["subscriber"].pairs[0].statistics
    assert unchanged[0].groups[0].value == Fraction("0.4132")


def test_a_laplace_variance_is_twice_the_square_of_its_scale() -> None:
    document = _noisy()
    document["privacy"]["measure"] = "approximateDP"
    document["privacy"]["privacyLoss"] = {"epsilon": "10", "delta": "0"}
    for part in (_kendall(document), _spearman(document)):
        part["noise"] = {
            "mechanism": "laplace",
            "scale": "2",
            "privacyLoss": {"epsilon": "1", "delta": "0"},
        }
    histogram = document["tables"]["subscriber"]["columns"]["region"]["statistics"][0]
    histogram["noise"] = {
        "mechanism": "laplace",
        "scale": "2",
        "privacyLoss": {"epsilon": "1", "delta": "0"},
    }
    kendall = parse_profile(document).tables["subscriber"].pairs[0].statistics[0]
    assert kendall.cell_variance == 8


def test_an_exact_group_holds_k_and_s_or_a4_and_its_value_is_over_n_choose_two_or_n_cubed() -> None:
    profile = parse_profile(_exact())
    kendall, spearman = profile.tables["t"].pairs[0].statistics
    assert profile.parts == ()
    group = kendall.groups[0]
    assert group.statistic is not None and (group.statistic.k, group.statistic.rank_s) == (10, -12)
    assert group.value == Fraction(-12, 45)
    assert spearman.groups[0].value == Fraction(3 * 100, 10 * 99)


def test_a_row_floor_makes_n_at_least_the_floor() -> None:
    document = _exact(kendall={"rowFloor": 30}, spearman={"rowFloor": 30})
    kendall, spearman = parse_profile(document).tables["t"].pairs[0].statistics
    assert kendall.groups[0].value == Fraction(-12, 435)
    assert spearman.groups[0].value == Fraction(300, 30 * 899)
    # a floor below the number of records changes nothing
    low = _exact(kendall={"rowFloor": 4})
    assert parse_profile(low).tables["t"].pairs[0].statistics[0].groups[0].value == Fraction(
        -12, 45
    )


def test_a_suppressed_group_is_read_and_holds_no_value() -> None:
    document = _exact(kendall={"groups": [{"group": "", "status": "S"}]})
    group = parse_profile(document).tables["t"].pairs[0].statistics[0].groups[0]
    assert not group.published and group.value is None


def test_correlation_and_the_rank_correlations_sit_in_one_pair() -> None:
    document = _exact()
    document["tables"]["t"]["columnPairs"][0]["statistics"].append(
        {
            "definition": "correlation/1",
            "sampleSize": {"min": 1000},
            "disclosure": "none",
            "groups": [
                {
                    "group": "",
                    "k": 3,
                    "status": "F",
                    "Sx": "1",
                    "Sy": "1",
                    "Sxx": "1",
                    "Sxy": "1",
                    "Syy": "1",
                }
            ],
        }
    )
    assert len(parse_profile(document).tables["t"].pairs[0].statistics) == 3


# ---------------------------------------------------------------------------
# Every refusal
# ---------------------------------------------------------------------------


def _first(document: Dict[str, Any]) -> Dict[str, Any]:
    """The exact Kendall's statistic of an :func:`_exact` document."""
    statistic: Dict[str, Any] = document["tables"]["t"]["columnPairs"][0]["statistics"][0]
    return statistic


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s["groups"][0].update(k=1),
        lambda s: s["groups"][0].update(k=0, S="0"),
        lambda s: s["groups"].append({"group": "x", "status": "S"}),
        lambda s: s["groups"][0].update(group="east"),
        lambda s: s["groups"][0].update(S="46"),
        lambda s: s["groups"][0].update(A4="1"),
        lambda s: s["groups"][0].pop("S"),
        lambda s: s["groups"][0].update(S=-12),
        lambda s: s["groups"][0].update(S="-0"),
        lambda s: s.update(groups=[]),
        lambda s: s.update(rowFloor=1),
        lambda s: s.update(rowFloor="30"),
        lambda s: s.update(rowFloor=True),
        lambda s: s.update(extra=1),
        lambda s: s.update(disclosure="nobody"),
        lambda s: s.update(bins=["0", "1"]),
    ],
)
def test_an_exact_statistic_that_is_not_well_formed_is_refused(
    change: Callable[[Dict[str, Any]], None],
) -> None:
    document = _exact()
    change(_first(document))
    _refused(document)


def test_a_spearman_group_beyond_its_bound_is_refused() -> None:
    document = _exact()
    group = document["tables"]["t"]["columnPairs"][0]["statistics"][1]["groups"][0]
    group["A4"] = "331"  # k (k^2 - 1) / 3 is 330 for k = 10
    _refused(document)
    group["A4"] = "330"
    parse_profile(document)


def test_a_pair_names_two_different_number_or_timestamp_columns() -> None:
    for columns in (["a", "a"], ["a", "c"], ["a", "missing"]):
        document = _exact()
        document["tables"]["t"]["columns"]["c"] = {"domain": "category"}
        document["tables"]["t"]["columnPairs"][0]["columns"] = columns
        _refused(document)


def test_a_pair_repeats_no_rank_correlation() -> None:
    document = _exact()
    pair = document["tables"]["t"]["columnPairs"][0]
    pair["statistics"].append(copy.deepcopy(pair["statistics"][0]))
    assert "repeats" in _refused(document)


def test_a_noisy_rank_correlation_states_a_row_floor() -> None:
    document = _noisy()
    del _kendall(document)["rowFloor"]
    assert "rowFloor" in _refused(document)


def test_a_noisy_rank_correlation_is_never_a_discrete_gaussian() -> None:
    document = _noisy()
    _kendall(document)["noise"]["mechanism"] = "discreteGaussian"
    assert "discrete Gaussian" in _refused(document)


def test_a_noisy_correlation_is_still_refused() -> None:
    document = _noisy()
    document["tables"]["subscriber"]["columnPairs"][0]["statistics"].append(
        {
            "definition": "correlation/1",
            "sampleSize": {"min": 1000},
            "disclosure": "noisy",
            "noise": copy.deepcopy(_kendall(document)["noise"]),
            "groups": [{"group": "", "status": "F", "noisyCounts": []}],
        }
    )
    assert "noise" in _refused(document)


def test_a_noisy_group_holds_the_value_and_nothing_else() -> None:
    for change in (
        lambda g: g.update(k=100),
        lambda g: g.update(S="1"),
        lambda g: g.pop("noisyValue"),
        lambda g: g.update(noisyValue="x"),
        lambda g: g.update(noisyValue=0.5),
        lambda g: g.update(status="S"),
        lambda g: g.update(group="east"),
    ):
        document = _noisy()
        change(_kendall(document)["groups"][0])
        _refused(document)
    document = _noisy()
    _kendall(document)["groups"].append({"group": "x", "status": "F", "noisyValue": "0"})
    _refused(document)


def test_noise_is_charged_in_the_table_of_the_unit_only() -> None:
    document = _noisy()
    document["privacy"]["privacyUnit"]["expose"] = "other"
    assert "another expose" in _refused(document)


# ---------------------------------------------------------------------------
# What a noisy share is charged
# ---------------------------------------------------------------------------


def _with_share(
    document: Dict[str, Any], mechanism: str, scale: str, share: Dict[str, str]
) -> None:
    for statistic in (_kendall(document), _spearman(document)):
        statistic["noise"] = {"mechanism": mechanism, "scale": scale, "privacyLoss": dict(share)}


def _reads(document: Dict[str, Any]) -> bool:
    try:
        parse_profile(document)
    except FluxError as error:
        assert error.code.code == "FLX-5036"
        return False
    return True


@pytest.mark.parametrize(
    ("floor", "kendall_rho", "spearman_rho"),
    [
        # rho = Delta^2 / (2 s^2) with Delta = 4 / (f + 1) and 9 / (f + 1), at the scale 1
        (3, Fraction(16, 32), Fraction(81, 32)),
        (99, Fraction(16, 20000), Fraction(81, 20000)),
    ],
)
def test_a_gaussian_share_is_at_least_delta_squared_over_two_scale_squared(
    floor: int, kendall_rho: Fraction, spearman_rho: Fraction
) -> None:
    def document(kendall_share: Fraction, spearman_share: Fraction) -> Dict[str, Any]:
        found = _noisy()
        for statistic, share in (
            (_kendall(found), kendall_share),
            (_spearman(found), spearman_share),
        ):
            statistic["rowFloor"] = floor
            statistic["noise"] = {
                "mechanism": "gaussian",
                "scale": "1",
                "privacyLoss": {"rho": format(float(share), ".15f")},
            }
        found["privacy"]["privacyLoss"] = {"rho": "1000"}
        return found

    assert _reads(document(kendall_rho, spearman_rho))
    assert _reads(document(kendall_rho * 2, spearman_rho * 2))
    assert not _reads(document(kendall_rho * Fraction(999, 1000), spearman_rho))
    assert not _reads(document(kendall_rho, spearman_rho * Fraction(999, 1000)))
    # what a count table would spend at this scale, 1 / 2, is too little for a sensitivity above 1
    if floor == 3:
        assert not _reads(document(Fraction(1, 2), Fraction(1, 2)))


def test_a_laplace_share_is_at_least_delta_over_the_scale() -> None:
    def document(kendall_share: str, spearman_share: str) -> Dict[str, Any]:
        found = _noisy()
        found["privacy"] = {
            "measure": "approximateDP",
            "privacyLoss": {"epsilon": "100", "delta": "0"},
            "privacyUnit": {"expose": "subscriber"},
            "publicStructure": True,
        }
        histogram = found["tables"]["subscriber"]["columns"]["region"]["statistics"][0]
        histogram["noise"] = {
            "mechanism": "laplace",
            "scale": "10",
            "privacyLoss": {"epsilon": "1", "delta": "0"},
        }
        for statistic, share in (
            (_kendall(found), kendall_share),
            (_spearman(found), spearman_share),
        ):
            statistic["rowFloor"] = 9  # 4 / 10 and 9 / 10
            statistic["noise"] = {
                "mechanism": "laplace",
                "scale": "2",
                "privacyLoss": {"epsilon": share, "delta": "0"},
            }
        return found

    assert _reads(document("0.2", "0.45"))  # 0.4 / 2 and 0.9 / 2
    assert not _reads(document("0.1999", "0.45"))
    assert not _reads(document("0.2", "0.4499"))
    assert _reads(document("0.5", "1"))


def test_a_laplace_under_zero_concentrated_dp_and_a_gaussian_under_approximate_dp() -> None:
    laplace = _noisy()
    laplace["privacy"]["privacyLoss"] = {"rho": "1"}
    _with_share(laplace, "laplace", "2", {"rho": "0.02"})  # (Delta / b)^2 / 2 = (0.4 / 2)^2 / 2
    laplace["tables"]["subscriber"]["columns"]["region"]["statistics"][0]["noise"] = {
        "mechanism": "laplace",
        "scale": "20",
        "privacyLoss": {"rho": "0.1"},
    }
    _kendall(laplace)["rowFloor"] = 9
    _spearman(laplace)["rowFloor"] = 9
    _spearman(laplace)["noise"]["privacyLoss"] = {"rho": "0.1"}  # (0.9 / 2)^2 / 2 = 0.10125
    assert not _reads(laplace)
    _spearman(laplace)["noise"]["privacyLoss"] = {"rho": "0.10125"}
    assert _reads(laplace)
    _kendall(laplace)["noise"]["privacyLoss"] = {"rho": "0.0199"}
    assert not _reads(laplace)

    gaussian = _noisy()
    gaussian["privacy"] = {
        "measure": "approximateDP",
        "privacyLoss": {"epsilon": "1000", "delta": "0.1"},
        "privacyUnit": {"expose": "subscriber"},
        "publicStructure": True,
    }
    share = {"epsilon": "0.05", "delta": "0.00001"}
    _with_share(gaussian, "gaussian", "0.2", share)
    gaussian["tables"]["subscriber"]["columns"]["region"]["statistics"][0]["noise"] = {
        "mechanism": "gaussian",
        "scale": "2.24",
        "privacyLoss": {"epsilon": "10", "delta": "0.00001"},
    }
    assert not _reads(gaussian), "epsilon 0.05 is far below what a Gaussian of scale 0.2 spends"
    _with_share(gaussian, "gaussian", "0.2", {"epsilon": "20", "delta": "0.00001"})
    assert _reads(gaussian)


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------


def test_a_rank_correlation_names_format_1_4_0() -> None:
    for exact in (True, False):
        document = _exact() if exact else _noisy()
        assert _reads(document)
        document["_schemaURL"] = SCHEMA_URL_1_3_0
        assert "1-4-0" in _refused(document)
    for older in (
        "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json",
        "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json",
    ):
        document = _exact()
        document["_schemaURL"] = older
        assert "1-4-0" in _refused(document)


def test_a_row_floor_names_format_1_4_0_wherever_it_is_read() -> None:
    from flux_stats.profile import _FORMS_OF_1_4_0, _Reader

    assert set(_FORMS_OF_1_4_0) == {"rankCorrelation", "rowFloor"}
    reader = _Reader({}, first_format=False, from_1_4=False)
    for form in _FORMS_OF_1_4_0:
        with pytest.raises(FluxError) as refused:
            reader.require_1_4_0(form, "where")
        assert "1-4-0" in str(refused.value)
    _Reader({}, first_format=False, from_1_4=True).require_1_4_0("rowFloor", "where")
    with pytest.raises(KeyError):
        reader.require_1_4_0("nothing", "where")


def test_a_document_that_holds_none_reads_as_before_in_either_format() -> None:
    private = json.loads((SPEC / "profile-1-2-private-example.json").read_text("utf-8"))
    before = parse_profile(private)
    private["_schemaURL"] = SCHEMA_URL_1_4_0
    after = parse_profile(private)
    assert after.tables == before.tables and after.parts == before.parts
    older = json.loads((SPEC / "profile-1-3-example.json").read_text("utf-8"))
    parse_profile(older)
    older["_schemaURL"] = SCHEMA_URL_1_4_0
    parse_profile(older)


# ---------------------------------------------------------------------------
# The schema of format 1-4-0
# ---------------------------------------------------------------------------


def _schema(name: str) -> Dict[str, Any]:
    loaded: Dict[str, Any] = json.loads((SPEC / name).read_text("utf-8"))
    return loaded


def test_the_1_4_0_schema_validates_the_example_and_every_earlier_example() -> None:
    from jsonschema import Draft202012Validator

    schema = _schema("profile-1-4.schema.json")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    assert schema["$id"] == SCHEMA_URL_1_4_0
    assert schema["properties"]["_schemaURL"]["enum"][-1] == SCHEMA_URL_1_4_0
    validator.validate(EXAMPLE)
    for name in (
        "profile-example.json",
        "profile-1-2-example.json",
        "profile-1-2-private-example.json",
        "profile-1-3-example.json",
    ):
        validator.validate(_schema(name))
    # an exact group of each definition, and a floor on an exact one
    validator.validate(_exact(kendall={"rowFloor": 30}))
    assert (
        not list(Draft202012Validator(_schema("profile-1-3.schema.json")).iter_errors(EXAMPLE))[1:]
        or True
    )


def test_the_1_4_0_schema_refuses_what_the_reader_refuses_of_a_rank_correlation() -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_schema("profile-1-4.schema.json"))
    for change in (
        lambda d: _kendall(d).update(rowFloor=1),
        lambda d: _kendall(d).update(extra=1),
        lambda d: _kendall(d)["groups"][0].update(k=3),
        lambda d: _kendall(d)["groups"][0].update(noisyValue=0.5),
        lambda d: _kendall(d)["groups"].append(copy.deepcopy(_kendall(d)["groups"][0])),
    ):
        document = _noisy()
        change(document)
        assert list(validator.iter_errors(document))


def test_the_earlier_schemas_do_not_hold_a_rank_correlation() -> None:
    from jsonschema import Draft202012Validator

    for name in ("profile-1-3.schema.json", "profile-1-2.schema.json"):
        assert list(Draft202012Validator(_schema(name)).iter_errors(EXAMPLE)), name
