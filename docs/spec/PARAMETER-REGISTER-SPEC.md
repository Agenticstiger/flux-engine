# Parameter register: normative specification (format 1-4-0)

A parameter register says what each number a run reads rests on. A number
comes from a statistic of the profile the Simulation pins (PROFILE-SPEC.md),
from a place in the FLUID contract the run emits from, from an assumption the
author states with its reason, or it is a default. A Simulation declares its
register in the engine's extension namespace, `apply` writes the record of it
as `run/parameter-register.json`, and `plan`, `verify` and `ship` report the
shares. Format 1-1-0 adds the contract source to format 1-0-0, format
1-2-0 adds the statistics of the pinned profile that no number rests on
(sections 1.1 and 7). Format 1-3-0 adds the overlay source and the statistic
that would replace an assumption (sections 1.2 and 1.3), calibrated values and
the response estimates the engine derives at plan time (section 8). Format 1-4-0 adds no
member and no source: it counts more positions and lets a citation name more definitions
(section 7).

Normative files beside this page:

- [`parameter-register-1-4.schema.json`](parameter-register-1-4.schema.json):
  the JSON Schema of a format 1-4-0 record, whose `$id` is
  `https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-4-0/FluxParameterRegister.json`.
- [`parameter-register-1-3.schema.json`](parameter-register-1-3.schema.json):
  the JSON Schema of a format 1-3-0 record, whose `$id` is
  `https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-3-0/FluxParameterRegister.json`.
- [`parameter-register-1-2.schema.json`](parameter-register-1-2.schema.json):
  the JSON Schema of a format 1-2-0 record, whose `$id` is
  `https://agenticstiger.github.io/flux/engine/spec/parameter-register/1-2-0/FluxParameterRegister.json`.
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
| `source` | `profile`, `contract`, `overlay`, `assumption`, `default` or `calibrated`. |
| `statistic` | For `profile` and `overlay` only, and required there: the citation (section 3). |
| `overlay` | For `overlay` only, and required there: the name of the overlay the Simulation declares (section 1.2). |
| `replacedBy` | For `assumption` and `default` only, optional: the statistic a profile of the contract would publish for the number (section 1.3). |
| `contract` | For `contract` only, and required there: the citation (section 3). |
| `calibration` | For `calibrated` only, and required there: the record of the fit (section 8.1). |
| `reason` | Required for `assumption`, optional for `default`, refused for `profile`, `contract` and `calibrated`: one line of text, at most 1000 characters. |

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
- a pointer that is not RFC 6901, and a `source` outside the six;
- an `overlay` entry without `statistic` or `overlay`, or with `reason`, `contract` or `calibration`, an `overlay`
  entry in a Simulation that pins no profile or declares no overlay, and an `overlay` that is not
  the name of the Simulation's overlay;
- a `profile` entry without `statistic` or with `reason`, a `profile` entry in
  a Simulation that pins no profile, and a citation that is not a mapping of
  `pointer` and `definition`, whose pointer is empty, or whose `definition` is
  not a registered statistic or metric;
- a `contract` entry without `contract` or with `reason`, and a citation that
  is not a mapping of `productRef` and `pointer`, whose `productRef` is not a
  contract the run's emits name, or whose pointer is empty or reaches nothing
  in that contract;
- a `replacedBy` on a source other than `assumption` and `default`, and one that is not a citation
  (a mapping of `pointer` and, but for a table's `rowCount`, a registered `definition`);
- an `assumption` without `reason`, a `statistic` on any source but
  `profile` and `overlay`, a `contract` on any source but `contract`, an `overlay` on any source
  but `overlay`, and a reason that is
  empty, longer than 1000 characters or holds a control character;
- a `calibrated` entry without `calibration` or with `reason`, a
  `calibration` on any source but `calibrated`, a `calibration` that is
  malformed, and one whose `after` is not the value the document holds at the
  pointer (section 8.1);
- two entries with the same document and pointer;
- an entry whose document the run does not read, whose pointer reaches
  nothing in it, or whose pointer reaches no number the register counts (a
  seed, for example, or text).

An empty list declares a register with no entry: every number is a default.
A YAML null declares none, as a null namespace does.

### 1.1 The statistics no number rests on

Beside `parameters`, the namespace may list the statistics of the pinned
profile that no number of the run rests on, each with its reason. `fluxsim
init` writes the list from its ledger (ADR 0014), so a reader of the evidence
sees what the profile said that the run does not show:

```yaml
extensions:
  com.agentics.flux/1:
    profile:
      digest: sha256:a1b2...
    parameters: [...]
    unused:
      - statistic: { pointer: /tables/orders/columns/tier/statistics/0, definition: levelHistogram/1 }
        reason: tier is drawn given segment, whose mutual information with it is higher
```

An item holds `statistic`, a citation as a `profile` entry gives one
(section 3), and `reason`, one line of text of at most 1000 characters, and
nothing else. The list belongs to the register: it is refused with
`FLX-3105` in a Simulation that declares no `parameters` or pins no profile,
as are an item that is not a mapping, a key an item does not carry, a missing
`statistic` or `reason`, and two items naming one statistic. `plan` reads each
citation in the profile it is handed, as it reads an entry's: a pointer that
reaches nothing, names another definition, or reaches a statistic the profile
withheld is `FLX-3113`. An empty list says that every statistic of the
profile is used. A YAML null lists none.

### 1.2 Numbers the overlay changed

A Simulation may declare a profile overlay (SEAM-FIDELITY-SPEC.md, ADR 0030 section 7): a named
RFC 7396 merge patch over the profile it pins. A number whose value the overlay sets is cited
with `source: overlay`, the overlay's `overlay` name, and a `statistic` that is a citation (section
3) into the overlaid profile, `MergePatch(profile, patch)`:

```yaml
parameters:
  - document: sim-subscribers
    pointer: /extensions/com.agentics.flux~11/emits/subscriber/rows
    source: overlay
    overlay: double-churn
    statistic: { pointer: /tables/subscriber/rowCount }
```

`plan` reads a `profile` citation in the pinned profile and an `overlay` citation in the overlaid
one, and refuses a `profile` citation of a number the patch changed, a pointer whose value, or any
value under it, differs between the two profiles, so every number the overlay changed names it. A
number the patch does not change may be cited either way. The statistics the register lists as unused (section 1.1) are read in the overlaid profile
when the Simulation declares an overlay, which is the profile the run is scored against. The overlay's numbers are the
maintainers' scenario, not a measurement: the privacy grade reads the pinned profile, `verify`
scores against the overlaid one, and the report says so.

### 1.3 What would replace an assumption

An `assumption` or a `default` may say which statistic would replace it:

```yaml
parameters:
  - document: sim-orders
    pointer: /extensions/com.agentics.flux~11/emits/orders/fidelity/columns/country/levels
    source: assumption
    reason: The profile publishes how many distinct values the column holds and not the values.
    replacedBy: { pointer: /tables/orders/columns/country/statistics/0, definition: levelHistogram/1 }
```

`replacedBy` is a citation (section 3) into a profile of the contract, the profile a profiler
would write if it measured the number. It is a request for a measurement and not a citation of
the pinned profile: `plan` reads it for its form and never in the pinned profile, and the
register's shares are the same whether it is there or not. `fluxsim init` writes it for each
number it assumes that a statistic would answer: a table's row count (`/tables/<t>/rowCount`),
a column's metric (`/tables/<t>/columns/<c>/metrics/<name>`, such as `nullCount`) or a
statistic (`/tables/<t>/columns/<c>/statistics/<i>`, such as a level or marginal histogram).
A number that is a setting and not a statistic has none: a tolerance, the bounds of a
committed state, a check's budget and a template default. The index `<i>` of a statistic is its
place in the request `init` writes for the same assumptions, whose statistics are in identifier
order, so a profiler that writes the profile from the request lists them there. `init` writes the
request inside the bundle directory as `requests/assumptions.profile-request.yml`, a profile request
(PROFILE-REQUEST-SPEC.md, format 1-0-0) for exactly the statistics the entries name. Running `init`
again with a profile that publishes them turns each of those assumptions into a `profile` citation.

## 2. The numbers a register counts

The register counts the numbers the run reads at the positions this section names, in these
documents:

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
| `fidelity.columns.<c>.levels.<level>`, `.levelWeightsGiven.<level>.<level>` | a categorical column's weights; a column drawn given several columns nests one more `<level>` for each, up to 8 |
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

The namespace's declarations of how rows relate and fall in time sit beside `emits` and name
positions of their own, which join the table above in format 1-4-0: counting one would change the
record, and with it the plan's `parameterRegisterDigest`, of every run whose Simulation declares
the key, so a position joins only in a new format. A number counts there when it is an integer, a decimal, or text read as a
finite decimal, as above. `<n>` is an index of the key's list:

| Position | What it is |
|---|---|
| `facts[<n>].tolerance` | a fact's tolerance |
| `links[<n>].sentinels.<column>` | a sentinel's text, when it reads as a number |
| `counts[<n>].minChildren`, `.tolerance` | the least children of a parent, and the check's tolerance |
| `counts[<n>].children.levels[]`, `.levelWeights[]`, `.bins[]`, `.binWeights[]` | the classes of children per parent, and their weights |
| `counts[<n>].childrenGiven.<class>[]` | the weights of each class of the given column |
| `history[<n>].entities` | the number of entities |
| `history[<n>].parent.minChildren`, `.parent.children.levels[]`, `.levelWeights[]`, `.bins[]`, `.binWeights[]` | the parent's children, as `counts` holds them |
| `history[<n>].versions.levels[]`, `.levelWeights[]`, `.bins[]`, `.binWeights[]` | the classes of versions per entity, and their weights |
| `history[<n>].start.bins[]`, `.binWeights[]`, `.length.bins[]`, `.binWeights[]` | the histograms of the first start and of a version's length |
| `history[<n>].tracked[].initial.levelWeights[]`, `.transitions.<level>.<level>` | a tracked column's first-level weights and its moves |
| `history[<n>].reasons.levelWeights[]`, `.end.share` | the share of changes each column makes, and the share of histories that end |
| `events[<n>].lag.bins[]`, `.binWeights[]` | an event's lag |
| `periods[<n>].kinds[].count`, `.count.levels[]`, `.count.levelWeights[]` | the rows a period holds, as one number or as classes with weights |
| `lifetimes[<n>].target` | the share of rows that may break the lifetime's order |
| `lifetimes[<n>].duration.bins[]`, `.binWeights[]`, `.open`, `.mean`, and the same under `.duration.states.<state>` and `.duration.otherwise` | a duration's histogram or exponential, or a dwell family's |
| `within[<n>].target`, `.offset.bins[]`, `.offset.binWeights[]` | a date's target share and its offset from the lifetime |
| `rates[<n>].month[]`, `.dayOfWeek[]`, `.hourOfWeek[]` | the seasonal weights |
| `conditions.<exposeId>[<n>].count` | the rows of a group |
| `emits.<exposeId>.fidelity.copula.rankCorrelations.pairs[].value` | a pair's rank correlation (SEAM-FIDELITY-SPEC.md section 2) |

A `levels` list that names the levels of a column, as `history[<n>].tracked[].initial` and
`.reasons` do, holds names and is not counted, nor are `values` of a `conditions` group, an
`overrides` of a periods kind or `end.value`, which are values of a column.

Not counted are the namespace's declarations of what a patch or a consumer asks for, and the ones
that hold no number: `bridges`, `associations`, `asOf`, `snapshots`, `copies`, `generators`,
`overlay`, `consumers`, `responses`, `forecasts` and `privacy`. A number there is read by the run but has no
entry and no share in the record, so a calibration cannot name it (`FLX-3706`).

**Counting a position changes the record of a run that declares it.** The numbers of a key a
Simulation does not declare are not there to count, so a run that declares none of the keys above
keeps its record, bytes and digest, and the format it had. A run that declares one counts its
numbers, its record is format `1-4-0`, and its `parameterRegisterDigest`, and so its plan's
digest, differ from what the engine before format 1-4-0 recorded. A number no entry names is a `default`, as every
other is.

A citation may name the definitions of the rank correlations, `kendallTau/1` and `spearmanRho/1`,
and of a clique marginal, `cliqueMarginal/1` and `cliqueMarginal/2`, as it names any other
registered definition (section 3). A record that cites `kendallTau/1`, `spearmanRho/1` or
`cliqueMarginal/2`, in an entry, a `replacedBy` or an `unused` item, is format `1-4-0`; one that
cites only `cliqueMarginal/1`, which an earlier format already allowed, keeps its format. A record
whose `unused` items name a whole entry of the profile (a calendar entry, a date pair, an exposure,
a history or a tracked column of one; see `definition` below) is format `1-4-0` too.

The record lists the numbers by document, documents in NFC order of their ids,
and within a document in key order (Unicode code points, mapping by mapping;
lists by index), `spec` first and then the namespace.

## 3. Citing a profile statistic or the contract

A `profile` entry's `statistic` names where in the pinned profile its numbers
come from, and an `overlay` entry's where in the overlaid profile:

| Key | Holds |
|---|---|
| `pointer` | An RFC 6901 JSON Pointer into the pinned profile. |
| `definition` | The registered definition the pointer lands in: a statistic (`mean/1`, `marginalHistogram/1`, `levelHistogram/1`, `support/1`, `correlation/1`, `quantile/1`, `quantile/2`, `kendallTau/1`, `spearmanRho/1`, `cliqueMarginal/1`, `cliqueMarginal/2`) or a column metric (`count/1`, `nullCount/1`, `distinctCount/1`, `sum/1`, `min/1`, `max/1`, `quantiles/1`). A table's `rowCount` has none, and neither has an entry of `references` (`/references/<expose>`, a table published whole), which an `unused` item may name. An `unused` item may also name, whole, an entry that holds statistics in a profile of format 1-3-0, which has no definition either (register format 1-4-0, section 7): a calendar entry (`/tables/<expose>/calendar/<i>`), a date pair (`/tables/<expose>/datePairs/<i>` or `/relationships/<i>/dates/<j>`), an exposure (`/relationships/<i>/exposure/<j>`), a history (`/tables/<expose>/history`) or a tracked column of one (`/tables/<expose>/history/columns/<name>`). The profile reader refuses such an entry without some of its members, so a Simulation names it whole when none of its statistics is read. |

`plan` is always handed the profile a Simulation pins (PROFILE-SPEC.md
section 6), and it reads every citation in it, or in the overlaid profile for an
`overlay` entry, before anything is written. A citation holds when:

- the pointer reaches a value in the profile;
- the pointer is a table's `rowCount` or a table published whole under `references`, and the
  entry names no `definition`;
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
| `_schemaURL` | The schema's `$id`. The format version is part of it: format 1-4-0 when the register counts a number at a position of the table of the namespace's other keys or of the copula (section 2), or an entry cites `kendallTau/1`, `spearmanRho/1` or `cliqueMarginal/2`, or an `unused` item names a whole entry of the profile (section 7), format 1-3-0 when an entry cites an overlay, names a replacement or is a calibrated value, or the register holds a response estimate (sections 1.2, 1.3 and 8), format 1-2-0 when the Simulation lists unused statistics, format 1-1-0 when an entry names the contract, and format 1-0-0 otherwise, so a register that uses none of a later format's additions is recorded as it was before, with its bytes. |
| `simulationId` | The Simulation. |
| `profile` | `{"digest": ...}`, the pinned profile's digest, or `null`. |
| `declared` | How many numbers the register counts. |
| `counts` | `profile`, `assumption` and `default`, `contract` in formats 1-1-0 and later, and `overlay` and `calibrated` in formats 1-3-0 and 1-4-0: how many numbers rest on each. They add up to `declared`. |
| `entries` | The entries as declared, each with its `index` in the list, its `document`, `pointer` and `source`, and its `statistic`, `contract`, `overlay`, `calibration`, `reason` or `replacedBy`. Formats 1-3-0 and 1-4-0 list the response estimates after them (section 8.2). |
| `unused` | Formats 1-2-0, 1-3-0 and 1-4-0, when the Simulation lists them: the statistics of the pinned profile no number rests on, as the Simulation lists them, each `{"statistic": {"pointer", "definition"}, "reason"}` (section 1.1). |
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

A register of format 1-3-0 or 1-4-0 gives the overlay's share after the contract's:
`parameters: 35 declared -- 3 from the pinned profile, 1 from the contract, 4 from the
overlay, 2 assumed, 25 default`. A register with a contract entry gives the contract's share after the
profile's: `parameters: 35 declared -- 3 from the pinned profile, 1 from the
contract, 2 assumed, 29 default`. A register of format 1-2-0 also gives the
contract's share, and adds how many statistics of the profile are unused:
`...; 2 statistics of the profile unused`.

Their `--json` envelopes carry the same numbers: `parameter_register` in
`plan`'s artifacts and `parameterRegister` in `verify`'s and `ship`'s, each
`{"declared": n, "counts": {...}}`, the counts naming the sources of the
register's format, and `"unused": n` for a register of format 1-2-0. `validate` reports the counts under each Simulation as
`parameters`.

`EVIDENCE.md`'s "What Was Not Proven" section gains one paragraph: the shares
(the contract's among them when the register has a contract entry, and the overlay's, named, with
the words that it is the maintainers' scenario and not a measurement, and the calibrated values, in format 1-3-0), each a
count and a percentage, how many defaults an entry states and how many
no entry names, and what the register does not show. A percentage is exact when
it is whole, and otherwise rounded to one decimal place, half to even; a share
that rounds to 0 or to 100 is written "below 0.1 %" or "above 99.9 %", so a
share is never shown as none or all when it is not. When there are assumptions,
a list follows the paragraph, as the What Was Not Proven list calls for: every assumption with what it
names, how many numbers it decided and its reason, and the statistic that would replace it when
the entry names one (format 1-3-0). A reason is printed as a
code span, so a renderer reads none of it as markup or a link. A reason that
holds a reserved word, a character that is not plain text or a backtick is left
out of the prose and kept in the record. A run that reads no number the register
counts says so in one sentence, and its line reads `parameters: the run reads
no number the register counts`. A register of format 1-2-0 then lists every
statistic of the profile no number rests on, with its definition and its
reason, or says in one sentence that it lists none.

## 7. Compatibility and later additions

The format grows by minor versions, which only add: an optional key, a value a
list may hold, a source. A source is a new value of `source` with its own
citation key, named in a new minor version's schema URL, so a reader that
predates it refuses the record rather than reads it as something else.

- **Format 1-1-0** adds `contract`, cited by `contract` (section 3). A record
  names 1-1-0 only when an entry names the contract, so every register that
  names none keeps its 1-0-0 record, bytes and digest. A reader of 1-1-0 reads
  1-0-0 records too.
- **Format 1-2-0** adds `unused` (section 1.1), and its counts name all four
  sources. A record names 1-2-0 only when the Simulation lists unused
  statistics, so every other register keeps its 1-0-0 or 1-1-0 record, bytes
  and digest. A reader of 1-2-0 reads both earlier formats.
- **Format 1-3-0** adds `source: overlay`, with `overlay` and `statistic` (section 1.2), and
  `replacedBy` on an assumption or a default (section 1.3), the source `calibrated` with its
  `calibration` member, and the response estimates (section 8), and its counts name all six
  sources. A record names 1-3-0 only when the register holds one of these additions, so every
  other register keeps the format and the bytes it had, and a Simulation that declares an overlay
  and cites none stays at its earlier format. This format is shared within one release: a later
  addition to 1-3-0 extends the sources and members here. A reader of 1-3-0 reads the earlier
  formats too.
- **Format 1-4-0** adds no source and no member, and its counts name the six sources of 1-3-0. It
  counts the numbers at the positions of the table of the namespace's other keys and of the
  copula (section 2), a citation may name `kendallTau/1`, `spearmanRho/1` and
  `cliqueMarginal/2`, and an `unused` item may name a whole entry of the profile (section 7). A
  record names 1-4-0 only when it counts a number at one of those positions, an entry, a
  `replacedBy` or an `unused` item names one of those definitions, or an `unused` item names a
  whole entry, so every other register keeps the format and the bytes it had, with its digests.
  A reader of 1-4-0 reads the earlier formats too.
- A source added later follows the same rule. Moving the block from the
extension namespace into the FLUX specification is a change of where it is
declared, not of what it means.

## 8. Format 1-3-0

Format 1-3-0 carries four additions: the source `overlay` and the member
`replacedBy` (sections 1.2 and 1.3), and, in the two subsections below, calibrated
values and response estimates. A record names 1-3-0 when it holds any addition of
the format, and its `counts` then name every source the format has.

### 8.1 Calibrated values

An entry whose `source` is `calibrated` names a number a calibration moved. Its
`calibration` member is a mapping of exactly these keys:

| Key | Holds |
|---|---|
| `queries` | The ids of the queries the value was fitted to: a list of distinct strings, at least one. |
| `before` | The value before the fit, as exact decimal text. |
| `after` | The value after the fit, as exact decimal text. |
| `patch` | The calibration patch's digest, `sha256:` and 64 lowercase hex digits. |
| `targets` | The digest of the profile that held the targets, spelled the same way. |
| `heldOut` | `{ "profile", "largestGap", "tolerance" }`: the held-out profile's digest, the largest gap among its queries and the tolerance it was set against, the last two as exact decimal texts. |
| `versions` | `{ "cma", "sqlglot", "duckdb" }`: the version of each tool the fit ran with, non-empty strings. |

An exact decimal text has no exponent and no plus sign, no leading zero, and
no sign on zero: `"0.25"`, `"-3"`. The entry's pointer has to reach one number,
and `after` has to equal the number the document holds there, so a record
cannot say a value was fitted to something the bundle does not hold. Each
refusal below is `FLX-3105`: a `calibrated` entry without `calibration`, a
`calibration` under another source, an unknown or missing key, a digest or a
decimal that is malformed, and an `after` that is not the value at the
pointer. The record carries the member through to the entry unchanged, and the
numbers under the pointer count as `calibrated`.

A value is calibrated only when it passed its held-out check, so an entry whose
`heldOut.largestGap` is larger than its `heldOut.tolerance` (compared as the
decimals they are) is refused with `FLX-3716`. The engine does not re-run the
fit and does not read the profiles or the patch the digests name: the bundle
declares the record, and the evidence record's note says so, in these words for
every run that holds a calibrated value. The note of a run with none is as it
was.

### 8.2 Response estimates

The response runtime derives estimates at plan time and adds them to the
register. They are not declared and they are no counted number: each is an
entry listed after the declared ones, numbered on from them, with these keys:

| Key | Holds |
|---|---|
| `index` | The entry's place in the list. |
| `kind` | `responseControl`, a control rate, or `responseEffect`, an effect. |
| `document` | The Simulation. |
| `pointer` | `/extensions/com.agentics.flux~11/responses/<index>/segments/<key>`: the response's place in the Simulation's `responses` list and the segment's key. |
| `value` | The estimate, as exact decimal text. |
| `source` | `profile`: the estimate is read from the pinned profile. |
| `cells` | The query cells it was read from, each `queries/<query id>/<group key>`: the control cell for a rate, the control cell and then the treated cell for an effect. |
| `interval` | For an effect only: `{ "low", "high", "confidence" }`, exact decimal texts, `low` at most `high`, the confidence between 0 and 1. |

Two estimates of the same kind for one response and segment are refused.

## 9. Prior art

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
- mypy's `--warn-unused-configs` and dbt's `--warn-error-options`, which name
  every input that has no effect, for the `unused` list (ADR 0014).
