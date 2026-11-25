---
title: "Project a history"
description: "Derive a snapshot, a change event stream and periodic rows from a history, with row counts worked out at plan time, and grow the window without rewriting a row."
---

# Project a history

Use this page when a history (see [Give an entity a history](./give-an-entity-a-history.md)) has tables beside it that a warehouse would derive from it: the current row of each entity, a stream of changes as a change data capture tool writes them, and one row per entity and month while a version is valid. Drawn on their own statistics, a current row would not equal its entity's last version, an event would match no change, and a monthly row could fall in a month in which its version was not valid. A projection reads the history instead, so each of them agrees with it by construction. It needs kernel 0.2.0, which a new plan takes.

There are three projections, declared as three keys beside `history`: `snapshots`, `events` and `periods`. The bundle below declares all three for one history.

## Declare the projections

The contract has four tables: `subscription_version`, the history, and three tables that read it. Its persona and world are the same as on the other pages.

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
# ledger.fluid.yml
fluidVersion: "0.7.7"
kind: DataProduct
id: billing.subscriptions
name: Subscriptions
metadata:
  owner:
    team: subscriptions
exposes:
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
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE, required: false }
        - { name: plan, type: STRING, required: true }
        - { name: status, type: STRING, required: true }
    semantics:
      entities:
        - { name: subscription_version, type: primary, expr: subscription_sk }
        - { name: subscription, type: natural, expr: subscription_id }
      dimensions:
        - { name: valid_from, type: time, typeParams: { validityParams: { isStart: true } } }
        - { name: valid_to, type: time, typeParams: { validityParams: { isEnd: true } } }
        - { name: plan, type: categorical }
        - { name: status, type: categorical }
  - exposeId: subscription_current
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: billing, table: subscription_current, region: eu-west-1 }
    contract:
      schema:
        - { name: subscription_id, type: INT, required: true }
        - { name: plan, type: STRING, required: true }
        - { name: status, type: STRING, required: true }
    semantics:
      entities:
        - { name: subscription, type: primary, expr: subscription_id }
  - exposeId: subscription_change
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: billing, table: subscription_change, region: eu-west-1 }
    contract:
      schema:
        - { name: event_id, type: INT, required: true }
        - { name: subscription_id, type: INT, required: true }
        - { name: op, type: STRING, required: true }
        - { name: changed_at, type: DATE, required: true }
        - { name: recorded_at, type: DATE, required: true }
        - { name: before_plan, type: STRING, required: false }
        - { name: after_plan, type: STRING, required: false }
    semantics:
      entities:
        - { name: event, type: primary, expr: event_id }
  - exposeId: subscription_month
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: billing, table: subscription_month, region: eu-west-1 }
    contract:
      schema:
        - { name: period_id, type: INT, required: true }
        - { name: subscription_id, type: INT, required: true }
        - { name: month, type: DATE, required: true }
        - { name: plan, type: STRING, required: true }
        - { name: kind, type: STRING, required: true }
    semantics:
      entities:
        - { name: period, type: primary, expr: period_id }
      dimensions:
        - { name: month, type: time, typeParams: { timeGranularity: month } }
```

The Simulation names the history, then one entry for each projected table. A projected emit declares no `rows`: the next section says where its count comes from.

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
    end: "2026-01-01T00:00:00Z"
  personaRefs: [persona-subscriptions]
  emits:
    - { productRef: billing.subscriptions, exposeId: subscription_version, fluidVersion: "0.7.7", provenance: { contractDigest: "sha256:9a6510a0dfdfe664dc1bc801b0923d327dc1c9d176e057a543205400e7e0d74d" } }
    - { productRef: billing.subscriptions, exposeId: subscription_current, fluidVersion: "0.7.7", provenance: { contractDigest: "sha256:9a6510a0dfdfe664dc1bc801b0923d327dc1c9d176e057a543205400e7e0d74d" } }
    - { productRef: billing.subscriptions, exposeId: subscription_change, fluidVersion: "0.7.7", provenance: { contractDigest: "sha256:9a6510a0dfdfe664dc1bc801b0923d327dc1c9d176e057a543205400e7e0d74d" } }
    - { productRef: billing.subscriptions, exposeId: subscription_month, fluidVersion: "0.7.7", provenance: { contractDigest: "sha256:9a6510a0dfdfe664dc1bc801b0923d327dc1c9d176e057a543205400e7e0d74d" } }
extensions:
  com.agentics.flux/1:
    history:
      - emit: subscription_version
        entities: 60
        versions: { bins: ["1", "2", "4", "9"], binWeights: ["0.45", "0.35", "0.20"] }
        start: { bins: ["2023-01-01", "2024-01-01", "2025-07-01"], binWeights: ["0.55", "0.45"] }
        length: { bins: ["30", "90", "365", "1096"], binWeights: ["0.30", "0.45", "0.25"], closed: lower }
        until: "2025-12-31"
        tracked:
          - column: plan
            transitions:
              basic: { pro: "0.6", team: "0.4" }
              pro: { basic: "0.5", team: "0.5" }
              team: { basic: "0.5", pro: "0.5" }
        end: { mode: finalVersion, share: "0.2", column: status, value: cancelled }
    snapshots:
      - { emit: subscription_current, of: subscription_version }
    events:
      - emit: subscription_change
        of: subscription_version
        at: changed_at
        date: recorded_at
        lag: { bins: ["0", "1", "8"], binWeights: ["0.7", "0.3"] }
        op: op
        before: { before_plan: plan }
        after: { after_plan: plan }
    periods:
      - emit: subscription_month
        of: subscription_version
        column: month
        grain: month
        match: overlap
        kinds:
          - { count: "1", overrides: { kind: base } }
          - { count: { levels: ["0", "1", "2"], levelWeights: ["0.5", "0.3", "0.2"] }, when: "plan = 'team'", overrides: { kind: usage } }
    emits:
      subscription_version:
        rows: 200
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
          properties:
            - property: mean
              column: subscription_sk
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
      subscription_current:
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            subscription_id: { range: { min: '1', max: '60' } }
            plan: { levels: { basic: '0.5', pro: '0.3', team: '0.2' } }
            status: { levels: { active: '1', cancelled: '0' } }
          properties:
            - property: mean
              column: subscription_id
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
      subscription_change:
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            event_id: { range: { min: '1', max: '1000' } }
            subscription_id: { range: { min: '1', max: '60' } }
            op: { levels: { c: '1', u: '0', d: '0' } }
            changed_at: { range: { min: '2023-01-01', max: '2025-12-31' } }
            recorded_at: { range: { min: '2023-01-01', max: '2025-12-31' } }
            before_plan: { levels: { basic: '0.5', pro: '0.3', team: '0.2' } }
            after_plan: { levels: { basic: '0.5', pro: '0.3', team: '0.2' } }
          properties:
            - property: mean
              column: event_id
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
      subscription_month:
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            period_id: { range: { min: '1', max: '5000' } }
            subscription_id: { range: { min: '1', max: '60' } }
            month: { range: { min: '2023-01-01', max: '2025-12-31' } }
            plan: { levels: { basic: '0.5', pro: '0.3', team: '0.2' } }
            kind: { levels: { base: '1' } }
          properties:
            - property: mean
              column: period_id
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
```

```console
$ fluxsim validate .
.: valid -- 3 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
$ fluxsim run .
plan written to ...
applied to ...
emitted subscription_version: 200 rows (generator); subscription_current: 60 rows (generator); subscription_change: 200 rows (generator); subscription_month: 1891 rows (generator)
run ... complete
$ fluxsim verify . --run-id $(ls .flux/runs)
...
  rules subscription_change: event:matches_transition 60 of 60 (100.00%); event:transition_matches 128 of 128 (100.00%)
  rules subscription_current: snapshot:equals_last_version 60 of 60 (100.00%)
  rules subscription_month: period:within_validity 1891 of 1891 (100.00%); period:matches_valid_version 1891 of 1891 (100.00%)
  rules subscription_version: history:no_gaps_no_overlap 60 of 60 (100.00%); history:one_current 60 of 60 (100.00%); history:ended_is_last 60 of 60 (100.00%)
...
```

## Rows come from the history

A projected emit declares no `rows`, and a declared one is refused with [FLX-3403](../reference/errors/FLX-3403.md). `fluxsim plan` works its number of rows out from the history's layouts, chains, ended entities and change reasons, and for periods the versions and the periods, none of which needs a cell, and records it in `plan.json` as the emit's `rows`, with `rowsFrom` saying where it comes from.

```python
import glob
import json

plan = json.load(open(glob.glob(".flux/runs/*/plan.json")[0]))["emits"]
assert plan["subscription_current"]["rows"] == 60
assert plan["subscription_current"]["rowsFrom"] == {"of": "subscription_version", "projection": "snapshots"}
assert plan["subscription_change"]["rowsFrom"]["projection"] == "events"
assert plan["subscription_month"]["rowsFrom"]["projection"] == "periods"
```

The recorded count enters the plan digest, so the plan names exactly the rows the run writes. `--scale F` scales the history's `entities` and `rows`, and a projection follows from the scaled history; it is not scaled again. `--shard I/N` takes contiguous shares of the derived rows, as it takes shares of declared ones.

## Snapshots

A `snapshots` entry is `{emit, of, ended}`: one row per entity of the history `of` names, holding its last version.

| Key | Meaning |
|---|---|
| `emit` | The projected expose. Every column of its contract is a column of the history's emit, of the same name and type. Required. |
| `of` | The history's emit. Required. |
| `ended` | `keep`, the default, keeps every entity, an ended one with its last version. `drop` leaves out the entities the history ended, as a table of live entities does, so the rows are the entities less the ended ones. |

Each column holds the entity's last version's final value, its rules included. A snapshot draws nothing, and its rows follow the history's order, so they come grouped by parent.

## Change events

An `events` entry writes one row for each change, as a change data capture stream does, with the state before and after: a `c` for an entity's first version, a `u` for each later version, and a `d` when the history ended it. Under `finalVersion` the change into the final version is the `d`; under `closeLast` the close of the last version is.

| Key | Meaning |
|---|---|
| `emit`, `of` | The projected expose and the history's emit. Required. |
| `date` | A DATE or TIMESTAMP column that holds when the event was recorded. Required. |
| `at` | A column of the same type that holds the change's instant: the version's start, or for a `closeLast` `d` the last version's end. |
| `lag` | A histogram whose edges are whole days, in the spelling of a fidelity `distribution`: the time from `at` to `date`. It is drawn inside the interval from zero to `until` less the change's instant, so no event is dated after the history's `until`. Without it an event is recorded at its change. |
| `on` | A list of `c`, `u` and `d`: the events that are kept. All three by default. |
| `op` | A text column that holds `c`, `u` or `d`. |
| `type` | `{column, values}`: the text written in `column` for `c`, `d`, `end` and each tracked column. A `u` takes the text of its change reason. Every kept op and reason needs a text. |
| `before`, `after` | Map an event column to a column of the history's emit. `before` holds the previous version's value, NULL on a `c`; `after` holds this version's, NULL on a `d`. Under `closeLast` the `d` takes the last version's own value in `before`. |
| `when` | A FLUID SQL/1 predicate over the version the event is about: its tracked columns, validity and activation, and through `previous.` those of the version before. An event exists only where it is true. It may read no column a cell draws. |

An event column that names a column of the history's emit and has no mapping holds the value of the version the event is about, such as the entity's key. Every other column is drawn from the event emit's own statistics. A `before` column that the contract requires is refused while `on` keeps `c`, and an `after` column it requires while `on` keeps `d`, since those events write NULL there.

Read them back:

```python
emits = glob.glob(".flux/runs/*/emits/")[0]
versions = [json.loads(line) for line in open(emits + "subscription_version.ndjson")]
events = [json.loads(line) for line in open(emits + "subscription_change.ndjson")]

by_entity = {}
for row in versions:
    by_entity.setdefault(row[1], []).append(row)
ids = [row[1] for row in events]
assert ids == sorted(ids)  # events come by entity

for entity, chain in by_entity.items():
    mine = [row for row in events if row[1] == entity]
    ops = [row[2] for row in mine]
    assert len(mine) == len(chain)  # one event for each change
    assert ops[0] == "c" and ops.count("c") == 1
    assert "d" not in ops[:-1]
    for row, version in zip(mine, chain):
        assert row[3] == version[2]  # at is the version's start
        assert row[3] <= row[4] <= "2025-12-31"  # recorded after the change, never after until
    assert all(row[5] is None for row in mine[:1])  # no before on a create
    assert all(row[6] is None for row in mine if row[2] == "d")  # no after on a delete
```

## Periodic rows

A `periods` entry writes one row for each entity and period while a version is valid, such as a subscription's monthly rows.

| Key | Meaning |
|---|---|
| `emit`, `of` | The projected expose and the history's emit. Required. |
| `column` | A DATE column of the emit that holds the period's first day, which lies inside the window. Required. |
| `grain` | `day`, `week`, `month`, `quarter` or `year`. It defaults to the column's FLUID `timeGranularity`, and a column that declares none needs it. |
| `match` | Which versions a period holds. `overlap`, the default, holds every version valid at some instant of the period, so a period with a change holds both versions. `end` holds the version valid at the period's last instant inside the window, one for each entity at most, a periodic snapshot at the period's close. `start` holds the one valid at its first instant. |
| `kinds` | A list of `{count, when, overrides}`. Without it there is one kind, one row for each held pair. |

| `from` | A DATE text `YYYY-MM-DD` (quote it in YAML). The first period starts on this day, the anchor every period is counted from. It defaults to the day of `timeBounds.start`, and is refused (`FLX-3403`) before that day or when it is not before `timeBounds.end`. |
| `through` | A DATE text `YYYY-MM-DD`. The last period is the last one that starts on or before this day, the day included. It defaults to every period that starts before `timeBounds.end`, and is refused before `from` or when it is not before `timeBounds.end`. A longer window appends no period past it. |

Period `p` runs from the window's first day (or `from`) plus `p` grains to the day `p + 1` grains on, for as long as it starts before the window's end, which is excluded, and on or before `through`, as dbt-utils' `date_spine` lays its periods. Each period's first day is counted from the window's start, and a month added to a day the month lacks lands on the month's last day. An open version is valid up to the window's end, and an entity holds no period before its first start or after a closed end. A version and a period it holds are a **pair**.

A kind gives a count of rows for each pair: `count` is a whole number, or `levels` with `levelWeights` over whole counts. In each period the pairs the kind holds take their counts as the layout of children per parent does, so every period meets the kind's shares exactly, up to the largest remainder, and no period's allotment reads another period. `when` is a FLUID SQL/1 predicate over the pair's version and the period column: a pair the kind does not hold has none of its rows. `overrides` maps a column to a text written as the column's type on the kind's rows, such as the kind's own label. In the bundle above every pair has one `base` row, and a pair whose plan is `team` has none, one or two `usage` rows.

Each column of a periodic row that names a column of the history's emit holds the pair's version's final value, so the row carries the version valid in its period. Every other column is drawn from the periodic emit's own statistics.

A periodic emit's key is laid out from the low end of its declared range, one step for each row: row `i` takes `lo + i`, so a longer window appends keys and keeps every earlier one. A primary key that is not a whole number, and a column under `allocation: exact`, are refused on a periodic emit.

```python
months = [json.loads(line) for line in open(emits + "subscription_month.ndjson")]
assert len(months) == plan["subscription_month"]["rows"]
assert [row[0] for row in months] == list(range(1, len(months) + 1))
starts = [row[2] for row in months]
assert starts == sorted(starts)  # period by period
assert starts[0] == "2023-01-01" and starts[-1] <= "2025-12-01"  # the window's end is excluded
assert {row[4] for row in months} == {"base", "usage"}
```

## Row order

The rows of each projected emit follow a fixed order, so a consumer can rely on it and a run read in slices cuts at known places.

- The history's emit: grouped by parent, then entity, then version.
- A snapshot: by entity, grouped by parent as the history is.
- Change events: by entity, then in version order within an entity. They are not in date order; a consumer that needs one sorts them.
- Periodic rows: period by period, then entity, then version, then kind, then the kind's rows. A consumer that needs them grouped by entity sorts them.

## A longer window appends

A history stops changing at its `until`, and nothing in a projection reads the window's end except the periods. Extending `spec.timeBounds.end` past `until` therefore rewrites nothing: the history, the snapshot and the events are the same bytes, and the periodic rows begin with the shorter window's rows, byte for byte, followed by the periods the longer window adds. Run the bundle again with a window a year longer:

```python
import os

short = sorted(glob.glob(".flux/runs/*/"), key=os.path.getmtime)[-1]
text = open("simulation.flux.yml").read().replace("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
open("simulation.flux.yml", "w").write(text)
```

```console
$ fluxsim run .
plan written to ...
applied to ...
emitted subscription_version: 200 rows (generator); subscription_current: 60 rows (generator); subscription_change: 200 rows (generator); subscription_month: 2743 rows (generator)
run ... complete
```

The first three tables are the same bytes, and the periodic table gains the new periods after its old rows:

```python
long = sorted(glob.glob(".flux/runs/*/"), key=os.path.getmtime)[-1]
assert short != long


def rows(run, name):
    return open(run + "emits/" + name + ".ndjson", "rb").read()


for name in ("subscription_version", "subscription_current", "subscription_change"):
    assert rows(short, name) == rows(long, name)
assert rows(long, "subscription_month").startswith(rows(short, "subscription_month"))
assert len(rows(long, "subscription_month")) > len(rows(short, "subscription_month"))
```

Growing the history itself, with changes after `until`, means moving `until`, which lays the chains out again.

## What `fluxsim verify` checks

Each line is enforced, so one violating row fails the run. A check that needs the history's rows, when the history's emit is hidden, is listed as not scored, with that reason.

| Line | Holds when |
|---|---|
| `snapshot:equals_last_version` | each snapshot row equals its entity's last version, column for column; each entity appears once, and under `ended: drop` exactly the ended ones are absent. |
| `event:matches_transition` | for each entity, the events equal its changes as a set: one `c` for the first version, one `u` for each later one, one `d` when it ended, each with its `before` and `after`, and `date` not before `at`. |
| `event:transition_matches` | each `u` that is not an ending change moves exactly one tracked column, along a transition the history gives a weight above zero. |
| `period:within_validity` | each periodic row's period holds a version of its entity under the entry's `match`. |
| `period:matches_valid_version` | each periodic row's columns that name the history's columns equal a version of its entity that the period holds. |

## What is refused at plan time

Every refusal comes before a row exists, and reads declared values and the plan only.

- `FLX-3105`: a `snapshots`, `events` or `periods` key is declared under kernel 0.1.0, or a key of an entry is not one of those above. The message names the kernel.
- `FLX-3403`: the entry cannot be laid out as declared. These cases are refused:
  - a projected emit declares `rows`:

    ```text
    FLX-3403: ... snapshots (subscription_current): the emit 'subscription_current' is projected and has declared rows: its rows follow from the history
    ```

  - an emit that two entries name, or a projected emit that has a `history`, a `reference` block, or a `counts` entry over one of its relationships;
  - `of` does not name an emit that a `history` entry declares;
  - a snapshot column that is not a column of the history's emit with the same name and type;
  - a `before` column the event contract requires while `on` keeps `c`, an `after` column it requires while `on` keeps `d`, and an op, a change reason or an `end` with no text in `type`;
  - a `when` that reads a column a cell draws;
  - a period `grain` that is neither given nor declared by the column, and a primary key of a periodic emit that is not a whole number;
  - a relationship whose parent is a periodic emit, since that parent's rows grow with the window and every pick over them would move. A snapshot or an events emit may be a parent.

## From a contract

`fluxsim init` writes projections from the contract alone, with no lag and one row for each pair. For each versioned table that it gives a history, it writes a `snapshots` entry for a table with no validity window whose columns the history's table declares; a `periods` entry for a table with a to-one relationship to the natural entity and exactly one DATE dimension that declares a `timeGranularity`, with `match: overlap` and one kind; and an `events` entry for a table with such a relationship, a column whose declared levels lie within `c`, `u` and `d`, and a `defaultAggTimeDimension`. A table that meets part of a rule is recorded as unused, with the part it misses.

The periods and the events start where the data does. A `periods` entry gets `from` and `through` from the contract's declared `range` on the period column, or else from the first and last bin of its published histogram, when they lie inside the window; with neither, the periods run from the window's start. An `events` entry for a history that ends no entity gets a `when` on the version's validity start that keeps the versions whose event falls inside the date column's declared range or published minimum and maximum. A history that ends entities is left unlimited, since a `d` event is dated by a version's end, and the notes say so.
