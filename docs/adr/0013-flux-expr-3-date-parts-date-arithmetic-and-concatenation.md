# ADR 0013: flux-expr/3 adds a date part, date arithmetic and string concatenation to flux-expr/2

## Status

Accepted 2026-11-18. The specification is [EXPR-SPEC-3](../spec/EXPR-SPEC-3.md). The engine implements
the language (`flux_engine.expr2` takes a `language` argument); reading FLUX-side rules written in it from a bundle,
and generating from them, are later changes. Nothing a bundle does today changes.

**Amended 2026-11-18:** the engine now reads FLUX-side rules from a bundle, and generates from those that read their
own row. This record amends
[ADR 0008](./0008-flux-expr-2-derived-columns-and-rules.md)'s list of extensions: the four it names stay the only
ones of flux-expr/2, and flux-expr/3 is flux-expr/2 with three more.

## Context

[ADR 0011](./0011-rows-follow-their-key-entity-and-parent.md) plans three steps so that synthetic rows follow their
key, their entity and their parent. Its step 1 makes a reference dimension's attributes rules over its key. The
calendar rules that FLUID SQL/1 can state go in the contract. Three columns of a calendar need what FLUID SQL/1
leaves out (the FLUID RFC `RFC-data-rules.md`, section 2.7):

- the day of the month (`d_dom`), which is a date part;
- the same day a year back (`d_same_day_ly`), which is date arithmetic, with the day clamped where the earlier
  year has no such day;
- a quarter's name (`d_quarter_name`, `'1998Q1'`), which joins a number to text.

Once `d_dom` exists, the first day of the month and dsdgen's quarters are core rules again: they are written from
`d_dom` with the whole-unit differences, `ROUND` and `CASE` that FLUID SQL/1 has.

EXPR-SPEC-2 closes flux-expr/2 and sends the growth of the extensions to flux-expr/3. A rule that needs these forms
is a FLUX-side rule, because a contract's texts are FLUID SQL/1 and the contract carries no language key.

## Options

Each of the three needs one spelling. DuckDB 2.0.0 is the oracle where the language borrows SQL's behaviour, and the
probes below were run locally on that version.

### A date part

- **`EXTRACT(YEAR|MONTH|DAY FROM d)`.** The SQL standard's extract expression. PostgreSQL, DuckDB, Snowflake,
  BigQuery and Spark all accept it. DuckDB gives `extract(day FROM DATE '1998-03-15')` as 15, a BIGINT, so the
  result is an integer of scale 0.
- `DAY(d)`, `MONTH(d)`, `YEAR(d)`. Short, and common in MySQL, Spark, Snowflake and Db2, but not the standard's
  spelling, and PostgreSQL has none. Three function names against one construct.
- `DATE_PART('day', d)`. PostgreSQL's and DuckDB's, with the field as a string. Not the standard's, and a closed set
  of words reads better as keywords than as string literals the checker has to validate.
- Substrait's `extract`. Its component argument needs an indexing argument (one-based or zero-based) that SQL's
  spelling does not have, and nothing here needs the other components.

Recommended: `EXTRACT`, with the fields `YEAR`, `MONTH` and `DAY`. They are the ones the calendar rules need and the
ones whose meaning is the same everywhere. The other fields (week, day of week, quarter) differ between engines, so
they stay out.

### Date arithmetic

- **`ADD_DAYS(d, n)`, `ADD_MONTHS(d, n)`, `ADD_YEARS(d, n)`.** Db2's names. Spark, Snowflake and Oracle have
  `ADD_MONTHS`. Three functions, each a date and an integer, each giving a date. A month or year move clamps the day
  to the last day of the target month, as PostgreSQL, DuckDB and Db2 do. Oracle's `ADD_MONTHS` does not clamp: it
  returns the last day of the target month when the start is the last day of its own, so `ADD_MONTHS(DATE '2001-02-28', 1)`
  is the 31st of March there. These are exactly the inverse of FLUID SQL/1's whole-unit differences, because
  `flux_engine/expr2/functions.py`'s `add_months` already defines `MONTHS_BETWEEN`: the largest `k` such that the
  start moved forward `k` months is not after the end.
- SQL interval arithmetic, `d + INTERVAL 1 MONTH`. It needs an interval type, which FLUID SQL/1 does not have. The
  standard raises on a day that does not exist, where the other engines clamp, and DuckDB returns a TIMESTAMP:
  `DATE '2001-01-31' + INTERVAL 1 MONTH` is `2001-02-28 00:00:00`, so a rule would need a cast back to a date.
- `DATE_ADD(d, INTERVAL n unit)`. The MySQL and BigQuery spelling. It has the interval type's cost and one function
  for three units.

DuckDB's behaviour, measured:

- `DATE '2001-01-31' + INTERVAL 1 MONTH` is `2001-02-28 00:00:00`, a TIMESTAMP, with the day clamped;
- `DATE '2000-02-29' - INTERVAL 1 YEAR` is `1999-02-28 00:00:00`, also a TIMESTAMP, with the day clamped;
- `DATE '2001-01-31' + 5` is a DATE, so adding days to a date stays a date;
- `CAST(DATE '2001-01-31' + to_months(1) AS DATE)` is `2001-02-28`, and `to_years(-1)` and `to_years(4)` on
  `DATE '2000-02-29'` give `1999-02-28` and `2004-02-29`, so the oracle's spelling of `ADD_MONTHS` and `ADD_YEARS`
  is a month or year interval cast back to a date.

Recommended: the three `ADD_` functions. A result outside 0001-01-01 to 9999-12-31 is NULL, never an error, because
the language's evaluator is total.

### String concatenation

- **`a || b`.** The standard's operator. It takes strings and integers, and writes an integer as its canonical
  decimal text, which is what PostgreSQL and DuckDB give for an integer operand. A NULL operand makes the result
  NULL: the standard's rule, PostgreSQL's and DuckDB's `||`, and Substrait's `ACCEPT_NULLS` null-handling
  option for its concatenation (the result is NULL if any argument is NULL, where `IGNORE_NULLS` skips them).
- `CONCAT(a, b, ...)`. MySQL, PostgreSQL, DuckDB and Spark have it, but it skips NULLs, so a NULL part gives
  `'Q'` where `||` gives NULL. Two behaviours for one job would split the language.
- Requiring a `CAST(x AS VARCHAR)` for an integer. Standard, but FLUID SQL/1 leaves `CAST` out and a cast is no
  help to a rule that writes `d_year || 'Q' || d_qoy`.

DuckDB's behaviour, measured:

- `1998 || 'Q' || 1` is `'1998Q1'`, and `1 || 2 + 3` is `'15'`, so `||` binds looser than `+`;
- `NULL || 'Q'` is NULL;
- `concat(NULL, 'Q')` is `'Q'`.

Recommended: `||`. It binds looser than `+` and `-` and tighter than the comparisons, as PostgreSQL and DuckDB
parse it, and associates to the left.

## Decision

flux-expr/3 is flux-expr/2 with these three extensions, and everything that EXPR-SPEC-2 does not change stays as it
is.

1. **Where it is used.** A text uses the new forms only as a FLUX-side rule that declares `language: flux-expr/3`,
   and never in a contract. A contract's texts are FLUID SQL/1. Under `language: flux-expr/2`, or in a core text,
   each new form is `FLX-6103`, exactly as any word outside the grammar is now.
2. **flux-expr/2 stays closed and unchanged.** Every flux-expr/2 text, its canonical form, its vectors and its
   digests are the same under flux-expr/3, byte for byte. A text moves to flux-expr/3 by changing its declared
   language.
3. **The forms.** `EXTRACT(YEAR|MONTH|DAY FROM <date>)`, `ADD_DAYS|ADD_MONTHS|ADD_YEARS(<date>, <integer>)`, and
   `<a> || <b>` over strings and integers of scale 0. The result of `EXTRACT` is an integer of scale 0, of `ADD_*` a
   date, and of `||` a string with no enum. All three are exact and total, and a NULL argument or operand gives NULL.
4. **Refusals.** A concatenation derived into an enum column is `FLX-6110`, because a string joined from parts is
   not known to be one of the levels. Any other operand type is `FLX-6109`. A FLUX-side rule that declares
   flux-expr/3 and uses none of the seven extensions is `FLX-6209`, since the contract can carry it.
5. **ADR 0008 is amended** once this record is accepted: its list of four extensions becomes the first four of
   seven, and `FLX-6102` accepts `flux-expr/2` and `flux-expr/3` for a FLUX-side rule.

## Consequences

- ADR 0011's step 1 can write `d_dom`, `d_same_day_ly` and `d_quarter_name` as rules, and the columns that turn on
  the day of the month become core rules. Reading these rules from a bundle and generating from them is a later
  change that needs this one.
- The cost is a lexer token (`||`), three node kinds, and code in the parser, the checker and the evaluator. The language gains no dependency. DuckDB stays a test-only oracle, and every vector with a new form
  runs as a DuckDB query and must agree.
- `ADD_YEARS(d, n)` is `ADD_MONTHS(d, 12 * n)`, so the same day a year back is `ADD_YEARS(d, -1)`, with 29 February
  clamped to 28 February in a year that is not a leap year.
- A text that declares flux-expr/3 and uses a core form only is refused (`FLX-6209`), so a rule never moves into a
  FLUX-side bundle without need.
- The checker's refusals are about texts and types, and no row is read. No row value reaches a diagnostic.

## What would change this decision

- If FLUID SQL grows a date part, date arithmetic or string functions, the rules written with them move into the
  contract, and flux-expr/3's forms become core. A form the RFC's subset comes to cover is core, not an extension
  (EXPR-SPEC-2, preamble).
- If a calendar needs more date parts (the day of the week, the week or the quarter), they are a flux-expr/4
  question, since their meaning differs between engines and each needs its own oracle check.
- If DuckDB, PostgreSQL or the standard change how `||` treats NULL or how a month move clamps, the vectors that pin
  them are re-measured and this record is amended.
