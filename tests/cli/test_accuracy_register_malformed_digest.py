# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``accuracy register`` with a malformed ``run_digest`` is a catalogued refusal that names the key.

It used to escape as a bare ``ValueError: runDigest: malformed digest`` with no code and a
request to report a bug. The input key is ``run_digest``, and the refusal is ``FLX-7005``
(the document does not have the required shape).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli.conftest import parse_json_stdout, run_flux

pytestmark = pytest.mark.integration


def _register(tmp_path: Path, run_digest: object) -> "tuple[int, str, dict]":  # type: ignore[type-arg]
    document = {
        "prediction_id": "pred-1",
        "run_digest": run_digest,
        "metric_id": "telco/saved_customers",
        "statement": "saved customers rise",
        "predicted": "12",
        "horizon_end": "2026-10-01T00:00:00Z",
        "registered_at": "2026-09-01T00:00:00Z",
    }
    path = tmp_path / "pred.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    state = tmp_path / "state"
    plain = run_flux(
        ["accuracy", "register", "--file", str(path), "--state-dir", str(state)], cwd=tmp_path
    )
    shaped = run_flux(
        ["--json", "accuracy", "register", "--file", str(path), "--state-dir", str(state)],
        cwd=tmp_path,
    )
    assert not (state / "accuracy" / "predictions").exists() or not list(
        (state / "accuracy" / "predictions").iterdir()
    )
    return plain.returncode, plain.stderr, parse_json_stdout(shaped)


@pytest.mark.parametrize("value", ["sha256:xyz", "SHA256:" + "0" * 64, "sha256:" + "A" * 64])
def test_a_malformed_run_digest_is_flx_7005_and_names_run_digest(
    tmp_path: Path, value: object
) -> None:
    code, stderr, envelope = _register(tmp_path, value)
    assert code == 1
    assert envelope["error"]["code"] == "FLX-7005"
    assert "run_digest" in envelope["error"]["message"]
    assert "ValueError" not in stderr and "runDigest" not in stderr
    assert "more: fluxsim explain FLX-7005" in stderr
    assert "report it at" not in stderr
