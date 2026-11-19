# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One definition per statistic, shared by everything that forms or reads one.

:mod:`flux_stats.registry` maps a stable identifier (``mean/1``) to a
:class:`StatisticDefinition`: the quantities the statistic keeps, the pure
function that forms them from values already read onto the grid, their
canonical spelling, and the comparison of two of them.
:mod:`flux_stats.definitions` registers the seven statistics the
fidelity check compares today, one per property kind and one more for a
quantile that declares bins (ADR 0012), and
:mod:`flux_stats.column_metrics` the seven column metrics a profile carries
under OpenLineage's names. :mod:`flux_stats.profile` reads the
profile document (docs/spec/PROFILE-SPEC.md) those statistics are written in.
:mod:`flux_stats.reading` reads the records they are formed from: a cell, a
null, a number, an instant, a category and a group, as the fidelity check reads
them. :mod:`flux_stats.fidelity` parses the fidelity block a Simulation
declares, the grammar the check and a profiler share.

The fidelity check's boundary and verifier in flux-sim-engine import this
package to form and compare ``S`` (SEAM-FIDELITY-SPEC §4, §5), and a profiler
imports it to form the same numbers over the data it profiles, without the
engine: the package imports the standard library and nothing else.
:mod:`flux_stats.fixedpoint` is the fixed-point grid every statistic is
formed and compared on (KERNEL-SPEC §12), and :mod:`flux_stats.errors` the
``FLX-nnnn`` code catalog its refusals are numbered in (KERNEL-SPEC §13).
:mod:`flux_stats.tails` decides binomial tails exactly without writing them
out, for the shape checks that read a run against a profile.
:mod:`flux_stats.sampling` estimates a ratio from a sample of whole blocks,
with an exact 95 % interval, for sample-based verification.
:mod:`flux_stats.integers` tests a count, an ordinal or a size: an ``int``,
never a ``bool``, and not below a floor.
:mod:`flux_stats.canonical` is the canonical form every FLUX digest is taken
over (KERNEL-SPEC §2.1), and :mod:`flux_stats.exact_yaml` the exact, bounded read
of a bundle's YAML text the engine takes it over; that one needs PyYAML, the
``yaml`` extra, when it first reads a document.
"""

from __future__ import annotations

from flux_stats import canonical as _canonical  # noqa: F401  (registers FLX-1001, FLX-1003..1007)
from flux_stats import column_metrics as _column_metrics  # noqa: F401  (registers them)
from flux_stats import exact_yaml as _exact_yaml  # noqa: F401  (registers FLX-3105)
from flux_stats import fidelity as _fidelity  # noqa: F401  (registers FLX-3001, FLX-5030..5034)
from flux_stats import reading as _reading  # noqa: F401  (registers FLX-5022, FLX-5031)
from flux_stats.definitions import bin_index, level_order
from flux_stats.fidelity import definition_for_property
from flux_stats.registry import (
    Check,
    GroupStatistic,
    MetricDefinition,
    Parameters,
    StatisticDefinition,
    definition,
    definition_for_kind,
    definitions,
    metric,
    metrics,
    read_group,
    register,
    register_metric,
)

__all__ = [
    "Check",
    "GroupStatistic",
    "MetricDefinition",
    "Parameters",
    "StatisticDefinition",
    "bin_index",
    "definition",
    "definition_for_kind",
    "definition_for_property",
    "definitions",
    "level_order",
    "metric",
    "metrics",
    "read_group",
    "register",
    "register_metric",
]
