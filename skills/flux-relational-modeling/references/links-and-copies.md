# Links, copies, sentinels, bridges and the codes

Read this when you declare a copy, a conditional link, a sentinel or a bridge, or when a plan refuses one. The record is [ADR 0022](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/adr/0022-rows-read-their-linked-rows.md); the installed engine's help and `fluxsim explain` are the authority for the installed version.

## What a link is

A relationship of the contract names, for each row of a child table, one row of its parent table: the row its foreign key takes. The engine generates the tables of a contract together, so every foreign key names a row its parent holds. A path such as `organisation.region` reads a column of that parent row, as the parent's own pass writes it: its draw, its dependencies and its rules.

Where a declaration lives:

- the contract (the owner's) holds the relationships, `sharedColumns`, `sharedAttribute` and a `derive` over a path;
- the bundle holds the module facts, in the Simulation's `extensions` under the namespace `com.agentics.flux/1` ([ADR 0018](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/adr/0018-module-facts-live-in-the-flux-bundle.md)): `copies`, `links` and `bridges`.

The namespace's keys are closed: a key nothing reads is refused. No page that the docs tests run shows `copies`, `links` or `bridges`, so write them only from the record's text, key for key, and say so to the user.

## Kernel

| Kernel | What happens |
|---|---|
| 0.2.0 (the default of a new plan) | copies generate where the Simulation declares `copies: true`; paths, conditional links, sentinels, sibling windows and bridges generate |
| 0.1.0 (a plan with `--kernel 0.1.0`, kept for replay) | `sharedColumns` and `sharedAttribute` stay declared and not generated; a rule that reads another row is `FLX-6210`; `links`, `bridges` and `copies` are `FLX-3105`, naming the kernel |

A run executes the kernel its plan names. To move a bundle to the newer kernel, plan again; never edit a recorded plan.

## Copies

A copy equals its parent's value on every row whose link reaches a row.

- `sharedColumns` on a relationship: the copy `derive: <column>`, `expr: <relationship>.<column>` on every row.
- `sharedAttribute` on a column: drawn on its owner, the member every other member reaches on exactly one path of relationships; the others copy it along that path. No owner, or two paths to it, is `FLX-6210`.
- A `derive` over a path in the contract, and a chain of to-one relationships in a FLUX-side `derive`, generate with NULLs first.
- A contract that lists `sharedColumns` or `sharedAttribute` keeps its old behaviour until the Simulation declares `copies: true`: the key is a new declaration, so a plan written before it replays to its digests. `copies` has one value, `true`; anything else is `FLX-3105`. `fluxsim init` writes the key only for a contract that declares one of the two.
- A copied column has no draw and no `nullFraction` of its own. With a `nullFraction` above zero its own NULLs break a copy that allows none, and the plan refuses it in every run with `FLX-6204`. A rule with a `rows` band that admits violations keeps its NULLs.
- A copy into a key or foreign key column is not generated, and a rule that determines one is `FLX-6210`.
- A copy through the inner of two relationships that set one column is `FLX-6210`.

A copy that the Simulation does not declare is a gap a strict plan names: `fluxsim plan bundle --strict-coverage` refuses it with `FLX-3305`, and the evidence pack lists it as not proven.

## Conditional links

The namespace key `links` lists one entry per relationship it configures: `{emit, relationship, when, sentinels, weight}`.

- `when` is a FLUID SQL/1 predicate over the emit's own columns. Where it is not TRUE the relationship's columns are NULL. Elsewhere the foreign key is what it drew. The pick is still drawn on every row, so no draw moves.
- A copy through a conditional link sees the NULL, because the step that clears the columns is ordered before the copy.
- A condition on an `inner` relationship, on one whose columns are a key or part of one, or on one another relationship implies, is `FLX-3403`.

## Sentinels

A `sentinels` map names a column the entry's relationship copies (a `sharedColumns` column of it, or a `derive` whose whole value is a path through it) and the text of its sentinel, read as the column's type. Where the link reaches no row, the copy writes the sentinel in place of NULL; a row whose own value is NULL keeps it. A sentinel that does not read as the column's type, or names a column the relationship does not copy, is `FLX-3105`.

## Sibling windows

A FLUX-side `derive` whose value holds a window over the rows that share a parent (the first row of an account) generates. The partition is the rows whose relationship columns are equal, rows with a NULL link forming one partition. A window whose value depends on ties needs a total order, or the plan is refused with `FLX-6108`: add the missing key columns to the end of its `ORDER BY`. Each process that writes a slice of such a table makes the whole table first, so the cost is one pass over it per process; the bytes are the same for any `--jobs` or `--shard`.

## Bridge pairs

`bridges` lists `{emit, relationships: [a, b]}`: two many-to-one relationships of one emit, laid out together as one key of two parts so no pair repeats, and each side names a row its parent holds. More rows than pairs, a `nullFraction` on a side, a relationship that is already distinct or part of a key, and a condition or a weight on either are `FLX-3403`.

## Codes

Run `fluxsim explain` on each before you act on it.

| Code | Meaning here |
|---|---|
| `FLX-3105` | the bundle does not validate: kernel 0.1.0 with a key of this page, `copies` with another value, a sentinel that is not the column's type |
| `FLX-3403` | the related tables cannot be generated as declared: a bad condition, bridge or weight |
| `FLX-6210` | a rule that determines a column in a way this version does not generate: no owner for a `sharedAttribute`, a copy into a key, a cycle among the tables rules read through |
| `FLX-6204` | a band this run cannot meet: a copied column with a `nullFraction` |
| `FLX-6108` | a window whose order is not total |
| `FLX-3305` | a declaration is accepted and not generated, and coverage is strict |

## What verify scores

`fluxsim verify` scores each declaration on the run's rows, as a line of its `rules`:

- a copy, as a rule of origin `shared` named `<relationship>.<column>`: its covered rows are those whose link reaches a row, and each holds when the column equals the source;
- a conditional link, as `link:<relationship>`: of the rows whose link reaches a row, the share on which its condition holds;
- a sentinel, as `sentinel:<relationship>.<column>`: of the rows whose path reaches no row, the share whose column is the sentinel;
- a bridge, as `bridge:<a>,<b>`: of every row, the share whose pair of parent keys occurs once.

All four are enforced, so one violating row fails the run. A copy the run did not make is reported `(not enforced)` and fails nothing, so read that mark before you tell the user a copy holds. Each key is also checked for integrity: each child key is found in its parent, parent keys are unique, and NULLs appear only where `joinType: left` allows them. The page that shows these lines is [score a run against a profile](https://github.com/Agenticstiger/flux-engine/blob/v0.13.1/docs/how-to/score-a-run-against-a-profile.md), and the `flux-run-and-verify` skill reads the results.

## When a check fails

Never loosen the declaration, widen a band or drop a check to get a run past a failure. Read the code with `fluxsim explain`, make the change its remedy names inside the bundle, and run `fluxsim validate bundle` and the plan again. A remedy that needs a change to the contract goes to its owner.
