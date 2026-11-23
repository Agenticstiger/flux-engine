# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn given a column drawn from bins, held to ``kernel2-binned-given-vectors.json``.

ADR 0036 decision 3 adds no seed path: ``amount`` (a histogram), ``balance`` (a histogram given
``amount``'s bin) and ``region`` (a categorical given ``balance``'s bin) each draw on their own
cell path, ``("sim", simId, "gen", exposeId, "row", row, column)``, at the indices a histogram or
categorical column draws at: a histogram's bin at index 0 and its place at index 1, a
categorical's level at index 0. The bin the parent drew only chooses the weight table the child's
word is searched in. The vector file holds, for chosen rows, each column's key, counters, blocks,
bin, class, place and value. The values are written out from the specification with the
reference's Philox and Lemire draw (``tests/kernel2/reference.py``), never typed; the first test
fails until the file matches. Regenerate it with::

    python -m tests.kernel2.test_binned_given_vectors

The rest of the module asks the engine for each cell, on the per-row path and on the column path.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root
from flux_engine.generator import columns
from flux_engine.generator.paths import generator_column_path
from flux_engine.generator.rows import generate_row_slice
from tests.kernel2 import reference as k2

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
FILE = VECTORS / "kernel2-binned-given-vectors.json"
REGENERATE = "python -m tests.kernel2.test_binned_given_vectors"

SEAM = bytes(range(32))
SEED = 20261123
ROOT = k2.root(SEAM, SEED)
SIM = "sim-ledger"
EXPOSE = "accounts"
ROWS = (0, 1, 2, 3, 17, 1000, 4294967296)

#: The three columns, as the fidelity block declares them. A bin is named by its lower edge's text.
AMOUNT: Dict[str, Any] = {
    "bins": ["0", "100", "400", "1000"],
    "binWeights": ["0.5", "0.3", "0.2"],
    "scale": 2,
}
BALANCE: Dict[str, Any] = {
    "bins": ["0", "50", "200", "500"],
    "given": "amount",
    "binWeightsGiven": {
        "0": ["0.8", "0.1", "0.1"],
        "100": ["0.1", "0.8", "0.1"],
        "400": ["0.1", "0.1", "0.8"],
    },
    "scale": 0,
}
REGION: Dict[str, Any] = {
    "given": "balance",
    "levelWeightsGiven": {
        "0": {"north": "0.95", "south": "0.05"},
        "50": {"north": "0.5", "south": "0.5"},
        "200": {"north": "0.05", "south": "0.95"},
    },
}


def _path(row: int, column: str) -> Tuple[Any, ...]:
    return ("sim", SIM, "gen", EXPOSE, "row", row, column)


def _blocks(path: Sequence[Any], draws: int) -> Dict[str, Any]:
    label, _ordinals = k2.split_path(path)
    key = k2.stream_key(ROOT, path)
    return {
        "path": list(path),
        "keyLabel": label.hex(),
        "key": f"{key[0]:016x}{key[1]:016x}",
        "counters": [[format(w, "016x") for w in k2.counter(path, d)] for d in range(draws)],
        "blocks": [[format(w, "016x") for w in k2.block(ROOT, path, d)] for d in range(draws)],
    }


def _levels(weights: Sequence[str], names: Sequence[str]) -> Dict[str, Decimal]:
    total = sum(Decimal(w) for w in weights)
    return {name: Decimal(w) / total for name, w in zip(names, weights, strict=True)}


def _histogram_cell(row: int, column: str, law: Dict[str, Any], weights: Sequence[str]) -> Any:
    """A histogram cell: the bin at index 0, the grid point inside it at index 1."""
    path = _path(row, column)
    names = [str(i).zfill(2) for i in range(len(weights))]
    picked = int(k2.categorical(k2.uniform53(k2.block(ROOT, path, 0)[0]), _levels(weights, names)))
    edges = [Decimal(e) for e in law["bins"]]
    step = Decimal(1).scaleb(-law["scale"])
    units = [int(e / step) for e in edges]
    width = units[picked + 1] - units[picked] + (1 if picked == len(weights) - 1 else 0)
    place = k2.bounded_draw(ROOT, path, 1, width)
    value = edges[picked] + Decimal(place).scaleb(-law["scale"])
    return {
        **_blocks(path, 2),
        "bin": picked,
        "class": law["bins"][picked],
        "width": width,
        "index": place,
        "value": format(value, "f"),
    }


def _categorical_cell(row: int, column: str, table: Dict[str, str], parent_class: str) -> Any:
    path = _path(row, column)
    names = sorted(table)
    level = k2.categorical(
        k2.uniform53(k2.block(ROOT, path, 0)[0]), _levels([table[n] for n in names], names)
    )
    return {**_blocks(path, 1), "given": parent_class, "value": level}


def _row_vector(row: int) -> Dict[str, Any]:
    amount = _histogram_cell(row, "amount", AMOUNT, AMOUNT["binWeights"])
    balance = _histogram_cell(row, "balance", BALANCE, BALANCE["binWeightsGiven"][amount["class"]])
    region = _categorical_cell(
        row, "region", REGION["levelWeightsGiven"][balance["class"]], balance["class"]
    )
    balance["given"] = amount["class"]
    return {"row": row, "amount": amount, "balance": balance, "region": region}


def binned_given_vectors() -> Dict[str, Any]:
    return {
        "description": (
            "ADR 0036 decision 3: a column drawn given a column drawn from bins adds no seed "
            "path. For chosen rows of amount (a histogram), balance (a histogram given amount's "
            "bin) and region (a categorical given balance's bin), the key, counters and blocks "
            "of each column's own cell path, the bin and class the parent drew, the place inside "
            "the bin and the value. A bin is named by the text of its lower edge. Keys and words "
            "are hex, other integers decimal. Computed by "
            "tests/kernel2/test_binned_given_vectors.py, never typed."
        ),
        "regenerate": REGENERATE,
        "seam": SEAM.hex(),
        "seed": str(SEED),
        "root": ROOT.hex(),
        "columns": {"amount": AMOUNT, "balance": BALANCE, "region": REGION},
        "rows": [_row_vector(row) for row in ROWS],
    }


def _render(vectors: Dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2) + "\n"


STORED: Dict[str, Any] = (
    json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else binned_given_vectors()
)


def test_the_vector_file_is_current() -> None:
    assert FILE.read_text(encoding="utf-8") == _render(binned_given_vectors()), f"run {REGENERATE}"


def test_the_rows_hold_more_than_one_class_of_every_parent() -> None:
    assert len({row["amount"]["class"] for row in STORED["rows"]}) >= 2
    assert len({row["balance"]["class"] for row in STORED["rows"]}) >= 2
    assert len({row["region"]["value"] for row in STORED["rows"]}) == 2


def test_a_column_given_a_bin_draws_on_its_own_path() -> None:
    for vector in STORED["rows"]:
        for name in ("amount", "balance", "region"):
            held = vector[name]
            path = generator_column_path(SIM, EXPOSE, vector["row"], name)
            assert tuple(held["path"]) == path
            assert held["path"][:6] == ["sim", SIM, "gen", EXPOSE, "row", vector["row"]]


def _plan(tmp_path: Path) -> Any:
    from tests.seam.test_copula_block import _plan as plan_of
    from tests.stages.test_binned_given_run import BALANCE as B
    from tests.stages.test_binned_given_run import REGION as R
    from tests.stages.test_binned_given_run import binned_bundle

    bundle = binned_bundle(tmp_path, "vectors", edits=(B, R))
    return plan_of(bundle, rows=1 << 33, seam_digest="sha256:" + SEAM.hex())


def _engine_rows(plan: Any, rows: Sequence[int]) -> List[Tuple[Any, ...]]:
    root = derive_root(SEAM, SEED, seed_tree_version=kernel2.SEED_TREE_VERSION)
    return [generate_row_slice(plan, row, row + 1, root=root)[0] for row in rows]


def _expected(vector: Dict[str, Any]) -> Tuple[str, str, str]:
    return vector["amount"]["value"], vector["balance"]["value"], vector["region"]["value"]


def _cells(row: Sequence[Any]) -> Tuple[str, str, str]:
    names = ["acct_id", "amount", "balance", "opened_at", "closed_on", "region", "limit_raised"]
    by = dict(zip(names, row, strict=True))
    return format(by["amount"], "f"), str(by["balance"]), by["region"]


def test_the_engines_cells_are_the_vectors_on_the_column_path_and_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan = _plan(tmp_path)
    small = [v for v in STORED["rows"] if v["row"] < 1 << 20]
    by_columns = _engine_rows(plan, [v["row"] for v in small])
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        by_rows = _engine_rows(plan, [v["row"] for v in STORED["rows"]])
    assert [_cells(row) for row in by_columns] == [_expected(v) for v in small]
    assert [_cells(row) for row in by_rows] == [_expected(v) for v in STORED["rows"]]


def main() -> None:
    FILE.write_text(_render(binned_given_vectors()), encoding="utf-8")
    sys.stdout.write(f"wrote {FILE}\n")


if __name__ == "__main__":
    main()
