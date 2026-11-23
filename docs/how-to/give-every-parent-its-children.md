---
title: "Give every parent its children"
description: "Declare how many children each parent has with a counts entry, so no invoice is without a line, the histogram of children per parent is met class for class, and a parent's summary holds."
---

# Give every parent its children

Use this page when the tables you generate are related and a parent should hold a stated number of children. A foreign key that picks its parent one child at a time leaves some parents with no child at all: with 1,000 invoices and 3,000 lines, about 50 invoices receive none, and a total written on the invoice cannot hold, because no row knows every line that names it. A `counts` entry lays the children out parent by parent instead. It needs kernel 0.2.0, which a new plan takes, and the tables must be generated from statistics, as [Use the FLUID seam](./use-the-fluid-seam.md) describes.

## Declare the layout

The bundle below has two tables, `invoice` and `invoice_line`. Its contract is the FLUID document `shop.fluid.yml`, and a line names its invoice through the relationship `invoice`.

```yaml
# persona.flux.yml
fluxVersion: "0.5.0"
kind: Persona
id: persona-shop
name: Persona for Shop
metadata:
  owner:
    team: shop
spec:
  traits: { digitalFluency: 0.5 }
```

```yaml
# world.flux.yml
fluxVersion: "0.5.0"
kind: World
id: world-shop
name: World for Shop
metadata:
  owner:
    team: shop
spec:
  seed: 42
  vertical: telco
  population: { size: 1, lifecycleMix: { active: 1 } }
```

The invoice carries two summaries of its lines, a `total` and a `line_count`. They are contract `derive` invariants with an `aggregate`, which the engine holds on every invoice once the relationship is counted (see [Summaries of the children](#summaries-of-the-children)).

```yaml
# shop.fluid.yml
fluidVersion: "0.7.7"
kind: DataProduct
id: retail.shop
name: Shop
metadata:
  owner:
    team: shop
exposes:
  - exposeId: invoice
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: retail, table: invoice, region: eu-west-1 }
    contract:
      schema:
        - { name: invoice_id, type: INT, required: true }
        - { name: status, type: STRING, required: true }
        - { name: total, type: "DECIMAL(14,2)", required: true }
        - { name: line_count, type: INT, required: true }
      invariants:
        - id: invoice_total
          derive: total
          aggregate: { function: sum, column: amount, from: invoice_line, via: invoice, ifNone: 0 }
        - id: invoice_line_count
          derive: line_count
          aggregate: { function: count, from: invoice_line, via: invoice }
    semantics:
      entities:
        - { name: invoice, type: primary, expr: invoice_id }
  - exposeId: invoice_line
    kind: table
    binding:
      platform: aws
      format: glue_table
      location: { database: retail, table: invoice_line, region: eu-west-1 }
    contract:
      schema:
        - { name: line_id, type: INT, required: true }
        - { name: invoice_id, type: INT, required: true }
        - { name: amount, type: "DECIMAL(12,2)", required: true }
    semantics:
      entities:
        - { name: invoice_line, type: primary, expr: line_id }
      relationships:
        - name: invoice
          to: invoice
          fromColumns: [invoice_id]
          toColumns: [invoice_id]
          cardinality: many_to_one
          joinType: inner
```

The Simulation adds a `counts` list under the `com.agentics.flux/1` extensions, next to `emits`. This entry says that every invoice that is not void has at least one line, that a void invoice has none, and how many lines the others have: half of them one or two, three tenths three to five, and a fifth six to eleven.

```yaml
# simulation.flux.yml
fluxVersion: "0.5.0"
kind: Simulation
id: sim-shop
name: Shop
metadata:
  owner:
    team: shop
spec:
  worldRef: world-shop
  fidelity: deterministic
  seed: 42
  timeBounds:
    start: "2026-07-01T00:00:00Z"
    end: "2026-09-30T23:59:59Z"
  personaRefs: [persona-shop]
  emits:
    - productRef: retail.shop
      exposeId: invoice
      fluidVersion: "0.7.7"
      provenance: { contractDigest: "sha256:357370cb9ee2e7b62f6d8b91c1b7aca2ee1956bbe2661680d662bdf2df6c7cd1" }
    - productRef: retail.shop
      exposeId: invoice_line
      fluidVersion: "0.7.7"
      provenance: { contractDigest: "sha256:357370cb9ee2e7b62f6d8b91c1b7aca2ee1956bbe2661680d662bdf2df6c7cd1" }
extensions:
  com.agentics.flux/1:
    counts:
      - emit: invoice_line
        relationship: invoice
        minChildren: 1
        parentWhen: "status <> 'void'"
        children: { bins: ["1", "3", "6", "11"], binWeights: ["0.5", "0.3", "0.2"] }
    emits:
      invoice:
        rows: 200
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            invoice_id: { range: { min: '1', max: '200' } }
            status: { levels: { paid: '0.7', open: '0.25', void: '0.05' } }
          properties:
            - property: mean
              column: invoice_id
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
      invoice_line:
        rows: 500
        fidelity:
          encoding: { fractionalBits: 16, rounding: floor }
          records: { min: 1, max: 1000000 }
          columns:
            line_id: { range: { min: '1', max: '500' } }
            amount: { range: { min: '1', max: '900' } }
          properties:
            - property: mean
              column: amount
              tolerance: { type: absolute, value: "1000" }
          budget: { maxChecksPerState: 20, maxStateAge: P30D }
          failureAction: withholdBundle
```

Plan and apply it, as for any bundle:

```console
$ fluxsim validate .
.: valid -- 3 FLUX document(s), 1 FLUID contract(s), 1 Simulation(s)
$ fluxsim run .
plan written to ...
applied to ...
emitted invoice: 200 rows (generator); invoice_line: 500 rows (generator)
run ... complete
```

## What the layout guarantees

Read the rows back and count the lines of each invoice:

```python
import glob
import json
from collections import Counter

emits = glob.glob(".flux/runs/*/emits/")[0]
invoices = [json.loads(line) for line in open(emits + "invoice.ndjson")]
lines = [json.loads(line) for line in open(emits + "invoice_line.ndjson")]
status = {row[0]: row[1] for row in invoices}
per_invoice = Counter(row[1] for row in lines)

assert sum(per_invoice.values()) == 500
assert all(status[invoice] != "void" for invoice in per_invoice)
assert all(invoice in per_invoice for invoice, kind in status.items() if kind != "void")
assert all(count >= 1 for count in per_invoice.values())
assert all(count <= 11 for count in per_invoice.values())
```

Five things hold, whatever the seed:

- **A whole number of children per parent, in a run of consecutive child rows.** The children of the first invoice that has any are the first lines, those of the next invoice follow them, and so on in the parent's row order.
- **The histogram, class for class.** The parents of each class of `children` are the largest remainder of the eligible parents over the class weights, so the run's histogram of children per parent is the declared one. It is exact when the child rows total what the classes give; when they do not, the largest remainder spreads the difference over the parents.
- **The total is exact.** The counts add up to the child table's rows, 500 here, whatever the shape says. The spare children, those above the minimum, are shared out in proportion to what each parent's class asks for.
- **No eligible parent is below its minimum.** Every invoice that is not void has at least one line.
- **No child under a parent outside the condition.** A void invoice has no line.

## The `counts` key

`counts` is a list in the Simulation's `com.agentics.flux/1` extensions, with one entry per relationship the layout covers. A relationship with no entry picks its parent as before.

| Key | Meaning |
|---|---|
| `emit` | The child expose, the table that holds the foreign key. Required. |
| `relationship` | The name of a relationship that expose declares in its contract, `semantics.relationships[].name`. Required, and at most one entry per relationship. |
| `minChildren` | A whole number, default `0`. Every parent the condition admits has at least that many children. |
| `parentWhen` | A FLUID SQL/1 predicate over the parent expose's own columns. Only a parent whose final row makes it true has children, and every other parent has none. With no condition every parent is eligible. |
| `children` | The shape of the number of children per parent, in the spelling of a fidelity column: `levels` with `levelWeights`, or `bins` with `binWeights`, over whole counts. Each level is one count. Each bin is the whole counts from its lower edge up to the next edge, and the last bin holds its upper edge. A bin spreads its parents evenly over its counts. With no `children` the children are spread as evenly as whole counts allow. |
| `tolerance` | A decimal, default `0.10`: the largest total variation distance a check of children per parent allows against a profile. |

No other key is read, and an unknown key is refused with [FLX-3105](../reference/errors/FLX-3105.md). A module may also supply an expected number of children for each parent, which the layout reads as a weight; that is not a key of the bundle.

The entry changes the rows the child table generates, so a change to it moves the plan's digests. A bundle with no `counts` entry keeps every byte and digest it had.

## Summaries of the children

A contract `derive` with `aggregate` over a counted relationship holds on every parent. The parent's value is the summary of the child rows the layout gives it:

- `count` is the number of children for which `filter` is true.
- `sum`, `min` and `max` run over the children whose `column` is not null, and are null when there are none.
- `ifNone` is the value in place of a null summary, so a parent with no child can still hold `0`.

In the bundle above, every invoice's `total` is the sum of the `amount` of its lines, and its `line_count` is the number of its lines.

```python
amounts = Counter()
for row in lines:
    amounts[row[1]] += float(row[2])

assert all(row[3] == per_invoice.get(row[0], 0) for row in invoices)
assert all(abs(float(row[2]) - amounts.get(row[0], 0)) < 0.005 for row in invoices)
```

An aggregate over a relationship that is not counted is refused with [FLX-6210](../reference/errors/FLX-6210.md), and its message names the `counts` key. So is an aggregate written inside a FLUX expression; write it as a contract `derive`. A summary that reads another summary back through a path is a cycle, [FLX-6208](../reference/errors/FLX-6208.md).

## What `fluxsim verify` checks

`fluxsim verify` checks the layout on the run's rows. Each `counts` entry adds two lines to the rules of the child, `counts:<relationship>.minChildren` and `counts:<relationship>.parentWhen`: the share of eligible parents with at least `minChildren` children, and the share of parents outside the condition that have no child. Both are enforced, so one violating parent fails the run. A contract `derive` with `aggregate` over a counted relationship is checked on every parent row, joining each child to its parent through the relationship, and is enforced too.

```console
$ fluxsim verify . --run-id $(ls .flux/runs)
...
  rules invoice: invoice_total 200 of 200 (100.00%); invoice_line_count 200 of 200 (100.00%)
  rules invoice_line: counts:invoice.minChildren 191 of 191 (100.00%); counts:invoice.parentWhen 9 of 9 (100.00%)
...
```

Here 191 invoices are eligible and 9 are void. An aggregate that does not generate, because its relationship is not counted, is refused at plan time as above.

When the pinned profile publishes a `marginalHistogram/1` of children per parent for the relationship, the scorecard gains one more row under `crossTable`, `childrenPerParent`, with the `propertyId` `children:<child exposeId>.<relationship>` (scorecard format `1-4-0`, [SCORECARD-SPEC](../spec/SCORECARD-SPEC.md) section 3). It counts the run's parents into the profile's bins by their children and compares the result as a histogram is compared: the score is one minus the total variation distance, and the row is `within` when the distance is at most the entry's `tolerance`, `0.10` for a relationship with no entry. A scorecard with no such row stays in the format it was.

## Exact shares for a column

A fidelity column declared from `levels`, from `bins` with `binWeights`, or given a column that is itself exact, may declare `allocation: exact`. Each of its classes, and NULL when the column declares a `nullFraction`, then holds exactly its share of the table's rows, up to the largest remainder, instead of a share each row draws for itself. A bin's value inside the bin is drawn as always. A small table, such as a 26-row catalogue, then meets its own shares instead of missing them by sampling.

```text
columns:
  category: { levels: { tools: '0.4', toys: '0.3', books: '0.2', games: '0.1' }, allocation: exact }
```

Kernel 0.1.0 refuses the key with [FLX-3105](../reference/errors/FLX-3105.md). A column that is given a linked row's column, or given a column that is not exact, is refused with [FLX-3403](../reference/errors/FLX-3403.md). Under `--scale` the shares apply to the scaled rows.

## A tolerance that follows the row count

A distance tolerance may declare `sampling: true`. The run is a sample of its rows, so its shares differ from the profile's by sampling alone even when the generator is exact in expectation, and the margin is widened, group by group, by a term that grows as the group's rows shrink (SCORECARD-SPEC section 9a). For a column under `allocation: exact` only the rounding term remains. A tolerance without the key is compared as before.

```text
tolerance: { type: distance, measure: totalVariation, max: "0.1", sampling: true }
```

## What is refused at plan time

Every refusal comes before a row exists.

- `FLX-3105`: the key is declared under kernel 0.1.0, which draws each foreign key row by row. The message names the kernel. Plan with `--kernel 0.2.0`, the default.
- `FLX-3403`: the entry cannot be laid out as declared. These cases are refused:
  - the expose does not declare the relationship;
  - the foreign key column declares a `nullFraction` or a draw of its own (`levels`, `bins`, a mean or `given`), since the layout sets it;
  - the relationship is `distinct`, is part of a key, is implied by another, is a side of a bridge, or points at its own table;
  - a `links` entry gives the relationship a `when` or a `weight`;
  - the target is neither generated from statistics nor a reference expose;
  - `children` has a count below zero, a fraction, or a class whose least count is above its greatest;
  - `minChildren` is above every count `children` allows;
  - on a `one_to_one` relationship, `children` allows a count above one or `minChildren` is above one;
  - `parentWhen` reads a column of another row, through a link, `previous`, children or siblings.
- `FLX-3403`, too few child rows: the child rows cannot give every eligible parent its minimum. With no `parentWhen` this is refused at plan time. With one, the number of eligible parents is known only when the parents are generated, and the refusal comes then. The message gives the child rows, the minimum and the eligible parents.

```console
$ cp simulation.flux.yml ../simulation.keep
$ sed -i.bak 's/minChildren: 1/minChildren: 12/' simulation.flux.yml
$ rm simulation.flux.yml.bak
$ fluxsim plan .   # exit 1
FLX-3403: the related tables cannot be generated as declared -- expose 'invoice_line' relationship 'invoice' has minChildren 12, above the most children its shape allows (11); lower minChildren or raise the shape's top count
more: fluxsim explain FLX-3403  ...
$ cp ../simulation.keep simulation.flux.yml
```

Under `--scale` the child rows and the parent rows are scaled alike but each is rounded on its own. When a run that is planned at scale 1 leaves too few child rows at another scale, the refusal says so and names `--scale`: raise the scale, lower `minChildren` or declare more child rows. See [Size a run, split it and time it](./size-and-split-a-run.md).

## Row order

A child table on the counted layout comes out grouped by parent in row order, as a table loaded parent by parent does. A consumer that needs another order sorts it.

The child's own key is not drawn; it is laid out from the row index (KERNEL-SPEC section 8.2). What that does to a parent's children depends on its type:

- A number or date key is laid out in ascending order, so a parent's children have consecutive keys. In the run above, the lines of each invoice are the lines `line_id` `k` to `k + n - 1`.
- A text or UUID key is permuted, so a parent's children are consecutive rows but their keys are not consecutive in any order.

```python
keys = {}
for row in lines:
    keys.setdefault(row[1], []).append(row[0])

assert all(max(held) - min(held) == len(held) - 1 for held in keys.values())
```

Both are the same rows in the same order; only the key values differ. Code that needs a parent's children can read the rows by their foreign key and does not depend on the key's order.
