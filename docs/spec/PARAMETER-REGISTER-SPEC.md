# Parameter register: normative specification (format 1-1-0)

A parameter register says what each number a run reads rests on. A number
comes from a statistic of the profile the Simulation pins (PROFILE-SPEC.md),
from a place in the FLUID contract the run emits from, from an assumption the
author states with its reason, or it is a default. A Simulation declares its
register in the engine's extension namespace, `apply` writes the record of it
as `run/parameter-register.json`, and `plan`, `verify` and `ship` report the
shares. Format 1-1-0 adds the contract source to format 1-0-0 (section 7).

Normative files beside this page:

- [`parameter-register-1-1.schema.json`](parameter-register-1-1.schema.json):
  the JSON Schema of a format 1-1-0 record, whose `$id` is
  `https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-1-0/FluxParameterRegister.json`.
- [`parameter-register-1.schema.json`](parameter-register-1.schema.json): the
  JSON Schema of a format 1-0-0 record, whose `$id` is
  `https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-0-0/FluxParameterRegister.json`.
  The schemas check the shape. This page states the rules they cannot express.
- [`parameter-register-example.json`](parameter-register-example.json): the
  record of a run of the small telco test bundle, with numbers from its
  pinned profile, two assumptions and the defaults. The tests validate it
  against the schema.

A Simulation that declares no register behaves exactly as before: its plan
has the bytes and the `planDigest` it had, `apply` writes no record, and the
evidence report says nothing new.

## 1. Declaring the register

The register is the top-level `parameters` key of the engine's namespace,
beside the profile pin its citations depend on. It belongs to the Simulation,
because it speaks for every document of the run, not for one expose:

```yaml
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:a1b2...
    parameters:
      - document: sim-payment-recovery
        pointer: /extensions/com.agentics.flux~11/emits/payment_recovery_moment/rows
        source: profile
        statistic: { pointer: /tables/payment_recovery_moment/rowCount }
      - document: sim-payment-recovery
        pointer: /extensions/com.agentics.flux~11/emits/payment_recovery_moment/fidelity/columns/amount/range/min
        source: profile
        statistic: { pointer: /tables/payment_recovery_moment/columns/amount/metrics/min, definition: min/1 }
      - document: pb-billing-history
        pointer: /spec/scorecard
        source: assumption
        reason: The billing team's backtest of the last quarter, not recomputed here.
      - document: q3-retention-world
        pointer: /spec/population/size
        source: default
        reason: The template's population size.
```

`parameters` is a list of entries. An entry holds:

| Key | Holds |
|---|---|
| `document` | The id of a document the run reads (section 2). |
| `pointer` | An RFC 6901 JSON Pointer into that document. It names one number, or every number under a mapping or a list. |
| `source` | `profile`, `contract`, `assumption` or `default`. |
| `statistic` | For `profile` only, and required there: the citation (section 3). |
| `contract` | For `contract` only, and required there: the citation (section 3). |
| `reason` | Required for `assumption`, optional for `default`, refused for `profile` and `contract`: one line of text, at most 1000 characters. |

A pointer is read as RFC 6901 reads it: tokens after each `/`, with `~1`
standing for `/` and `~0` for `~`. The engine's own namespace key contains a
`/`, so a pointer into it starts `/extensions/com.agentics.flux~11/`. A list
index is a decimal integer with no leading zero. Keys are compared in Unicode
Normalization Form C.

**The most specific entry decides.** A number takes the source of the entry
whose pointer reaches it with the most tokens. So an entry can name a whole
mapping, `/spec/traits` of a Persona, and a longer entry can give one number
under it another source.

**A number no entry names is a default,** decided by the engine rather than by
an entry. The shares always cover every number the run reads, so a register
that names one number cannot report that number as all of them. The record
tells the two kinds of default apart: a default an entry states names that
entry, and a default no entry names has none. This is SDMX's rule for an
observation with no status, which reads as normal.

Refused with `FLX-3105`, by `validate` as by every other command, before
anything is written:

- a block that is not a list, an entry that is not a mapping, a key an entry
  does not carry, and a missing `document`, `pointer` or `source`;
- a pointer that is not RFC 6901, and a `source` outside the four;
- a `profile` entry without `statistic` or with `reason`, a `profile` entry in
  a Simulation that pins no profile, and a citation that is not a mapping of
  `pointer` and `definition`, whose pointer is empty, or whose `definition` is
  not a registered statistic or metric;
- a `contract` entry without `contract` or with `reason`, and a citation that
  is not a mapping of `productRef` and `pointer`, whose `productRef` is not a
  contract the run's emits name, or whose pointer is empty or reaches nothing
  in that contract;
- an `assumption` without `reason`, a `statistic` on any source but
  `profile`, a `contract` on any source but `contract`, and a reason that is
  empty, longer than 1000 characters or holds a control character;
- two entries with the same document and pointer;
- an entry whose document the run does not read, whose pointer reaches
  nothing in it, or whose pointer reaches no number the register counts (a
  seed, for example, or text).

An empty list declares a register with no entry: every number is a default.
A YAML null declares none, as a null namespace does.

## 2. The numbers a register counts

The register counts every number the run reads, in these documents:

- the run's cast: the Simulation and every document it reaches;
- every Playback whose `worldRef` names the cast's World. A Playback's
  scorecard is what an emit's `minCredibility` is checked against, so its
  numbers bear on the run although no reference reaches it.

**Under `spec`,** every JSON number counts: an integer or a decimal, never a
boolean. Text is not a number, so a timestamp, a band such as `"35-44"` and a
duration are not counted. These are not counted either, because they say which
runs are made, not what the world is like:

| Kind | Not counted |
|---|---|
| World | `spec.seed` |
| Simulation | `spec.seed`, everything under `spec.sweep` |
| Experiment | `spec.assignment.seed` |

**In the Simulation's `com.agentics.flux/1` namespace,** the fidelity block
writes its numbers as decimal strings, so the positions are named. A value at
one of them counts when it is an integer, a decimal, or text the block's own
readers read as a finite decimal (`"1e4"`, `".5"` and `"+5"` count; a
timestamp does not). A number that needs more than 1000 digits written out is
refused with `FLX-5030`, the block's own rule:

| Position (under `emits.<exposeId>`) | What it is |
|---|---|
| `rows` | the declared row count |
| `fidelity.records.min`, `.max` | the bounds on a committed state's size |
| `fidelity.columns.<c>.range.min`, `.max` | a column's declared range (a timestamp bound is text and not counted) |
| `fidelity.columns.<c>.maxCardinality` | a support column's cardinality bound |
| `fidelity.columns.<c>.bins[]`, `.binWeights[]`, `.binWeightsGiven.<level>[]` | a histogram's edges and weights |
| `fidelity.columns.<c>.levels.<level>`, `.levelWeightsGiven.<level>.<level>` | a categorical column's weights |
| `fidelity.columns.<c>.mean`, `.stddev`, `.rate`, `.nullFraction`, `.identifierLength` | the generator's draw parameters |
| `fidelity.properties[].tolerance.value`, `.max` | a property's tolerance |
| `fidelity.properties[].bins[]`, `.level`, `.minVariance` | a property's bins, quantile level and variance floor |
| `rules[].mustBe`, `.mustBeLessThan`, `.mustBeLessOrEqualTo`, `.mustBeGreaterThan`, `.mustBeGreaterOrEqualTo`, `.mustBeBetween[]` | the band of a FLUX-side rule (EXPR-SPEC-2 section 1) |

and the scorecard thresholds' operands: `scorecard.thresholds.overall.<op>`,
`scorecard.thresholds.dimensions.<d>.<op>` and
`scorecard.thresholds.tables.<t>.<op>` (SCORECARD-SPEC.md section 4).

Not counted: the fidelity block's `encoding` (the grid the check computes on)
and `budget` (when the check runs), the namespace's `profile`,
`strictCoverage` and `parameters`, other namespaces, and anything outside
`spec` and this namespace, such as `metadata`, `agentPolicy` and `skills`.

The record lists the numbers by document, documents in NFC order of their ids,
and within a document in key order (Unicode code points, mapping by mapping;
lists by index), `spec` first and then the namespace.

## 3. Citing a profile statistic or the contract

A `profile` entry's `statistic` names where in the pinned profile its numbers
come from:

| Key | Holds |
|---|---|
| `pointer` | An RFC 6901 JSON Pointer into the pinned profile. |
| `definition` | The registered definition the pointer lands in: a statistic (`mean/1`, `marginalHistogram/1`, `levelHistogram/1`, `support/1`, `correlation/1`, `quantile/1`, `quantile/2`) or a column metric (`count/1`, `nullCount/1`, `distinctCount/1`, `sum/1`, `min/1`, `max/1`, `quantiles/1`). A table's `rowCount` has none. |

`plan` is always handed the profile a Simulation pins (PROFILE-SPEC.md
section 6), and it reads every citation in it before anything is written. A
citation holds when:

- the pointer reaches a value in the profile;
- the pointer is a table's `rowCount`, and the entry names no `definition`;
  or the nearest object on the way that carries a registered `definition`
  carries the one the entry names. The pointer may stop at the statistic or
  metric, or go into it: a group, a cell, a quantile level;
- nothing on the way is withheld: no object with a `status` other than `F`; a
  statistic named whole, or its list of groups, has at least one published
  group; a cell of a count table has the status `F` in its group's
  `cellStatus`; and a count table named whole has at least one such cell.

Any other citation is refused with `FLX-3113`. The message names the entry and
what is wrong, and never quotes a value. Because a profile records what a
disclosure rule withheld, a number cannot claim a statistic the profile itself
says it does not publish.

A `contract` entry's `contract` names where in a FLUID contract its numbers
come from, such as the levels an `enum(...)` type spells out:

| Key | Holds |
|---|---|
| `productRef` | The contract's `id`, as an emit of the Simulation names it. |
| `pointer` | An RFC 6901 JSON Pointer into that contract. |

The contract is a document of the bundle, so the citation is read wherever the
register is: `validate` and every other command refuse, with `FLX-3105`, a
`productRef` that no emit of the run names and a pointer that reaches nothing
in that contract, before anything is written. As with a profile citation, no
number is compared with the value the pointer reaches. The contract is one of
the bundle's files, so the run's `inputDigest` covers the place a citation
names.

The plan reads where a citation points. It compares no number with the value
there, and no value of the profile is written into the plan, the record, the
reports or the pack. The profile stays outside the run directory, as it does
for the scorecard.

## 4. The record

`apply` writes `parameter-register.json` into the run directory, beside
`column-provenance.json`, with sorted keys, a two-space indent and a final
newline. It is a function of the bundle's documents and the Simulation's pin,
never of the profile's content, so `apply --run-id` and `replay`, which are not
handed the profile, write the same bytes.

| Key | Holds |
|---|---|
| `_producer` | A URI naming the tool that wrote the record. |
| `_schemaURL` | The schema's `$id`. The format version is part of it: format 1-1-0 when an entry names the contract, and format 1-0-0 otherwise, so a register with no contract entry is recorded as it was before. |
| `simulationId` | The Simulation. |
| `profile` | `{"digest": ...}`, the pinned profile's digest, or `null`. |
| `declared` | How many numbers the register counts. |
| `counts` | `profile`, `assumption` and `default`, and `contract` in format 1-1-0: how many numbers rest on each. They add up to `declared`. |
| `entries` | The entries as declared, each with its `index` in the list, its `document`, `pointer` and `source`, and its `statistic`, `contract` or `reason`. |
| `parameters` | Every counted number: `document`, `kind`, `pointer`, `value` (the number as declared, in the canonical decimal spelling of KERNEL-SPEC section 2.1: no exponent, the declared scale kept, so `1e4` is `10000` and `.50` is `0.50`), `source` and `entry`, the index of the entry that decided it, or `null` for a default no entry names. |

A profile statistic is cited by a pointer and a definition, beside the pinned
profile's digest: the statistic's identity and the digest of the document it is
in. The record names no value of the profile.

`_producer` and `_schemaURL` are the OpenLineage facet envelope (`BaseFacet`),
so the record can travel on a lineage event as a custom run facet.

## 5. How the plan binds the record

`plan.json` holds `parameterRegisterDigest`, the `raw-bytes` digest of the
record's exact bytes (KERNEL-SPEC section 2.3), only when the Simulation
declares a register. The plan's own digest covers it, so the record is bound to
the run as `column-provenance.json` is by `columnProvenanceDigest`.

- `apply` refuses, with `FLX-2116` and before it writes a row, a record its
  own execution would write that does not hash to the plan's digest, a record
  the plan holds no digest of, and a digest the run has no record for.
- `verify` and `ship` read the record only through the plan's digest. A record
  that is missing, does not hash to the digest, or is not one this build can
  read is `FLX-2116`. So is a `parameter-register.json` in the run directory of
  a plan that binds none, which `ship` would otherwise pack as if the plan
  vouched for it.
- `ship` packs the bytes it checked, as `run/parameter-register.json`, never a
  file it opened again by name.

## 6. What the commands report

`plan`, `verify` and `ship` print one line for a run that binds a register:

```text
parameters: 35 declared -- 3 from the pinned profile, 2 assumed, 30 default
```

A register with a contract entry gives the contract's share after the
profile's: `parameters: 35 declared -- 3 from the pinned profile, 1 from the
contract, 2 assumed, 29 default`.

Their `--json` envelopes carry the same numbers: `parameter_register` in
`plan`'s artifacts and `parameterRegister` in `verify`'s and `ship`'s, each
`{"declared": n, "counts": {...}}`, the counts naming the sources of the
register's format. `validate` reports the counts under each Simulation as
`parameters`.

`EVIDENCE.md`'s "What Was Not Proven" section gains one paragraph: the shares
(the contract's among them when the register has a contract entry), each a
count and a percentage, how many defaults an entry states and how many
no entry names, and what the register does not show. A percentage is exact when
it is whole, and otherwise rounded to one decimal place, half to even; a share
that rounds to 0 or to 100 is written "below 0.1 %" or "above 99.9 %", so a
share is never shown as none or all when it is not. When there are assumptions,
a list follows the paragraph, as the What Was Not Proven list calls for: every assumption with what it
names, how many numbers it decided and its reason. A reason is printed as a
code span, so a renderer reads none of it as markup or a link. A reason that
holds a reserved word, a character that is not plain text or a backtick is left
out of the prose and kept in the record. A run that reads no number the register
counts says so in one sentence, and its line reads `parameters: the run reads
no number the register counts`.

## 7. Compatibility and later additions

The format grows by minor versions, which only add: an optional key, a value a
list may hold, a source. A source is a new value of `source` with its own
citation key, named in a new minor version's schema URL, so a reader that
predates it refuses the record rather than reads it as something else.

- **Format 1-1-0** adds `contract`, cited by `contract` (section 3). A record
  names 1-1-0 only when an entry names the contract, so every register that
  names none keeps its 1-0-0 record, bytes and digest. A reader of 1-1-0 reads
  1-0-0 records too.
- A source added later, such as a fitted-parameters document (fitting), follows
  the same rule. Moving the block from the
extension namespace into the FLUX specification is a change of where it is
declared, not of what it means.

## 8. Prior art

- **The ODD protocol** (Grimm et al. 2020, JASSS 23(2) 7): describe the basis
  of every parameter value. The register makes that a field per number.
- **W3C PROV-O** (Recommendation, 2013-04-30): a measured number is an entity
  `prov:wasDerivedFrom` the profile, its primary source; an assumption is
  `prov:wasAttributedTo` its author. The register keeps the classes and leaves
  out the RDF.
- **SDMX** `CL_OBS_STATUS` (version 2.3): one status per observation, and an
  observation with no status reads as normal. A number no entry names is a
  default in the same way.
- **RFC 6901** for the pointers, and the **OpenLineage** `BaseFacet` envelope
  for the record.
- Configuration tools that record where each value came from, among them
  Spring Boot's `env` endpoint (a value and its origin per property), for the
  shape of a value with its source beside it.
