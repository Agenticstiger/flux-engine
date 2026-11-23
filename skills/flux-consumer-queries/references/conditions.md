# Fixed values and seed rows

`conditions` declares groups of rows that hold given values, with the rest of each row drawn consistently with them. It is for a test that needs "a premium subscriber in the EU who bought in a store" and its neighbours. The worked page is
[fix values and seed rows](https://github.com/Agenticstiger/flux-engine/blob/v0.10.0/docs/how-to/fix-values-and-seed-rows.md).

## What it is

`conditions` is a key of the Simulation's namespace, beside `emits`. It maps an expose to a list of groups. A group has `values`, the columns it fixes, each to the text of a level, and `count`, how many rows hold them. A seed row is a group with a count of 1. No group can leave rows free, so each level of a fixed column gets a group of its own:

```yaml
conditions:
  subscriptions:
    - values: { plan: premium, region: US, channel: online, tier: gold }
      count: 1
    - values: { plan: premium }
      count: 399
    - values: { plan: basic }
      count: 600
```

- Groups take their rows in the order listed, so a row's group is a function of its ordinal.
- The groups' rows are exactly the emit's rows: the counts must add up to the emit's `rows` (`FLX-3641`). A group with no `count` is allowed only as the emit's only group.
- The unfixed columns are drawn given the fixed ones, exactly: the engine conditions the junction tree, with no rejection and no approximation. The draw adds no seed path.
- Kernel 0.2.0 only; kernel 0.1.0 refuses the key (`FLX-3105`). A bundle without `conditions` keeps its bytes and digests.
- `--scale` scales each group's count by the largest remainder, so the counts add up to the scaled total. A shard takes its rows and their groups.

## What is refused, at `plan`

| Code | Case |
|---|---|
| `FLX-3640` | the declaration is malformed |
| `FLX-3641` | the counts do not give the emit's rows, or a count is not a whole number of at least 1 |
| `FLX-3642` | a name that is not a column of the emit, or a value that is not one of the column's levels |
| `FLX-3643` | a column that cannot be fixed: a key, an identifier, a column a rule sets (a copied column too), a column a link or a lifetime writes, one that is not categorical, or one with a null fraction |
| `FLX-3644` | an emit with no fidelity block, or a reference expose |
| `FLX-3645` | a column whose sensitivity needs a strong grade: a fixed value is a declaration, and no grade sees a declaration, so it could be a record's value copied in |
| `FLX-3646` | the values together have no weight in the declared conditionals |
| `FLX-3647` | the group cannot be drawn exactly from this emit's conditionals |

Run `fluxsim explain` on the code. For `FLX-3645`, leave the column to be drawn; a column's sensitivity label is the contract owner's decision, never yours to change.

## Rules for the agent

- Fix only what the test needs. A fixed column departs from the profile's marginal, and the fidelity check still compares the run with the profile, so a group that fixes a column to a share the profile does not hold shows up as a refusal at `ship`. Fix the plan alone if its shares must stay exact.
- A group is a declaration. Write values the user chose. Never write a value read from a record, and never open the records to find one: the engine cannot tell where a value came from, and keeping ADR 0003's rule is your part.
- `EVIDENCE.md` names each conditioned emit with its groups' counts and the columns they fix, and prints no fixed value.
- For a world that follows given rates and not given values, declare consumer queries and calibrate instead.
