# ADR 0002: flux-expr/1 diverges from CEL

## Status

Accepted (W0)

## Context

Google's Common Expression Language (CEL) is the standard non-Turing-
complete, side-effect-free expression language for exactly this shape of
problem -- a predicate evaluated against a small attribute model.
Borrow-before-build search: "cel-python Common Expression Language"
(2026-09-06); fetched https://github.com/cloud-custodian/cel-python
(Apache-2.0, pure Python on Lark, custom functions via
`env.program(ast, functions)`, activation-dict binding). Google's own
`cel-expr-python` (wrapping the C++ implementation) shipped in March
2026.

CEL's own grammar accepts expression forms FLUX does not want to accept,
and is versioned by Google's own release cadence, not FLUX's. The build
plan requires `flux-expr/1` to be a frozen, closed grammar whose AST
compiles to a typed closure at plan time and is digest-stable across
engine versions -- a property that has to be true of the language FLUX
ships, not of whatever CEL happens to accept in a given release.

## Decision

`flux-expr/1` is FLUX's own small, closed EBNF grammar (to be documented
in a forthcoming spec RFC, not in this ADR), parsed by a hand-written
tokenizer and Pratt parser into a frozen AST, evaluated over three fixed
namespaces (subject / event / observed) with `Decimal` numerics. No
dependency on cel-python or any CEL implementation.

## Consequences

- flux_engine controls the grammar's version and stability guarantees
  directly, rather than inheriting a third party's release cadence.
- The attribute model (what names resolve to what types) is derived at
  plan time from the bundle and the vertical pack, matching the
  closed-world validation story the FLUX schema already commits to.
- The cost is writing and testing a parser and evaluator instead of
  depending on one. cel-python remains cited prior art for the shape of a
  non-Turing-complete, linear-time expression evaluator; flux-expr/1's own
  conformance vectors will be published in the FLUX spec repository the
  same way the RFC-04 enforcement vectors are today.
