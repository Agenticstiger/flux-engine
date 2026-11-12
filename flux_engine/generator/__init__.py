# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Contract-schema-driven synthetic row generation (6 column types x 4 distribution families).

SEAM-FIDELITY-SPEC §1. The generator takes a *resolved expose* -- the
ordered column set :func:`flux_engine.seam.contract.compile_expose`
produces -- and the aggregate statistics the emit's fidelity block
declares, and produces rows. It reads no records, and there is no code
path by which it could: its only inputs are a schema and a set of declared
aggregates (see :mod:`flux_engine.generator.statistics` and ADR-0003).

The three-line version::

    plan = build_plan(expose, sim_id=..., seam_digest=H, rows=n, fidelity=block)
    rows = generate_rows(plan, root=seed_tree_root)
    writer.write_all(rows)          # flux_engine.seam.emit renders and digests

``plan.digest()`` is SEAM-FIDELITY-SPEC §6's ``generatorDigest``: it
covers the generator's version, ``H``, the row count and every column's
family and declared parameters, so §8's change table holds mechanically.

Everything fails at plan time or not at all: :func:`build_plan` resolves
every column to a family and checks that family's parameters are declared
before a single draw is made.
"""

from __future__ import annotations

from flux_engine.generator.families import (
    FAMILY_TABLE,
    FLX_3401,
    ColumnType,
    Family,
    FamilyRow,
    choose_family,
    classify_column,
    is_identifier,
)
from flux_engine.generator.paths import (
    GENERATOR_PATH_SHAPE,
    GeneratorStreams,
    StreamSource,
    generator_column_path,
    matches_generator_shape,
    resolve_stream_source,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    IDENTIFIER_DEFAULT_LENGTH,
    ColumnPlan,
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
    identifier_value,
)
from flux_engine.generator.statistics import (
    COLUMN_STATISTIC_KEYS,
    FLX_3402,
    ColumnStatistics,
    exact_decimal,
    statistics_for,
    statistics_from_fidelity,
)

__all__ = [
    "COLUMN_STATISTIC_KEYS",
    "FAMILY_TABLE",
    "FLX_3401",
    "FLX_3402",
    "GENERATOR_PATH_SHAPE",
    "GENERATOR_VERSION",
    "IDENTIFIER_DEFAULT_LENGTH",
    "ColumnPlan",
    "ColumnStatistics",
    "ColumnType",
    "Family",
    "FamilyRow",
    "GeneratorPlan",
    "GeneratorStreams",
    "StreamSource",
    "build_plan",
    "choose_family",
    "classify_column",
    "exact_decimal",
    "generate_column",
    "generate_row_slice",
    "generate_rows",
    "generator_column_path",
    "identifier_value",
    "is_identifier",
    "matches_generator_shape",
    "resolve_stream_source",
    "statistics_for",
    "statistics_from_fidelity",
]
