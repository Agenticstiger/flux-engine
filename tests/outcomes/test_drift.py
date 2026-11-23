# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Drift between the two states flags a stale calibration (ADR 0029 section 6).

Pinned here: the shares, and the psi, kl and ks of small histograms against hand-computed values;
that ks reads the cells in the profile's order; which statistics are compared (each level and
marginal histogram of a column, each query cell the families read) and which are left out and
listed (a withheld cell in either state, a statistic the other state does not hold with the same
definition); that a value above the threshold is stale and one at it is not; that the Playback's
``drift`` is read only when ``forecasts.playback`` names it; that every verdict of the pass records
it; ``warn`` and ``fail`` exit codes, with every verdict written either way; and no drift without a
named Playback.
"""

from __future__ import annotations

import json
import shutil
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import pytest

from flux_engine.accuracy.store import AccuracyStore
from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.outcomes.drift import (
    FLX_7145,
    DriftRule,
    compare_states,
    kl,
    ks,
    psi,
    read_drift_rule,
    shares,
    statistics_of,
)
from flux_engine.outcomes.forecast import forecast_make
from flux_engine.outcomes.scoring import forecast_score, score_forecasts
from flux_engine.seam.resolve import declared_responses, load_bundle
from tests.outcomes.support import (
    BUNDLE,
    PROFILE,
    SIMULATION,
    edited_profile,
    make_bundle,
    namespace,
    next_state,
)

pytestmark = pytest.mark.unit

REGISTERED = "2026-07-01T00:00:00Z"
HORIZON = "2026-09-30T23:59:59Z"
OBSERVED = "2026-10-01T00:00:00Z"
STATE_HEX = "817592e3a2233384"
PREMIUM = f"premium-offered.{STATE_HEX}"
BASIC = f"basic-control.{STATE_HEX}"
Q_OFFERED = "renewed-offered"
Q_CONTROL = "renewed-not-offered"
PREMIUM_CELL = "query:renewed-offered/premium"
BASIC_CELL = "query:renewed-not-offered/basic"
PLAN = "subscriptions.plan/levelHistogram/1"
AMOUNT = "subscriptions.amount/marginalHistogram/1"

TOLERANCE = Fraction(1, 10**26)


def _ln(value: str) -> Decimal:
    with localcontext() as context:
        context.prec = 60
        return Decimal(value).ln()


def _near(value: Decimal, expected: Decimal) -> bool:
    return abs(Fraction(value) - Fraction(expected)) < TOLERANCE


def _half(*values: str) -> Sequence[Fraction]:
    return tuple(Fraction(value) for value in values)


# ------------------------------------------------------------------ the shares


def test_a_share_is_the_count_and_a_half_over_the_total_and_half_the_cells() -> None:
    # 3 and 1 of 4 in 2 cells: (3 + 1/2) / (4 + 1) and (1 + 1/2) / (4 + 1)
    assert shares([3, 1]) == (Fraction(7, 10), Fraction(3, 10))


def test_no_share_is_zero_and_the_shares_add_up_to_one() -> None:
    found = shares([0, 40, 0, 0, 7])
    assert all(share > 0 for share in found)
    assert sum(found) == 1
    assert shares([0, 0]) == (Fraction(1, 2), Fraction(1, 2))


def test_a_statistic_has_at_least_one_cell() -> None:
    with pytest.raises(ValueError):
        shares([])


# ------------------------------------------------------------------ the three metrics


P = _half("1/2", "1/2")
Q = _half("3/4", "1/4")


def test_psi_is_the_sum_of_q_minus_p_times_the_log_of_q_over_p() -> None:
    # (1/4) ln(3/2) + (-1/4) ln(1/2) = ln(3) / 4
    assert _near(psi(P, Q), _ln("3") / 4)
    assert _near(psi(P, Q), Decimal("0.2746530721670274228488113092"))


def test_psi_is_symmetric_and_zero_for_equal_shares() -> None:
    assert _near(psi(P, Q), psi(Q, P))
    assert psi(P, P) == 0


def test_kl_is_the_sum_of_q_times_the_log_of_q_over_p() -> None:
    # (3/4) ln(3/2) + (1/4) ln(1/2)
    expected = Decimal("0.75") * _ln("1.5") + Decimal("0.25") * _ln("0.5")
    assert _near(kl(P, Q), expected)
    assert _near(kl(P, Q), Decimal("0.1308120359411369591292018062"))
    assert kl(P, P) == 0


def test_kl_is_not_symmetric() -> None:
    # q = (1/2, 1/2) against p = (3/4, 1/4): (1/2) ln(2/3) + (1/2) ln(2) = ln(2) - ln(3) / 2
    assert kl(Q, P) != kl(P, Q)
    assert _near(kl(Q, P), _ln("2") - _ln("3") / 2)


def test_ks_is_the_largest_gap_between_the_cumulative_shares() -> None:
    # cumulative p: 1/2, 1; cumulative q: 3/4, 1
    assert ks(P, Q) == Decimal("0.25")
    assert ks(P, P) == 0


def test_ks_reads_the_cells_in_the_order_the_profile_lists_them() -> None:
    p, q = _half("1/2", "3/10", "1/5"), _half("3/10", "1/5", "1/2")
    # cumulative p: .5 .8 1, q: .3 .5 1: gaps .2 and .3
    assert ks(p, q) == Decimal("0.3")
    # the two middle cells swapped in both: p .5 .7 1, q .3 .8 1: gaps .2 and .1
    swapped_p, swapped_q = _half("1/2", "1/5", "3/10"), _half("3/10", "1/2", "1/5")
    assert ks(swapped_p, swapped_q) == Decimal("0.2")


# ------------------------------------------------------------------ what is compared


def _families() -> Any:
    loaded = load_bundle(BUNDLE)
    families, _ = declared_responses(loaded.flux_documents[SIMULATION])
    return families


def _compare(earlier: Any, later: Any, *, metric: str = "psi", threshold: str = "0.2") -> Any:
    return compare_states(
        earlier, later, _families(), DriftRule(metric=metric, threshold=Decimal(threshold))
    )


def _values(report: Any) -> Dict[str, Decimal]:
    return dict(report.values)


def test_the_statistics_held_are_the_histograms_of_a_column_and_the_query_cells_read() -> None:
    held = statistics_of(edited_profile(), _families())
    names = set(held)
    assert "subscriptions.plan/levelHistogram/1" in names
    assert "subscriptions.region/levelHistogram/1" in names
    assert "subscriptions.offered/levelHistogram/1" in names
    assert "subscriptions.renewed/levelHistogram/1" in names
    # Each segment of the family, for the treated and the control query: standard is not one.
    assert {name for name in names if name.startswith("query:")} == {
        "query:renewed-offered/basic",
        "query:renewed-offered/premium",
        "query:renewed-not-offered/basic",
        "query:renewed-not-offered/premium",
    }
    assert held[PLAN].counts == (600, 240, 360)
    assert held[PREMIUM_CELL].counts == (36, 144)


def test_equal_states_have_no_drift() -> None:
    report = _compare(edited_profile(), edited_profile())
    assert report.values and all(value == 0 for _, value in report.values)
    assert report.left_out == ()
    assert report.stale is False


def test_a_query_cell_drifts_by_its_hand_computed_psi_kl_and_ks() -> None:
    # Two records in state k, both unmatched, give the shares 5/6 and 1/6; one more of each in
    # state k + 1 gives 3/4 and 1/4, so the cell is the small histogram of the metric tests above.
    earlier = edited_profile(cells={(Q_OFFERED, "premium"): (2, 0)})
    later = edited_profile(cells={(Q_OFFERED, "premium"): (2, 1)})
    values = {
        metric: _values(_compare(earlier, later, metric=metric))[PREMIUM_CELL]
        for metric in ("psi", "kl", "ks")
    }
    assert _near(values["psi"], Decimal(5) / 24 * _ln("3"))
    assert _near(values["kl"], Decimal(11) / 8 * _ln("3") - 2 * _ln("2"))
    assert _near(values["ks"], Decimal(5) / 24)


def test_a_cell_with_no_records_in_either_state_is_not_compared_and_listed() -> None:
    full = edited_profile(cells={(Q_OFFERED, "premium"): (1, 2)})
    empty = edited_profile(cells={(Q_OFFERED, "premium"): (0, 0)})
    for earlier, later, where in (
        (full, empty, "next"),
        (empty, full, "pinned"),
        (empty, empty, ""),
    ):
        for metric in ("psi", "kl", "ks"):
            report = _compare(earlier, later, metric=metric, threshold="0.001")
            assert PREMIUM_CELL not in _values(report)
            assert "no records" in dict(report.left_out)[PREMIUM_CELL]
            assert where in dict(report.left_out)[PREMIUM_CELL]
            # Everything else is still compared, and the empty cell alone cannot mark it stale.
            assert BASIC_CELL in _values(report)
        assert report.largest is None or report.largest[0] != PREMIUM_CELL


def test_a_level_histogram_of_a_column_is_compared_over_its_levels() -> None:
    def move(document: Dict[str, Any]) -> None:
        group = document["tables"]["subscriptions"]["columns"]["plan"]["statistics"][0]["groups"][0]
        group["levelCounts"] = [300, 300, 600]

    report = _compare(edited_profile(), edited_profile(move), metric="ks")
    # (600, 240, 360) against (300, 300, 600), over levels in the profile's order
    assert _values(report)[PLAN] == ks(shares([600, 240, 360]), shares([300, 300, 600]))
    assert _values(report)[PLAN] > Decimal("0.2")
    # Nothing else moved.
    assert [name for name, value in report.values if value != 0] == [PLAN]


def test_a_marginal_histogram_of_a_column_is_compared_over_its_bins() -> None:
    def histogram(counts: Sequence[int]) -> Any:
        def edit(document: Dict[str, Any]) -> None:
            column = document["tables"]["subscriptions"]["columns"]["amount"]
            column["statistics"] = [
                {
                    "definition": "marginalHistogram/1",
                    "disclosure": "acro-default",
                    "bins": ["5", "75.01", "95"],
                    "sampleSize": {"min": 1000, "max": 9999},
                    "groups": [
                        {"group": "", "k": sum(counts), "binCounts": list(counts), "status": "F"}
                    ],
                }
            ]

        return edited_profile(edit)

    report = _compare(histogram([900, 300]), histogram([300, 900]))
    p, q = shares([900, 300]), shares([300, 900])
    assert _near(_values(report)[AMOUNT], psi(p, q))
    assert _values(report)[AMOUNT] > Decimal("0.2")


def test_a_statistic_with_another_definition_in_the_next_state_is_left_out() -> None:
    def other_bins(document: Dict[str, Any]) -> None:
        document["tables"]["subscriptions"]["columns"]["amount"]["statistics"] = [
            {
                "definition": "marginalHistogram/1",
                "disclosure": "acro-default",
                "bins": ["5", "50", "95"],
                "sampleSize": {"min": 1000, "max": 9999},
                "groups": [{"group": "", "k": 1200, "binCounts": [900, 300], "status": "F"}],
            }
        ]

    def bins(document: Dict[str, Any]) -> None:
        document["tables"]["subscriptions"]["columns"]["amount"]["statistics"] = [
            {
                "definition": "marginalHistogram/1",
                "disclosure": "acro-default",
                "bins": ["5", "75.01", "95"],
                "sampleSize": {"min": 1000, "max": 9999},
                "groups": [{"group": "", "k": 1200, "binCounts": [900, 300], "status": "F"}],
            }
        ]

    report = _compare(edited_profile(bins), edited_profile(other_bins))
    assert AMOUNT not in _values(report)
    left = dict(report.left_out)
    assert "same definition" in left[AMOUNT]


def test_a_withheld_cell_in_either_state_leaves_the_statistic_out_and_listed() -> None:
    clear = edited_profile()
    withheld = edited_profile(cells={(Q_OFFERED, "premium"): None})
    after = _compare(clear, withheld)
    assert PREMIUM_CELL not in _values(after)
    assert dict(after.left_out)[PREMIUM_CELL].endswith("in the next state")
    before = _compare(withheld, clear)
    assert PREMIUM_CELL not in _values(before)
    assert dict(before.left_out)[PREMIUM_CELL].endswith("in the pinned state")
    # Everything else is still compared.
    assert PLAN in _values(after) and BASIC_CELL in _values(after)


def test_a_cell_with_a_suppressed_count_is_left_out() -> None:
    def suppress(document: Dict[str, Any]) -> None:
        group = document["tables"]["subscriptions"]["columns"]["region"]["statistics"][0]["groups"][
            0
        ]
        group["cellStatus"] = ["F", "S", "F", "F"]
        group["levelCounts"][1] = None

    report = _compare(edited_profile(), edited_profile(suppress))
    assert "subscriptions.region/levelHistogram/1" not in _values(report)
    assert "subscriptions.region/levelHistogram/1" in dict(report.left_out)


def test_the_largest_value_and_its_statistic_are_reported() -> None:
    later = edited_profile(
        cells={(Q_OFFERED, "premium"): (144, 36), (Q_CONTROL, "basic"): (80, 70)}
    )
    report = _compare(edited_profile(), later)
    assert report.largest is not None
    name, value = report.largest
    assert name == PREMIUM_CELL
    assert value == max(value for _, value in report.values)


def test_a_value_above_the_threshold_is_stale_and_one_at_it_is_not() -> None:
    later = edited_profile(cells={(Q_OFFERED, "premium"): (144, 36)})
    report = _compare(edited_profile(), later)
    assert report.largest is not None
    largest = report.largest[1]
    assert report.stale is True
    at = compare_states(
        edited_profile(), later, _families(), DriftRule(metric="psi", threshold=largest)
    )
    assert at.stale is False
    below = compare_states(
        edited_profile(),
        later,
        _families(),
        DriftRule(metric="psi", threshold=largest - Decimal("0.0000001")),
    )
    assert below.stale is True


def test_one_drifted_statistic_marks_the_calibration_stale() -> None:
    small = edited_profile(cells={(Q_CONTROL, "basic"): (76, 74)})
    assert _compare(edited_profile(), small).stale is False
    assert _compare(edited_profile(), small, threshold="0.0000001").stale is True


def test_no_statistic_in_common_is_not_stale() -> None:
    def bare(document: Dict[str, Any]) -> None:
        document["tables"]["subscriptions"]["columns"] = {
            name: {**column, "statistics": []}
            for name, column in document["tables"]["subscriptions"]["columns"].items()
        }
        document["queries"] = []

    report = _compare(edited_profile(bare), edited_profile(bare))
    assert report.values == () and report.largest is None and report.stale is False
    assert report.as_member()["largest"] is None and report.as_member()["stale"] is False


def test_the_member_a_verdict_records() -> None:
    later = edited_profile(cells={(Q_OFFERED, "premium"): (144, 36)})
    member = _compare(edited_profile(), later).as_member()
    assert set(member) == {"metric", "threshold", "largest", "statistic", "stale"}
    assert member["metric"] == "psi" and member["threshold"] == "0.2"
    assert member["statistic"] == PREMIUM_CELL and member["stale"] is True
    assert Decimal(member["largest"]) > Decimal("0.2")


# ------------------------------------------------------------------ the Playback's drift


def _documents(playback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    loaded = load_bundle(BUNDLE)
    documents = {key: dict(value) for key, value in loaded.flux_documents.items()}
    if playback is not None:
        spec = dict(documents["pb-renewal"]["spec"])
        spec["drift"] = playback
        documents["pb-renewal"] = {**documents["pb-renewal"], "spec": spec}
    return documents


def test_the_fixtures_playback_declares_psi_at_a_fifth_that_warns() -> None:
    rule = read_drift_rule(_documents(), "pb-renewal", where="sim")
    assert rule == DriftRule(
        metric="psi", threshold=Decimal("0.2"), on_breach="warn", playback="pb-renewal"
    )


def test_a_playback_nothing_names_is_not_read() -> None:
    broken = _documents({"metric": "chi2", "threshold": 0.2})
    assert read_drift_rule(broken, None, where="sim") is None


def test_a_named_playback_without_a_drift_block_reports_no_drift() -> None:
    documents = _documents()
    spec = {key: value for key, value in documents["pb-renewal"]["spec"].items() if key != "drift"}
    documents["pb-renewal"] = {**documents["pb-renewal"], "spec": spec}
    assert read_drift_rule(documents, "pb-renewal", where="sim") is None


@pytest.mark.parametrize(
    "block",
    [
        {"metric": "chi2", "threshold": 0.2},
        {"metric": "psi", "threshold": 0},
        {"metric": "psi", "threshold": -1},
        {"metric": "psi", "threshold": True},
        {"metric": "psi", "threshold": "soon"},
        {"metric": "psi"},
        {"metric": "ks", "threshold": 0.2, "onBreach": "stop"},
        "psi",
    ],
    ids=["metric", "zero", "negative", "bool", "text", "absent", "onBreach", "not-a-mapping"],
)
def test_a_drift_block_the_engine_cannot_compare_with_is_refused(block: Any) -> None:
    with pytest.raises(FluxError) as refused:
        read_drift_rule(_documents(block), "pb-renewal", where="sim")
    assert refused.value.code is FLX_7145


def test_the_three_metrics_and_both_breach_actions_are_read() -> None:
    for metric in ("psi", "kl", "ks"):
        for action in ("warn", "fail"):
            rule = read_drift_rule(
                _documents({"metric": metric, "threshold": 0.5, "onBreach": action}),
                "pb-renewal",
                where="sim",
            )
            assert rule is not None and rule.metric == metric and rule.on_breach == action
            assert rule.threshold == Decimal("0.5")


# ------------------------------------------------------------------ the pass


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> Path:
    state = tmp_path_factory.mktemp("drift") / "state"
    result = forecast_make(
        BUNDLE, registered_at=REGISTERED, horizon_end=HORIZON, state_dir=state, profile=PROFILE
    )
    assert result.success, result.stderr
    return state


@pytest.fixture()
def state(made: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "state"
    shutil.copytree(made, copy)
    return copy


def _verdicts(state: Path) -> Dict[str, Any]:
    store = AccuracyStore.for_state_dir(state)
    found = {name: store.read_verdict(name) for name in (PREMIUM, BASIC)}
    assert all(document is not None for document in found.values())
    return found


STALE = {(Q_OFFERED, "premium"): (144, 36)}


def test_every_verdict_of_the_pass_records_the_drift(state: Path, tmp_path: Path) -> None:
    report = score_forecasts(
        BUNDLE,
        profile=PROFILE,
        next_profile=next_state(tmp_path, cells=STALE),
        observed_at=OBSERVED,
        state_dir=state,
    )
    assert report.drift is not None and report.drift.stale is True
    for name, verdict in _verdicts(state).items():
        drift = verdict["drift"]
        assert drift["metric"] == "psi" and drift["threshold"] == "0.2"
        assert drift["stale"] is True and drift["statistic"] == PREMIUM_CELL
        assert Decimal(drift["largest"]) > Decimal("0.2"), name


def test_a_next_state_that_has_not_drifted_is_not_stale(state: Path, tmp_path: Path) -> None:
    score_forecasts(
        BUNDLE,
        profile=PROFILE,
        next_profile=next_state(tmp_path),
        observed_at=OBSERVED,
        state_dir=state,
    )
    for verdict in _verdicts(state).values():
        assert verdict["drift"]["stale"] is False
        assert Decimal(verdict["drift"]["largest"]) < Decimal("0.2")


def test_a_withheld_cell_is_listed_by_the_command_and_the_scores_are_written(
    state: Path, tmp_path: Path
) -> None:
    result = forecast_score(
        BUNDLE,
        profile=PROFILE,
        next_profile=next_state(tmp_path, cells={(Q_OFFERED, "premium"): None}),
        observed_at=OBSERVED,
        state_dir=state,
    )
    assert result.exit_code == 0
    drift = result.artifacts["drift"]
    assert {item["statistic"] for item in drift["leftOut"]} == {PREMIUM_CELL}
    assert "left out" in result.stdout and PREMIUM_CELL in result.stdout
    verdicts = _verdicts(state)
    assert verdicts[PREMIUM]["outcome"] == "not-scored"
    assert verdicts[PREMIUM]["drift"]["statistic"] != PREMIUM_CELL


def test_warn_reports_a_stale_calibration_and_exits_zero(
    state: Path, tmp_path: Path, capsys: Any
) -> None:
    code = main(
        [
            "forecast",
            "score",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--next",
            str(next_state(tmp_path, cells=STALE)),
            "--observed-at",
            OBSERVED,
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True
    assert out["artifacts"]["drift"]["stale"] is True
    assert out["artifacts"]["drift"]["statistic"] == PREMIUM_CELL


@pytest.fixture(scope="module")
def failing(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A bundle whose Playback says ``onBreach: fail``, with its forecasts registered."""
    root = tmp_path_factory.mktemp("failing")

    def breach(document: Dict[str, Any]) -> None:
        document["spec"]["drift"]["onBreach"] = "fail"

    bundle = make_bundle(root, "bundle", documents={"playback.flux.yml": breach})
    state = root / "state"
    result = forecast_make(
        bundle, registered_at=REGISTERED, horizon_end=HORIZON, state_dir=state, profile=PROFILE
    )
    assert result.success, result.stderr
    return bundle, state


def test_fail_exits_one_after_every_verdict_is_written(
    failing: Any, tmp_path: Path, capsys: Any
) -> None:
    bundle, made = failing
    state = tmp_path / "state"
    shutil.copytree(made, state)
    code = main(
        [
            "forecast",
            "score",
            str(bundle),
            "--profile",
            str(PROFILE),
            "--next",
            str(next_state(tmp_path, cells=STALE)),
            "--observed-at",
            OBSERVED,
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert out["error"]["code"] == "FLX-7146" and PREMIUM_CELL in out["error"]["message"]
    # Both scores are on record, with the drift that failed the command.
    assert len(out["artifacts"]["verdicts"]) == 2
    for verdict in _verdicts(state).values():
        assert verdict["drift"]["stale"] is True
        assert verdict["outcome"] in ("within-interval", "outside-interval")
        assert "intervalScore" in verdict


def test_fail_does_not_fail_a_calibration_that_is_not_stale(
    failing: Any, tmp_path: Path, capsys: Any
) -> None:
    bundle, made = failing
    state = tmp_path / "state"
    shutil.copytree(made, state)
    code = main(
        [
            "forecast",
            "score",
            str(bundle),
            "--profile",
            str(PROFILE),
            "--next",
            str(next_state(tmp_path)),
            "--observed-at",
            OBSERVED,
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["artifacts"]["drift"]["stale"] is False


@pytest.fixture(scope="module")
def unnamed(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A bundle whose forecasts name no Playback, though its Playback declares drift."""
    root = tmp_path_factory.mktemp("unnamed")

    def drop(document: Dict[str, Any]) -> None:
        namespace(document)["forecasts"].pop("playback")

    bundle = make_bundle(root, "bundle", simulation=drop)
    state = root / "state"
    result = forecast_make(
        bundle, registered_at=REGISTERED, horizon_end=HORIZON, state_dir=state, profile=PROFILE
    )
    assert result.success, result.stderr
    return bundle, state


def test_without_a_named_playback_the_pass_reports_no_drift(unnamed: Any, tmp_path: Path) -> None:
    bundle, made = unnamed
    state = tmp_path / "state"
    shutil.copytree(made, state)
    result = forecast_score(
        bundle,
        profile=PROFILE,
        next_profile=next_state(tmp_path, cells=STALE),
        observed_at=OBSERVED,
        state_dir=state,
    )
    assert result.exit_code == 0
    assert result.artifacts["drift"] is None
    assert "no Playback" in result.stdout
    for verdict in _verdicts(state).values():
        assert "drift" not in verdict
        assert "intervalScore" in verdict


def test_fail_does_not_fail_on_a_cell_the_next_state_holds_no_records_for(
    failing: Any, tmp_path: Path, capsys: Any
) -> None:
    bundle, made = failing
    state = tmp_path / "state"
    shutil.copytree(made, state)
    code = main(
        [
            "forecast",
            "score",
            str(bundle),
            "--profile",
            str(PROFILE),
            "--next",
            str(next_state(tmp_path, cells={(Q_OFFERED, "premium"): (0, 0)})),
            "--observed-at",
            OBSERVED,
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True
    drift = out["artifacts"]["drift"]
    assert drift["stale"] is False
    left = {item["statistic"]: item["reason"] for item in drift["leftOut"]}
    assert "no records" in left[PREMIUM_CELL]
