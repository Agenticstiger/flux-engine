# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``cliqueSelection`` (profile format 1-4-0): how a table's cliques were chosen, measured only.

The shares of ``selection`` and ``domain`` are parts of the account, in its measure, each entered
once and summed exactly with every other part. ``published: estimated``, ``measurements`` and a
clique's ``estimate`` are refused: this format reads measured cliques only.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from flux_stats.errors import FluxError
from flux_stats.noise import PrivacyShare
from flux_stats.profile import SCHEMA_URL_1_3_0, parse_profile

pytestmark = pytest.mark.unit


def _spec() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent / "docs" / "spec"
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _spec()
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-4-cliques-example.json").read_text("utf-8"))


def _document(change: Callable[[Dict[str, Any]], None] = lambda d: None) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _selection(document: Dict[str, Any]) -> Dict[str, Any]:
    block: Dict[str, Any] = document["tables"]["subscriber"]["cliqueSelection"]
    return block


def _refused(document: Dict[str, Any], *words: str) -> str:
    with pytest.raises(FluxError) as raised:
        parse_profile(document)
    message = str(raised.value)
    assert "FLX-5036" in message
    for word in words:
        assert word in message, message
    return message


def test_the_example_reads_and_its_shares_are_parts_of_the_account_once_each() -> None:
    profile = parse_profile(EXAMPLE)
    table = profile.tables["subscriber"]
    chosen = table.clique_selection
    assert chosen is not None
    assert (chosen.method, chosen.published) == ("aim", "measured")
    assert chosen.producer == "https://github.com/opendp/opendp"
    assert chosen.selection is not None and chosen.selection.loss.rho == Fraction(1, 100)
    assert chosen.domain is not None and chosen.domain.loss.rho == Fraction(1, 100)
    places = [where for where, _ in profile.parts]
    assert places.count("profile.tables.subscriber.cliqueSelection.selection") == 1
    assert places.count("profile.tables.subscriber.cliqueSelection.domain") == 1
    assert len(places) == len(set(places)) == 4


def test_a_bare_share_is_a_part_with_a_loss_and_no_mechanism_and_no_scale() -> None:
    parts = dict(parse_profile(EXAMPLE).parts)
    share = parts["profile.tables.subscriber.cliqueSelection.selection"]
    assert isinstance(share, PrivacyShare)
    assert not hasattr(share, "mechanism") and not hasattr(share, "scale")
    assert share.loss.rho == Fraction(1, 100)


def test_the_budget_sums_the_shares_exactly_with_every_other_part() -> None:
    # two cliques at 0.02 and the two shares at 0.01 are 0.06: the total fits to the last digit
    def at_the_total(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyLoss"] = {"rho": "0.06"}

    assert parse_profile(_document(at_the_total))

    def one_digit_over(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyLoss"] = {"rho": "0.0599999999999999999999"}

    _refused(_document(one_digit_over), "profile.privacy", "add up to more than the total rho")

    def a_share_alone_breaks_it(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyLoss"] = {"rho": "0.06"}
        _selection(document)["domain"]["privacyLoss"]["rho"] = "0.010000000000000000001"

    _refused(_document(a_share_alone_breaks_it), "4 parts")


def test_the_shares_are_in_the_accounts_measure_and_add_in_its_terms() -> None:
    def approximate(document: Dict[str, Any]) -> None:
        document["privacy"] = {
            "measure": "approximateDP",
            "privacyLoss": {"epsilon": "10", "delta": "0.001"},
            "privacyUnit": {"expose": "subscriber"},
            "publicStructure": True,
        }
        loss = {"epsilon": "1", "delta": "0.0001"}
        for clique in document["tables"]["subscriber"]["cliques"]:
            clique["noise"] = {"mechanism": "laplace", "scale": "1", "privacyLoss": dict(loss)}
        _selection(document)["selection"]["privacyLoss"] = {"epsilon": "2", "delta": "0.0001"}
        _selection(document)["domain"]["privacyLoss"] = {"epsilon": "3", "delta": "0.0001"}

    assert parse_profile(_document(approximate)).accounting is not None

    def over_in_delta(document: Dict[str, Any]) -> None:
        approximate(document)
        document["privacy"]["privacyLoss"]["delta"] = "0.0003"

    _refused(_document(over_in_delta), "total delta")

    def over_in_epsilon(document: Dict[str, Any]) -> None:
        approximate(document)
        document["privacy"]["privacyLoss"]["epsilon"] = "6.9999"

    _refused(_document(over_in_epsilon), "total epsilon")

    def in_the_wrong_measure(document: Dict[str, Any]) -> None:
        _selection(document)["selection"]["privacyLoss"] = {"epsilon": "1", "delta": "0"}

    _refused(_document(in_the_wrong_measure), "cliqueSelection.selection.privacyLoss")


def test_a_fixed_selection_states_no_selection_share_and_may_state_no_share_at_all() -> None:
    def fixed(document: Dict[str, Any]) -> None:
        document["tables"]["subscriber"]["cliqueSelection"] = {
            "method": "fixed",
            "producer": "urn:example:analyst",
            "published": "measured",
        }

    chosen = parse_profile(_document(fixed)).tables["subscriber"].clique_selection
    assert chosen is not None and chosen.selection is None and chosen.domain is None

    def fixed_with_a_share(document: Dict[str, Any]) -> None:
        fixed(document)
        _selection(document)["selection"] = {"privacyLoss": {"rho": "0.01"}}

    _refused(_document(fixed_with_a_share), "cliqueSelection.selection", "spent nothing")


@pytest.mark.parametrize("method", ["aim", "mst", "other"])
def test_every_method_the_format_names_is_read(method: str) -> None:
    document = _document(lambda d: _selection(d).update(method=method))
    chosen = parse_profile(document).tables["subscriber"].clique_selection
    assert chosen is not None and chosen.method == method


def test_a_selection_with_no_share_adds_no_part() -> None:
    def bare(document: Dict[str, Any]) -> None:
        del _selection(document)["selection"], _selection(document)["domain"]

    places = [where for where, _ in parse_profile(_document(bare)).parts]
    assert len(places) == 2 and not any("cliqueSelection" in where for where in places)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_an_estimated_release_is_refused_in_every_spelling_with_the_same_message() -> None:
    def estimated(document: Dict[str, Any]) -> None:
        _selection(document)["published"] = "estimated"

    def measurements(document: Dict[str, Any]) -> None:
        _selection(document)["measurements"] = [
            {
                "columns": ["region"],
                "noise": {"mechanism": "gaussian", "scale": "5", "privacyLoss": {"rho": "0.02"}},
            }
        ]

    def estimate(document: Dict[str, Any]) -> None:
        document["tables"]["subscriber"]["cliques"][1]["estimate"] = {"variance": "25"}

    def all_of_it(document: Dict[str, Any]) -> None:
        estimated(document)
        measurements(document)
        estimate(document)
        del document["tables"]["subscriber"]["cliques"][1]["noise"]

    for change in (estimated, measurements, estimate, all_of_it):
        message = _refused(_document(change))
        assert "estimated cliques are not read in this format" in message


@pytest.mark.parametrize(
    ("change", "where"),
    [
        (lambda s: s.update(extra=1), "unknown key"),
        (lambda s: s.pop("method"), "missing key 'method'"),
        (lambda s: s.pop("producer"), "missing key 'producer'"),
        (lambda s: s.pop("published"), "missing key 'published'"),
        (lambda s: s.update(method="private-pgm"), "a method is one of"),
        (lambda s: s.update(method=1), "a method is one of"),
        (lambda s: s.update(producer="no-colon"), "the producer is a URI"),
        (lambda s: s.update(producer=""), "producer"),
        (lambda s: s.update(published="yes"), "is 'measured'"),
        (lambda s: s["selection"].update(extra=1), "unknown key"),
        (lambda s: s["selection"].update(privacyLoss={"rho": "0"}), "above zero"),
        (lambda s: s["selection"].update(privacyLoss={"rho": "-1"}), "privacyLoss.rho"),
        (lambda s: s["domain"].pop("privacyLoss"), "missing key 'privacyLoss'"),
        (lambda s: s.update(selection="0.01"), "expected an object"),
    ],
)
def test_every_refusal_of_a_selection_names_where_and_quotes_no_value(
    change: Callable[[Dict[str, Any]], None], where: str
) -> None:
    document = _document(lambda d: change(_selection(d)))
    _refused(document, "cliqueSelection", where)


def test_a_selection_is_a_form_of_1_4_0_and_needs_the_account_and_the_units_table() -> None:
    def older(document: Dict[str, Any]) -> None:
        document["_schemaURL"] = SCHEMA_URL_1_3_0
        document["tables"]["subscriber"]["cliques"] = document["tables"]["subscriber"]["cliques"][
            :1
        ]

    _refused(_document(older), "cliqueSelection", "1-4-0", "earlier format")

    def no_account(document: Dict[str, Any]) -> None:
        del document["privacy"]

    _refused(_document(no_account), "privacy block")

    def other_unit(document: Dict[str, Any]) -> None:
        document["privacy"]["privacyUnit"] = {"expose": "elsewhere"}

    _refused(_document(other_unit), "unit of privacy")


def test_the_schema_holds_a_measured_selection_and_nothing_else() -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(
        json.loads((SPEC / "profile-1-4.schema.json").read_text("utf-8"))
    )
    validator.validate(EXAMPLE)
    for change in (
        lambda s: s.update(published="estimated"),
        lambda s: s.update(measurements=[]),
        lambda s: s.update(method="private-pgm"),
        lambda s: s.pop("producer"),
        lambda s: s.update(producer="no-colon"),
        lambda s: s["selection"].update(extra=1),
        lambda s: s["domain"].update(privacyLoss={"rho": "0.01", "epsilon": "1"}),
    ):
        document = _document(lambda d, change=change: change(_selection(d)))
        assert list(validator.iter_errors(document))
