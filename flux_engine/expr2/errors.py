# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``FLX-61nn`` and ``FLX-62nn`` codes that flux-expr/2 and flux-expr/3 raise (EXPR-SPEC-2 §12, EXPR-SPEC-3 §7).

KERNEL-SPEC §13 gives the 6000 band to the expression language, and the
catalog rule of :mod:`flux_engine.core.errors` is that the module which
raises a code registers it at import time. This module is that place for
flux-expr/2: importing it registers every code below, and
``flux_engine.core.errors._CORE_CODE_MODULES`` names it so that
``fluxsim explain`` finds them without an expression having been parsed.

flux-expr/1's ``FLX-6001`` to ``FLX-6005`` are never raised here. The rule
codes that need bands, FLUX settings or the planner are registered here too:
generation from rules (:mod:`flux_engine.generator.rules`) raises them. The
codes of the steps that draw violations and generate dependencies join when
those land (maintainers' decision, 2026-11-16).

Every diagnostic names a position in the NFC-normalized text, a name or a
construct. None quotes a value read from a row (KERNEL-SPEC §9).
"""

from __future__ import annotations

from typing import Mapping, Optional

from flux_engine.core.errors import ErrorCode, FluxError, register

__all__ = [
    "CATALOG",
    "Expr2Error",
    "FLX_6101",
    "FLX_6102",
    "FLX_6103",
    "FLX_6104",
    "FLX_6105",
    "FLX_6106",
    "FLX_6107",
    "FLX_6108",
    "FLX_6109",
    "FLX_6110",
    "FLX_6111",
    "FLX_6112",
    "FLX_6113",
    "FLX_6201",
    "FLX_6202",
    "FLX_6203",
    "FLX_6204",
    "FLX_6205",
    "FLX_6206",
    "FLX_6208",
    "FLX_6209",
    "FLX_6210",
]

FLX_6101 = register(
    "FLX-6101",
    "a FLUX-side rule declares no language",
    "A rule declared in a bundle names the grammar of its texts in a required "
    "`language` key (EXPR-SPEC-2 §1). Add `language: flux-expr/2`, or "
    "`language: flux-expr/3` for a rule that uses a date part, date arithmetic or "
    "`||` (EXPR-SPEC-3 §1). A rule that needs none of the extensions belongs in "
    "the FLUID contract instead.",
)
FLX_6102 = register(
    "FLX-6102",
    "the declared language is not one this entry accepts",
    "A FLUX-side rule accepts `language: flux-expr/2` or `language: flux-expr/3`. "
    "flux-expr/1 is the "
    "language of segments, journeys, detectors and persona mappings, and a "
    "contract's texts carry no language key: the contract's fluidVersion names it.",
)
FLX_6103 = register(
    "FLX-6103",
    "outside the core grammar and the extensions, or past a limit",
    "flux-expr/2 and flux-expr/3 are closed: a text parses only from FLUID SQL/1's "
    "grammar (RFC-data-rules.md §2.1) or, in a FLUX-side rule, from the four "
    "extensions of EXPR-SPEC-2 §2 and, under `language: flux-expr/3`, the three of "
    "EXPR-SPEC-3 §2 (EXTRACT, ADD_DAYS, ADD_MONTHS, ADD_YEARS and `||`). A text is at most 4,096 code points after NFC "
    "normalization and its syntax tree at most 64 levels deep, where every "
    "operator, NOT, minus, call, CASE and parenthesis is a level. It holds no "
    "`;`, and no `--` or `/*` outside a string or a quoted name, because SQL "
    "reads those as comments: write a double negation as `x - -1`. ROUND's "
    "scale is at most 38, and its mode `'HALF_AWAY_FROM_ZERO'` or "
    "`'HALF_TO_EVEN'`, in upper case.",
)
FLX_6104 = register(
    "FLX-6104",
    "a reserved word or the NULL literal used where it has no meaning",
    "A keyword stands where a name or a value belongs: quote a column whose "
    'name is a keyword (`"end"`). NULL takes its type from a typed operand '
    "beside it, as in `x + NULL` or `CASE WHEN c THEN x ELSE NULL END`. It is "
    "never an operand of a comparison, BETWEEN or an IN list, where the test "
    "would always be FALSE: write `x IS NULL`, `x IS NOT NULL` or "
    "`a IS NOT DISTINCT FROM b`. Nor is it the operand of IS [NOT] NULL, a "
    "minus or ROUND, a bool (a NULL predicate counts as FALSE), a whole text, "
    "or every operand of a construct.",
)
FLX_6105 = register(
    "FLX-6105",
    "a first name that is a root and also a column or relationship",
    "In a FLUX-side text, row, link, previous and CHILDREN are roots. When the "
    "expose also has a column or relationship of that name, say which one you "
    "mean: `row.<name>` for the column, `link.<name>` for the relationship. A "
    "PARTITION BY relationship whose name is also a column is written "
    "`link.<name>`.",
)
FLX_6106 = register(
    "FLX-6106",
    "a path that does not resolve as written",
    "The path names something the expose has, but not as a path can reach it: "
    "`previous` on an expose whose entity has no versions, `CHILDREN.<expose>` "
    "with no relationship to this expose or several and none named, a children "
    "collection outside an aggregate's first argument, a partition that is not "
    "a relationship of the row, or a hop through a relationship whose target "
    "declares a validity window (RFC-data-rules.md §2.2). When the emit that declares the "
    "window is a history, a path or a summary reads its tracked, validity or rule columns "
    "only through an `asOf` entry that names the relationship and the date.",
)
FLX_6107 = register(
    "FLX-6107",
    "an aggregate or window where it is not allowed",
    "An aggregate's first argument is a children collection, and its second "
    "argument holds no window and no aggregate over the same collection. A "
    "window's arguments hold no window. Aggregates and windows are FLUX-side "
    "extensions: a contract writes a summary as `derive` with `aggregate`.",
)
FLX_6108 = register(
    "FLX-6108",
    "a window whose value depends on ties has an order that is not total",
    "ROW_NUMBER, FIRST_VALUE, LAST_VALUE, LAG and LEAD depend on how tied rows "
    "are ordered. Their ORDER BY keys, with the partitioning relationship's "
    "columns, must name every key column the expose declares, so that no two "
    "rows tie. Add the missing key columns to the end of ORDER BY. RANK and "
    "DENSE_RANK take any order.",
)
FLX_6109 = register(
    "FLX-6109",
    "a type error",
    "Operands or arguments have the wrong type (comparisons take one type, "
    "arithmetic takes numbers, BETWEEN and ORDER BY take an orderable type, "
    "EXTRACT and ADD_* take a date, ADD_* a count of scale 0, `||` strings and "
    "integers of scale 0), a "
    "string literal is neither a level nor a placeholder of the enum column it "
    "is compared with, or a literal argument has the wrong form (a DATE that is "
    "not a calendar date, a LAG offset). FLUX checks contract texts as well as "
    "FLUX-side ones, because it evaluates both.",
)
FLX_6110 = register(
    "FLX-6110",
    "a derived value does not fit its column, or a sum has an unbounded scale",
    "A derive's value must have its column's type, and a number a static scale "
    "no larger than the column's: wrap a product or a quotient in "
    "`ROUND(x, s)`. A float column, or a decimal without (p,s), is never a "
    "derive target, and an enum column is never the target of a concatenation "
    "(`||`). SUM and AVG take a number of bounded scale.",
)
FLX_6111 = register(
    "FLX-6111",
    "a name for a column that has no FLUID SQL/1 type",
    "FLUID SQL/1 types six kinds of column (RFC-data-rules.md §2.3). A column "
    "declared as time, interval, money, json, array, struct, map, binary or "
    "any other type outside that table cannot be named in an expression.",
)
FLX_6112 = register(
    "FLX-6112",
    "a value does not fit its column when the row is made",
    "A computed value has more digits than the column's precision allows, more "
    "decimal places than its scale, or lies outside the column's integer range. "
    "The row would violate the rule that derives it; FLUX refuses to write it.",
)
FLX_6113 = register(
    "FLX-6113",
    "a name that the row scope does not know",
    "Names resolve against the expose that declares the text: its columns, its "
    "relationships and their targets' columns, and the exposes whose rows point "
    "to it. A name is case-sensitive and spelled as the contract's schema "
    "spells it.",
)
FLX_6201 = register(
    "FLX-6201",
    "a rule has no id, a repeated id, or an id that is not an identifier",
    "Every rule and dependency has an `id` of ASCII letters, digits and "
    "underscores, starting with a letter. A contract's ids are unique within "
    "that contract, and a bundle's FLUX-side ids are unique within the bundle.",
)
FLX_6202 = register(
    "FLX-6202",
    "a FLUX setting names a column for a rule that cannot generate it",
    'The `generates` setting of a contract rule (extensions["com.agentics.flux/1"]'
    ".emits[<exposeId>].invariants.<id>) names the column that a `check` of bounds or "
    "difference form generates. It is refused on a rule that is not a `check`, on a `check` "
    "of any other form, and when it names a column that the value side or the `when` reads.",
)
FLX_6203 = register(
    "FLX-6203",
    "a column has two sources across the contract and the bundle",
    "Every value has one source (EXPR-SPEC-2 §9). A `derive` together with a `generates` "
    "on the same column, two `generates` on one column, a `dependency`'s `to` column that "
    "another rule or dependency also determines, or a scenario mapping on a column that "
    "a rule determines are two sources, whatever their `when`s. So is a rule on the column "
    "a history's `end.column` names, which the history sets: its end value on an ending "
    "version and a drawn level on the others. A rule on a column that a snapshots, events or "
    "periods entry sets (a column of the version, an image, the op) is one too, since the "
    "projection writes that cell. Several `derive` rules on one column are one source.",
)
FLX_6204 = register(
    "FLX-6204",
    "a band that this run cannot meet",
    "This version generates no violating row beyond a dependency's `to` columns' own NULLs. "
    "Under a strict run, a rule whose band no violation would meet is refused: its band needs "
    "violating rows, and the run would generate none. A dependency is also refused, in every "
    "run, when its band allows fewer than no violating rows, or when its upper bound is below "
    "the share of rows that the `to` column with the most declared NULLs can make violate on "
    "its own; in a strict run, also when the bound leaves less than four standard deviations "
    "of the count above that share at the declared row count. On an emit that declares no "
    "`rows`, a dependency's `rows` band, and in a strict run any upper bound of one, is refused "
    "as well, since the plan has no count to read it against: declare the emit's `rows`. "
    "A column that a rule over a link determines (a copy, or a `derive` over a path) is refused "
    "in every run when the rule allows no violating row and the column declares a `nullFraction` "
    "above zero, since its own NULLs come first and would violate the rule: drop the "
    "`nullFraction`, or give the rule a `rows` band that admits them. "
    "Otherwise widen the band, or drop the strict run. Several `within` entries for one date "
    "along a chain are refused too when a middle expose of the chain does not declare a "
    "`within` entry that ties its lifetime's start to the next relationship of the chain: "
    "without that tie the lifetimes of the levels need not meet. `fluxsim init` refuses with it "
    "too when the declared date ranges it widens for an enforced `within` entry do not settle: "
    "the plan still asks for wider ranges once they are written.",
)
FLX_6205 = register(
    "FLX-6205",
    "an interval open on one side with nothing declared to draw it",
    "A value drawn inside an interval that has no upper end needs a declared draw over it. A "
    "date that `within` sets, in a run with no `spec.timeBounds`, is drawn inside its parent's "
    "lifetime, which is open when the parent has no end: the entry then declares an `offset` "
    "with a positive weight on a day from 0 up, or the run declares its time bounds. A row whose "
    "interval has no upper end and whose offset keeps no value inside it is refused as well.",
)
FLX_6206 = register(
    "FLX-6206",
    "a rule's check, when, or the expr of a bool column is not bool",
    "A `check`, a `when` and the `expr` of a `derive` whose column is bool are "
    "predicates. Write a comparison, IS [NOT] NULL, a bool column or a bool "
    "CASE there.",
)
FLX_6208 = register(
    "FLX-6208",
    "rules that determine columns in a cycle",
    "The determined columns form a graph through what each rule reads (its `expr`, `when`, "
    "`k` and `x`, or a dependency's `from` columns). A column that is read by a rule that determines it, directly or through "
    "other rules, has no order in which to generate; a dependency that names one column on both sides "
    "is the shortest case. The message names the rules.",
)
FLX_6209 = register(
    "FLX-6209",
    "a FLUX-side rule the contract can carry",
    "A rule that uses none of the extensions (previous, multi-link paths, "
    "aggregates over children, windows over siblings, and under flux-expr/3 "
    "EXTRACT, ADD_DAYS, ADD_MONTHS, ADD_YEARS and `||`), or whose whole value is "
    "exactly a contract `derive` with `aggregate`, is a rule about the data "
    "itself. Declare it in the FLUID contract's `invariants`, where every "
    "consumer reads it.",
)
FLX_6210 = register(
    "FLX-6210",
    "a rule that determines a column in a way this version does not generate yet",
    "FLUX generates a `derive` with `expr` that reads the row's own columns, a `check` "
    "of difference form over days that the Simulation names a column for, and a one-way "
    "`dependency` whose band is zero violations or an upper bound (its violating rows are its "
    "`to` columns' own NULLs). On kernel 0.2.0 it also generates a `derive` with `expr`, and a "
    "difference form over days, that read a column of the row a relationship reaches (one hop "
    "in a contract, a chain of to-one relationships in a FLUX-side rule), and a FLUX-side "
    "`derive` whose value holds a window over the row's siblings, and a contract `derive` with "
    "`aggregate` over a relationship the Simulation lays out with a `counts` entry. Every "
    "other rule that would determine a column is refused: an aggregate over any other relationship, a "
    "dependency with bothWays or a "
    "band with a lower bound above zero, a dependency `to` column that is an identifier, is "
    "drawn given another or declares no draw and is not a foreign key, a bounds form, a "
    "month or year difference, a rule that reads another row (previous, children, a bounds "
    "form over a relationship, and any rule over a relationship "
    "or a window on kernel 0.1.0 included), a cycle among the exposes that rules read through, a path into an expose the "
    "Simulation does not generate from statistics, a FLUX-side check with a generates "
    "setting, a percent band that needs violations, a key or foreign key (a dependency may "
    "set a single foreign key), a relationship's "
    "toColumns, a column another is drawn given, and an emit with no fidelity block. A "
    "`sharedColumns` copy into another relationship's foreign key is refused when the table "
    "the copy goes through does not hold the column as a foreign key to the same keys of "
    "that relationship's target, when the relationship is inner and the copy may be NULL "
    "through a left join, and when copies read each other in a cycle.",
)

#: Every code this package raises, by code string.
CATALOG: Mapping[str, ErrorCode] = {
    entry.code: entry
    for entry in (
        FLX_6101,
        FLX_6102,
        FLX_6103,
        FLX_6104,
        FLX_6105,
        FLX_6106,
        FLX_6107,
        FLX_6108,
        FLX_6109,
        FLX_6110,
        FLX_6111,
        FLX_6112,
        FLX_6113,
        FLX_6201,
        FLX_6202,
        FLX_6203,
        FLX_6204,
        FLX_6205,
        FLX_6206,
        FLX_6208,
        FLX_6209,
        FLX_6210,
    )
}


class Expr2Error(FluxError):
    """A refusal of a flux-expr/2 text, with its code and, where it has one, its position.

    ``position`` is the 0-based offset into the NFC-normalized text (the
    lexer normalizes before it reads a character), so it indexes the text a
    caller gets back from :func:`flux_engine.expr2.lexer.normalize`.
    """

    def __init__(self, code: ErrorCode, detail: str, *, position: Optional[int] = None) -> None:
        self.position = position
        where = "" if position is None else f" (at offset {position})"
        super().__init__(code, detail + where)
