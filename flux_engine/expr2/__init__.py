# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux-expr/2``: FLUID SQL/1 and FLUX's four extensions (EXPR-SPEC-2, ADR 0008).

``flux-expr/3`` is flux-expr/2 plus a date part, date arithmetic and string
concatenation (EXPR-SPEC-3, ADR 0013): ``parse(text, side=Side.FLUX,
language="flux-expr/3")``. A text that names no language is flux-expr/2.

The language of the rules about valid data. Its core is FLUID SQL/1, the
closed SQL expression subset of the FLUID RFC ``RFC-data-rules.md`` §2,
which a contract's invariants are written in; a FLUX-side rule may also use
the previous version of an entity, paths over several links, aggregates
over a parent's children and windows over a row's siblings. flux-expr/1
(:mod:`flux_engine.expr`) is a separate language and is untouched.

The plan-time path is three calls::

    from flux_engine.expr2 import Side, check, parse

    parsed = parse("ROUND(net * (1 + tax_rate), 2)", side=Side.CORE)  # FLX-6103/6104
    checked = check(parsed, model, "invoices", position="value", target="gross")
    checked.canonical            # the text plan.json stores
    checked.evaluate(row)        # an exact value, or None; never raises

Numbers are exact rationals (``Fraction``), truth is two-valued, and an
expression reads no clock, no environment and no seed-tree path. Nothing in
the engine generates from a contract's rules that read the row's own columns
(:mod:`flux_engine.generator.rules`); checking runs against rules and the rest of
EXPR-SPEC-2 §6 to §9 come next.
"""

from __future__ import annotations

from flux_engine.expr2.ast import LANGUAGES, Node, to_json, unparse
from flux_engine.expr2.check import PREDICATE, VALUE, Checked, Read, check
from flux_engine.expr2.errors import CATALOG, Expr2Error
from flux_engine.expr2.evaluate import RowView, Session
from flux_engine.expr2.functions import (
    add_days,
    add_months,
    add_years,
    days_between,
    months_between,
    round_exact,
    try_add_months,
    years_between,
)
from flux_engine.expr2.parser import MAX_DEPTH, Parsed, Side, parse
from flux_engine.expr2.rows import MemoryRow, Rows
from flux_engine.expr2.rules import (
    LANGUAGE,
    FluxSideRule,
    aggregate_tree,
    check_flux_side_rule,
    check_language,
    check_rule_ids,
    is_contract_aggregate,
    uses_extension,
)
from flux_engine.expr2.scope import Expose, Link, Model, Versions, model_from_json
from flux_engine.expr2.types import MAX_ROUND_SCALE, ColumnType, Kind, SqlType, column_type, fit

#: The language this package implements.
EXPR_LANGUAGE = LANGUAGE

__all__ = [
    "CATALOG",
    "EXPR_LANGUAGE",
    "LANGUAGE",
    "LANGUAGES",
    "MAX_DEPTH",
    "MAX_ROUND_SCALE",
    "PREDICATE",
    "VALUE",
    "Checked",
    "ColumnType",
    "Expose",
    "Expr2Error",
    "FluxSideRule",
    "Kind",
    "Link",
    "MemoryRow",
    "Model",
    "Node",
    "Parsed",
    "Read",
    "RowView",
    "Rows",
    "Session",
    "Side",
    "SqlType",
    "Versions",
    "add_days",
    "add_months",
    "add_years",
    "aggregate_tree",
    "check",
    "check_flux_side_rule",
    "check_language",
    "check_rule_ids",
    "column_type",
    "days_between",
    "fit",
    "is_contract_aggregate",
    "model_from_json",
    "months_between",
    "parse",
    "round_exact",
    "to_json",
    "try_add_months",
    "unparse",
    "uses_extension",
    "years_between",
]
