# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``--json`` envelope contract, on the paths that used to break it.

The documentation promises that every command prints the same envelope
shape and that ``error`` is always present with a ``message``
(docs/reference/cli/README.md). Four paths did not keep that
promise, and each has a test here:

* ``run --ship`` without ``-o`` returned from the CLI before the envelope
  was built, so ``--json`` printed **nothing at all** to stdout;
* ``error.code`` was scraped out of ``artifacts["code"]``, so a real
  catalogued failure could report ``null`` and ``fluxsim explain FLX-9999``
  reported the code the user *asked about* as the reason the command
  failed;
* anything that was not a ``FluxError`` escaped as a raw Python traceback
  with zero bytes on stdout.
"""

from __future__ import annotations

import json

import pytest

from flux_engine.cli import main

pytestmark = pytest.mark.unit


def _envelope(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.out.strip(), "no envelope was printed to stdout"
    return json.loads(captured.out)


def test_run_ship_without_out_dir_still_prints_an_envelope(tmp_path, capsys) -> None:
    exit_code = main(["run", str(tmp_path), "--ship", "--json"])
    envelope = _envelope(capsys)
    assert exit_code == 2
    assert envelope["exitCode"] == 2
    assert envelope["ok"] is False
    assert envelope["error"]["message"]
    assert "--out-dir" in envelope["error"]["message"]


def test_a_catalogued_stage_failure_reports_its_code(tmp_path, capsys) -> None:
    exit_code = main(["verify", str(tmp_path), "--run-id", "0123456789abcdef", "--json"])
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["error"]["code"] == "FLX-2109"
    assert "no applied run" in envelope["error"]["message"]


def test_explain_does_not_report_the_asked_about_code_as_the_failure(capsys) -> None:
    """FLX-9999 is the command's INPUT, never the reason it failed."""
    exit_code = main(["explain", "FLX-9999", "--json"])
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["error"]["code"] != "FLX-9999"
    assert envelope["artifacts"].get("code") is None
    assert envelope["artifacts"]["requestedCode"] == "FLX-9999"


def test_a_non_flux_exception_becomes_an_envelope_not_a_traceback(tmp_path, capsys) -> None:
    request = tmp_path / "bad.json"
    request.write_text("not json {", encoding="utf-8")
    exit_code = main(["--json", "gate", "decide", "--request", str(request)])
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["message"]
    assert "not valid JSON" in envelope["error"]["message"]


def test_a_missing_request_file_becomes_an_envelope(tmp_path, capsys) -> None:
    exit_code = main(["--json", "gate", "decide", "--request", str(tmp_path / "absent.json")])
    envelope = _envelope(capsys)
    assert exit_code == 1
    assert "no such file" in envelope["error"]["message"]
