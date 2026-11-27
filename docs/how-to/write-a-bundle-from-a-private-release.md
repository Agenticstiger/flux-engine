---
title: "Write a bundle from a private release"
description: "Write a bundle from a profile that the profiler released under differential privacy: how init reads noisy counts, histograms, histories, null counts and cliques, and how to declare a history yourself with init --history."
---

# Write a bundle from a private release

Use this page when the profile you hold is a private release: the profiler added noise to its statistics before it left the data owner. `fluxsim init` reads such a profile as it reads any other and takes each noisy statistic through its point estimate. Start from [Write a bundle from a profile](./write-a-bundle-from-a-profile.md), which writes the bundle step by step, and come here for what changes when a statistic is noisy. Section 1 is background: how `init` reads the members of a private release. Section 2 is the procedure: how to declare a history yourself.

You need a contract, a profile of format 1-2-0 or later that carries noisy statistics, and the command of the other page:

```text
fluxsim init bundle --contract contract.orders.fluid.yaml --profile orders.profile.json
```

Section 1 says how `init` reads a noisy release. Section 2 holds for any profile: it shows how to declare a history yourself when the profile publishes none.

## 1. Read a release under noise

A profile of format 1-2-0 can carry statistics that the profiler released under noise
([PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) section 3a). `fluxsim init` reads one of them as a
point estimate with a variance. The noisy counts can be negative or fractional, so one rule, in exact arithmetic, makes them a distribution. The counts are projected onto the non-negative vectors that add up to their noisy total. A vector that is non-negative already is only normalised. The shares are those counts over the total. The draw takes them as whole counts that add up to the estimated number of records. A table with no `rowCount` takes
its row count from the larger estimate of the records its noisy statistics give. The output
lists each noisy statistic with its mechanism, scale and the variance of each cell, and the
ledger in `--json` carries the same, so you can see how much noise stands behind a number. A
register citation of a noisy cell points at the profile's own `noisyCounts`.

A date or timestamp column's noisy histogram is read only where its counts stand clear of the noise. The floor is `sqrt(2v(5 + ceil(ln m)))` for Gaussian noise of variance `v` over `m` bins, and `b(5 + ceil(ln m))` for Laplace noise of scale `b` ([ADR 0019](../adr/0019-one-privacy-budget-per-profile-release.md), the second amendment of 2026-11-25). A count must be above it. A bin below that floor carries no weight, so noise in a grid's empty years draws no
dates there, and the run's window is the hull of the bins above it. When no bin is above it, which
is common for a release of a few hundred records, the column is drawn evenly within the run's
window, and the output says so. The column still declares the histogram's whole grid as its range,
and its histogram property still compares the run with the release.

A number column's noisy histogram is read the same way (the amendment of 2026-11-26). A bin below the floor carries no weight. A contract `derive` over the column is bounded by the hull of the bins above it. A column with no bin above it is drawn evenly over its declared range, with its note. A column the
contract makes a key keeps its bins as they stand, since its rows hold distinct values.

A table whose profile carries noisy cliques (section 5a of the spec) is drawn through a junction tree. The cliques are arranged into a tree rooted at the one with the most columns (the first by name among equals). Each column is drawn given the columns its clique shares with the one before it, so the chain of conditionals reproduces the cliques' joint. Each row of
weights is the point estimate of the clique's noisy counts for that combination of levels.
Cliques that cannot form a tree (the running intersection property fails) are refused, naming
them, and the levels a clique lists for a column must be the ones the contract declares. The
run is scored on the one- and two-way level histograms the cliques answer.

A release can also carry a noisy history, a noisy null count and a noisy query rate
(profile format 1-5-0, [PROFILE-SPEC.md](../spec/PROFILE-SPEC.md) section 3a). `init` reads each of
them through its point estimate, as it reads any noisy statistic:

- **A noisy `history` member.** A cell of the member that is inside the noise carries no
  weight. The test is the noise floor over all the member's cells. When no cell stands clear of
  the floor, `init` reads the member as withheld.
- **A noisy `nullCount/2`.** The statistic is a noisy count of a column's known and NULL
  rows. When the NULL cell stands clear of the noise, a nullable column takes the NULL share the
  estimate states. When it does not, the release cannot tell the count from none, so `init`
  draws no nulls and writes an assumption that names the interval the release allows. Such a
  column can still be one that another column is drawn given, a clique column or a tracked one.
- **A clique column.** A column that a clique draws holds no null. If the release states a NULL
  share for one, `init` sets that share aside and draws the cliques. The ledger names the
  share it set aside, and `--strict` refuses it as an unused statistic.
- **The request.** The profile request `init` writes beside a bundle asks for `history` on a
  table and for `nullCount/2` on a column (request format 1-3-0) only when the profile you gave
  is a private release. Every other request keeps its format and its bytes. The request names as
  `cliqueColumns` only the columns the contract requires, since a profiler releases an optional
  column over its known values, outside the cliques.

## 2. Declare the history yourself

A profile need not publish a history. With `--history FILE`, you declare the history of a
versioned table and its projections, and `init` merges them into the bundle in place of the ones
it would infer from the profile:

```text
fluxsim init bundle --contract contract.fluid.yaml --profile profile.json --history history.yml
```

`init --history` needs `--contract` and `--profile`, and exits 2 without either. The file is one YAML document: a mapping with these keys, and no others.

| Key | Holds |
|---|---|
| `history` | A non-empty list of history entries, as [give an entity a history](./give-an-entity-a-history.md) describes them. |
| `snapshots`, `events`, `periods` | Lists of the projections of the history, as [project a history](./project-a-history.md) describes them. |
| `asOf` | A list of as-of reads. Leave it out and `init` writes its own. |
| `timeBounds` | `start` and `end` of the run's time window. With `--window` too, `init` exits 2: give the window once. |

Each entry of `history`, `snapshots`, `events` and `periods` needs an `emit`, and each projection needs an `of`, the history it projects. `init` refuses an entry without them and names its position, as `history[0].emit`.

`init` writes the bundle to fit the file. It writes no counts entry over a projected emit or over the history's parent relationship. It writes no exact allotment on the columns of the history or of a projected emit. It writes no weight for the ended status on a column the history carries its end in. It
lists the profile's statistics for those as unused, with the reason. The numbers of the history
are assumed, for the reason "declared by --history", and cite nothing of the profile's history.

`init` refuses a file that cannot be read, holds more than one document, or holds a key not in the table. It also refuses an open end spelled as the year 9999 in `until`, a `start` bin or the `timeBounds` end. The message names the file and the position. When `plan` still refuses the
bundle, `init` says so, names the file and writes nothing. `init --json` carries the file's base name
as `historyFile`, with no directory.
