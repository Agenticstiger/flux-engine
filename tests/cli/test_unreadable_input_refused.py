# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim validate`` and ``fluxsim plan`` refuse three inputs with an FLX code.

Each used to crash a command or pass it:

* an unquoted integer of 5000 digits stopped both commands with a bare
  ``ValueError`` from inside the YAML read;
* ``mean: "1E+99999999"``, a key only the generator reads, stopped ``plan``
  with an uncaught ``decimal.Overflow``;
* ``fidelity: oops`` counted as no fidelity block, so both commands passed it.

Through the real binary: each refusal is one line that names the field or
its position and never the value, then the pointer to its explanation, the
``--json`` envelope names the code, and a refused ``plan`` writes nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from flux_engine._links import error_url
from tests.bundles import bundle_with_fidelity, bundle_with_text

from .conftest import FIXTURE_BUNDLE, parse_json_stdout, run_flux

SIMULATION_ID = "sim-payment-recovery"

TOO_LONG = (
    "FLX-1003: integer outside the JSON safe-integer domain -- simulation.flux.yml: an "
    "integer of more than 4300 digits is too long to read (line 23, column 15)"
)
DRAW_TOO_LONG = (
    "FLX-5030: the fidelity block is malformed -- amount: mean: written out, this number "
    "needs 100000000 digits, and a number in a fidelity block may need at most 1000"
)
NOT_A_MAPPING = "FLX-5030: the fidelity block is malformed -- fidelity must be a mapping, got str"


def _pointer(code: str) -> str:
    return f"more: fluxsim explain {code}  {error_url(code)}"


def _says(argv: List[str], cwd: Path) -> List[str]:
    """The stderr lines of a command that must fail with exit code 1 and print nothing."""
    result = run_flux(argv, cwd=cwd)
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    return result.stderr.splitlines()


def _error(argv: List[str], cwd: Path) -> Dict[str, Any]:
    envelope = parse_json_stdout(run_flux([*argv, "--json"], cwd=cwd))
    assert envelope["ok"] is False
    assert envelope["exitCode"] == 1
    return envelope["error"]


def test_an_integer_too_long_to_read_is_refused_by_validate_and_plan(tmp_path: Path) -> None:
    bundle = bundle_with_text(
        FIXTURE_BUNDLE,
        tmp_path / "bundle",
        "simulation.flux.yml",
        "rows: 64 ",
        "rows: " + "9" * 5000 + " ",
    )
    state = tmp_path / ".flux"

    for argv in (["validate", str(bundle)], ["plan", str(bundle), "--state-dir", str(state)]):
        assert _says(argv, tmp_path) == [TOO_LONG, _pointer("FLX-1003")]
        assert _error(argv, tmp_path) == {"code": "FLX-1003", "message": TOO_LONG}
    assert not state.exists()


def test_a_draw_parameter_too_long_to_form_is_refused_by_plan(tmp_path: Path) -> None:
    declared = "              null: exclude\n"
    bundle = bundle_with_text(
        FIXTURE_BUNDLE,
        tmp_path / "bundle",
        "simulation.flux.yml",
        declared,
        declared + '              mean: "1E+99999999"\n',
    )
    state = tmp_path / ".flux"
    argv = ["plan", str(bundle), "--state-dir", str(state)]

    assert _says(argv, tmp_path) == [DRAW_TOO_LONG, _pointer("FLX-5030")]
    assert _error(argv, tmp_path) == {"code": "FLX-5030", "message": DRAW_TOO_LONG}
    assert not state.exists()


def test_a_fidelity_value_that_is_not_a_mapping_is_refused_by_validate_and_plan(
    tmp_path: Path,
) -> None:
    bundle = bundle_with_fidelity(FIXTURE_BUNDLE, tmp_path / "bundle", "oops")
    state = tmp_path / ".flux"
    validate = ["validate", str(bundle)]
    plan = ["plan", str(bundle), "--state-dir", str(state)]

    assert _says(validate, tmp_path) == [f"{SIMULATION_ID}: {NOT_A_MAPPING}", _pointer("FLX-5030")]
    assert _error(validate, tmp_path) == {
        "code": "FLX-5030",
        "message": f"{SIMULATION_ID}: {NOT_A_MAPPING}",
    }
    assert _says(plan, tmp_path) == [NOT_A_MAPPING, _pointer("FLX-5030")]
    assert _error(plan, tmp_path) == {"code": "FLX-5030", "message": NOT_A_MAPPING}
    assert not state.exists()
