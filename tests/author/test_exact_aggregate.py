# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A small table whose column an aggregate ``derive`` sets takes no ``allocation: exact``.

``init`` once wrote ``allocation: exact`` on such a column and its own self-check refused the bundle
(FLX-3402: allocation exact is not allowed on a column a rule sets). Pinned here, from the
``invoice-book-init`` contract and profile: ``init`` succeeds, the aggregate columns carry no
``allocation``, and the bundle plans, applies and verifies.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest
import yaml

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "invoice-book-init"
AGGREGATED = {"total", "line_count", "max_qty"}


def _fluxsim(args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    argv = [sys.executable, "-m", "flux_engine.cli", *args]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=300)


def test_init_of_an_aggregate_derive_is_not_refused_and_runs(tmp_path: Path) -> None:
    contract = str(FIXTURE / "invoice-book.fluid.yml")
    profile = str(FIXTURE / "profile.json")
    done = _fluxsim(["init", "b", "--contract", contract, "--profile", profile], tmp_path)
    assert done.returncode == 0, done.stderr or done.stdout

    text = (tmp_path / "b" / "simulation.flux.yml").read_text("utf-8")
    simulation = yaml.safe_load(text)
    invoice = [
        found for found in _walk(simulation) if isinstance(found, dict) and AGGREGATED <= set(found)
    ]
    assert invoice, "the invoice columns are planned"
    for column in AGGREGATED:
        assert "allocation" not in invoice[0][column], column

    planned = _fluxsim(["plan", "b", "--state-dir", "st", "--profile", profile], tmp_path)
    assert planned.returncode == 0, planned.stderr or planned.stdout
    found = re.search(r"runId=([0-9a-f]+)", planned.stdout)
    assert found, planned.stdout
    run_id = found.group(1)
    applied = _fluxsim(["apply", "b", "--state-dir", "st", "--run-id", run_id], tmp_path)
    assert applied.returncode == 0, applied.stderr or applied.stdout
    verified = _fluxsim(
        ["verify", "b", "--state-dir", "st", "--run-id", run_id, "--profile", profile], tmp_path
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout


def _walk(node: object) -> "list[object]":
    found: "list[object]" = [node]
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    for child in children:
        found.extend(_walk(child))
    return found


def test_init_of_guarded_rules_leaves_exact_allocation_off_the_columns_they_set(
    tmp_path: Path,
) -> None:
    text = (FIXTURE / "invoice-book.fluid.yml").read_text("utf-8")
    count_old = "          derive: line_count\n"
    max_old = (
        "          derive: max_qty\n"
        "          aggregate: { function: max, column: quantity, from: invoice_line, via: invoice }\n"
    )
    assert text.count(count_old) == 1 and text.count(max_old) == 1
    text = text.replace(count_old, count_old + "          when: \"status = 'paid'\"\n")
    text = text.replace(
        max_old,
        "          derive: max_qty\n          expr: line_count + 1\n"
        "          when: \"status = 'paid'\"\n",
    )
    contract = tmp_path / "guarded.fluid.yml"
    contract.write_text(text, "utf-8")
    profile = str(FIXTURE / "profile.json")
    done = _fluxsim(["init", "b", "--contract", str(contract), "--profile", profile], tmp_path)
    assert done.returncode == 0, done.stderr or done.stdout

    simulation = yaml.safe_load((tmp_path / "b" / "simulation.flux.yml").read_text("utf-8"))
    invoice = [
        found for found in _walk(simulation) if isinstance(found, dict) and AGGREGATED <= set(found)
    ]
    assert invoice, "the invoice columns are planned"
    for column in ("line_count", "max_qty"):
        assert "allocation" not in invoice[0][column], column

    ran = _fluxsim(["run", "b", "--state-dir", "st", "--profile", profile, "--verify"], tmp_path)
    assert ran.returncode == 0, ran.stderr or ran.stdout
