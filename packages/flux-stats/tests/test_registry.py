# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One definition per statistic, in a registry a profiler can import alone.

Three kinds of assertion. Each identifier and each property kind has
exactly one definition, so a second computation cannot hide behind a
name already in use. The package imports the standard library and
nothing else, checked both on the source and in a fresh interpreter.
And the definitions, called the way a profiler calls them, form and
compare the quantities SEAM-FIDELITY-SPEC §4 and §5 name.

Nothing here imports the engine: these tests run where flux-sim-engine is
not installed (``make test-stats-alone``). What ties the registry to the
engine's own grammar is checked on the engine's side, in
``tests/stats/test_engine_agreement.py`` at the repository root.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, FrozenSet, Sequence, Set

import pytest

from flux_stats import (
    Check,
    GroupStatistic,
    Parameters,
    bin_index,
    definition,
    definition_for_kind,
    definitions,
    level_order,
    register,
)
from flux_stats import registry as registry_module
from flux_stats.errors import FluxError

pytestmark = pytest.mark.unit

#: ``packages/flux-stats``, or the copy of it mutmut runs these tests in.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC = PACKAGE_ROOT / "src"
STATS = SRC / "flux_stats"

#: Every module the registry's own source may import. A closed list, so a
#: new import is a decision someone makes here rather than a dependency
#: the profiler inherits without anyone noticing.
ALLOWED_IMPORTS: FrozenSet[str] = frozenset(
    {
        "__future__",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "fractions",
        "functools",
        "json",
        "math",
        "re",
        "sys",
        "types",
        "typing",
        "unicodedata",
        # flux_stats.exact_yaml's, imported when a document is first read: the
        # `yaml` extra (PyYAML). Importing the package never loads it.
        "yaml",
        "flux_stats",
        "flux_stats.canonical",
        "flux_stats.column_metrics",
        "flux_stats.definitions",
        "flux_stats.errors",
        "flux_stats.exact_yaml",
        "flux_stats.fidelity",
        "flux_stats.fixedpoint",
        "flux_stats.integers",
        "flux_stats.reading",
        "flux_stats.registry",
        "flux_stats.spelling",
    }
)

#: The modules importing the registry loads, and no others but the standard
#: library's.
STATS_CLOSURE: FrozenSet[str] = frozenset(
    {
        "flux_stats",
        "flux_stats.canonical",
        "flux_stats.column_metrics",
        "flux_stats.definitions",
        "flux_stats.errors",
        "flux_stats.exact_yaml",
        "flux_stats.fidelity",
        "flux_stats.fixedpoint",
        "flux_stats.reading",
        "flux_stats.registry",
        "flux_stats.spelling",
    }
)

#: The quantities §4 names, as each statistic spells them besides ``group``
#: and ``k``.
SPELLED: Dict[str, Set[str]] = {
    "mean/1": {"Sx"},
    "marginalHistogram/1": {"binCounts"},
    "levelHistogram/1": {"levelCounts"},
    "support/1": {"support"},
    "correlation/1": {"Sx", "Sy", "Sxx", "Sxy", "Syy"},
    "quantile/1": {"quantileWitness", "quantileBelow", "quantileAtOrBelow"},
    "quantile/2": {"quantileBin", "binBelow", "binThrough"},
}


# ---------------------------------------------------------------------------
# One definition per identifier, one per kind
# ---------------------------------------------------------------------------


def test_each_identifier_is_registered_exactly_once() -> None:
    identifiers = [entry.identifier for entry in definitions()]
    assert len(identifiers) == len(set(identifiers))
    assert identifiers == sorted(identifiers)


def test_registering_the_same_definition_again_is_harmless() -> None:
    mean = definition("mean/1")
    assert register(mean) is mean
    assert [entry.identifier for entry in definitions()].count("mean/1") == 1


def test_a_second_definition_under_one_identifier_is_refused() -> None:
    with pytest.raises(ValueError, match="already registered"):
        register(replace(definition("mean/1"), summary="another computation"))


def test_a_second_statistic_for_one_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="one kind has one statistic"):
        register(replace(definition("mean/1"), identifier="mean/2"))
    with pytest.raises(KeyError):
        definition("mean/2")


@pytest.mark.parametrize(
    "identifier", ["Mean/1", "mean", "mean/0", "mean/01", "mean-x/1", "mean/1/2", ""]
)
def test_a_malformed_identifier_is_refused(identifier: str) -> None:
    with pytest.raises(ValueError, match="<name>/<major>"):
        register(replace(definition("mean/1"), identifier=identifier))


def test_an_empty_summary_is_refused() -> None:
    with pytest.raises(ValueError, match="summary"):
        register(replace(definition("mean/1"), identifier="median/1", summary=" "))


def test_a_new_definition_is_found_by_its_identifier_and_its_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The definitions register at import time, before any test runs; registering
    # one more, into copies of the two tables, exercises register itself.
    monkeypatch.setattr(registry_module, "_DEFINITIONS", dict(registry_module._DEFINITIONS))
    monkeypatch.setattr(registry_module, "_BY_KIND", dict(registry_module._BY_KIND))
    median = register(replace(definition("mean/1"), identifier="median/1", kind="median"))
    assert definition("median/1") is median
    assert definition_for_kind("median") is median
    assert median in definitions()


def test_an_unknown_identifier_or_kind_names_what_is_registered() -> None:
    identifiers = sorted(entry.identifier for entry in definitions())
    with pytest.raises(KeyError) as unknown:
        definition("median/1")
    assert unknown.value.args == (
        f"no statistic is registered as 'median/1'; registered: {identifiers}",
    )
    with pytest.raises(KeyError) as unserved:
        definition_for_kind("median")
    served = ["correlation", "levelHistogram", "marginalHistogram", "mean", "quantile"]
    served += ["quantile (bins)", "support"]
    assert unserved.value.args == (f"no statistic serves kind 'median'; registered: {served}",)


def test_each_statistic_spells_the_quantities_section_4_names() -> None:
    state = GroupStatistic(group="", k=0)
    for entry in definitions():
        assert set(state.to_document(entry)) == {"group", "k"} | SPELLED[entry.identifier]
    assert state.to_document("quantile")["quantileWitness"] is None


# ---------------------------------------------------------------------------
# Small enough to import alone
# ---------------------------------------------------------------------------


def _imported_names(path: Path) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


#: The one import mutation testing adds. mutmut 3 rewrites every module it
#: mutates to import this trampoline, writes the rewritten tree to a
#: directory called ``mutants`` and runs these tests there, as the weekly
#: mutation run (``[tool.mutmut]`` in pyproject.toml) does. In that copy the
#: import is the instrumentation, not a dependency of the registry.
MUTATION_TRAMPOLINE = "mutmut.mutation.trampoline"


def _instrumented() -> bool:
    """Whether these tests run over mutmut's rewritten copy of the tree.

    Both conditions, so that the import committed to the real tree is still
    a stray: only mutmut's copy sits in a directory called ``mutants``.
    """
    return PACKAGE_ROOT.name == "mutants" and any(
        MUTATION_TRAMPOLINE in _imported_names(path) for path in STATS.rglob("*.py")
    )


def test_the_registry_source_imports_only_its_allowlist() -> None:
    allowed = ALLOWED_IMPORTS | ({MUTATION_TRAMPOLINE} if _instrumented() else set())
    for path in sorted(STATS.rglob("*.py")):
        stray = _imported_names(path) - allowed
        assert not stray, f"{path.relative_to(PACKAGE_ROOT)} imports {sorted(stray)}"


def _fresh(statement: str) -> Dict[str, Any]:
    """Run *statement* in a fresh interpreter: what it loaded, and from where.

    ``-I`` keeps the working directory and PYTHONPATH off the path, and the
    checkout under test goes first explicitly, so what is measured is this
    checkout's package and not whichever copy of it is installed.
    """
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        f"{statement}\n"
        "stats = sys.modules.get('flux_stats')\n"
        "print(json.dumps({'modules': sorted(sys.modules),"
        " 'stats': getattr(stats, '__file__', None)}))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    loaded: Dict[str, Any] = json.loads(completed.stdout)
    return loaded


@pytest.mark.parametrize(
    "module, closure",
    [
        ("flux_stats", STATS_CLOSURE),
        ("flux_stats.profile", STATS_CLOSURE | {"flux_stats.profile"}),
    ],
)
def test_a_fresh_interpreter_imports_the_registry_and_nothing_else(
    module: str, closure: FrozenSet[str]
) -> None:
    """What importing the registry or the profile reader loads, against a bare interpreter.

    The difference is taken so that whatever the environment loads before
    any import (a coverage hook, an editable-install finder) is not
    counted against the registry. Where the engine is installed too, as in
    the engine's own test run, importing this package still loads none of it.
    """
    bare = _fresh("")
    loaded = _fresh(f"import {module}")
    assert str(loaded["stats"]).startswith(str(SRC)), loaded["stats"]
    added = set(loaded["modules"]) - set(bare["modules"])
    own = {name for name in added if name.split(".")[0] == "flux_stats"}
    assert own == closure
    assert not {name for name in added if name.split(".")[0] == "flux_engine"}
    if _instrumented():
        # A mutation run loads mutmut's trampoline and what it imports; the
        # closure above is the part that is the registry's own.
        return
    outside = {name.split(".")[0] for name in added} - set(sys.stdlib_module_names)
    assert outside == {"flux_stats"}, f"third-party modules loaded: {sorted(outside)}"


# ---------------------------------------------------------------------------
# The definitions, called as a profiler calls them
# ---------------------------------------------------------------------------


def _measure(kind: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    return definition_for_kind(kind).measure("", values, parameters)


def test_the_six_measures_form_the_section_4_quantities() -> None:
    assert _measure("mean", [3, 5, 7], Parameters()).sum_x == 15
    histogram = _measure("marginalHistogram", [0, 1, 2, 4, 4], Parameters(edges=(0, 2, 4)))
    assert (histogram.k, histogram.bin_counts) == (5, (2, 3))
    levels = _measure("levelHistogram", [2, 0, 2], Parameters(level_count=3))
    assert levels.level_counts == (1, 0, 2)
    support = _measure("support", ["IE", "GB", "IE"], Parameters())
    assert (support.support, support.support_counts) == (("GB", "IE"), (1, 2))
    moments = _measure("correlation", [(1, 2), (3, 5)], Parameters())
    assert (moments.k, moments.sum_x, moments.sum_y) == (2, 4, 7)
    assert (moments.sum_xx, moments.sum_yy, moments.sum_xy) == (10, 29, 17)
    quantile = _measure("quantile", [9, 1, 5, 5], Parameters(level=Fraction(1, 2)))
    assert quantile.quantile_witness == 5
    assert (quantile.quantile_below, quantile.quantile_at_or_below) == (1, 3)
    empty = definition_for_kind("quantile").measure("g", [], Parameters(level=Fraction(1, 2)))
    assert (empty.group, empty.k, empty.quantile_witness) == ("g", 0, None)


def test_the_cell_rules_are_the_shared_ones() -> None:
    assert [bin_index(units, (0, 2, 4)) for units in (0, 1, 2, 3, 4)] == [0, 0, 1, 1, 1]
    assert level_order(["pro", "basic", "plus"]) == ("basic", "plus", "pro")


def _check(**fields: Any) -> Check:
    return Check(subject="p0", **fields)


def test_the_mean_compares_by_the_declared_tolerance_type() -> None:
    mean = definition_for_kind("mean")
    reference = GroupStatistic(group="", k=2, sum_x=20)
    synthetic = GroupStatistic(group="", k=2, sum_x=22)
    absolute = _check(tolerance="absolute", margin_units=1)
    relative = _check(tolerance="relative", margin=Fraction(1, 10))
    assert mean.compare(synthetic, reference, absolute) is True
    assert mean.compare(synthetic, reference, replace(absolute, margin_units=0)) is False
    assert mean.compare(synthetic, reference, relative) is True
    assert mean.compare(synthetic, reference, replace(relative, margin=Fraction(1, 20))) is False


def test_the_cell_statistics_compare_by_total_variation() -> None:
    distance = _check(tolerance="distance", margin=Fraction(1, 10))
    bins = definition_for_kind("marginalHistogram")
    levels = definition_for_kind("levelHistogram")
    reference = GroupStatistic(group="", k=10, bin_counts=(5, 5), level_counts=(5, 5))
    near = GroupStatistic(group="", k=10, bin_counts=(6, 4), level_counts=(6, 4))
    far = GroupStatistic(group="", k=10, bin_counts=(8, 2), level_counts=(8, 2))
    for statistic in (bins, levels):
        assert statistic.compare(near, reference, distance) is True
        assert statistic.compare(far, reference, distance) is False
    # Each reads its own cells: a histogram never reads level counts.
    mixed = GroupStatistic(group="", k=10, bin_counts=(5, 5), level_counts=(8, 2))
    assert bins.compare(mixed, reference, distance) is True
    assert levels.compare(mixed, reference, distance) is False


def test_support_counts_strange_records_not_strange_values() -> None:
    support = definition_for_kind("support")
    reference = GroupStatistic(group="", k=3, support=("GB", "IE"))
    synthetic = GroupStatistic(group="", k=4, support=("FR", "IE"), support_counts=(3, 1))
    count = _check(tolerance="count", max_non_members=2)
    assert support.compare(synthetic, reference, count) is False
    assert support.compare(synthetic, reference, replace(count, max_non_members=3)) is True


def test_a_quantile_without_a_witness_names_its_side_and_property() -> None:
    quantile = definition_for_kind("quantile")
    check = _check(tolerance="absolute", margin_units=0, level=Fraction(1, 2))
    witnessed = _measure("quantile", [1, 2, 3], Parameters(level=Fraction(1, 2)))
    empty = GroupStatistic(group="", k=0)
    assert quantile.compare(witnessed, witnessed, check) is True
    with pytest.raises(FluxError, match="committed side offers no quantile witness for p0"):
        quantile.compare(witnessed, empty, check)
    with pytest.raises(FluxError, match="profile side offers no quantile witness for p0"):
        quantile.compare(witnessed, empty, replace(check, reference_side="profile"))
    lying = replace(witnessed, quantile_below=2)
    with pytest.raises(FluxError, match="synthetic side's quantile witness for p0"):
        quantile.compare(lying, witnessed, check)
