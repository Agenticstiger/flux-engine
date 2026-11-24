# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The copula's path and draws held to ``kernel2-copula-vectors.json`` (ADR 0035 decision 6).

The vector file pins, for small copulas, the path's key label, key, counters and blocks, and for
chosen rows the uniforms, the normals, the latent values, the thresholds, the bins and the values.
It holds three copulas: one whose matrix passes, one that is shrunk, and one with a bin of weight
zero. The key, counters and blocks come from the reference's Philox (``tests/kernel2/reference.py``);
the bins and values are found by the two bisections written out from the specification over exact
fractions, and the uniforms, normals and thresholds by the module's own primitives, which
``test_copula_draw.py`` holds to an independent inverse. The first test fails until the file
matches. Regenerate it with::

    python -m tests.kernel2.test_copula_vectors

The rest of the module asks the engine for each of them.
"""

from __future__ import annotations

import hashlib
import json
import sys
from decimal import Decimal
from fractions import Fraction
from math import lcm
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.core import copula, kernel2
from flux_engine.core.seedtree import counter_stream, derive_root, key_label
from flux_engine.generator.paths import COPULA_PATH_SHAPE, copula_path
from tests.kernel2 import reference as k2

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
FILE = VECTORS / "kernel2-copula-vectors.json"
REGENERATE = "python -m tests.kernel2.test_copula_vectors"

SEAM = hashlib.sha256(b"kernel2 copula vectors: H").digest()
SEED = 20261123
ROOT = k2.root(SEAM, SEED)
SIM = "s"
EXPOSE = "ledger"
SEPARATOR = b"\x1f"
ROWS = (0, 1, 2, 3, 1000, 4294967296)

#: Each column: its bin edges (decimal text), their weights, and the scale of its grid.
CASES: Tuple[Dict[str, Any], ...] = (
    {
        "name": "a matrix that passes",
        "measure": "kendall",
        "columns": ["amount", "balance", "tenure"],
        "pairs": [["amount", "balance", "0.5"], ["amount", "tenure", "-0.25"]],
        "laws": {
            "amount": {
                "bins": ["0", "10", "40", "100"],
                "binWeights": ["0.5", "0.3", "0.2"],
                "scale": 0,
            },
            "balance": {"bins": ["0", "5", "20"], "binWeights": ["0.6", "0.4"], "scale": 2},
            "tenure": {"bins": ["1", "2", "3", "4"], "binWeights": ["1", "1", "1"], "scale": 0},
        },
    },
    {
        "name": "a matrix that is shrunk",
        "measure": "kendall",
        "columns": ["amount", "balance", "tenure"],
        "pairs": [
            ["amount", "balance", "0.9"],
            ["balance", "tenure", "0.9"],
            ["amount", "tenure", "-0.9"],
        ],
        "laws": {
            "amount": {
                "bins": ["0", "10", "40", "100"],
                "binWeights": ["0.5", "0.3", "0.2"],
                "scale": 0,
            },
            "balance": {"bins": ["0", "5", "20"], "binWeights": ["0.6", "0.4"], "scale": 2},
            "tenure": {"bins": ["1", "2", "3", "4"], "binWeights": ["1", "1", "1"], "scale": 0},
        },
    },
    {
        "name": "a bin of weight zero",
        "measure": "spearman",
        "columns": ["amount", "balance"],
        "pairs": [["amount", "balance", "0.6"]],
        "laws": {
            "amount": {
                "bins": ["0", "4", "8", "12"],
                "binWeights": ["0.25", "0", "0.75"],
                "scale": 0,
            },
            "balance": {"bins": ["0", "3", "9"], "binWeights": ["1", "3"], "scale": 1},
        },
    },
)


def _label_of(path: Sequence[Any]) -> bytes:
    """The key label: the parts joined by the separator, the row part empty."""
    return SEPARATOR.join(b"" if i == 5 else str(part).encode() for i, part in enumerate(path))


def _key(path: Sequence[Any]) -> Tuple[int, int]:
    material = k2.SEED_TREE_VERSION.encode() + k2.NUL + ROOT + k2.NUL + _label_of(path)
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big"), int.from_bytes(digest[8:16], "big")


def _shares(weights: Sequence[str]) -> List[Fraction]:
    total = sum((Fraction(Decimal(w)) for w in weights), Fraction(0))
    return [Fraction(Decimal(w)) / total for w in weights]


def _widths(edges: Sequence[str], scale: int) -> List[int]:
    """Grid values per bin: a bin below the last stops one step short of the next edge."""
    step = Fraction(1, 10**scale)
    units = [Fraction(Decimal(e)) / step for e in edges]
    return [
        int(units[i + 1] - units[i]) + (1 if i == len(edges) - 2 else 0)
        for i in range(len(edges) - 1)
    ]


def _threshold(fraction: Fraction) -> float:
    return copula.threshold(fraction.numerator, fraction.denominator)


def _locate(shares: Sequence[Fraction], widths: Sequence[int], x: float) -> Tuple[int, int]:
    """The two bisections of decision 6, written out over exact fractions."""
    cumulative = [Fraction(0)]
    for share in shares:
        cumulative.append(cumulative[-1] + share)
    positive = [b for b, share in enumerate(shares, 1) if share > 0]
    lo, hi = 0, positive[-1]
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _threshold(cumulative[mid]) <= x:
            lo = mid
        else:
            hi = mid
    b = lo + 1
    width = widths[b - 1]
    lo, hi = 0, width
    while hi - lo > 1:
        mid = (lo + hi) // 2
        before = cumulative[b - 1] + shares[b - 1] * Fraction(mid, width)
        if _threshold(before) <= x:
            lo = mid
        else:
            hi = mid
    return b, lo


def _row_vector(case: Dict[str, Any], plan: Any, row: int) -> Dict[str, Any]:
    path = copula_path(SIM, EXPOSE, row)
    key = _key(path)
    counters = [(row, 0, d, 0) for d in range(len(case["columns"]))]
    blocks = [k2.philox_block(c, key) for c in counters]
    uniforms = [copula.uniform(block[0]) for block in blocks]
    normals = [copula.normal(block[0]) for block in blocks]
    latent = copula.latent_row([list(r) for r in plan.factor], normals)
    out: Dict[str, Any] = {
        "row": row,
        "counters": [[format(word, "016x") for word in c] for c in counters],
        "blocks": [[format(word, "016x") for word in block] for block in blocks],
        "u": [value.hex() for value in uniforms],
        "z": [value.hex() for value in normals],
        "x": [value.hex() for value in latent],
        "columns": [],
    }
    for name, x in zip(case["columns"], latent, strict=True):
        law = case["laws"][name]
        shares, widths = _shares(law["binWeights"]), _widths(law["bins"], law["scale"])
        b, j = _locate(shares, widths, x)
        edge = Decimal(law["bins"][b - 1])
        value = edge + Decimal(j).scaleb(-law["scale"])
        out["columns"].append({"column": name, "bin": b, "index": j, "value": format(value, "f")})
    return out


def _case_vector(case: Dict[str, Any]) -> Dict[str, Any]:
    pairs = {(a, b): Decimal(v) for a, b, v in case["pairs"]}
    plan = copula.plan_copula(case["columns"], case["measure"], pairs)
    path = copula_path(SIM, EXPOSE, 41)
    key = _key(path)
    laws = []
    for name in case["columns"]:
        law = case["laws"][name]
        shares, widths = _shares(law["binWeights"]), _widths(law["bins"], law["scale"])
        denominator = lcm(*(share.denominator for share in shares))
        cumulative = [0]
        for share in shares:
            cumulative.append(cumulative[-1] + int(share * denominator))
        laws.append(
            {
                "column": name,
                **law,
                "widths": widths,
                "shares": [f"{s.numerator}/{s.denominator}" for s in shares],
                "binThresholds": [copula.threshold(c, denominator).hex() for c in cumulative],
            }
        )
    return {
        "name": case["name"],
        "measure": case["measure"],
        "columns": case["columns"],
        "pairs": case["pairs"],
        "alpha": plan.alpha,
        "factor": [[value.hex() for value in row] for row in plan.factor],
        "factorDigest": plan.digest,
        "path": [str(part) if not isinstance(part, int) else part for part in path],
        "keyLabel": _label_of(path).hex(),
        "key": f"{key[0]:016x}{key[1]:016x}",
        "laws": laws,
        "rows": [_row_vector(case, plan, row) for row in ROWS],
    }


def copula_vectors() -> Dict[str, Any]:
    return {
        "description": (
            "ADR 0035 decision 6: the key label, key, counters and blocks of the copula path "
            "(sim, simId, gen, exposeId, copula, row), and for chosen rows the uniforms, normals, "
            "latent values, bin thresholds, bins and values of three small copulas: one whose "
            "matrix passes, one that is shrunk and one with a bin of weight zero. Keys and words "
            "are hex, floats are their hex spelling, other integers decimal. Computed by "
            "tests/kernel2/test_copula_vectors.py, never typed."
        ),
        "regenerate": REGENERATE,
        "seam": SEAM.hex(),
        "seed": str(SEED),
        "root": ROOT.hex(),
        "cases": [_case_vector(case) for case in CASES],
    }


def _render(vectors: Dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2) + "\n"


STORED: Dict[str, Any] = (
    json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else copula_vectors()
)


def _root() -> Any:
    return derive_root(
        bytes.fromhex(STORED["seam"]),
        int(STORED["seed"]),
        seed_tree_version=kernel2.SEED_TREE_VERSION,
    )


def test_the_vector_file_is_current() -> None:
    assert FILE.read_text(encoding="utf-8") == _render(copula_vectors()), f"run {REGENERATE}"


def test_the_engine_root_is_the_vector_root() -> None:
    assert _root().digest.hex() == STORED["root"]


def test_the_cases_are_a_passing_a_shrunk_and_a_zero_weight_one() -> None:
    alphas = [Decimal(case["alpha"]) for case in STORED["cases"]]
    assert alphas[0] == 0 and alphas[1] > 0
    zero = STORED["cases"][2]["laws"][0]
    assert "0" in zero["binWeights"]
    chosen = {c["bin"] for row in STORED["cases"][2]["rows"] for c in row["columns"][:1]}
    assert 2 not in chosen


@pytest.mark.parametrize("case", STORED["cases"], ids=lambda case: case["name"])
def test_the_engines_path_key_counters_and_blocks_are_the_vectors(case: Dict[str, Any]) -> None:
    path = tuple(case["path"])
    assert path == copula_path(SIM, EXPOSE, 41)
    label, ordinals = key_label(path, COPULA_PATH_SHAPE)
    assert label.hex() == case["keyLabel"] and ordinals == (41, 0)
    for vector in case["rows"]:
        stream = counter_stream(_root(), copula_path(SIM, EXPOSE, vector["row"]), COPULA_PATH_SHAPE)
        for draw, block in enumerate(vector["blocks"]):
            assert [format(word, "016x") for word in stream.block(draw)] == block
            assert vector["counters"][draw] == [
                format(word, "016x") for word in (vector["row"], 0, draw, 0)
            ]


@pytest.mark.parametrize("case", STORED["cases"], ids=lambda case: case["name"])
def test_the_engines_plan_draw_and_laws_are_the_vectors(case: Dict[str, Any]) -> None:
    pairs = {(a, b): Decimal(v) for a, b, v in case["pairs"]}
    plan = copula.plan_copula(case["columns"], case["measure"], pairs)
    assert plan.alpha == case["alpha"] and plan.digest == case["factorDigest"]
    factor = [[float.fromhex(value) for value in row] for row in case["factor"]]
    assert [list(row) for row in plan.factor] == factor
    laws = [
        copula.ColumnLaw([Fraction(s) for s in law["shares"]], law["widths"])
        for law in case["laws"]
    ]
    for law, vector in zip(laws, case["laws"], strict=True):
        assert [law.bin_threshold(m).hex() for m in range(law.bins + 1)] == vector["binThresholds"]
    for row in case["rows"]:
        words = [int(block[0], 16) for block in row["blocks"]]
        assert [copula.uniform(w).hex() for w in words] == row["u"]
        z = [copula.normal(w) for w in words]
        assert [v.hex() for v in z] == row["z"]
        x = copula.latent_row(factor, z)
        assert [v.hex() for v in x] == row["x"]
        for law, value, held in zip(laws, x, row["columns"], strict=True):
            assert law.locate(value) == (held["bin"], held["index"])


@pytest.mark.parametrize("case", STORED["cases"], ids=lambda case: case["name"])
def test_the_column_forms_give_the_vector_rows(case: Dict[str, Any]) -> None:
    factor = [[float.fromhex(value) for value in row] for row in case["factor"]]
    laws = [
        copula.ColumnLaw([Fraction(s) for s in law["shares"]], law["widths"])
        for law in case["laws"]
    ]
    words = [
        np.array([int(row["blocks"][d][0], 16) for row in case["rows"]], dtype=np.uint64)
        for d in range(len(case["columns"]))
    ]
    z = [copula.normal_array(w) for w in words]
    x = copula.latent_columns(factor, z)
    for i, law in enumerate(laws):
        bins, index = law.locate_columns(x[i])
        for position, row in enumerate(case["rows"]):
            held = row["columns"][i]
            assert (int(bins[position]), int(index[position])) == (held["bin"], held["index"])


def test_a_value_is_inside_its_bin() -> None:
    for case in STORED["cases"]:
        for row in case["rows"]:
            for held, law in zip(row["columns"], case["laws"], strict=True):
                edges = [Decimal(e) for e in law["bins"]]
                low, high = edges[held["bin"] - 1], edges[held["bin"]]
                assert low <= Decimal(held["value"]) <= high
                assert 0 <= held["index"] < law["widths"][held["bin"] - 1]


if __name__ == "__main__":  # pragma: no cover - regenerates the vector file
    FILE.write_text(_render(copula_vectors()), encoding="utf-8")
    sys.stdout.write(f"wrote {FILE}\n")
