# Children per parent: counts, minimum, condition, sums, scale

Read this when a parent should hold a stated number of children, such as an invoice with its lines, or a summary written on the parent must hold. The record is [ADR 0016](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/adr/0016-children-per-parent-are-counted.md) and the tested page is [give every parent its children](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/give-every-parent-its-children.md). The installed engine's help and `fluxsim explain` are the authority for the installed version.

## Why

A foreign key picks its parent one child at a time, so with 1,000 invoices and 3,000 lines about 50 invoices get no line, and a total written on an invoice cannot hold, because no row knows every line that names it. A `counts` entry decides how many children each parent has first, then lays the child rows out parent by parent: a parent's children are consecutive child rows, in the parent's row order.

## Kernel and tables

It needs kernel 0.2.0, which a new plan takes, and the tables must be generated from statistics (or, for the target, be a reference expose). Under kernel 0.1.0 the key is refused with `FLX-3105`, naming the kernel; plan with `--kernel 0.2.0`, the default, and never edit a recorded plan. A bundle with no `counts` entry keeps every byte and digest it had; an entry changes the child's rows, so it moves the plan's digests.

## The `counts` key

`counts` is a list in the Simulation's `com.agentics.flux/1` extensions, next to `emits`, one entry per relationship. A relationship with no entry picks as before. The entry below is from the how-to page:

```yaml
extensions:
  com.agentics.flux/1:
    counts:
      - emit: invoice_line
        relationship: invoice
        minChildren: 1
        parentWhen: "status <> 'void'"
        children: { bins: ["1", "3", "6", "11"], binWeights: ["0.5", "0.3", "0.2"] }
```

| Key | Meaning |
|---|---|
| `emit` | the child expose, the table that holds the foreign key; required |
| `relationship` | a relationship that expose declares in its contract (`semantics.relationships[].name`); required, at most one entry each |
| `minChildren` | a whole number, default `0`: every eligible parent has at least that many children |
| `parentWhen` | a FLUID SQL/1 predicate over the parent expose's own columns: only a parent whose final row makes it true has children, every other parent has none |
| `children` | the shape of children per parent: `levels` with `levelWeights`, or `bins` with `binWeights`, over whole counts. A level is one count; a bin is the whole counts from its lower edge up to the next edge, the last bin holding its upper edge, and spreads its parents evenly. With no `children` the children are spread as evenly as whole counts allow |
| `tolerance` | a decimal, default `0.10`: the largest total variation distance the children-per-parent check allows against a profile |
| `given` | a column of the parent expose whose class sets the weights of `children`: a category column with declared levels, or a column drawn from bins, with no `nullFraction`. Needs `children`; kernel 0.2.0 only |
| `childrenGiven` | for each class of `given` (a level, or a bin's lower edge as text), a list of weights over the classes `children` declares. A class with no list takes `children`'s weights, and `plan.json` says which did, under `countsGiven` |

No other key is read; an unknown key is `FLX-3105`. A module may supply an expected count per parent, but that is not a key of the bundle and you never write one.

What the layout guarantees, whatever the seed: the counts total the child table's rows exactly; no eligible parent has fewer than `minChildren`; no parent outside `parentWhen` has a child; the histogram of children per parent is the declared one, class for class, when the child rows total what the classes give, and otherwise the largest remainder spreads the difference.

## What is refused at plan time

Every refusal comes before a row exists.

- `FLX-3403`, the entry cannot be laid out as declared: the expose does not declare the relationship; the foreign key column declares a `nullFraction` or a draw of its own (`levels`, `bins`, a mean or `given`), since the layout sets it; the relationship is `distinct`, part of a key, implied by another, a side of a bridge, or points at its own table; a `links` entry gives the relationship a `when` or a `weight` (the message names `counts`); the target is neither generated from statistics nor a reference expose; `children` has a count below zero, a fraction, or a class whose least count is above its greatest; `minChildren` is above every count `children` allows; on a `one_to_one` relationship `children` allows a count above one or `minChildren` is above one; `parentWhen` reads a column of another row (a link, `previous`, children or siblings).
- `FLX-3403`, too few child rows: they cannot give every eligible parent its minimum. With no `parentWhen` this is refused at plan time; with one, the eligible parents are known only when the parents are generated, so the refusal comes then. The message gives the child rows, the minimum and the eligible parents.

A relationship that carries a `when` or a `weight` and a `counts` entry is a conflict: remove the condition or the weight, or drop the entry, and ask the user which they want. Do not work round the refusal.

## `--scale` and `--shard`

Under `--scale` the child rows and the parent rows are scaled alike but each is rounded on its own. When a run planned at scale 1 leaves too few child rows at another scale, the refusal says so and names `--scale`: raise the scale, lower `minChildren` or declare more child rows; ask the user which. The `flux-scale-and-ship` skill covers `--scale` and `--shard`.

The layout draws no value and is worked out once per process over every parent row, and every slice of the child reads it. A `--jobs 1` and a `--jobs 4` run of a bundle with a `counts` entry give the same digests (ADR 0016, Consequences). The cost follows the parent's rows, paid once per process.

## Summaries of the children

A contract `derive` with an `aggregate` over a counted relationship holds on every parent, as the page shows (the contract is the owner's):

```yaml
      invariants:
        - id: invoice_total
          derive: total
          aggregate: { function: sum, column: amount, from: invoice_line, via: invoice, ifNone: 0 }
        - id: invoice_line_count
          derive: line_count
          aggregate: { function: count, from: invoice_line, via: invoice }
```

- `count` is the number of children for which `filter` is true; `sum`, `min` and `max` run over the children whose `column` is not null, and are null when there are none; `ifNone` is the value in place of a null summary.
- An aggregate over a relationship that is not counted is `FLX-6210`, and its message names the `counts` key. So is an aggregate written inside a FLUX expression; it is written as a contract `derive`.
- A summary that reads another summary back through a path is a cycle, `FLX-6208`. A summary that does not fit its column is `FLX-6110` at plan time or `FLX-6112` when the row is made.
- Another expose that reads a summary through a link is `FLX-6210` in this version.

## What `fluxsim init` writes

From a contract and a profile, for each relationship the contract declares and the profile describes with a published histogram of children per parent, `fluxsim init` writes a `counts` entry whose `children` are the histogram's classes, for a key of any type. A `one_to_one` relationship takes `levels: ["0", "1"]`. The class of parents with no child is read as published. A relationship the profile marks `structural: [childless]` is written as `minChildren: 1` and its class of none takes no weight; `orphan` is listed as checked, since every foreign key finds its parent. A histogram of children per parent grouped by a category column of the parent, or by the bins of a number or timestamp one, is written as the entry's `given` and `childrenGiven` beside `children`; a column that cannot be given (one with a null fraction, a key, a rule's column, or one a summary over the children sets) is passed over, with the reason in the ledger. Init also writes `sampling: true` on each distance tolerance it writes, and `allocation: exact` on the columns of a small table that can take it. A relationship the layout cannot place (a key or part of one, a relationship to its own table, a foreign key with a null share, or one with a draw or contract rule of its own) keeps the approximation of a foreign key's draw: its share of parents with no child comes out larger than published. A bundle made again from the same contract and profile differs from one made before; that is a new bundle. Init never writes `parentWhen`, and never writes the contract. See [write a bundle from a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/how-to/write-a-bundle-from-a-profile.md) and [the init reference](https://github.com/Agenticstiger/flux-engine/blob/v0.11.0/docs/reference/cli/init.md).

## How verify reports it

`fluxsim verify` checks the layout on the run's rows. Each `counts` entry adds two lines to the rules of the child, and both are enforced, so one violating parent fails the run:

```text
  rules invoice_line: counts:invoice.minChildren 191 of 191 (100.00%); counts:invoice.parentWhen 9 of 9 (100.00%)
```

`counts:<relationship>.minChildren` is the share of eligible parents with at least `minChildren` children; `counts:<relationship>.parentWhen` is the share of parents outside the condition that have no child. A contract `derive` with `aggregate` over a counted relationship is checked on every parent row, and is enforced too.

When the pinned profile publishes a histogram of children per parent for the relationship, the scorecard gains a `childrenPerParent` row under `crossTable`, with the `propertyId` `children:<child exposeId>.<relationship>`. It counts the run's parents into the profile's bins by their children; the score is one minus the total variation distance, and the row is `within` when the distance is at most the entry's `tolerance`, `0.10` for a relationship with no entry. The `flux-run-and-verify` skill reads the scorecard. With a `given` entry and a profile histogram grouped by that column, the row is scored per group instead (`groups`, each with its own `distance`), and a scorecard that holds it names format `1-5-0`. A failure is met with the remedy `fluxsim explain` prints; never widen `tolerance` or lower `minChildren` to get a run past it without the user's decision.

## Row order

A child table on the counted layout comes out grouped by parent in row order. A consumer that needs another order sorts it. The child's own key is laid out from the row index: a number or date key in ascending order gives a parent's children consecutive keys, and a text or UUID key, permuted, does not (the rows are still consecutive). Code that needs a parent's children reads them by their foreign key.
