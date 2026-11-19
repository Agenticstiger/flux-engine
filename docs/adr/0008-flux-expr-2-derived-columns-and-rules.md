# ADR 0008: flux-expr/2 implements FLUID SQL/1 and adds FLUX's extensions; flux-expr/1 unchanged

## Status

Accepted (2026-11-16). The engine implements the language (`flux_engine.expr2`); generating from rules and
checking runs against them come next. The specification is
[EXPR-SPEC-2](../spec/EXPR-SPEC-2.md). Its core is FLUID SQL/1, defined by the FLUID RFC `RFC-data-rules.md` in the
forge-cli repository (Agenticstiger/forge-cli#896, Proposed). **Amended 2026-11-18:** flux-expr/3 adds three
extensions, so the four of decision 3 are the first four of seven (see the amendment below).

## Context

FLUX is moving from one flat stream per emit to several related tables per run, linked by the keys that FLUID 0.7.7
declares (`exposes[].semantics.relationships[]`). Three needs follow, and flux-expr/1 serves none of them:

- A column copied from a linked row (the parent, the previous version of the entity, a catalogue item) or derived
  from the row and its links, including aggregates over a parent's children and windows over a row's siblings ("the
  first child is the primary one").
- Relations between columns declared once and used both to generate and to check: arithmetic within a row, date
  order, values as of a date (age, tenure), functional dependencies, summaries across tables, and flags that agree
  with values. Each holds on a declared share of rows, as real data does: a relation that holds on 98 % of real rows
  is generated and checked at 98 %. No column such a relation determines is drawn on its own.
- One source per attribute: an attribute that several tables carry is generated once and copied through the link.

flux-expr/1 is a closed predicate grammar with no arithmetic, aggregates, windows or linked rows, and EXPR-SPEC sends
its growth to flux-expr/2 by policy. That growth has five constraints: exact arithmetic only, SQL window semantics as
the reference, flux-expr/1 valid and unchanged, a declared version, and refusals as FLX codes.

These relations describe the data a contract stands for, not only a synthetic copy of it, so they belong in the
contract. FLUID already carries the keys (RFC-semantics-tier1). `RFC-data-rules.md` carries the rest:
- invariants, each a `check`, a `derive` with `expr` or `aggregate`, or a `dependency`, with a band of violating rows
  in ODCS's operators;
- identifier formats and placeholder values;
- shared attributes;
- FLUID SQL/1, the closed SQL expression subset their texts are written in. Its semantics are two-valued and its
  numbers exact.

FLUX reads all of these under its rule of referencing the contract and never restating it.

Borrow-before-build search (2026-11-16):

- CEL (cel-spec `langdef.md`): its numerics are int64, uint64 and IEEE double. It has no decimal type and no
  aggregate or window functions, so ADR 0002's reasons to diverge still hold.
- SQL, as the base: a subset of SQL is what data contracts and their readers already speak, and FLUID SQL/1 is that
  subset for FLUID as a whole. FLUID's validator parses it with sqlglot; flux-expr/2 parses the closed subset itself,
  because a general SQL parser would sit on the cold path that flux-engine's `tests/test_startup_budget.py` keeps
  small. SQL's division is not exact: DuckDB 2.0.0 computes `/` and `avg` over `DECIMAL` in `DOUBLE`, checked locally.
- DuckDB 2.0.0, checked locally, is the oracle for what flux-expr/2 takes from SQL:
  - window functions; NULLS LAST as the default in both directions; an arbitrary order among ties under
    `row_number`; and a default frame for `last_value` that ends at the current row;
  - `date_sub`, which counts whole days, months and years and clamps a month to its last day. It agrees with the
    definition the RFC and EXPR-SPEC-2 share on 40,864 pairs of dates, month ends and leap days included (122,592
    comparisons).
- Polars `Expr.over()` and PRQL's `window` transform give partitioned windows in a closed expression API. Pattern
  only.
- XSLT 2.0's `version` attribute scopes a language version to the element that carries it, and FLUX's own
  `Segment.spec.queryLanguage` sits beside its query. Both are the pattern for declaring the version of a FLUX-side
  expression.
- Great Expectations' `mostly` argument and Deequ's metric assertions (`_ >= 0.5`) are the pattern for a rule that
  holds on a declared share of rows; the RFC writes the share with ODCS's `mustBe*` operators.
- PostgreSQL's generated columns (`GENERATED ALWAYS AS (expression) STORED`, since PostgreSQL 12) are the pattern for
  a `derive`: a column defined by an expression over its row, with a share and a check added.

## Decision

The maintainers settled each open point on 2026-11-16:

1. **The core is FLUID SQL/1.** The rules about valid data are declared in the FLUID contract, and FLUX reads them
   there and never restates them. Their texts are FLUID SQL/1, and flux-expr/2 implements that subset exactly and is
   spelled as it is: one language for contract texts and FLUX-side rules. The RFC's grammar, names, types, null rules
   and functions are the core's, and its conformance vectors decide what the core accepts and means. The RFC's
   §2.9 table gives the meaning each construct's tree must have, in flux-expr/1's notation, not a second spelling.
2. **flux-expr/1 stays separate.** flux-expr/2 is not a superset of it. flux-expr/1 keeps its sites, parser, texts,
   error codes and digests, and a site that moves to flux-expr/2 later rewrites its texts in SQL spelling.
3. **Four extensions, FLUX's only:**
   - the previous version of an entity, and paths over several links;
   - `COUNT`, `SUM`, `MIN`, `MAX` and `AVG` over a parent's children, written inside an expression;
   - `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `FIRST_VALUE`, `LAST_VALUE`, `LAG` and `LEAD` over a row's siblings, in
     SQL's `OVER` spelling with whole-partition frames and no explicit frames.

   A rule that needs one is a FLUX-side rule, declared in the bundle with `language: flux-expr/2`. One that needs
   none, or whose whole value is exactly a contract `aggregate`, belongs in the contract (`FLX-6209`).
4. **Semantics are the RFC's.** Truth is two-valued, numbers are exact rationals with `x / 0` as NULL, and `ROUND`
   rounds ties away from zero, with `'HALF_TO_EVEN'` for the even neighbor. Strings compare by their stored text, and
   the whole-unit date differences take an explicit as-of date. A `derive` holds where its column is not distinct from
   the value, so a NULL copy holds, while a `check` written `a = b` is FALSE on NULL. These settle what earlier drafts
   proposed.
5. **Deterministic windows.** A window whose value depends on how ties are ordered needs a total order, one that
   names the expose's key, or the plan refuses. `RANK` and `DENSE_RANK` take any order. Nulls sort last unless
   written otherwise.
6. **One declaration, two uses.** Every run checks every rule against its band, counting violating rows as the RFC
   does, so FLUX and every other consumer of the contract reach one verdict. A rule also generates the column it
   determines, from one of four forms:
   - a `derive` with `expr`, as a generated column;
   - a `derive` with `aggregate`;
   - a `check` of bounds form, with a FLUX setting naming the column;
   - a `check` of difference form solved for a date, with the same setting.

   Several `derive` rules may determine one column, and the first whose `when` holds covers the row. No row is left
   out implicitly: a NULL bound makes the row infeasible.
7. **Shares below 100 %.** The band is the check. FLUX generates at a target inside it, a FLUX setting that defaults
   to the band's middle.
   - A per-row draw on the rule's own seed stream, keyed by contract and id, picks the violating rows, so any
     partitioning gives the same rows.
   - Violating values are drawn from the column's statistics at fixed stream positions, so there is no rejection
     loop.
   - Bands that cannot be met in expectation are checked and generate no violation.
8. **Functional dependencies.** FLUX reads them from the contract as structure and checks them with the g3 count,
   forward and reverse separately. Each `from` value, keyed by a typed canonical encoding of its stored values, gets
   its `to` value once.
9. **One source per value.** The contract's sources are the RFC's. FLUX adds FLUX-side rules, scenario mappings, the
   identifier scheme, statistics and defaults, and refuses a column with two sources, deciding that without looking
   at rows. A shared attribute is generated once and read as a copy along its path.

## Consequences

- flux-expr/1 cannot move. Its parser stays and its sites keep calling it.
- The core follows the RFC. flux-engine runs forge-cli's `fluid-sql-vectors.json` through flux-expr/2's parser, and a
  change to FLUID SQL/1 reaches FLUX through the RFC's next version.
- A bundle with no rule keeps its attribute model, and one with no rule and no dependency keeps every stream and every
  digest. The new stream shapes are derived the way the generator derives its own row streams, outside KERNEL-SPEC
  §3's canonical table. Adding them to the table is the `kernelVersion` bump that the generator's row path still waits
  for.
- A rule added to a contract changes the contract digest, which seeds every stream. That is the existing rule for any
  change to a contract.
- The cost is a parser and evaluator for FLUID SQL/1 and the extensions, and a rule step in the generator, all written
  and tested in this repository. Aggregates and windows are evaluated once per row of their scope, so the work stays
  bounded by the rows read times the size of the text. DuckDB is a test-only oracle, never imported by the engine.
- Targets are met in expectation, not exactly, which is why the band is the check.
- Some parts belong to other features and are defined with them:
  - how a catalogue is declared;
  - the planner's column order;
  - how generation is partitioned across roots.
- Twenty-two FLX codes are reserved: `FLX-6101` to `FLX-6113` for the language, and `FLX-6201` to `FLX-6209` for
  rules and dependencies.

## Amendment of 2026-11-18

[ADR 0013](./0013-flux-expr-3-date-parts-date-arithmetic-and-concatenation.md), accepted on 2026-11-18, adds
flux-expr/3, the route this record keeps for new extensions:

1. **Seven extensions.** The four of decision 3 are the first four of seven. flux-expr/3 is flux-expr/2 with three
   more: a date part (`EXTRACT`), date arithmetic (`ADD_DAYS`, `ADD_MONTHS`, `ADD_YEARS`) and string concatenation
   (`||`), as [EXPR-SPEC-3](../spec/EXPR-SPEC-3.md) specifies. flux-expr/2 keeps its four, stays closed, and is
   unchanged.
2. **Two languages for a FLUX-side rule.** `FLX-6102` accepts `flux-expr/2` and `flux-expr/3` as a FLUX-side rule's
   `language`. A contract's texts stay FLUID SQL/1.
