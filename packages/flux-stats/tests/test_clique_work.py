# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The work a table of cliques asks of the reader grows with its cells, not with its pairs.

Each test counts the passes over a clique's cells and the cells they visit, never a clock.
"""

from __future__ import annotations

from fractions import Fraction
from itertools import product
from typing import Any, Dict, List, Tuple

import pytest
from test_profile_cliques_v2 import _small, _with_cliques

from flux_stats import cliques as cliques_module
from flux_stats.cliques import CliqueEntry, derived_sets
from flux_stats.errors import FluxError
from flux_stats.profile import parse_profile

pytestmark = pytest.mark.unit

Pass = Tuple[int, int, int]


@pytest.fixture
def passes(monkeypatch: pytest.MonkeyPatch) -> List[Pass]:
    """Every pass over a clique's cells: (clique index, sets summed onto, cells visited)."""
    seen: List[Pass] = []
    original = CliqueEntry._summed

    def counting(self: CliqueEntry, sets: Any) -> Any:
        seen.append((self.index, len(sets), len(self.noisy)))
        return original(self, sets)

    monkeypatch.setattr(CliqueEntry, "_summed", counting)
    return seen


def _wide(big: int, columns: int = 32) -> Dict[str, Any]:
    """One clique of *columns* columns: two of *big* levels and the rest of one, all counts zero."""
    names = [f"c{n}" for n in range(columns)]
    levels = [f"l{n:04d}" for n in range(big)]
    domains = [{"levels": levels}] * 2 + [{"levels": ["x"]}] * (columns - 2)
    clique = _small(names, domains, ["0"] * (big * big))
    return _with_cliques(clique, columns={name: {"domain": "category"} for name in names})


def _hub(levels: int, tiny: int) -> Dict[str, Any]:
    """One clique over c0 and c1 and *tiny* cliques that each pair c0 with a column of their own."""
    names = ["c0", "c1"] + [f"t{n}" for n in range(tiny)]
    texts = [f"l{n:04d}" for n in range(levels)]
    big = _small(["c0", "c1"], [{"levels": texts}] * 2, ["0"] * (levels * levels))
    small = [
        _small(["c0", f"t{n}"], [{"levels": texts}, {"levels": ["x"]}], ["0"] * levels)
        for n in range(tiny)
    ]
    document = _with_cliques(big, *small, columns={n: {"domain": "category"} for n in names})
    document["privacy"]["privacyLoss"] = {"rho": "5000"}
    return document


def test_a_wide_clique_is_summed_in_one_pass_for_every_marginal_it_forms(
    passes: List[Pass],
) -> None:
    profile = parse_profile(_wide(60))
    (clique,) = profile.tables["subscriber"].cliques2
    assert len(derived_sets(clique)) == 32 + 32 * 31 // 2
    assert passes == [(0, 32 + 32 * 31 // 2, 3600)]


def test_a_hub_of_many_cliques_sums_each_clique_once_whatever_the_pairs(
    passes: List[Pass],
) -> None:
    tiny = 100
    parse_profile(_hub(40, tiny))
    assert sorted(index for index, _, _ in passes) == list(range(tiny + 1))
    # the big clique is summed onto c0, c1 and the pair, once; each small one onto its two columns
    # and the one it shares
    assert max(sets for _, sets, _ in passes) == 3
    assert sum(cells for _, _, cells in passes) == 40 * 40 + tiny * 40


def test_a_table_over_the_work_limit_is_refused_before_any_count_is_summed(
    passes: List[Pass],
) -> None:
    # 276 x 276 cells in 528 marginals is over 40,000,000; 275 x 275 is not
    assert cliques_module.MOST_TABLE_WORK == 40_000_000
    with pytest.raises(FluxError) as raised:
        parse_profile(_wide(276))
    assert "FLX-5036" in str(raised.value) and "cell visits" in str(raised.value)
    assert passes == []


def test_the_work_limit_counts_cells_marginals_and_the_cells_each_comparison_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _hub(10, 5)
    # 100 cells in 3 marginals, five cliques of 10 cells in 3 marginals, and the five-and-one cliques
    # compared on c0: 10 cells for each of 6
    work = 100 * 3 + 5 * 10 * 3 + 10 * 6
    monkeypatch.setattr(cliques_module, "MOST_TABLE_WORK", work)
    assert parse_profile(document)
    monkeypatch.setattr(cliques_module, "MOST_TABLE_WORK", work - 1)
    with pytest.raises(FluxError, match="cell visits"):
        parse_profile(document)


def test_a_marginal_in_any_order_is_the_naive_sum_whether_primed_or_not() -> None:
    levels = (("a", "b", "c"), ("x", "y"), ("p", "q", "r", "s"))
    noisy = tuple(Fraction(n * n, 7) - 3 for n in range(24))
    clique = CliqueEntry(
        expose_id="t",
        index=0,
        columns=("u", "v", "w"),
        levels=levels,
        noisy=noisy,
        noise=None,  # type: ignore[arg-type]
    )

    def naive(keep: List[str]) -> Dict[Tuple[str, ...], Fraction]:
        at = [clique.columns.index(name) for name in keep]
        out: Dict[Tuple[str, ...], Fraction] = {}
        for cell, value in zip(product(*levels), noisy, strict=True):
            key = tuple(cell[i] for i in at)
            out[key] = out.get(key, Fraction(0)) + value
        return out

    keeps = [
        ["u"],
        ["w", "u"],
        ["u", "w"],
        ["v", "w"],
        ["w", "v"],
        ["u", "v", "w"],
        ["w", "u", "v"],
    ]
    for keep in keeps:
        assert clique.marginal(keep) == naive(keep)
    clique.prime([(0,), (0, 2), (1, 2), (0, 1, 2)])
    for keep in keeps:
        assert clique.marginal(keep) == naive(keep)
        assert list(clique.marginal(keep)) == list(naive(keep))
    clique.clear_sums()
    assert clique.marginal(["w", "u"]) == naive(["w", "u"])
