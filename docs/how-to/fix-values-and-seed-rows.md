---
title: "Fix values and seed rows"
description: "Declare conditions so that a group of rows holds fixed values and the rest of each row is drawn given them, exactly, with seed rows as groups of one, and read what the evidence pack says about them."
---

# Fix values and seed rows

Use this page when you need a world that holds given values: every subscriber on one plan, a few hundred
premium subscribers in one region, or a handful of rows that start from values you chose, with the rest of
each row consistent with them. A test that needs "a premium subscriber in the EU who bought in a store"
wants that row and its neighbours to look as the data does. The design is
[ADR 0028](../adr/0028-consumer-queries-run-locked-compile-requirements-and-calibrate.md) section 10, and
the rules are in [the specification](../spec/CONSUMER-QUERIES-SPEC.md) section 10.

The steps:

1. declare `conditions`: groups of rows, each with the values it fixes and how many rows hold them;
2. `plan` checks them and `apply` draws the rows, the rest of each row given the fixed values;
3. read what is refused, and why;
4. read what `EVIDENCE.md` says about them.

## 1. The scenario

The page uses the engine's own test fixture, `tests/fixtures/conditions`, so run it from a checkout of the
repository. It is a table of subscriptions whose categorical columns are drawn through a chain of
conditionals: `region` given `plan`, `channel` given `region`, and `tier` given `plan` and `channel`.

```python
import shutil
from pathlib import Path

import flux_engine

source = Path(flux_engine.__file__).resolve().parents[1] / "tests" / "fixtures" / "conditions"
shutil.copytree(source, "bundle")
```

```console
$ sed -n '/^            plan:/,/^            spend:/p' bundle/simulation.flux.yml
            plan: { levels: { basic: '0.6', premium: '0.4' } }
            region:
              given: plan
              levelWeightsGiven:
                basic: { APAC: '1', EU: '3', US: '1' }
                premium: { APAC: '2', EU: '1', US: '2' }
            channel:
              given: region
              levelWeightsGiven:
                APAC: { online: '1', store: '1' }
                EU: { online: '1', store: '3' }
                US: { online: '4', store: '1' }
            tier:
              given: [plan, channel]
              levelWeightsGiven:
                basic:
                  online: { gold: '1', silver: '2' }
                  store: { gold: '0', silver: '1' }
                premium:
                  online: { gold: '3', silver: '1' }
                  store: { gold: '1', silver: '1' }
            spend:
```

Without conditions, premium subscribers in the EU are 8 rows in 100 (0.4 times 0.2). To get a given number
of them you could draw many rows and keep those that match, which is slow for a rare value and exact only in the
limit. Conditions draw them directly and exactly.

## 2. Declare conditions

`conditions` is a key of the Simulation's namespace, beside `emits`. It maps an `exposeId` to a list of
groups. A group has `values`, the columns it fixes, each to the text of a level, and `count`, how many rows
hold them:

```console
$ cat >> bundle/simulation.flux.yml <<'EOF'
    conditions:
      subscriptions:
        - values: { plan: premium, region: EU }
          count: 400
        - values: { plan: basic }
          count: 600
EOF
$ tail -6 bundle/simulation.flux.yml
    conditions:
      subscriptions:
        - values: { plan: premium, region: EU }
          count: 400
        - values: { plan: basic }
          count: 600
```

- Groups take their rows in the order listed, so a row's group is a function of its ordinal: the first 400
  rows are premium subscribers in the EU, and the next 600 are on the basic plan.
- The groups' rows are exactly the emit's rows, so the counts must add up to the emit's `rows`, 1000
  here. A group without a `count` is allowed only when it is the emit's only group, and then covers every
  row.
- The key is read by kernel 0.2.0, the default. Kernel 0.1.0 refuses it with `FLX-3105`, naming the kernel.
  A bundle that declares no `conditions` has the bytes and digests it had.

## 3. Plan and apply

`plan` checks every group against the emit's columns before a row is drawn, and `apply` makes the run:

```console
$ fluxsim plan bundle
plan written to .flux/runs/.../plan.json (runId=..., planDigest=sha256:...)
staged 1000 rows in 1 emits under .flux/runs/.../.stage (...)
$ RUN=$(ls .flux/runs)
$ fluxsim apply bundle --run-id $RUN
applied to .flux/runs/... (runDigest=sha256:...)
emitted subscriptions: 1000 rows (generator)
```

Each group's rows hold its values. The streams hold one JSON array per row, in the contract's column order:
`sub_id`, `plan`, `region`, `channel`, `tier`, `spend`, `note`. Count the groups:

```console
$ cat > count.py <<'EOF'
import collections
import json
import sys

rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
first, second = rows[:400], rows[400:]
print("group 1:", dict(sorted(collections.Counter((r[1], r[2]) for r in first).items())))
print("group 2:", dict(sorted(collections.Counter(r[1] for r in second).items())))
print("group 1 channel:", dict(sorted(collections.Counter(r[3] for r in first).items())))
print("group 1 channel, tier:", dict(sorted(collections.Counter((r[3], r[4]) for r in first).items())))
EOF
$ python count.py .flux/runs/$RUN/emits/subscriptions.ndjson
group 1: {('premium', 'EU'): 400}
group 2: {'basic': 600}
group 1 channel: {'online': 115, 'store': 285}
group 1 channel, tier: {('online', 'gold'): 88, ('online', 'silver'): 27, ('store', 'gold'): 156, ('store', 'silver'): 129}
```

The columns a group does not fix are drawn given the ones it does, so a row is consistent with its group.
In the EU, `channel` is `online` with weight 1 and `store` with weight 3, so about a quarter of group 1 is
online, and 115 of 400 are. A premium online row has a gold tier with weight 3 against 1, so about three
quarters of those are gold: 88 of 115. Nothing was drawn and thrown away to get there. The engine conditions
the junction tree that draws the columns (ADR 0021): the cells that disagree with the fixed values are set to
zero, each clique passes a message to its parent, and each remaining column is drawn from its exact
conditional. The arithmetic is rational, so the rows are exactly the joint the declared weights describe,
given the fixed values, and the draw of a cell takes the same path as an unconditioned cell, with only the
table it searches changed. No seed-tree path is added.

### Seed rows

A seed row is a group with a count of 1. It starts a row from values you choose, and the rest of the row is
drawn given them. A test that needs one particular premium subscriber in the US who bought online and holds
the gold tier writes it as a group. The groups' rows are exactly the emit's rows and no group can leave rows
free (a group with no `values` is `FLX-3640`), so every other row belongs to a group of its own. Give each
level of a column you fix a group, here the two levels of `plan`, and the rest of those rows is drawn as
before, given the plan:

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

Only a categorical column can be fixed. A number such as `spend`, which is drawn from a histogram, cannot,
and neither can a key, an identifier or a column a rule sets.

A seed row is a declared value. The engine cannot tell whether a value was copied from a record of the
data, so keeping ADR 0003's rule, that the engine reads aggregates and never a record, is your part: write
values you chose, not values you read in a table.

### Scale and shards

`--scale` scales each group's count as it scales the emit's rows, by the largest remainder over the groups,
so the counts add up to the scaled total. A shard takes the rows of its ordinals and with them their
groups.

## 4. What is refused, and why

A fixed column must be one the engine can set without breaking something else. Each refusal names the
group, the column and the case, and every one is raised at `plan`, before a row is drawn. Try a group that
asks for a combination the weights rule out. A basic subscriber who bought in a store has the gold tier with
weight 0:

```console
$ cp -R bundle impossible
$ python - <<'EOF'
import re

path = "impossible/simulation.flux.yml"
text = open(path, encoding="utf-8").read()
groups = "    conditions:\n      subscriptions:\n        - values: { plan: basic, channel: store, tier: gold }\n          count: 1000\n"
open(path, "w", encoding="utf-8").write(re.sub(r"    conditions:.*", groups, text, flags=re.S))
EOF
$ fluxsim plan impossible   # exit 1
FLX-3646: a conditions group leaves no row possible -- conditions group 0 (channel=store, plan=basic, tier=gold) leaves no row possible: the declared conditionals give these values together no weight
more: fluxsim explain FLX-3646  ...
```

The other refusals, each with its own code:

- `FLX-3642`: a name that is not a column of the expose, or a value that is not one of the column's levels.
  Fixing `plan: gold` names the levels the column draws, `basic` and `premium`.
- `FLX-3643`: a column that cannot be fixed because something else decides its value. A key or foreign key,
  an identifier such as `sub_id`, a column a rule sets (a copied column included), a column a conditional
  link writes, a column that is not categorical (`spend` is a histogram), and a column that declares a
  `nullFraction` (`note`). A fixed column is categorical and is drawn through a junction tree or a Chow-Liu
  forest, or from its own marginal with no column drawn given it. Conditions on an emit that declares a
  history or is projected from one are refused too, because the history lays the rows out. So is a group
  on an emit where a column that declares `allocation: exact` is fixed, drawn given a fixed column, or the
  one a fixed column is drawn given: remove `allocation: exact` from that column, or drop the group.
- `FLX-3645`: a column whose FLUID `sensitivity` sets a minimum grade of P3 or stronger. A fixed value is a
  declaration, and a grade never sees a declaration, so a value in a P3 column could be a record's value
  copied in. Leave the column to be drawn.
- `FLX-3641`: counts that do not give the emit's rows, a count that is not a whole number of at least 1, or
  a group without a count beside others.
- `FLX-3644`: an emit with no fidelity block, or a reference expose, which have no cell whose weights a
  group could choose.

Run `fluxsim explain FLX-3643` for the whole text of any of them.

## 5. What the evidence pack says

`ship` writes `EVIDENCE.md`, and names each conditioned emit under "What Was Not Proven". The pack needs a
profile to check the run against, so this section uses the subscriptions bundle of the
[consumer queries page](./check-consumer-queries.md), which pins one, and gives it groups that keep its plan
shares:

```python
source = Path(flux_engine.__file__).resolve().parents[1] / "tests" / "fixtures" / "consumers"
shutil.copytree(source, "fixture")
```

```console
$ cp -R fixture/bundle subscriptions
$ python - <<'EOF'
path = "subscriptions/simulation.flux.yml"
text = open(path, encoding="utf-8").read()
groups = """    conditions:
      subscriptions:
      - values: {plan: premium, region: east}
        count: 240
      - values: {plan: basic}
        count: 600
      - values: {plan: standard}
        count: 360
    emits:
      subscriptions:
"""
head, marker, tail = text.partition("    emits:\n      subscriptions:\n")
assert marker
open(path, "w", encoding="utf-8").write(head + groups + tail)
EOF
$ fluxsim run subscriptions --profile fixture/subscriptions.profile.json --ship -o out > refused.log 2>&1   # exit 1
$ grep -m1 -o 'outside the margin.*levelHistogram of region)' refused.log
outside the margin against the pinned profile: 001:levelHistogram:region (levelHistogram of region)
```

The counts 240, 600 and 360 are the plan shares of the profile, `0.2`, `0.5` and `0.3` of 1200, so the plan
marginal is met. The fixed `region` is not: putting all 240 premium rows in one region moves the region
histogram away from the profile's, and the fidelity gate withholds the pack. A fixed value is a departure
from the data's statistics, and the gate says so. Fix only the plan, which the groups' counts keep exact:

```console
$ sed -i.bak 's/, region: east//' subscriptions/simulation.flux.yml
$ rm subscriptions/simulation.flux.yml.bak
$ fluxsim run subscriptions --profile fixture/subscriptions.profile.json --ship -o out | tail -1
run ... complete; shipped to out
```

The report names each group, the number of rows it holds and the columns it fixes:

```console
$ grep -h 'Conditioned emit' .flux/runs/*/EVIDENCE.md
Conditioned emit `subscriptions`: group 1 holds 240 rows and fixes `plan`; group 2 holds 600 rows and fixes `plan`; group 3 holds 360 rows and fixes `plan`.
$ grep -h 'declarations of the bundle' .flux/runs/*/EVIDENCE.md
The values a group fixes are declarations of the bundle: no statistic stands behind them and no privacy grade covers them. They are not printed here.
```

The report never prints a fixed value, only the names of the columns. A value a group fixes is a
declaration of the bundle: no statistic of a profile stands behind it, and no privacy grade covers it. A
grade is read from the profile a run pins, and a declaration never enters it.

## What to keep in mind

- Fix only what a test needs. Every fixed column departs from the profile's marginal, and the fidelity check
  still compares the run with the profile, so a group that fixes a column to a share the profile does not
  hold will show.
- A group is a declaration. If a value in it came from a real record, the copy is no longer a copy of
  aggregates.
- For a world that follows given rates and not given values, declare consumer queries and
  [calibrate to them](./check-consumer-queries.md) instead.
