# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A fidelity block is checked before any run exists.

SEAM-FIDELITY-SPEC §2 puts the block's rules on ``fluxsim validate``,
offline, and §3 computes every property's epsilon at plan time, refusing
one that is not smaller than its tolerance (``FLX-5002``). Both commands
used to read only the block's columns, so a malformed property of any kind
passed them and was refused only by ``verify`` or ``ship``, once a run and a
committed slice existed.

These tests hold ``validate`` and ``plan`` to the refusal ``verify`` gives,
with its code and its message, and hold a valid block to the result it had.
The oracle is ``verify``'s own call on the record ``apply`` writes for it,
not a copy of the rules.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

import pytest

from flux_engine.concordance.compiler import EXTENSION_TRANSPORT_KEY, compile_fidelity
from flux_engine.concordance.predicate import expose_from_contract
from flux_engine.core.canonical import canonicalise
from flux_engine.core.errors import FluxError
from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from tests.bundles import bundle_with_properties

from .conftest import FIXTURE_BUNDLE

SIMULATION_ID = "sim-payment-recovery"
EXPOSE_ID = "payment_recovery_moment"

#: A total-variation tolerance, the one both histogram properties consume.
_TV = '{ type: distance, measure: totalVariation, max: "0.05" }'

#: The fixture's own first property. Every malformed case below follows it,
#: so the refusal names ``fidelity.properties[1]`` and a valid property
#: before it is not enough to pass.
MEAN = '{ property: mean, column: amount, tolerance: { type: absolute, value: "50" } }'

#: One malformed property of each of the six kinds, with the code ``verify``
#: refuses it with and a phrase of the message. The fixture declares
#: ``amount`` and ``aborted_at`` with a range and ``market`` with a support.
MALFORMED = [
    pytest.param(
        '{ property: mean, column: amount, groupBy: region, tolerance: { type: absolute, value: "50" } }',
        "FLX-5034",
        "groupBy column 'region' has no declaration in fidelity.columns",
        id="mean-grouped-by-an-undeclared-column",
    ),
    pytest.param(
        f'{{ property: marginalHistogram, column: market, bins: ["0", "1"], tolerance: {_TV} }}',
        "FLX-5030",
        "a marginalHistogram comparison needs a range column, and 'market' declares a support",
        id="marginalHistogram-over-a-support-column",
    ),
    pytest.param(
        f'{{ property: marginalHistogram, column: amount, bins: ["1", "500", "100", "10000"], tolerance: {_TV} }}',
        "FLX-5032",
        "fidelity.properties[1].bins[2] does not increase",
        id="marginalHistogram-bins-that-do-not-increase",
    ),
    pytest.param(
        "{ property: support, column: amount, tolerance: { type: count, max: 0 } }",
        "FLX-5030",
        "a support comparison needs a support column, and 'amount' declares a range",
        id="support-over-a-range-column",
    ),
    pytest.param(
        '{ property: correlation, columns: [amount, aborted_at], tolerance: { type: absolute, value: "0.1" } }',
        "FLX-5033",
        "a correlation needs a minVariance",
        id="correlation-without-a-minVariance",
    ),
    pytest.param(
        '{ property: quantile, column: amount, tolerance: { type: absolute, value: "5" } }',
        "FLX-5034",
        "fidelity.properties[1] needs a level",
        id="quantile-without-a-level",
    ),
    pytest.param(
        f'{{ property: levelHistogram, column: market, bins: ["0", "1"], tolerance: {_TV} }}',
        "FLX-5030",
        "a levelHistogram counts its column's declared support levels and carries no bins",
        id="levelHistogram-with-bins",
    ),
    pytest.param(
        f"{{ property: levelHistogram, column: amount, tolerance: {_TV} }}",
        "FLX-5030",
        "a levelHistogram comparison needs a support column, and 'amount' declares a range",
        id="levelHistogram-over-a-range-column",
    ),
]


def refusal_verify_gives(bundle: Path) -> FluxError:
    """The refusal ``verify``'s fidelity leg raises for *bundle*'s one emit.

    Built the way the two stages hand the block over: ``apply`` writes the
    canonical contract, emit and extension entry to
    ``fidelity-input-<exposeId>.json``, and ``verify`` reads the file back,
    takes the expose from the recorded contract and calls
    :func:`~flux_engine.concordance.compiler.compile_fidelity`.
    """
    (entry,) = resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION_ID)
    emit = dict(canonicalise(entry.emit))
    emit[EXTENSION_TRANSPORT_KEY] = canonicalise(dict(entry.extension or {}))
    recorded = json.loads(
        json.dumps({"contract": canonicalise(entry.contract_document), "emit": emit})
    )
    expose = expose_from_contract(recorded["contract"], EXPOSE_ID)
    with pytest.raises(FluxError) as caught:
        compile_fidelity(contract=recorded["contract"], emit=recorded["emit"], expose=expose)
    return caught.value


def _bundle(tmp_path: Path, properties: Sequence[str]) -> Path:
    return bundle_with_properties(FIXTURE_BUNDLE, tmp_path / "bundle", properties)


@pytest.mark.parametrize(("declared", "code", "says"), MALFORMED)
def test_validate_refuses_a_malformed_property_as_verify_does(
    tmp_path: Path, declared: str, code: str, says: str
) -> None:
    bundle = _bundle(tmp_path, (MEAN, declared))
    expected = refusal_verify_gives(bundle)
    assert expected.code.code == code
    assert says in str(expected)

    result = validate(bundle)

    assert not result.success
    assert result.error_code == code
    # The message verify gives, word for word, under the Simulation it
    # belongs to, as validate reports every per-Simulation problem.
    assert result.stderr == f"{SIMULATION_ID}: {expected}"
    assert result.artifacts["problems"] == [f"{SIMULATION_ID}: {expected}"]


@pytest.mark.parametrize(("declared", "code", "says"), MALFORMED)
def test_plan_refuses_a_malformed_property_as_verify_does(
    tmp_path: Path, declared: str, code: str, says: str
) -> None:
    bundle = _bundle(tmp_path, (MEAN, declared))
    expected = refusal_verify_gives(bundle)
    state = tmp_path / ".flux"

    result = plan(bundle, state_dir=state)

    assert not result.success
    assert result.error_code == code
    assert result.stderr == str(expected)
    assert not state.exists(), "a refused plan writes no run directory"


def test_a_valid_property_of_every_kind_validates(tmp_path: Path) -> None:
    """Every kind, grouped and ungrouped, well formed: validate refuses none."""
    bundle = _bundle(
        tmp_path,
        (
            MEAN,
            '{ property: mean, column: amount, groupBy: market, tolerance: { type: absolute, value: "80" } }',
            "{ property: support, column: market, tolerance: { type: count, max: 0 } }",
            f'{{ property: marginalHistogram, column: amount, bins: ["1", "100", "1000", "10000"], tolerance: {_TV} }}',
            '{ property: quantile, column: amount, level: "0.5", tolerance: { type: absolute, value: "100" } }',
            f"{{ property: levelHistogram, column: market, tolerance: {_TV} }}",
            '{ property: correlation, columns: [amount, aborted_at], minVariance: "1", tolerance: { type: absolute, value: "0.5" } }',
        ),
    )

    result = validate(bundle)

    assert result.success, result.stderr
    assert result.error_code is None
    assert result.artifacts["simulations"][SIMULATION_ID]["emitsWithFidelity"] == [EXPOSE_ID]


@pytest.mark.parametrize(
    ("declared", "code", "says"),
    [
        pytest.param(
            '{ property: mean, column: amount, tolerance: { type: absolute, value: "0.00001" } }',
            "FLX-5002",
            "epsilon 1/65536 is not smaller than tolerance 1/100000",
            id="a-tolerance-inside-the-encoding-error",
        ),
        pytest.param(
            '{ property: correlation, columns: [amount, aborted_at], minVariance: "1", tolerance: { type: absolute, value: "0.5" } }',
            "FLX-5001",
            "correlationComparison needs 322 bits, capacity is 253",
            id="a-comparison-wider-than-the-field",
        ),
    ],
)
def test_a_margin_the_block_cannot_meet_is_refused_at_plan_not_at_validate(
    tmp_path: Path, declared: str, code: str, says: str
) -> None:
    """§3 computes epsilon at plan time; §2's rules, which validate owns, hold."""
    bundle = _bundle(tmp_path, (declared,))
    expected = refusal_verify_gives(bundle)
    assert expected.code.code == code
    assert says in str(expected)

    assert validate(bundle).success
    result = plan(bundle, state_dir=tmp_path / ".flux")

    assert not result.success
    assert result.error_code == code
    assert result.stderr == str(expected)


@pytest.mark.timeout(60)
def test_a_number_too_long_to_form_is_refused_at_once_by_validate_and_plan(
    tmp_path: Path,
) -> None:
    """``1E+99999999`` is twelve characters for a hundred-million-digit number.

    Forming it exactly took minutes, so a range bound spelled that way
    stalled ``validate`` and ``plan``, and ``verify`` and ``ship`` after them.
    All four read the block with one parser, and it refuses the number by
    its length before forming it.
    """
    bundle = _bundle(tmp_path, (MEAN,))
    simulation = bundle / "simulation.flux.yml"
    declared = 'range: { min: "1", max: "10000" }'
    text = simulation.read_text()
    assert text.count(declared) == 1, "the fixture's amount range moved"
    simulation.write_text(text.replace(declared, 'range: { min: "1", max: "1E+99999999" }'))
    expected = refusal_verify_gives(bundle)
    assert expected.code.code == "FLX-5030"
    assert "needs 100000000 digits" in str(expected)

    validated = validate(bundle)
    planned = plan(bundle, state_dir=tmp_path / ".flux")

    assert validated.error_code == "FLX-5030"
    assert validated.stderr == f"{SIMULATION_ID}: {expected}"
    assert planned.error_code == "FLX-5030"
    assert planned.stderr == str(expected)
