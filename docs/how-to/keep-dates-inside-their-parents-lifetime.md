---
title: "Keep dates inside their parent's lifetime"
description: "Read what fluxsim init writes from a profile with date pairs, calendars and exposure, a lifetime for each table, dates within a parent's lifetime, seasonal rates and exposure, see what it left unused and why, read the lifetime, within, calendar and exposure lines of fluxsim verify, and adjust a fact that fails."
---

# Keep dates inside their parent's lifetime

Use this page when your tables carry dates that depend on each other, and the rows you generate should keep those dependencies. A customer signs up and may leave, an account opens inside the customer's lifetime, and an invoice is issued inside the account's. Drawn column by column, an invoice is dated before its customer existed as often as the two columns overlap by chance, an end falls before its start, and every weekday and every month is as busy as any other. Four declarations in the Simulation's `com.agentics.flux/1` extensions say what the data does instead:

- `lifetimes`: a row's start, its end, and how many days lie between them.
- `within`: a date, or a whole period, that lies inside the lifetime of the row a link reaches.
- `rates`: the months, the days of the week and the hours of the week on which a date falls.
- `exposure: true` on a `within` entry: a parent with more active days has more children.

You rarely write them by hand. `fluxsim init` writes them from a profile that publishes date pairs, calendars and exposure ([ADR 0024](../adr/0024-dates-follow-their-parent-rows-lifetime.md) and [ADR 0025](../adr/0025-dates-follow-seasonal-rates-dwell-times-and-exposure.md) say why they work as they do). This page runs `init` on a small profile, reads what it wrote and what it left out, checks a run with `fluxsim verify`, and then changes a fact. All of it needs kernel 0.2.0, which a new plan takes; kernel 0.1.0 refuses each key with [FLX-3105](../reference/errors/FLX-3105.md).

## Write the contract and the profile

The contract has three tables. A customer signs up, may close, and has a segment and a renewal date. An account belongs to a customer and has its own opening and closing dates. An invoice belongs to an account.

```yaml
# billing.fluid.yaml
fluidVersion: "0.7.7"
kind: DataProduct
id: shop.silver.billing
name: Billing
metadata:
  owner:
    team: billing
exposes:
  - exposeId: customer
    kind: table
    binding: { platform: local, format: parquet, location: { path: ./out/customer.parquet } }
    contract:
      schema:
        - { name: customer_id, type: STRING, required: true }
        - { name: segment, type: STRING, required: true }
        - { name: signed_up, type: DATE, required: true }
        - { name: closed_on, type: DATE }
        - { name: renewed_on, type: DATE }
    semantics:
      entities:
        - { name: customer, type: primary, expr: customer_id }
  - exposeId: account
    kind: table
    binding: { platform: local, format: parquet, location: { path: ./out/account.parquet } }
    contract:
      schema:
        - { name: account_id, type: STRING, required: true }
        - { name: customer_id, type: STRING, required: true }
        - { name: opened_on, type: DATE, required: true }
        - { name: closed_on, type: DATE }
    semantics:
      entities:
        - { name: account, type: primary, expr: account_id }
      relationships:
        - { name: account_to_customer, to: customer, fromColumns: [customer_id], toColumns: [customer_id], cardinality: many_to_one }
  - exposeId: invoice
    kind: table
    binding: { platform: local, format: parquet, location: { path: ./out/invoice.parquet } }
    contract:
      schema:
        - { name: invoice_id, type: STRING, required: true }
        - { name: account_id, type: STRING, required: true }
        - { name: issued_on, type: DATE, required: true }
    semantics:
      entities:
        - { name: invoice, type: primary, expr: invoice_id }
      relationships:
        - { name: invoice_to_account, to: account, fromColumns: [account_id], toColumns: [account_id], cardinality: many_to_one }
```

The data owner makes the profile where the data lives, with the date members of profile format 1-3-0 ([PROFILE-SPEC](../spec/PROFILE-SPEC.md)). This page writes one in Python so that it runs anywhere. Read the profile's members, not the helper code:

- `datePairs`, on a table: for two date columns, a histogram of the days from the first to the second, a `levelHistogram/1` called `order` that counts how many rows put the second `after`, `before` or on the `same` day as the first, and optionally the same histogram grouped by a column.
- `dates`, on a relationship: the same two members for a parent's date and a child's.
- `calendar`, on a table: for a date column, how many rows fall in each month, each day of the week, and for a timestamp each hour of the week.
- `exposure`, on a relationship: how many child rows there are and how many days the parents were active inside a stated window, for one pair of the parent's lifetime columns.

```python
import json
from datetime import date

DAY = 86_400_000


def ms(year, month, day):
    return (date(year, month, day) - date(1970, 1, 1)).days * DAY


def stat(definition, groups, **extra):
    size = sum(group.get("k", 0) for group in groups)
    return {"definition": definition, "sampleSize": {"min": size, "max": size}, "disclosure": "none", "groups": groups, **extra}


def levels(names, counts):
    return stat("levelHistogram/1", [{"group": "", "k": sum(counts), "levelCounts": counts, "status": "F"}], levels=names)


def histogram(edges, counts, by=None):
    groups = counts if by else {"": counts}
    cells = [{"group": name, "k": sum(row), "binCounts": row, "status": "F"} for name, row in groups.items()]
    return stat("marginalHistogram/1", cells, bins=[str(edge) for edge in edges], **({"groupBy": by} if by else {}))


def metric(definition, value):
    return {"definition": definition, "status": "F", "value": value, "sampleSize": {"min": 100, "max": 100}, "disclosure": "none"}


def column(first, last, rows):
    low, high = ms(*first), ms(*last)
    metrics = {"min": metric("min/1", str(low)), "max": metric("max/1", str(high))}
    return {"domain": "timestamp", "metrics": metrics, "statistics": [histogram([low, high], [rows])]}


def pair(start, end, order, edges, counts, *extra):
    return {"start": start, "end": end, "order": levels(["after", "before", "same"], order), "statistics": [histogram(edges, counts), *extra]}


def link(parent, child, order, edges, counts):
    return {"parent": parent, "child": child, "order": levels(["after", "before", "same"], order), "statistics": [histogram(edges, counts)]}


def relationship(child, parent, key, dates=(), exposure=()):
    entry = {"parent": {"table": parent, "columns": [key]}, "child": {"table": child, "columns": [key]}, "statistics": [histogram([0, 3, 10], [60, 40])]}
    return {**entry, **({"dates": list(dates)} if dates else {}), **({"exposure": list(exposure)} if exposure else {})}


MONTHS = [f"{month:02d}" for month in range(1, 13)]
WEEKDAYS = [str(day) for day in range(1, 8)]

profile = {
    "_producer": "https://example.test/producer",
    "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json",
    "encoding": {"fractionalBits": 16, "rounding": "floor"},
    "disclosureRules": {"none": {}},
    "tables": {
        "customer": {
            "rowCount": 200,
            "columns": {
                "customer_id": {"domain": "category"},
                "segment": {"domain": "category", "statistics": [levels(["basic", "pro"], [120, 80])]},
                "signed_up": column((2023, 1, 1), (2023, 3, 31), 200),
                "closed_on": column((2023, 1, 1), (2025, 6, 30), 200),
                "renewed_on": column((2023, 1, 1), (2025, 6, 30), 200),
            },
            "datePairs": [
                # 180 customers close after they sign up and 20 the same day; the days are grouped by segment too.
                pair("signed_up", "closed_on", [180, 0, 20], [0, 365, 730, 900], [50, 70, 60],
                     histogram([0, 365, 730, 900], {"basic": [40, 30, 10], "pro": [10, 40, 50]}, by="segment")),
                # A renewal is before the signup for 90 of 200: no order to keep.
                pair("signed_up", "renewed_on", [110, 90, 0], [0, 30, 360], [60, 50]),
            ],
            "calendar": [{"column": "signed_up", "dayOfWeek": levels(WEEKDAYS, [40, 40, 40, 40, 40, 0, 0])}],
        },
        "account": {
            "rowCount": 300,
            "columns": {
                "account_id": {"domain": "category"},
                "customer_id": {"domain": "category"},
                "opened_on": column((2023, 1, 1), (2023, 6, 30), 300),
                "closed_on": column((2023, 1, 1), (2025, 6, 30), 300),
            },
            "datePairs": [pair("opened_on", "closed_on", [190, 0, 10], [0, 365, 900], [100, 200])],
        },
        "invoice": {
            "rowCount": 6000,
            "columns": {
                "invoice_id": {"domain": "category"},
                "account_id": {"domain": "category"},
                "issued_on": column((2023, 3, 1), (2025, 6, 30), 6000),
            },
            "calendar": [{
                "column": "issued_on",
                "month": levels(MONTHS, [150, 140, 340, 500, 480, 520, 870, 890, 630, 580, 450, 450]),
                "dayOfWeek": levels(WEEKDAYS, [1000, 1000, 1000, 1000, 1000, 500, 500]),
            }],
        },
    },
    "relationships": [
        relationship("account", "customer", "customer_id",
                     [link("signed_up", "opened_on", [250, 0, 50], [0, 30, 120], [200, 100])]),
        relationship("invoice", "account", "account_id",
                     [link("opened_on", "issued_on", [2250, 50, 100], [0, 900], [6000])],
                     [{"start": "opened_on", "end": "closed_on", "children": metric("count/1", 6000), "activeDays": metric("count/1", 91000),
                       "window": {"start": str(ms(2023, 1, 1)), "end": str(ms(2025, 6, 30))}}]),
    ],
}
with open("billing.profile.json", "w", encoding="utf-8") as handle:
    json.dump(profile, handle)
```

## What `fluxsim init` writes

One command writes the bundle. The directory must not exist yet.

```console
$ fluxsim init bundle --contract billing.fluid.yaml --profile billing.profile.json
created bundle from billing.fluid.yaml and billing.profile.json (4 files):
...
profile: 40 statistics -- 37 used, 3 unused, 0 withheld
unused (no number rests on these; the register lists them):
  - /tables/customer/calendar/0: rates shape only a date a within entry or a duration sets, and none of the bundle's does for this column
  - /tables/customer/datePairs/1: its order holds on 55% of the rows where both dates are known, below the bar of 90%
...
next:
  fluxsim run bundle --profile billing.profile.json
```

Every statistic of the profile is used or unused, and the command says which, with the reason ([ADR 0014](../adr/0014-init-reads-every-statistic-a-profile-publishes.md)). The same list is in the Simulation, under `unused`, and `plan`, `verify` and `ship` count it. Here two statistics are unused: the renewal date has no order to keep, since it is before the signup for 45% of the customers, and the signup's weekdays shape a column that no fact sets. The first reason shows the bar, which is 90%: `init` declares an order only where it holds on at least nine rows in ten of those where both dates are known. Two independent date columns break an order about as often as they keep it, so a lower bar would turn noise into a fact with a large target.

A small helper prints one part of the bundle's namespace, so the next sections can read it:

```python
SHOW = """\
import sys

import yaml

directory, *paths = sys.argv[1:]
with open(directory + "/simulation.flux.yml", encoding="utf-8") as handle:
    block = yaml.safe_load(handle)["extensions"]["com.agentics.flux/1"]
for path in paths:
    value = block
    for key in path.split("."):
        value = value[key]
    print(yaml.dump({path: value}, default_flow_style=None, sort_keys=False, width=100), end="")
"""
with open("show.py", "w", encoding="utf-8") as handle:
    handle.write(SHOW)
```

### Lifetimes

A `lifetimes` entry names a start, usually an end, and says how long the lifetime lasts. Its `duration` is the pair's histogram cut at day 0, since a lifetime does not run backward. A pair that is grouped by a column of the table gives a dwell family: `given` names the state, `states` holds one histogram for each published group, and `otherwise` is the ungrouped histogram, which a customer in no listed segment takes.

```console
$ python show.py bundle lifetimes
lifetimes:
- emit: customer
  start: signed_up
  end: closed_on
  duration:
    given: segment
    states:
      basic:
        bins: ['0', '365', '730', '900']
        binWeights: ['4', '3', '1']
      pro:
        bins: ['0', '365', '730', '900']
        binWeights: ['1', '4', '5']
    otherwise:
      bins: ['0', '365', '730', '900']
      binWeights: ['5', '7', '6']
- emit: account
  start: opened_on
  end: closed_on
  duration:
    bins: ['0', '365', '900']
    binWeights: ['1', '2']
```

A basic customer stays mostly under a year and a pro customer mostly over, as the grouped histogram says. A `target` would appear here if the pair's order broke on a share of the rows. These pairs keep the order on every row, so neither entry has one, and a row whose end is before its start counts as a violation that fails the run. A pair with no usable duration gives a lifetime with a start and no end, since an end drawn apart from its start keeps the order only by chance.

An end that lies after the run's window is NULL for a column the contract leaves nullable, because the window never saw it, and keeps its date for a column the contract requires. `endPastWindow` says which, and `init` writes `kept` when the end column's published maximum lies after the window.

### Dates within a lifetime

A `within` entry puts a date inside the lifetime of the row a link reaches. Its `offset` is the profile's histogram of the days from the parent's start to the child's date.

```console
$ python show.py bundle within
within:
- emit: account
  relationship: account_to_customer
  lifetime: true
  offset:
    bins: ['0', '30', '120']
    binWeights: ['2', '1']
- emit: invoice
  relationship: invoice_to_account
  date: issued_on
  offset:
    bins: ['0', '900']
    binWeights: ['1']
  target: '2.083333333'
  exposure: true
```

The first entry is a period: `lifetime: true` says the account's own lifetime, its start and its end, lies inside the customer's. `init` writes it when the child column starts the child's own lifetime. A child that ends after its parent ends is cut at the parent's end, so a child's end is NULL only under a parent that is still open: a `nullFraction` declared on the child's end column is met among the rows under open parents, and the run generates a lower share overall. `plan` and `run` print a note on stderr when a period's end declares one under a parent that can close. The second entry puts a single date, `issued_on`, inside the account's lifetime.

The `target` is the percent of rows that may break the fact, and `init` takes it from the pair's `order`: 50 of the 2400 invoices are dated before their account opened, so 2.083% of the rows break the fact. An invoice on the same day as its account's start does not break it. The share of dates after the parent's end is added when the lifetime has an end. An entry with no `target`, as the account's, is enforced: one row that breaks it fails the run. An entry with a target is checked against it.

When an entry is enforced, `init` also checks what `plan` checks, that no row can be left with an empty interval to draw its date in. If the child column's declared range starts after the earliest end its parent can have, `init` widens the range to the parent's lifetime, clipped to the run's time bounds, so that the bundle plans and the entry stays enforced. It never widens past a range the contract declares, or past the days a derive over the column keeps in range: it writes no entry for such a date instead. A later section shows both.

Entries chain. The invoice is inside its account's lifetime, and the account is inside its customer's, so the invoice is inside the customer's lifetime too. `verify` scores that outer level on its own line.

### Rates

A `rates` entry shapes the months, days of the week and hours of the week on which a column that a `within` entry or a duration sets falls. Each published histogram is divided, level by level, by the number of times that level occurs inside the run's window, so that a base spread evenly over the days gives back the published counts. A level the window does not hold takes the weight 1, and each list is scaled so that its largest weight is 1.

```console
$ python show.py bundle rates
rates:
- emit: invoice
  column: issued_on
  month: ['0.11236', '0.114739', '0.254682', '0.387016', '0.359551', '0.402497', '0.977528', '1', '0.731461',
    '0.651685', '0.522472', '0.505618']
  dayOfWeek: ['0.992366', '1', '1', '1', '1', '0.5', '0.496183']
```

The weekday is counted once. When a profile publishes the hour of the week for a timestamp column, the entry carries `hourOfWeek` and leaves `dayOfWeek` out, since the hour already carries the weekday, and the unused list says so. A date column has no hour, so its `hourOfWeek` is unused. A calendar for a column that no entry sets, such as the customer's `signed_up`, is unused with the reason you saw above.

### Exposure

The `exposure: true` on the invoice entry says that an account's number of invoices follows its active days. `init` writes it on the first `within` entry over a relationship whose profile publishes an `exposure` for the lifetime it wrote for the parent: the same start, the same end or none, both counts published, and at least one active day. An account that is active for two months gets about a thirtieth of the invoices of one active for five years, and none if it is never active inside the window. Exposure needs the counted layout of [Give every parent its children](./give-every-parent-its-children.md), and `init` writes the `counts` entry it rests on.

## Run it and read `fluxsim verify`

```console
$ fluxsim run bundle --profile billing.profile.json
plan written to ...
parameters: ...
applied to ...
emitted customer: 200 rows (generator); account: 300 rows (generator); invoice: 6000 rows (generator)
run ... complete
$ fluxsim verify bundle --run-id $(ls .flux/runs) --profile billing.profile.json | grep -E '^  (rules|calendar|exposure)'
  rules account: lifetime:opened_on.closed_on 300 of 300 (100.00%); within:account_to_customer.lifetime 300 of 300 (100.00%)
  rules customer: lifetime:signed_up.closed_on 200 of 200 (100.00%)
  rules invoice: within:invoice_to_account.issued_on 5877 of 6000 (97.95%); within:invoice_to_account.account_to_customer.issued_on 5918 of 6000 (98.63%)
  calendar:signed_up (customer): outside, not gated: no rates entry shapes this column -- dayOfWeek distance 0.31 (tolerance 0.1) (outside)
  calendar:issued_on (invoice): passed -- month distance 0.0745 (tolerance 0.1); dayOfWeek distance 0.016 (tolerance 0.1)
  exposure:invoice_to_account (invoice): passed -- children per active day 6000 over 91134 against 6000 over 91000, gap 0.0015 (tolerance 0.1000)
```

Each line is one kind of fact:

| Line | Holds when |
|---|---|
| `lifetime:<start>.<end>` | the end is not before the start. A row that breaks it is counted once. For every row that reads it as a parent, such a lifetime is open. |
| `within:<relationship>.<column>` | the date is not before its parent's start and, unless the parent's lifetime is open, not after its parent's end. `within:<relationship>.lifetime` is the same for a period. |
| `within:<relationship>.<relationship>.<column>` | the outer level of a chain: the invoice is inside the customer's lifetime. It is checked against the sum of the levels' targets, zero when every level is enforced. |
| `calendar:<column>` | the run's counts by month, day of week and hour of week are within the tolerance of the profile's, by total variation distance. Gated only where a `rates` entry shapes the column. |
| `exposure:<relationship>` | the run's children per active day match the profile's `children` over `activeDays`, within 10%, or the tolerance a `facts` entry gives. |

The `rules` lines count rows that hold. An enforced fact fails the run unless every covered row holds. A fact with a target is checked against it: the invoice's date holds on 97.95% of rows, which is what its target of 2.083% allows. The chain line is its own count: an invoice can break the first level and still lie inside the customer's lifetime.

The `calendar:signed_up` line is scored, since the profile publishes it, and it is outside the tolerance, but it does not fail the run: no entry shapes that column, so nothing could have met it. `init` said so when it left the statistic unused. A calendar line fails the run only where a `rates` entry shapes its column.

A hidden column or table cannot be scored, since the run never wrote it. Such a fact is listed as not scored, with that reason, and never counts as passing.

## When a fact fails

A fact fails when the entry asks for something the declared ranges do not allow. One more helper edits a copy of the bundle: each `PATH=JSON` argument sets a value of the namespace, a number in the path indexes a list, and a `PATH` alone removes the key or the list item.

```python
ADJUST = """\
import json
import sys

import yaml

directory, *edits = sys.argv[1:]
path = directory + "/simulation.flux.yml"
with open(path, encoding="utf-8") as handle:
    document = yaml.safe_load(handle)
block = document["extensions"]["com.agentics.flux/1"]
for edit in edits:
    name, _, value = edit.partition("=")
    *parents, last = [int(key) if key.isdigit() else key for key in name.split(".")]
    holder = block
    for key in parents:
        holder = holder[key]
    if not _:
        del holder[last]
    elif isinstance(holder, list) and last == len(holder):
        holder.append(json.loads(value))
    else:
        holder[last] = json.loads(value)


def reaches(node, tokens):
    for token in tokens:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        elif isinstance(node, dict) and token in node:
            node = node[token]
        else:
            return False
    return True


# A register entry names its number by position, so an entry whose number the edits removed goes too.
prefix = "/extensions/"
block["parameters"] = [
    entry
    for entry in block["parameters"]
    if not entry["pointer"].startswith(prefix)
    or reaches(document, entry["pointer"][1:].split("/"))
]
with open(path, "w", encoding="utf-8") as handle:
    yaml.safe_dump(document, handle, sort_keys=False)
"""
with open("adjust.py", "w", encoding="utf-8") as handle:
    handle.write(ADJUST)
```

Copy the bundle and narrow the invoice date's declared range to the start of 2025. Most invoices belong to accounts whose lifetime ended before then, so for them the lifetime and the range have no day in common.

```console
$ cp -r bundle narrow
$ python adjust.py narrow emits.invoice.fidelity.columns.issued_on.bins.0='"2025-01-01"' emits.invoice.fidelity.columns.issued_on.range.min='"2025-01-01"' emits.invoice.fidelity.properties.0.bins.0='"2025-01-01"'
$ fluxsim run narrow --state-dir narrow.state --profile billing.profile.json
plan written to ...
parameters: ...
applied to ...
emitted customer: 200 rows (generator); account: 300 rows (generator); invoice: 6000 rows (generator)
run ... complete
$ fluxsim verify narrow --state-dir narrow.state --run-id $(ls narrow.state/runs) --profile billing.profile.json 2>/dev/null | grep -E '^  (rules invoice|calendar:issued)' # exit 1
  rules invoice: within:invoice_to_account.issued_on 1575 of 6000 (26.25%) (outside its target); within:invoice_to_account.account_to_customer.issued_on 2857 of 6000 (47.61%) (outside its target)
  calendar:issued_on (invoice): failed -- month distance 0.645 (tolerance 0.1) (outside); dayOfWeek distance 0.0935 (tolerance 0.1)
```

The target allows 2.083% of the rows to break the fact, and 73.75% did. For a row with no day in common between its parent's lifetime and the column's range, the date keeps the value its own cell drew inside the range, and the row counts as a violation. The calendar line fails too, because the dates are no longer where the rates put them. The fix is in the range, not in the lifetime. Put the start back:

```console
$ python adjust.py narrow emits.invoice.fidelity.columns.issued_on.bins.0='"2023-03-01"' emits.invoice.fidelity.columns.issued_on.range.min='"2023-03-01"' emits.invoice.fidelity.properties.0.bins.0='"2023-03-01"'
$ fluxsim run narrow --state-dir fixed.state --profile billing.profile.json
plan written to ...
parameters: ...
applied to ...
emitted customer: 200 rows (generator); account: 300 rows (generator); invoice: 6000 rows (generator)
run ... complete
$ fluxsim verify narrow --state-dir fixed.state --run-id $(ls fixed.state/runs) --profile billing.profile.json | grep -E '^  (rules invoice|calendar:issued)'
  rules invoice: within:invoice_to_account.issued_on 5877 of 6000 (97.95%); within:invoice_to_account.account_to_customer.issued_on 5918 of 6000 (98.63%)
  calendar:issued_on (invoice): passed -- month distance 0.0745 (tolerance 0.1); dayOfWeek distance 0.016 (tolerance 0.1)
```

An entry with no target is stricter: it fails on one row that breaks it, and `plan` refuses it with `FLX-6204` before a row exists whenever the declared ranges can leave a row without a day to draw its date on. The message names the two remedies, to widen the column's declared range or to give the entry a `target`. `init` takes the first one for you, which a profile whose invoice dates start later than its accounts open shows. Here the account's opening date is published from 1 March while its customers sign up from 1 January. A customer who signs up in January and closes at once has a lifetime that ends before the range begins, so the account has no day to open on:

```python
profile["tables"]["account"]["columns"]["opened_on"] = column((2023, 3, 1), (2023, 6, 30), 300)
with open("later.profile.json", "w", encoding="utf-8") as handle:
    json.dump(profile, handle)
```

```console
$ fluxsim init later --contract billing.fluid.yaml --profile later.profile.json > /dev/null
$ python show.py later emits.account.fidelity.columns.opened_on.range
emits.account.fidelity.columns.opened_on.range: {min: '2023-01-01', max: '2023-06-30'}
$ fluxsim plan later --state-dir later.state --profile later.profile.json > later.plan.txt
$ head -1 later.plan.txt
plan written to ...
```

The profile says the opening date starts on 1 March, and the bundle declares 1 January, the earliest day a parent can start: just enough range to meet the plan's check. The entry stays enforced, as the clean profile says.

A range the contract declares is never widened, nor is it widened past the days a derive over the column keeps in range, since a row outside them breaks the contract. Had the contract declared the opening date's range from 1 March, `init` would write no entry for the opening date: it is drawn inside its range from its own statistics, and the relationship's statistic is listed as unused, with the range the plan's check asked for.

## Change a fact

Four changes cover what you will want. Each is an edit of the Simulation, so each moves the plan's digests.

- **Give a fact a target.** Set `target` on a `lifetimes` or `within` entry to the percent of covered rows that may break it, as a decimal string. The run then draws that share of violations on purpose, and `verify` checks the measured share against it. Use it for data that is not clean, such as an invoice that is backdated.
- **Widen a declared range.** The `range` of the date column's statistics, with the first and last edges of its histogram, bounds what the column may hold. A fact over a column that cannot reach its parent's lifetime fails, as above.
- **Add a `rates` entry.** An entry names one column of an emit that a `within` entry or a duration sets, and at most one of `month` (12 decimal weights, January first), `dayOfWeek` (7, Monday first) and `hourOfWeek` (168, from Monday 00:00 UTC, for a timestamp column). At least one is required, and a weight is zero or more. A weight of zero keeps the column off that level.
- **Remove an entry.** Delete a `lifetimes`, `within` or `rates` entry, or the `exposure` key of a `within` entry. The fact is no longer generated, and a date that no entry sets is drawn from its own statistics again. A `calendar:` line stays, since the profile still publishes it, but it is no longer gated. Removing `exposure` also changes which account an invoice picks, so read the `within` lines again afterwards.

Three of them on a copy of the bundle: the account's period gets a target of 5%, a `rates` entry is added for the account's opening date, and the invoice's `rates` entry is removed.

```console
$ cp -r bundle adjusted
$ python adjust.py adjusted within.0.target='"5"' rates.1='{"emit": "account", "column": "opened_on", "dayOfWeek": ["1", "1", "1", "1", "1", "0.2", "0.2"]}' rates.0
$ fluxsim run adjusted --state-dir adjusted.state --profile billing.profile.json
plan written to ...
parameters: ...
applied to ...
emitted customer: 200 rows (generator); account: 300 rows (generator); invoice: 6000 rows (generator)
run ... complete
$ fluxsim verify adjusted --state-dir adjusted.state --run-id $(ls adjusted.state/runs) --profile billing.profile.json | grep -E '^  (rules|calendar|exposure)'
  rules account: lifetime:opened_on.closed_on 300 of 300 (100.00%); within:account_to_customer.lifetime 287 of 300 (95.66%)
  rules customer: lifetime:signed_up.closed_on 200 of 200 (100.00%)
  rules invoice: within:invoice_to_account.issued_on 5886 of 6000 (98.10%); within:invoice_to_account.account_to_customer.issued_on 5700 of 6000 (95.00%)
  calendar:signed_up (customer): outside, not gated: no rates entry shapes this column -- dayOfWeek distance 0.24 (tolerance 0.1) (outside)
  calendar:issued_on (invoice): outside, not gated: no rates entry shapes this column -- month distance 0.1668 (tolerance 0.1) (outside); dayOfWeek distance 0.1156 (tolerance 0.1) (outside)
  exposure:invoice_to_account (invoice): passed -- children per active day 6000 over 98242 against 6000 over 91000, gap 0.0737 (tolerance 0.1000)
```

The account line now reads against its target, there is no `exposure:` line, and `calendar:issued_on` is scored and no longer gated, since no entry shapes the invoice date any more.

## Draw a column and never write it

A date that a fact reads does not always belong in the table you release. A hidden column is drawn like any other, read by rules, links and `within` entries, and never written: it is in no stream, no header, no `outputDigest` and no sink ([ADR 0024](../adr/0024-dates-follow-their-parent-rows-lifetime.md) section 2). An emit block lists it under `hiddenColumns` as `{name, type}`, with the type written as the contract writes one, and the emit's `fidelity` block describes it as it describes any column. Here the customer gets a hidden `trial_ends` date:

```console
$ cp -r bundle hidden
$ python adjust.py hidden emits.customer.hiddenColumns='[{"name": "trial_ends", "type": "DATE"}]' emits.customer.fidelity.columns.trial_ends='{"range": {"min": "2023-01-01", "max": "2023-12-31"}}'
$ fluxsim run hidden --state-dir hidden.state --profile billing.profile.json
plan written to ...
parameters: ...
applied to ...
emitted customer: 200 rows (generator); account: 300 rows (generator); invoice: 6000 rows (generator)
run ... complete
$ python -c "import glob, json; g = json.load(open(glob.glob('hidden.state/runs/*/plan.json')[0]))['emits']['customer']['generator']; print(g['columns']); print(g['hiddenColumns'])"
['customer_id', 'segment', 'signed_up', 'closed_on', 'renewed_on']
['trial_ends']
```

The plan lists `trial_ends` apart from the written columns, and the rows hold only the five of the contract. A hidden column that no rule, link or `within` entry reads is drawn and dropped, so declare one only for a value that a fact needs.

`plan` refuses what it cannot draw, with `FLX-3105`: a `hiddenColumns` that is not a non-empty list of `{name, type}` mappings, an entry with another key, an empty or repeated name, a name that is a contract column's, a type that is not a FLUID logical type, an emit with hidden columns and no `fidelity` block, and the key under kernel 0.1.0. A fact of `verify` that reads a hidden column is listed as not scored, since the run's streams hold none.

## What is refused

`plan` refuses before a row exists.

- `FLX-3105`: a `lifetimes`, `within` or `rates` key, or an `exposure`, under kernel 0.1.0 or on a run without `spec.timeBounds`, a `rates` entry on a column that no fact sets, an `hourOfWeek` on a date column, or an `exposure` on a chain of relationships.
- `FLX-6204`: an enforced entry that the declared ranges can leave without a day, or two entries on one chain, one of whose targets can make the other unmeetable.
- `FLX-6203`: a `lifetimes` or `within` entry on a column that a history sets, which would be a second source for one value.
- `FLX-6208`: a dwell family whose `given` column is the end or depends on it.

[ADR 0024](../adr/0024-dates-follow-their-parent-rows-lifetime.md) and [ADR 0025](../adr/0025-dates-follow-seasonal-rates-dwell-times-and-exposure.md) give the rules each draw follows, and the dates paragraphs of the [seam and fidelity spec](../spec/SEAM-FIDELITY-SPEC.md) give the keys.
