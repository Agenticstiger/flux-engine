# flux-expr/1: normative specification (v0.1)

`Segment.spec.queryLanguage` is `const: flux-expr/1` in FLUX 0.5.0 and no grammar exists anywhere in the spec.
This document defines it. The grammar is **closed**: anything not derivable is a parse error (`FLX-6001`), never an
extension point. Growth requests go to `flux-expr/2` by policy. CEL was evaluated and diverged from deliberately
(ADR-0002): the shipped example `products.has('tv')` is not CEL, the grammar must be frozen and digest-stable, and
the cold path must not carry a parser-generator dependency.

## 1. Grammar (EBNF)

```
expr        = or_expr ;
or_expr     = and_expr , { "||" , and_expr } ;
and_expr    = not_expr , { "&&" , not_expr } ;
not_expr    = [ "!" ] , primary ;
primary     = "(" , expr , ")" | comparison | membership | method | bool_lit ;
comparison  = operand , cmp_op , operand ;
cmp_op      = "==" | "!=" | "<" | "<=" | ">" | ">=" ;
membership  = operand , "in" , list_lit
            | operand , "in" , path ;                      (* right side must be set<string> *)
method      = path , "." , method_name , "(" , [ literal , { "," , literal } ] , ")" ;
method_name = "has" | "isNull" | "startsWith" | "endsWith" | "contains" ;
operand     = path | literal ;
path        = ident , { "." , ident } ;
ident       = ( letter | "_" ) , { letter | digit | "_" } ;
literal     = string | number | bool_lit | null_lit ;
list_lit    = "[" , [ literal , { "," , literal } ] , "]" ;
string      = "'" , { char - "'" | "\'" } , "'" ;
number      = [ "-" ] , digit , { digit } , [ "." , digit , { digit } ] ;
bool_lit    = "true" | "false" ;
null_lit    = "null" ;
```

Precedence, tightest first: method/path · `!` · comparison and `in` · `&&` · `||`. All left-associative.
Whitespace: space, tab. No arithmetic, no lambdas, no regex (`matches` is a **reserved word** that parses to
`FLX-6002` naming flux-expr/2), no temporal operators, no user functions, no double-quoted strings.

Method set (closed): `has(string)` on `set<string>`; `isNull()` on any path; `startsWith`, `endsWith`,
`contains` (one string literal) on `string`.

## 2. Types

Five static types: `bool`, `number` (evaluated as `Decimal`), `string`, `set<string>`, `enum<…>` (a string drawn
from a declared closed set; compares as string; a literal outside the set is `FLX-6003` at plan time). `null` is a
value any path may carry, not a type.

**Static type check at plan time** against the attribute model: unknown path `FLX-6004`; type mismatch
(`market > 3`, `traits.digitalFluency.has('x')`) `FLX-6005`; comparison between different types is an error (no
coercion; `'1' == 1` is `FLX-6005`). Numeric literals are parsed as `Decimal`, never `float`.

**Runtime totality**: a type-checked expression never raises. A `null` operand makes every comparison and `in`
evaluate to `false`, `!= null` to `true`, and `isNull()` to `true`. Methods on `null` return `false`.

## 3. Attribute model (`expr/attrs.py`) — three scopes, derived at `plan` time, printed by `fluxsim expr model`

A versioned, canonical JSON document (`attrmodel/1`) whose digest is inside `inputDigest`.

### `subject` scope — `Segment.spec.query`, `Journey` guards
| path | type | source |
|---|---|---|
| `id` | string | subject id |
| `ordinal` | number | population ordinal |
| `persona` | enum(Persona ids referenced by the Simulation) | assignment |
| `lifecycle` | enum(keys of `World.spec.population.lifecycleMix`) | LRM cohort |
| `market` | enum(`World.spec.population.markets`) | market assignment |
| `traits.<name>` | number (scalar or normal) \| enum(levels) (categorical) | Persona trait declarations |
| `worldview.<name>` | number | Persona worldview |
| vertical-pack attributes (telco: `products: set<string>`, `tenureMonths: number`, `latePaymentCount: number`, `arpu: number`) | per pack | `World.spec.vertical` → `verticals/<pack>.py` (v0.1 stopgap; FLUX 0.6 RFC moves the declaration into the spec) |
Bare `lifecycle`, `products`, … are sugar for `subject.<name>` where unambiguous.

### `event` scope — `Detector.pattern[].predicate`, `Journey.spec.trigger.condition`
CloudEvents context: `type`, `source`, `time` (string, RFC 3339), `subject` (the subject id); payload fields typed
from the Signal's `dataschema` when it is a bundled/fetchable JSON Schema, else from a `signalAttributes` block in
the vertical pack (telco `sig-payment-aborted`: `amount: number`, `attempts: number`, `channel: string`). Payload
paths are addressed as `data.<field>`; bare `amount` is sugar for `data.amount`.

### `observed` scope — `Playback.spec.personaMapping[].behaviorPattern`
Aggregate features per observed subject computed by the ingest step (telco: `late_payment_count: number`,
`tenure_months: number`, `arpu: number`). Never raw records.

## 4. Evaluation

Hand-written tokenizer + Pratt parser → frozen dataclass AST → `compile(expr, scope_model) -> Callable[[View], bool]`
(closure tree). `unparse(parse(x))` is a fixed point and is the canonical form stored in `plan.json`. The AST JSON
(canonical) is golden-pinned per expression.

## 5. Published vectors (`expr-vectors.json`, upstream to the open spec repo)

Each vector:
`{id, expr, scope, model_fragment, expect: {parse: ast|error_code, check?: "ok"|error_code, unparse?: text, eval?: [{row, result}]}}`.
`id` is the vector's stable name; `model_fragment` names an entry of the file's top-level `models` table rather than
inlining a model; `expect.check` is `"ok"` or the static-check error code; `expect.unparse` is the canonical text;
`expect.eval` is a **list** of `{row, result}` cases, of which the single-case form is one entry. The v0.1 set
covers: the four shipped example expressions; each operator; precedence; each method; each error code; null rules;
NFC/NFD identifier equivalence (`é` composed vs decomposed identifies the same path); the reserved `matches`.
