---
title: "Give an entity a history"
description: "Declare a history for a table with a validity window: every entity gets a chain of versions, a row can read the previous version, and a dated row reads the version that was valid on its date."
---

# Give an entity a history

Use this page when a table keeps every version of a record, a customer's subscription that changes plan, a product whose price moves, a dimension kept as a slowly changing type 2 table, and the rows you generate should look like that. A table with a validity window and no history is laid out one row at a time, so one entity's rows overlap, leave gaps, or never agree on the plan they hold. A `history` entry lays the rows out entity by entity instead: every entity has a chain of versions that meet end to start, one tracked column changes at each step, and the entity ends or stays open as you declare. It needs kernel 0.2.0, which a new plan takes, and the tables must be generated from statistics, as [Use the FLUID seam](./use-the-fluid-seam.md) describes.

The page has three parts: the `history` key, a rule that reads the `previous` version, and an `asOf` entry that lets a dated row read the version valid on its date. To derive a snapshot, change events or periodic rows from a history, see [Project a history](./project-a-history.md).

## Declare the history

The bundle has a `customer` table, a `subscription_version` table that holds every version of every subscription, and an `invoice` table. A subscription version is valid from `valid_from` until `valid_to`, and the contract says so with two time dimensions that declare `validityParams`.

```yaml
# persona.flux.yml
fluxVersion: "0.5.0"
kind: Persona
id: persona-subscriptions
name: Persona for Subscriptions
metadata:
  owner:
    team: subscriptions
spec:
  traits: { digitalFluency: 0.5 }
```

```yaml
# world.flux.yml
fluxVersion: "0.5.0"
kind: World
id: world-subscriptions
name: World for Subscriptions
metadata:
  owner:
    team: subscriptions
spec:
  seed: 42
  vertical: telco
  population: { size: 1, lifecycleMix: { active: 1 } }
```

```yaml
# shop.fluid.yml
fluidVersion: "0.7.7"
kind: DataProduct
id: billing.subscriptions
name: Subscriptions
metadata:
  owner:
    team: subscriptions
exposes:
  - exposeId: customer
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: billing, table: customer, region: eu-west-1 }
    contract:
      schema:
        - { name: customer_id, type: INT, required: true }
        - { name: region, type: STRING, required: true }
    semantics:
      entities:
        - { name: customer, type: primary, expr: customer_id }
  - exposeId: subscription_version
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: billing, table: subscription_version, region: eu-west-1 }
    contract:
      schema:
        - { name: subscription_sk, type: INT, required: true }
        - { name: subscription_id, type: INT, required: true }
        - { name: customer_id, type: INT, required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE, required: false }
        - { name: plan, type: STRING, required: true }
        - { name: prior_plan, type: STRING, required: false }
        - { name: status, type: STRING, required: true }
        - { name: started_on, type: DATE, required: true }
    semantics:
      entities:
        - { name: subscription_version, type: primary, expr: subscription_sk }
        - { name: subscription, type: natural, expr: subscription_id }
      dimensions:
        - { name: valid_from, type: time, typeParams: { validityParams: { isStart: true } } }
        - { name: valid_to, type: time, typeParams: { validityParams: { isEnd: true } } }
        - { name: plan, type: categorical }
        - { name: status, type: categorical }
        - { name: started_on, type: time }
      relationships:
        - name: customer
          to: customer
          fromColumns: [customer_id]
          toColumns: [customer_id]
          cardinality: many_to_one
          joinType: inner
  - exposeId: invoice
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: billing, table: invoice, region: eu-west-1 }
    contract:
      schema:
        - { name: invoice_id, type: INT, required: true }
        - { name: subscription_id, type: INT, required: true }
        - { name: invoice_date, type: DATE, required: true }
        - { name: amount, type: "DECIMAL(10,2)", required: false }
      invariants:
        - id: amount_of_plan
          derive: amount
          expr: "CASE WHEN subscription.plan = 'team' THEN 99 WHEN subscription.plan = 'pro' THEN 49 ELSE 19 END"
    semantics:
      entities:
        - { name: invoice, type: primary, expr: invoice_id }
      dimensions:
        - { name: invoice_date, type: time }
      relationships:
        - name: subscription
          to: subscription_version
          fromColumns: [subscription_id]
          toColumns: [subscription_id]
          cardinality: many_to_one
          joinType: inner
```

The Simulation adds a `history` list under the `com.agentics.flux/1` extensions, next to `emits`. This entry declares 60 subscriptions of 200 versions in all, spread over the 50 customers so that every customer has at least one. A subscription has one, two, four to eight or nine versions, one version in half of the cases; it first starts between the start of 2023 and the end of 2025; a version lasts a month, three months, a year or three years; the plan moves along the declared transitions; and a fifth of the subscriptions end by cancelling.

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-subscriptions
name: Subscriptions
metadata:
  owner:
    team: subscriptions
spec:
  worldRef: world-subscriptions
  fidelity: deterministic
  seed: 42
  timeBounds:
    start: "2023-01-01T00:00:00Z"
    end: "2027-01-01T00:00:00Z"
  personaRefs: [persona-subscriptions]
  emits:
    - productRef: billing.subscriptions
      exposeId: customer
      fluidVersion: "0.7.7"
      provenance: { contractDigest: "sha256:c987c9743012f9ee0e0655ade67a599b5695bdaaaddd3b3b60fce1eb5c984d4c" }
    - productRef: billing.subscriptions
      exposeId: subscription_version
      fluidVersion: "0.7.7"
      provenance: { contractDigest: "sha256:c987c9743012f9ee0e0655ade67a599b5695bdaaaddd3b3b60fce1eb5c984d4c" }
    - productRef: billing.subscriptions
      exposeId: invoice
      fluidVersion: "0.7.7"
      provenance: { contractDigest: "sha256:c987c9743012f9ee0e0655ade67a599b5695bdaaaddd3b3b60fce1eb5c984d4c" }
extensions:
  com.agentics.flux/1:
    history:
      - emit: subscription_version
        entities: 60
        parent: { relationship: customer, minChildren: 1 }
        versions: { bins: ["1", "2", "4", "9"], binWeights: ["0.45", "0.35", "0.20"] }
        start: { bins: ["2023-01-01", "2024-01-01", "2025-07-01"], binWeights: ["0.55", "0.45"] }
        length: { bins: ["30", "90", "365", "1096"], binWeights: ["0.30", "0.45", "0.25"], closed: lower }
        until: "2025-12-31"
        chain: nextStart
        activation: started_on
        tracked:
          - column: plan
            transitions:
              basic: { pro: "0.6", team: "0.4" }
              pro: { basic: "0.5", team: "0.5" }
              team: { basic: "0.5", pro: "0.5" }
        end: { mode: finalVersion, share: "0.2", column: status, value: cancelled }
    asOf:
      - { emit: invoice, relationship: subscription, date: invoice_date }
    emits:
      customer:
        rows: 50
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            customer_id: { range: { min: '1', max: '50' } }
            region: { levels: { eu: '0.6', us: '0.4' } }
          properties:
            - property: mean
              column: customer_id
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
      subscription_version:
        rows: 200
        rules:
          - { id: plan_before, language: flux-expr/2, derive: prior_plan, expr: previous.plan }
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            subscription_sk: { range: { min: '1', max: '200' } }
            subscription_id: { range: { min: '1', max: '60' } }
            valid_from: { range: { min: '2023-01-01', max: '2025-12-31' } }
            valid_to: { range: { min: '2023-01-01', max: '2025-12-31' } }
            plan: { levels: { basic: '0.5', pro: '0.3', team: '0.2' } }
            status: { levels: { active: '1', cancelled: '0' } }
            started_on: { range: { min: '2023-01-01', max: '2025-12-31' } }
          properties:
            - property: mean
              column: subscription_sk
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
      invoice:
        rows: 600
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            invoice_id: { range: { min: '1', max: '600' } }
            subscription_id: { range: { min: '1', max: '200' } }
            invoice_date: { range: { min: '2026-01-01', max: '2026-12-31' } }
          properties:
            - property: mean
              column: invoice_id
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
```

Plan, apply and verify it, as for any bundle:

```console
$ fluxsim validate .
.: valid -- 3 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
$ fluxsim run .
plan written to ...
applied to ...
emitted customer: 50 rows (generator); subscription_version: 200 rows (generator); invoice: 600 rows (generator)
run ... complete
$ fluxsim verify . --run-id $(ls .flux/runs)
...
  rules invoice: amount_of_plan 600 of 600 (100.00%); asOf:subscription.invoice_date 600 of 600 (100.00%) (not enforced)
  rules subscription_version: plan_before 200 of 200 (100.00%); history:no_gaps_no_overlap 60 of 60 (100.00%); history:one_current 60 of 60 (100.00%); history:ended_is_last 60 of 60 (100.00%)
...
```

## What the history guarantees

Read the versions back:

```python
import glob
import json

emits = glob.glob(".flux/runs/*/emits/")[0]
versions = [json.loads(line) for line in open(emits + "subscription_version.ndjson")]
assert len(versions) == 200

# Rows come grouped by customer, then subscription, then version in time order.
order = [(row[2], row[1], row[3]) for row in versions]
assert order == sorted(order)

by_subscription = {}
for row in versions:
    by_subscription.setdefault(row[1], []).append(row)
assert len(by_subscription) == 60

for chain in by_subscription.values():
    for before, after in zip(chain, chain[1:]):
        assert before[4] == after[3]  # a version ends where the next one starts
        assert before[5] != after[5] or after[7] == "cancelled"  # a change, or the ending version
        assert after[6] == before[5]  # previous.plan
    assert chain[0][6] is None
    assert chain[-1][4] is None  # the last version is open
    assert all(row[7] == "active" for row in chain[:-1])
    assert all(row[8] == chain[0][3] for row in chain)  # activation

ended = [chain for chain in by_subscription.values() if chain[-1][7] == "cancelled"]
assert abs(len(ended) - 0.2 * 60) < 1  # the share of 0.2 of every entity
```

Seven things hold, whatever the seed:

- **A chain with no gap and no overlap.** Under `chain: nextStart`, the default, a version ends at the instant the next one starts, a half-open interval as a dbt snapshot writes it. `dayBefore` ends it the day before the next start instead, for a DATE chain.
- **One open version.** The last version of an entity that has not ended has no end, or the text `openEnd` gives, such as `"9999-12-31"`.
- **One change at each step.** A later version differs from the one before it in one tracked column, the change reason. An ending version is the exception: it changes `status` and no tracked column. With several tracked columns, `reasons` gives the share of the changes each makes, exact up to the largest remainder.
- **Every other column is carried.** A column that the history does not set takes, on every version, the value drawn for the entity's first version, so a customer's region does not change from one version to the next.
- **The ended share is exact.** With an `end`, `share` of all the entities end, up to the largest remainder (fewer when too few have two versions to end on, and `plan` says what the share yields), and which ones is a permutation's choice, not a draw. Under `finalVersion` an ended entity gains a last version that holds `value` in `column` and stays open; under `closeLast` its last version is closed instead. The ending version is no change reason, so `value` appears on no other version.
- **The number of versions comes from a shape.** The versions per entity follow `versions` class for class, as the children per parent follow `children` in [Give every parent its children](./give-every-parent-its-children.md). An entity that has more room before `until` takes more of them. Without `versions` the versions are spread as evenly as whole counts allow.
- **A history stops changing at `until`.** Every version starts before `until`, every closed version ends before it, and nothing in a history reads `spec.timeBounds.end`. A longer window rewrites no row of the history, and what you project from it keeps its earlier rows too (see [Project a history](./project-a-history.md)).

The rows of a history's emit are grouped by parent, then by entity, then by version, and an entity's versions are consecutive rows. A consumer that needs another order sorts the table.

## Keep the history inside its parent's lifetime

A vehicle should not start before its depot opened, nor outlive the depot. Declare the depot's lifetime
with a [`lifetimes` entry](./keep-dates-inside-their-parents-lifetime.md) and ask the history to keep to it:

```yaml
lifetimes:
  - { emit: depot, start: opened_on, end: closed_on }
history:
  - emit: vehicle_version
    parent: { relationship: depot, minChildren: 1, lifetime: true }
```

An entity's first start is then drawn inside its depot's lifetime, with room for its versions before the depot's
last day, and its versions end by that day. An entity whose depot closed ends: under `finalVersion` its final version
starts, and under `closeLast`, or with no `end`, its last version closes, by the depot's last day. Those entities
count towards the `share` of the `end`. A depot with no end, a NULL end, or an end before its start cuts nothing.
The key needs a `lifetimes` entry on the emit the relationship reaches, or the plan refuses it with `FLX-3403`.
A history never writes a version it knows breaks the bound:

- An entity whose depot leaves room for fewer versions than it was given is capped at that room, and the versions it
  loses go, one at a time, to the entities with the most room left, so the emit keeps its declared `rows`. Only when
  no entity has room left does the history hold fewer rows, and `plan`, `apply` and `verify` count the rows it has.
  `plan` says how many entities were capped, how many versions moved and any shortfall.
- An entity whose depot leaves room for no version at all (it closed before the window, or opened in the last days
  before `until`) is `FLX-3403` at plan, naming `history(<emit>).parent.lifetime`, how many entities it is and the range
  of their depots' lifetimes. An entity whose depot has room for one version is not refused: its first start is drawn
  with room for that one.
- When the depot's emit declares a history of its own, a lifetime column it sets can differ between its versions, and
  the bound would hold an entity to an arbitrary one. The depot's history must carry the lifetime columns, or the plan
  refuses the key with `FLX-3403`, naming the emit and the columns.

## The `history` key

`history` is a list in the Simulation's `com.agentics.flux/1` extensions, with at most one entry for each emit. The emit's contract names the entity, the start and the end, so none of them is repeated in the bundle.

| Key | Meaning |
|---|---|
| `emit` | The expose that holds the versions. Its contract declares one `natural` entity and two time dimensions with `validityParams`, `isStart` and `isEnd`, both DATE or both TIMESTAMP. Required. |
| `entities` | A whole number above zero: how many entities. `--scale` scales it as it scales `rows`; the emit's `rows` is the number of versions. Required. |
| `parent` | Optional: `{relationship, minChildren, parentWhen, children, lifetime}`. A to-one relationship of the emit whose parent holds each entity, with the three layout keys of a [`counts` entry](./give-every-parent-its-children.md#the-counts-key), read over entities instead of rows. `lifetime: true` keeps the history to its parent's lifetime: see below. |
| `versions` | Optional: the shape of the number of versions per entity, `levels` with `levelWeights` or `bins` with `binWeights` over whole counts of at least 1. |
| `start` | Optional: a histogram of an entity's first start, `bins` and `binWeights` over values of the start column's type, with edges inside the window and not after `until`. Without it the first start is spread evenly over what the entity has room for. |
| `length` | Required: a version's length, a histogram whose edges are whole days of at least one, in the spelling of a fidelity `distribution` (`bins`, `binWeights`, `closed`). A TIMESTAMP chain draws whole milliseconds inside its day bins. |
| `until` | Required: the history's horizon, a value of the start column's type, inside the window. Every version starts before it. |
| `chain` | `nextStart`, the default, or `dayBefore` (DATE columns only). |
| `openEnd` | Optional: the text written in the end column of an open version, read as the column's type. Without it an open end is NULL. The one date allowed outside the window: one before `until`, or under `nextStart` one inside `timeBounds`, is refused, because a closed version could end there. `fluxsim init` copies it from the contract's `validityParams.openEnd` on the validity window's end dimension, and the declared `chain` sets `chain` the same way. |
| `activation` | Optional: a column of the start's type that holds the entity's first start on every version. |
| `tracked` | The columns that change between versions: each is `{column, initial, transitions}`, a categorical column with `levels`. `initial` gives the first version's value (the column's own levels by default), and `transitions` gives each level the weights of the levels it moves to, never itself. |
| `reasons` | `levels` with `levelWeights`, naming tracked columns: the share of the changes each makes. Required when more than one column is tracked. |
| `end` | Optional: `{mode, share, column, value}`. `mode` is `finalVersion` or `closeLast`; `share` is a decimal from 0 to 1; `column` and `value` mark an ended entity's last version, and `finalVersion` needs them. |

No other key is read, and an unknown key is refused with [FLX-3105](../reference/errors/FLX-3105.md).

The entry changes the rows of its emit, so a change to it moves the plan's digests. A bundle with no `history` entry keeps every byte and digest it had.

## A rule that reads the previous version

On a history's emit a FLUX-side rule may read `previous.<column>`, or `previous.<relationship>.<column>`, the same column or path on the entity's previous version. On an entity's first version every `previous` path is NULL. The bundle above declares one:

```text
rules:
  - { id: plan_before, language: flux-expr/2, derive: prior_plan, expr: previous.plan }
```

A rule is written at `emits.<exposeId>.rules` in the Simulation, because `previous` is an extension of FLUID SQL/1 and cannot sit in a contract. The previous version's value is the final one, its own rules included, so a chain of `previous` reads holds version by version. A rule that reads both `previous` and a window over the row's siblings is refused with [FLX-6210](../reference/errors/FLX-6210.md). On a table with a validity window and no `history` entry, a rule that reads `previous` is `FLX-6210` too, and on a table with no window it is [FLX-6106](../reference/errors/FLX-6106.md). `fluxsim verify` evaluates the rule on the output by the same definition, so the line `plan_before 200 of 200` above counts every row.

## A dated row reads the version valid on its date

An `invoice` points at a subscription, and a subscription has many versions. An `asOf` entry says which one a path through the relationship reads: the version valid on the row's own date. It is a list in the same extensions, with one entry for each relationship.

```text
asOf:
  - { emit: invoice, relationship: subscription, date: invoice_date }
```

| Key | Meaning |
|---|---|
| `emit` | The expose that holds the dated rows. Required. |
| `relationship` | A to-one relationship of `emit` whose target is a history's emit and whose `toColumns` are that history's natural entity. Required. |
| `date` | A DATE or TIMESTAMP column of `emit`. Required. |

In the bundle above, the invoice's `amount` is a contract `derive` over `subscription.plan`. With the entry, the plan it reads is the plan the subscription had on `invoice_date`. The check goes through every invoice:

```python
invoices = [json.loads(line) for line in open(emits + "invoice.ndjson")]
price = {"team": "99.00", "pro": "49.00", "basic": "19.00"}

for _, subscription, date, amount in invoices:
    valid = [
        row
        for row in by_subscription[subscription]
        if row[3] <= date and (row[4] is None or date < row[4])
    ]
    assert len(valid) == 1
    assert amount == price[valid[0][5]]
```

A version is found by bisection over the entity's starts, which its chain gives without reading the table. A few more facts:

- A row dated before its entity's first start, or after an ended entity's closed end, reads NULL through every path of the link, as a link that reaches no row does. `fluxsim verify` counts how many rows found a version on the line `asOf:<relationship>.<date>`. It is measured and not enforced.
- A DATE is the instant of its midnight UTC when it is read against a TIMESTAMP chain. Under `dayBefore` a DATE end covers its whole day.
- A column the reader requires and a rule sets from the link would be NULL for a row that finds no version, so it is refused with [FLX-3403](../reference/errors/FLX-3403.md): declare the column optional, as `amount` is here, or give the history no `end` and the date a range that starts at or after `until`.
- A child still picks a row of the history's emit as any child does, so an entity is picked in proportion to its versions. Which version a path reads is the date's, not the pick's.
- Without an `asOf` entry a path into a history's emit may read what is the same on every version, the entity's key, its activation and its carried columns. A tracked column, a validity column or a column a rule sets is `FLX-6106`, and the message names `asOf`.

## What `fluxsim verify` checks

Each line is enforced, so one violating entity fails the run, except the `asOf:` line, which is measured:

| Line | Holds when |
|---|---|
| `history:no_gaps_no_overlap` | an entity's versions meet end to start, as the chain says. |
| `history:one_current` | an entity has one open version, its last, unless it ended under `closeLast`, when it has none. |
| `history:ended_is_last` | with an `end.column` and `end.value`, the value appears only on an entity's last version. |
| `history:within_parent` | with `parent.lifetime: true`: an entity's first start is not before its parent's start, no version starts after the parent's last day, every closed end is at or before it, and an entity whose parent ended by `until` has ended. With no `end` column, a closed last version is allowed only when the parent's last day is at most `until`; any other entity keeps its last version open. It covers the entities whose parent row has a start. |
| `history:open_end` | with an `openEnd`, no version's end is NULL and only an entity's last version carries the `openEnd` value, so an end column that mixes NULL and the value fails. Without one, the line is present only when some end is in the year 9999 or later, an open end the entry does not declare, and each entity with one fails it. |
| `asOf:<relationship>.<date>` | how many rows read a version. Not enforced. |

A rule that reads `previous` is checked like any other rule, on every row.

## What is refused at plan time

Every refusal comes before a row exists.

- `FLX-3105`: a `history` or `asOf` key is declared under kernel 0.1.0, or a key of an entry is not one of those above. The message names the kernel. Plan with `--kernel 0.2.0`, the default.

  ```console
  $ fluxsim plan . --kernel 0.1.0 # exit 1
  FLX-3105: bundle does not validate, so its seam cannot be resolved -- sim-subscriptions: emit 'subscription_version' configures a history, which needs kernel 0.2.0; this run is kernel 0.1.0
  more: fluxsim explain FLX-3105  ...
  ```

- `FLX-3403`: the entry cannot be laid out as declared. These cases are refused:
  - the emit has no validity window, or a start and an end of different types, or a second `history` entry;
  - `parent` is not a to-one relationship of the emit, a `counts` entry also names it, or a `links` entry gives it a `when` or a `weight`;
  - a `versions` class is below 1, or there are more entities than versions, which names `--scale` when a scale caused it;
  - `until` is outside the window, a `start` bin lies outside it, or a `length` bin edge is below one day;
  - the first starts have no room for the most versions an entity takes;
  - with `parent.lifetime: true`, a parent whose lifetime leaves room for not even one version of an entity, and a parent emit that declares a history without carrying the lifetime columns;
  - `dayBefore` on TIMESTAMP columns;
  - a primary key other than one column, or the natural entity's columns and the start;
  - an `activation`, tracked or end column that is a key, a validity column or a column a rule sets, a tracked column that is not categorical, and an `end.value` that appears anywhere but the last version;
  - a DATE or TIMESTAMP column of the emit that declares no range, since it would be drawn inside the window and move with it, and a range or bin outside the window, which is only a warning on another table;
  - an `asOf` entry whose relationship does not point at a history, and a required column that a rule sets through an `asOf` link (above).
- `FLX-6203`: a `lifetimes` or `within` entry on the start, the end or the activation column, which would be a second source for one value.
- `FLX-6106`: a path into a history's emit reads a tracked, validity or rule column without an `asOf` entry.
- `FLX-6210`: a rule that reads `previous` with a window over siblings, or `previous` on a versioned emit that has no history.

Every one of them reads declared values and the layout's counts, never a drawn row.

## From a profile

`fluxsim init` writes a `history` entry for each table with a validity window whose profile publishes a `history` member (profile format 1-3-0, `tables.<id>.history`), and an `asOf` entry for each relationship into it from a table that declares a default time dimension. A bundle that declares these keys plans only under kernel 0.2.0. See [Write a bundle from a profile](./write-a-bundle-from-a-profile.md).

The contract can say more than the profile. The natural entity's `lifecycle` (FLUID 0.7.7) is read into the entry: `activation` names a column of the start's type that holds the first start on every version, which becomes the entry's `activation`; `endMarker`, a column and a value its ending version holds, becomes `end` with `mode: finalVersion` at the closed share of last versions, and the column's moves in the profile are written without that value, which only the ending version holds. `currentFlag` is not written, since nothing in the engine puts a value on an entity's last version alone; the column keeps its own draw. A member that names a column the table lacks, a column of another type, or a value the column does not declare is left out, and the assumptions say why. Without an `endMarker` the end stays `closeLast`.
