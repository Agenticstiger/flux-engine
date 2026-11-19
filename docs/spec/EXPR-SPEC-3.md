# flux-expr/3: normative specification (v0.1)

**Status: accepted 2026-11-18 ([ADR 0013](../adr/0013-flux-expr-3-date-parts-date-arithmetic-and-concatenation.md)). The
engine implements the language (`flux_engine.expr2`, with `language="flux-expr/3"`), reads FLUX-side rules written in
it from a bundle, and generates from those that read only their own row. A bundle that declares no FLUX-side rule
does not change.**

flux-expr/3 is [flux-expr/2](EXPR-SPEC-2.md) plus three extensions, and everything this page does not say is
EXPR-SPEC-2's: its grammar, row scope, types, numbers, nulls, dates, functions, rules, canonical form and refusals.
The extensions are a date part, date arithmetic and string concatenation. They are the forms that FLUID SQL/1 leaves
out (RFC `RFC-data-rules.md`, section 2.7) and that the calendar rules of ADR 0011 need: the day of the month, the
same day a year back, and a quarter's name.

flux-expr/2 stays closed and unchanged. Like it, flux-expr/3 is closed: anything outside the core and the seven
extensions is a parse error (`FLX-6103`). A form that FLUID SQL comes to cover is core, not an extension.

## 1. Where texts come from

`EXTRACT` and `||` are FLUX's, and never sit in a contract. A text uses them only as a FLUX-side rule that declares
`language: flux-expr/3`. `ADD_DAYS`, `ADD_MONTHS` and `ADD_YEARS` are also FLUID SQL/1's: a contract's text takes them, with
the meaning of §4, and only a FLUX-side text under `flux-expr/2` refuses them. A contract's texts are FLUID SQL/1 and carry no language key. The engine generates a column
from such a rule (a `derive` whose reads stay in the row), as EXPR-SPEC-2 §1 and §6 say.

- Under `language: flux-expr/2`, each new form is `FLX-6103`, exactly as any word outside the grammar is now. In a
  core (contract) text `EXTRACT` and `||` are `FLX-6103`, and the three `ADD_` functions parse.
- `FLX-6102` accepts `flux-expr/2` and `flux-expr/3` for a FLUX-side rule, and no other language.
- A FLUX-side text that declares flux-expr/3 is `FLX-6209` when it uses none of the seven extensions: the four of
  EXPR-SPEC-2 (`previous` and paths over several links, aggregates over children, windows over siblings) and these
  three. The contract can carry it.
- A flux-expr/2 text means the same under flux-expr/3: the same tree, the same canonical text, the same value. A text
  moves to flux-expr/3 by changing its declared language.

## 2. Grammar

The extensions add these forms to the extension primaries, in SQL's spelling:

```text
extract  = "EXTRACT" , "(" , ( "YEAR" | "MONTH" | "DAY" ) , "FROM" , expr , ")" ;
date_add = ( "ADD_DAYS" | "ADD_MONTHS" | "ADD_YEARS" ) , "(" , expr , "," , expr , ")" ;
concat   = value , { "||" , value } ;
(* expr and value are the core's, RFC §2.1; keywords are case-insensitive, as there *)
```

- `EXTRACT` and the `ADD_` functions are calls, so they are primaries. A name `extract` or `add_days` that is not
  followed by `(` is an ordinary name.
- `||` binds looser than `+` and `-` and tighter than the comparisons, and is left-associative, as PostgreSQL and
  DuckDB parse it: `a || b + c` is `a || (b + c)`, and `a || b = c` is `(a || b) = c`. The operands of the
  comparisons, `IN`, `BETWEEN` and `IS [NOT] DISTINCT FROM` are `concat`.
- `FROM` is already a keyword of the core. `YEAR`, `MONTH` and `DAY` are keywords only inside `EXTRACT`, so a column
  named `year` stays usable, bare, everywhere including inside `EXTRACT(YEAR FROM year)`.
- The limits of EXPR-SPEC-2 section 2 apply unchanged. `||`, `EXTRACT` and each `ADD_` call add a level to the
  deepest part they hold, as every operator and call does.
- A `||` needs a left operand. The lexer reads `||` as one token, so a single `|` stays an unexpected character
  (`FLX-6103`).

## 3. Types

| Form | Operands | Type |
|---|---|---|
| `EXTRACT(f FROM d)` | `d` a date | an integer, scale 0 |
| `ADD_DAYS(d, n)`, `ADD_MONTHS(d, n)`, `ADD_YEARS(d, n)` | `d` a date, `n` an integer of scale 0 | a date |
| `a \|\| b` | each of `a` and `b` a string or an integer of scale 0 | a string with no enum |

- An operand of any other type is `FLX-6109`. An integer is a number of static scale 0: a number with a fractional
  literal, a quotient, or any number of unbounded scale is not, and `ROUND(x, 0)` is.
- The result of `||` has no enum and no placeholders. A concatenation derived into a column whose type is an enum, or
  a `CASE` or `COALESCE` that can give one, is `FLX-6110`.
- The `NULL` literal takes its type from a typed operand beside it as EXPR-SPEC-2 section 4 says: `NULL || 'Q'` is a
  string and is NULL. It is `FLX-6104` as the operand of `EXTRACT`, as an argument of an `ADD_` function, and where
  every operand of a `||` is NULL.
- A comparison of a concatenation with a string literal reads the literal as an ordinary string: a concatenation
  has no enum.

## 4. Semantics

They are exact and total. Given type-checked operands, each form returns a value or NULL, and never raises.

- **`EXTRACT(f FROM d)`** is the year (1 to 9999), the month (1 to 12) or the day of the month (1 to 31) of `d`,
  in the proleptic Gregorian calendar, as a whole number. NULL when `d` is NULL. DuckDB 2.0.0 gives
  `extract(day FROM DATE '1998-03-15')` as 15.
- **`ADD_DAYS(d, n)`** is `d` moved `n` days forward, or back when `n` is negative. DuckDB's `d + n` on a date.
- **`ADD_MONTHS(d, n)`** is `d` moved `n` months, the day kept or, where the target month is shorter, clamped to its
  last day: `ADD_MONTHS(DATE '2001-01-31', 1)` is 2001-02-28. This is `add_months` of EXPR-SPEC-2's
  `MONTHS_BETWEEN`, so the whole-unit differences count the moves this function makes:
  `MONTHS_BETWEEN(d, ADD_MONTHS(d, n))` is `n` whenever the result is in range.
- **`ADD_YEARS(d, n)`** is `ADD_MONTHS(d, 12 * n)`: `ADD_YEARS(DATE '2000-02-29', -1)` is 1999-02-28 and
  `ADD_YEARS(DATE '2000-02-29', 4)` is 2004-02-29.
- A NULL `d` or `n` gives NULL. A result outside 0001-01-01 to 9999-12-31 is NULL, never an error.
- **`a || b`** is the text of `a` followed by the text of `b`. An integer is its canonical decimal text: its digits
  with no leading zeros, no sign when it is not negative, and a minus sign when it is. A string is taken as stored,
  with no normalization. A NULL operand makes the result NULL. `d_year || 'Q' || d_qoy` is `'1998Q1'` for 1998 and 1.

The semantics are borrowed from SQL and checked against DuckDB 2.0.0 (section 6): the standard's `EXTRACT` and `||`,
Db2's names `ADD_DAYS`, `ADD_MONTHS` and `ADD_YEARS`, the month-end clamping of PostgreSQL's `+ interval`, DuckDB and
Db2's labeled duration `+ n MONTHS` (Db2's own `ADD_MONTHS` keeps a month end on a month end), and Substrait's
`ACCEPT_NULLS` rule for a concatenation that gives NULL on a NULL operand.

## 5. Canonical form and the tree

- Keywords and function names are written in upper case: `EXTRACT(YEAR FROM d)`, with the field in upper case, and
  `ADD_DAYS(d, n)`. `||` is written with one space on each side, and the fewest parentheses the grammar needs:
  `||` is looser than `+` and `-`, so `a || (b || c)` keeps its parentheses and `(a || b) || c` is `a || b || c`.
  `unparse(parse(x))` is a fixed point, as in EXPR-SPEC-2 section 11.
- The new tree nodes, in the notation of EXPR-SPEC-2 section 11, with the units spelled as in `date_diff`:
  - `{"node": "extract", "field": "year" | "month" | "day", "arg"}`;
  - `{"node": "date_add", "unit": "day" | "month" | "year", "date", "count"}`;
  - `{"node": "concat", "left", "right"}`. A chain `a || b || c` is `concat(concat(a, b), c)`.
- A number keeps its digits as written, as in EXPR-SPEC-2, so `ADD_DAYS(d, 007)` has the number `007`.

## 6. Vectors and the oracle

`tests/vectors/expr3-vectors.json` holds the vectors of the three extensions in the shape of `expr2-vectors.json`
(EXPR-SPEC-2 section 11), with the file's `language` set to `flux-expr/3` and a vector's own `language` where it
differs, so that the vectors for the refusals under flux-expr/2 are in the same file. Every vector also runs as a
DuckDB query and must agree, written as:

| Form | DuckDB |
|---|---|
| `EXTRACT(DAY FROM d)` | `extract(day FROM d)` |
| `ADD_DAYS(d, n)` | `d + n` |
| `ADD_MONTHS(d, n)` | `CAST(d + to_months(n) AS DATE)` |
| `ADD_YEARS(d, n)` | `CAST(d + to_years(n) AS DATE)` |
| `a \|\| b` | `a \|\| b`, with `CAST(x AS VARCHAR)` for an integer operand |

DuckDB's dates run far past 0001-01-01 and 9999-12-31, where flux-expr/3's result is NULL, so the query writes that
range out: it keeps a date only when it is between those two. The oracle checks counts only inside DuckDB's own
range (`to_years(1000000)` is an error there), so huge counts and the NULL bound are checked by the engine's own
tests (`test_date_arithmetic_is_total`). Like the oracle of EXPR-SPEC-2, the flux-expr/3 oracle
(`test_expr3_oracle.py`) is skipped when `duckdb` is not installed, and the engine's own tests run either way. The vectors cover month-end clamping (31 January plus one month, 29 February back one year and forward four),
negative counts, NULLs, precedence (`a || b + c`), a column named `year`, the rules of ADR 0011's date dimension, and
each refusal code the extensions raise. DuckDB is a test dependency only, and the engine never imports it.

## 7. Refusals

No new code is raised. The codes of EXPR-SPEC-2 section 12 apply:

| Code | When |
|---|---|
| `FLX-6102` | a FLUX-side rule declares a language other than `flux-expr/2` and `flux-expr/3` |
| `FLX-6103` | `EXTRACT` or `||` in a core text, or any extension form under `flux-expr/2`; a field other than `YEAR`, `MONTH` and `DAY`; an `ADD_` call or an `EXTRACT` with the wrong shape |
| `FLX-6104` | a NULL literal as the operand of `EXTRACT`, as an argument of an `ADD_` function, or as every operand of `\|\|` |
| `FLX-6109` | an operand of another type than section 3 gives |
| `FLX-6110` | a concatenation derived into an enum column |
| `FLX-6209` | a FLUX-side rule declaring flux-expr/3 that uses none of the seven extensions |

## 8. Not in flux-expr/3

- other date parts (the day of the week, the week, the quarter), `INTERVAL` and `CAST`, and string functions other
  than `||` (`CONCAT`, `SUBSTRING`, `LENGTH`, `UPPER`);
- everything EXPR-SPEC-2 section 13 leaves out.
