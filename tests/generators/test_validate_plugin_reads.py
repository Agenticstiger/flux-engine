# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``validate`` and ``plan`` refuse a plugin expose the same way, and name it.

A plugin's values are not there when an aggregate summarises its rows or a counts ``parentWhen``
reads them, and a plugin column cannot be allocated exactly. A plugin ``options`` integer above 2^53
cannot be bound into the plan. Each is refused by ``validate`` with the code ``plan`` gives, naming
the expose, the column or the option at fault.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import pytest
import yaml

from flux_engine.cli import main

from .conftest import NAMESPACE

pytestmark = pytest.mark.integration

COUNTS = Path(__file__).resolve().parents[1] / "fixtures" / "counts-invoices"


def _bundle(
    tmp_path: Path,
    expose: str,
    change: Optional[Callable[[Dict[str, Any]], None]] = None,
    **extra: Any,
) -> Path:
    """The counted invoice book with *expose* made by the test plugin."""
    target = tmp_path / "book"
    shutil.copytree(COUNTS, target)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    entry: Dict[str, Any] = {
        "plugin": "learned",
        "distribution": "fxgen-plugin",
        "version": "1.0",
        "grade": "P5",
        "reproducibility": "bytes",
        **extra,
    }
    namespace = document["extensions"][NAMESPACE]
    namespace["generators"] = {expose: entry}
    block = namespace["emits"][expose]["fidelity"]
    # The plugin's values are not drawn to a declared mean, so a mean over one is left out.
    key = {"account": "account_id", "invoice_line": "line_id"}[expose]
    for item in block["properties"]:
        item["column"] = key
    if change is not None:
        change(namespace["emits"])
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


def _refusals(
    bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> "tuple[str, str]":
    """The (validate, plan) messages; both exit 1 and give the one code."""
    capsys.readouterr()
    assert main(["validate", str(bundle)]) == 1
    validated = capsys.readouterr().err
    state = ["--state-dir", str(tmp_path / "state")]
    assert main(["plan", str(bundle), *state, "--allow-generator", "learned"]) == 1
    planned = capsys.readouterr().err
    return validated, planned


def _code(message: str) -> str:
    start = message.index("FLX-")
    return message[start : start + 8]


def test_a_counts_parent_when_over_a_plugin_expose_is_refused_by_both(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_exact(emits: Dict[str, Any]) -> None:
        emits["account"]["fidelity"]["columns"]["status"].pop("allocation")

    bundle = _bundle(tmp_path, "account", no_exact)
    validated, planned = _refusals(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-3105"
    for message in (validated, planned):
        assert "'account'" in message and "counts[invoice.account].parentWhen" in message


def test_an_aggregate_over_a_plugin_expose_is_refused_by_both(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "invoice_line")
    validated, planned = _refusals(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-3105"
    for message in (validated, planned):
        assert "emit 'invoice'" in message and "'invoice_line'" in message
        assert "aggregate" in message


def test_an_exact_allocation_on_a_plugin_column_is_refused_by_both_naming_the_column(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "account")
    validated, planned = _refusals(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-3105"
    for message in (validated, planned):
        assert "'account'" in message and "'status'" in message and "allocation" in message


def test_an_option_above_two_to_the_53_is_refused_by_both_naming_the_option(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "account", options={"limits": {"seed": 2**53 + 1}})
    validated, planned = _refusals(bundle, tmp_path, capsys)
    assert _code(validated) == _code(planned) == "FLX-1003"
    for message in (validated, planned):
        assert "options['limits']['seed']" in message
