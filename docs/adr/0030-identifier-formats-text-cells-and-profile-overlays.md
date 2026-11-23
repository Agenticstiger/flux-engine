# ADR 0030: identifiers follow their format, text cells come from a pinned Faker, and each profile overlay makes one bundle

## Status

Proposed 2026-11-22, for review. Kernel 0.2.0 only. Every part is opt-in: a bundle that declares
none of the new keys `identifiers`, `text` and `overlay` keeps every byte and every digest it had,
under both kernels. Sections 1 to 6, the identifiers and the text cells, change the generator, so
they are built once the release's other changes to the generator (temporal integrity, exact counts
and versioned entities) have merged, on the generator as it then stands. Sections 7 and 8 do not
wait.

## Context

**Identifier formats.** FLUID 0.7.7 lets a string column declare `identifierFormat`, a list of
alternatives, each a list of literal `prefixes` and a COBOL PICTURE style `mask` (`9` a digit,
`A` an upper-case letter, `X` either, a backslash for a literal). The data rules RFC behind it
(section 3) says an alternative is enumerable, that FLUX's keyed Feistel permutation will decode an
ordinal into the mask's mixed radix so that every value is unique by construction, and that prefix
weights and prefixes taken from a linked row are FLUX settings. The engine has never read the key:
an identifier column is 32 hex digits derived from `H` and the row (KERNEL-SPEC-2 section 6.1),
whatever format its contract declares.

**Text.** A string column with no published levels has no plausible values. The maintainers decided
on 2026-11-20 that plausible text comes from Faker, in a `[text]` extra pinned exactly, with the
pin bound into the run and a run under another Faker version refused. That replaces the record of
2026-11-16 that Faker is never a dependency because its seeded output changes between versions.

**Variants.** A team that asks "what if this table doubles" today edits a bundle by hand, and the
register then cites the profile for a number the profile does not hold. It needs one bundle per
variant, a register that names the variant behind each changed number, and assumptions that say
which statistic would replace them.

Borrow-before-build (2026-11-22):

- **Faker.** PyPI's JSON for `faker`: 40.54.0, released 2026-11-10, MIT, Python 3.10 or later, no
  dependency but `tzdata` on Windows; the latest is 40.63.0 (2026-11-20). Search "Faker
  seed_instance reproducibility results not guaranteed across versions pin patch version": Faker's
  documentation says seeded results are not consistent across patch versions and asks that a test
  pin Faker to the patch, and a maintainer's answer in its issue tracker says provider data changes
  at nearly every release. Checked by running Faker 40.54.0 on Python 3.13, a fresh
  `random.Random` subclass per value, under nine locales: the formatters of section 5 call only
  `choice`, `choices`, `randint` and `randrange` of the generator's `random`, give the same value
  for one seed in any order, and give the same values under two `PYTHONHASHSEED` values, except
  `city` under `it_IT`, which does not. Of the others, `license_plate` and `md5` also call
  `random`, `uuid4` calls `getrandbits`, and the `ssn` providers call `sample` and `uniform`. The `color` provider, `ssn` providers, the `en_US`
  passport and the `uk_UA` licence plate call the module-level `random`; the `date_time`, `ssn`
  and `passport` providers and one `company` provider read the clock.
- **Python's `random`.** Fetched https://docs.python.org/3/library/random.html, "Notes on
  Reproducibility": most of the module's algorithms and seeders may change across Python
  versions; only `random()` under a compatible seeder is guaranteed to repeat. So a value that
  `choice` or `randrange` makes from a seeded Mersenne Twister is not promised across versions.
- **RFC 7396.** Fetched https://www.rfc-editor.org/rfc/rfc7396.txt (JSON Merge Patch, October
  2014, obsoletes RFC 7386). Its `MergePatch` function: an object patches member by member, `null`
  removes a member, anything else, a list included, replaces the target whole. The function is
  twelve lines, so it is written from the RFC; `json-merge-patch` 0.3.0 (BSD) on PyPI is not
  needed.
- **The engine itself.** `permute/2` (KERNEL-SPEC-2 section 7), the identifier keys of section
  6.1, the rule stream of EXPR-SPEC-2 (`<row path> + ("rule", R)`), the parameter register
  (PARAMETER-REGISTER-SPEC, format 1-2-0) and the author ledger of ADR 0014.

## Decision

### 1. A column follows its format only under `identifiers`

The engine reads a column's `identifierFormat` only when the Simulation lists the column under a
new top-level key of its namespace:

```yaml
extensions:
  com.agentics.flux/1:
    identifiers:
      towers:
        tower_id:
          shares: {"MSC99999": "0.6", "TWR99999": "0.3", "LEGACY-A9-99AA": "0.1"}
      antennas:
        antenna_id:
          prefixFrom: tower.region_code
          prefixMap: {north: MSC, south: TWR}
```

- A listed column is an identifier column of its expose, as `generator/families.py` decides one (a
  one-column string key, a `unique` statistic, an identifier `semanticType` or tag), it declares
  `identifierFormat`, and it is not a foreign key: a foreign key copies its parent's values, so it
  carries the parent's format already. Anything else is refused by `validate` with `FLX-3105`.
- A column whose contract declares a format and that the Simulation does not list generates as
  before, and `verify` reports its format as declared and not generated, as it reports a copy
  without `copies: true`. Reading the format without the new key would change the bytes of a bundle
  written before, which KERNEL-SPEC-2 section 12 does not allow.
- `fluxsim init` writes the key for every identifier column whose contract declares a format, with
  its shares (section 4).
- The key's entry for an expose is bound into that expose's `H`, and only when declared. Kernel
  0.1.0 refuses the key with `FLX-3105`, naming the kernel.

**Pattern labels.** Each alternative expands to one pattern per prefix. A pattern's label is its
prefix written in mask syntax (a backslash before each `9`, `A`, `X` and backslash of the prefix),
then its mask: `MSC99999`, `LEGACY-A9-99AA`, and `\A1999` for the prefix `A1` before the mask `999`.
Two patterns of one format never share a label, because two that did would overlap, which FLUID
refuses; `plan` checks the RFC's overlap rule again.

### 2. Unique by construction

A pattern holds `D = 10^d * 26^a * 36^x` values for a mask of `d` nines, `a` letters and `x`
alphanumerics. A number `v < D` decodes to the mask in mixed radix, the last symbol least
significant: a `9` takes `0` to `9`, an `A` takes `A` to `Z`, an `X` takes `0` to `9` then `A` to
`Z`, and a literal stands for itself.

For an expose of `n` rows (after `--scale`; each `--shard` takes its own rows of the same `n`):

1. Each pattern `g` gets a count `c_g`, the largest-remainder rounding of `n * share_g` (PROFILE-SPEC
   section 3a), ties to the earlier pattern.
2. Row `i` takes the position `j = permute2(K_rows, n, i)`. Its pattern is the one whose run of
   positions, in pattern order, holds `j`, and `r = j - start_g` is its rank there.
3. Its value is the pattern's prefix, then `decode(permute2(K_g, D_g, r))`.

Both maps are permutations, so no two rows share a value. `plan` refuses, with a new code of the
generator's family, a pattern whose count exceeds its `D`, and a pattern whose `D` exceeds `2^256`,
the domain `permute/2` holds. The keys follow section 6.1's derivation, `sha256(b"flux-gen/2" + NUL +
H_bytes + NUL + key_label(path))[0:16]`, on the rule stream's shape `("sim", sim_id, "gen",
exposeId, "row", rowIndex, "rule", R)`, with `R` the canonical JSON of `["format", <column>,
"rows"]` for `K_rows` and of `["format", <column>, "pattern", <label>]` for `K_g`. No contract or
bundle rule's `R` starts with `"format"`, so the addresses are new. They read `H` and never the seed:
a formatted identifier is derived from `(H, ordinal)` as every identifier is, and stays fixed
across a sweep. They are keys, so nothing is drawn. `tests/vectors/kernel2-streams-vectors.json`
gains both labels' keys and `permute/2` images under them, with decoded values of each mask symbol,
so an implementation is held to them (KERNEL-SPEC-2 section 12, rule 4).

`shares` holds one exact decimal per pattern label, adding up to one. A pattern with a share of zero
takes no row. A format with one pattern may leave `shares` out.

### 3. A prefix that follows the linked row

`prefixFrom: <relationship>.<column>` takes each row's prefix from the row its link names (ADR
0022's reading), in place of `shares`:

- When the linked column has a format, the row takes the prefix of the linked value. When it has
  none, the linked value must be one of the row's prefixes, or `prefixMap` maps it to one. `plan`
  refuses a declared level of the linked column that maps to no prefix, a prefix that more than one
  pattern of the row's format starts with, and a relationship whose link may be NULL.
- The suffix is `decode(permute2(K_g, D_g, i))`, from the row's own ordinal `i`, so the rows of one
  pattern never share a value. Its capacity is therefore one mask, not section 2's sum: the counts
  per prefix are known only once the links are drawn. `plan` refuses a pattern whose `D` is below
  `n`.
- **What moves with the seed.** A link is drawn, so an identifier under `prefixFrom` moves with its
  link, and so with the seed: across a sweep its prefix can change, while the part after the prefix
  still reads `(H, ordinal)` alone. That relaxes, for the columns that declare `prefixFrom` and for no
  other, the rule that an identifier is derived from `(H, ordinal)` and never drawn (SEAM-FIDELITY-SPEC
  section 1). Every other identifier, formatted or not, keeps it.
- **A foreign key to a formatted key.** A foreign key holds its parent's key as the parent's own pass
  writes it, read from the parent's final row (ADR 0022). A parent key under `shares` is section 2's
  value of the parent's ordinal. A parent key under `prefixFrom` is the prefix its own linked row gives,
  then section 3's suffix, so a child reads two rows: its parent's, then the row that parent's prefix
  comes from. A chain of such reads that would loop is refused, as ADR 0022 refuses a cycle.

### 4. Shapes in the profile, the request and the scorecard

- **Profile format 1-3-0** gains `shapeHistogram/1` for a column with a format: its levels are the
  pattern labels in the order the format expands them, and its counts follow `levelHistogram/1`'s
  rules, rounding, disclosure and `noise` included, so a noisy one keeps a run P3 as a noisy
  `levelHistogram/1` does (ADR 0020). A value's shape is its pattern; a placeholder, and a value of no
  pattern, counts in no level. It publishes shapes and never a value. Publishing it is the
  profiler's work; the engine reads it and verifies against it.
- **Shared versions.** Profile format 1-3-0, profile request format 1-1-0 and scorecard format 1-4-0
  are the release's one new version of each format, which its other changes extend too: whichever
  lands first defines each, and the others extend it.
- **Profile request format 1-1-0** lets a request ask for `shapeHistogram/1`, and `fluxsim init`
  asks for it for every column with a format.
- **Shares.** `fluxsim init` writes `shares` from the profile's `shapeHistogram/1` when it has one,
  and the register cites it. Otherwise the shares are equal, registered as a `default` that the
  statistic would replace (section 9).
- **Verify.** Every value of a listed column matches its format, by `re.fullmatch` against the
  anchored pattern the RFC compiles, with its placeholders exempt; a value that does not is a
  conformance failure. When the profile has the column's `shapeHistogram/1`, **scorecard format
  1-4-0** scores a `shapeHistogram` property, `1 - TV` over the pattern levels, in the `marginals`
  dimension.

### 5. Text cells under `text`

```yaml
extensions:
  com.agentics.flux/1:
    text:
      faker: "40.54.0"
      columns:
        subscriber:
          full_name: {formatter: name, locales: {en_GB: "0.7", de_DE: "0.3"}}
          street: {formatter: street_address, locales: {en_GB: "1"}}
```

- **The pin.** `faker` is the exact Faker version the bundle was written for. It is a declaration,
  so `inputDigest` covers it (ADR 0006) and the expose's `H` binds it with its columns. `validate`,
  `plan`, `apply`, `replay` and `verify` refuse to run, with a new code, when the installed Faker is
  another version, naming both, and name the `[text]` extra when Faker is not installed. The
  inputDigest document gains no member: the declaration is the pin, and the refusal makes the pin
  the version that ran. The engine reads the version twice, from the installed distribution
  (`importlib.metadata.version("faker")`) and from `faker.VERSION`, and both must equal the pin. Faker's
  module is held to ADR 0032 section 3's origin rule: its origin lies under the installation directory
  that holds its metadata, and is listed in its `RECORD`. The `[text]` extra installs `faker==40.54.0`.
- **The columns.** A listed column is a string column of its expose that is not a key, an
  identifier, a foreign key, a copy, a `given` column or the target of a `derive`, and the text
  stream draws it in place of its default. A column the Simulation does not list draws as before.
  `fluxsim init` writes no `text` entry: a person or the authoring skill does.
- **The formatters.** `formatter` names one of the engine's text formatters: Faker formatters that
  take no argument, read neither the clock nor the module-level `random`, return a string, and give
  the same values under two `PYTHONHASHSEED` values under every locale the table allows them in. The
  first table: `name`, `first_name`, `last_name`, `prefix`, `suffix`, `company`, `company_suffix`,
  `job`, `catch_phrase`, `bs`, `street_address`, `street_name`, `building_number`, `city`,
  `postcode`, `country`, `address`, `phone_number`, `safe_email`, `user_name`, `url`, `word`,
  `sentence`, `paragraph` and `text`. Left out: `email` and `free_email`, which name real mail
  providers; dates, identity numbers, passwords and hashes, which rest on the clock or a real
  scheme; identifiers, which section 1 makes; and `city` under `it_IT`.
- **The locales.** `locales` holds one exact decimal share per locale, adding up to one, each a
  locale the table allows for the formatter.
- **NULLs.** A column's `nullFraction` draws on the cell's own path at its usual index, as for every
  column, and a NULL cell draws nothing on its text stream.
- **What it states.** The column's provenance is `text`. `EVIDENCE.md` names its formatter, its
  locales and the Faker version, and says that its values are not drawn from the profile; `verify`
  scores no profile statistic of it and its coverage says so.

### 6. The text stream and its random source

- Each text cell draws on the rule stream of EXPR-SPEC-2, `("sim", sim_id, "gen", exposeId, "row",
  rowIndex, "rule", R)`, with `R` the canonical JSON of `["text", <column>]`. No contract or bundle
  rule's `R` starts with `"text"`, so the address is new (KERNEL-SPEC-2 section 12, rule 2). Under
  kernel 0.2.0 the stream is keyed with the row emptied and counted by the row, so a cell's draws
  depend on its row and nothing else, and any `--jobs` or `--shard` gives the same bytes.
- **Position 0** draws the locale, `categorical` over the shares by KERNEL-SPEC section 4's rule on
  `uniform53`, when the column has two locales or more.
- **Positions from 1** serve Faker. The engine sets the cell's locale generator's `random` (Faker's
  `Generator.random` setter) to a `random.Random` subclass of its own, `TextRandom`, whose methods
  each take the next position, one position per value drawn:
  - `random()`: `uniform53 / 2^53`;
  - `getrandbits(k)`, for `k` up to 256: the first `k` bits of the block, `w0` first, most
    significant bit first;
  - `randrange`, `randint` and `choice`: `bounded(m)` of KERNEL-SPEC-2 section 4 over the range's
    `m` values, its attempts on the position's attempt counter;
  - `choices(population, weights, cum_weights, k)`: `k` positions; without weights,
    `bounded(len(population))`; with them, each binary64 weight read as the exact rational it is,
    the running sums exact, and the element the first whose sum exceeds `uniform53 * total / 2^53`,
    compared exactly;
  - every other method raises, and the cell is refused; a formatter that calls one is not in the
    table. `TextRandom` overrides every method it inherits, the private ones included: `__init__`
    (because `Random.__init__` calls `seed`), `seed`, `getstate`, `setstate`, `_randbelow`, `shuffle`,
    `sample`, `uniform`, `triangular`, `randbytes`, `gauss` and every other distribution. A test lists
    `random.Random`'s attributes and fails on one that is neither defined above nor refusing.
- **The module-level `random`** is not patched while a cell is drawn. The engine is also a library
  (`flux_engine.api`), and patching the standard library would reach the program that embeds it. The
  table is the guard: CI draws every formatter and locale it allows with the module-level functions
  patched to fail, so a pinned Faker that reached them would fail its vectors before a release.
- So a cell's text depends on Faker's code and data, which the pin fixes, and on the engine's words,
  which the vectors fix, and never on the Python version, whose `random` promises only `random()`
  across versions. `tests/vectors/text-vectors.json` holds the stream's key, counters and blocks
  and, for each formatter and locale the table allows, the values of the first rows of a fixed
  stream, checked in CI under two `PYTHONHASHSEED` values with the module-level `random` patched to
  fail.
- A cell that makes more than 4,096 calls is refused. A value is written as Faker returns it. A
  value that is not a string, holds a lone surrogate, or fails its column's own checks (its type,
  its length bounds, its `validationRules` and its enum) is refused, naming the row, at `plan` when it
  stages and at `apply` otherwise, and nothing is repaired.

### 7. Profile overlays

An overlay is a named RFC 7396 merge patch over the profile the Simulation pins:

```yaml
extensions:
  com.agentics.flux/1:
    overlay:
      name: double-churn
      patch:
        tables:
          subscriber: {rowCount: 2400}
```

- **One bundle each.** `fluxsim init --contract C --profile P --overlay FILE`, repeatable, writes one
  bundle per overlay, each in a directory named after it, with the overlay in its Simulation. The
  file holds `name` (`[a-z][a-z0-9-]*`, at most 63 characters) and `patch`, read with the namespace's
  exact reader. Without `--overlay`, `init` writes what it wrote before.
- **What a patch may touch.** Only numbers a profile publishes: a table's `rowCount`, a column's
  `metrics` and `statistics`, a table's `columnPairs` and `periods`, and the top-level
  `relationships` and `invariants`. A patch that touches anything else (`_schemaURL`, `encoding`,
  `disclosureRules`, `privacy`, `publicStructure`, `references`, a column's `domain`) is refused. A
  patch replaces a list whole, so an overlay restates a column's `statistics` to change one of them,
  and a statistic it restates carries no `noise`.
- **The overlaid profile** is `MergePatch(profile, patch)`, read by flux-stats' profile reader at the
  pinned profile's format, so every bound and exactness rule of a profile holds for it.
- **The grade** reads the pinned profile and never the overlaid one (ADR 0020): an overlay's numbers
  are written by hand, as an assumption's are, and the report names them.
- **Plan** checks the register's `profile` citations against the pinned profile and its `overlay`
  citations against the overlaid one. A `profile` citation of a number the patch changed is
  refused, so every number the overlay changed names it.
- **Verify** scores against the overlaid profile, and scorecard format 1-4-0 names the overlay in a
  top-level `overlay` member, its name and digest.
- **Digests.** Each expose's `H` gains an `overlay` member, the `jcs-document` digest of `{name,
  patch}`, only when the Simulation declares one. Kernel 0.1.0 refuses the key with `FLX-3105`.

### 8. The register names the overlay, and what would replace an assumption

Parameter register format 1-3-0 adds:

- **`source: overlay`**, with `overlay`, the overlay's name, and `statistic`, a citation into the
  overlaid profile in the form a `profile` citation takes.
- **`replacedBy`** on an `assumption` or `default` entry: `{pointer, definition}`, the statistic a
  profile of the contract would publish for that number. `fluxsim init` writes it for every number
  it assumes. The ledger lists them, and `init` writes `<name>.assumptions.profile-request.yml`
  beside the bundle, a profile request (PROFILE-REQUEST-SPEC, in its current format) for exactly
  those statistics. Running `init` again with a profile that answers it turns each of those
  assumptions into a profile citation.
- Format 1-3-0 is shared with the consumer questions' change of the same release, which adds
  `source: calibrated` and a `calibration` member: whichever lands first defines 1-3-0, and the other
  extends it. A register that uses none of 1-3-0's additions stays at format 1-2-0, with its bytes.

### 9. What changes, in one place

| What | Change |
|---|---|
| namespace | new top-level keys `identifiers`, `text`, `overlay`; each bound into `H` only when declared; kernel 0.1.0 refuses each with `FLX-3105` |
| seed tree | rule-stream labels `["text", <column>]` (drawn) and `["format", <column>, ...]` (keys only); no existing path moves |
| profile 1-3-0 | `shapeHistogram/1`, read and verified here, published by the profiler (shared within the release) |
| profile request 1-1-0 | a request may ask for `shapeHistogram/1` (shared within the release) |
| scorecard 1-4-0 | the `shapeHistogram` property and the top-level `overlay` member (shared within the release) |
| parameter register 1-3-0 | `source: overlay` and `replacedBy` (shared within the release) |
| extras | `[text]`: `faker==40.54.0` |
| not changed | `kernelVersion`, the inputDigest document's members, every existing seed path, every golden |

### 10. Tests this record asks for

- A bundle that declares none of the three keys: a golden computed with the code before this change pins
  its digests, under both kernels.
- For each construct, a formatted key under `shares`, a key under `prefixFrom` and a text column: the
  column path equals the per-row path; `--jobs 1` equals `--jobs 4`; `--scale` below 1 and above it; and
  each `--shard I/N` against the whole run.
- Uniqueness at a pattern's capacity, and the refusal one row past it.
- The pin: a refusal when the distribution's version or `faker.VERSION` differs, and when Faker's origin
  fails the rule.
- `TextRandom`: every inherited attribute defined or refusing; the text vectors under two
  `PYTHONHASHSEED` values with the module-level `random` patched to fail.
- Overlays: each refused member, the grade unchanged, and a `profile` citation of a changed number
  refused.

## Consequences

- A bundle that declares none of the three keys has the bytes and digests it had. A golden computed
  with the code before this change pins that, under both kernels.
- A formatted identifier column is unique by construction, with the row count the masks can hold:
  the sum of the masks under shares, one mask under a linked prefix. A table larger than that is
  refused at `plan` rather than given duplicate keys.
- Text bytes rest on Faker's code and data. Moving the `[text]` pin moves no existing bundle's
  bytes: a bundle that pins the old version is refused until that version is installed, and a
  bundle written for the new one is a new bundle. A pin move still carries a
  `GOLDEN-CHANGELOG.md` row for the text vectors.
- Text cells are drawn in Python, one Faker call per value. A large table of text is slower than a
  table of levels, and `--jobs` splits it as it splits any column.
- An overlay never raises or lowers a grade. A run from a P3 profile with an overlay is P3, and its
  report says which numbers the overlay wrote, as ADR 0020 says of hand-written assumptions.

## What would change this decision

- Faker removing its `Generator.random` setter, or a formatter in the table starting to call a
  method `TextRandom` does not define: the table shrinks, or the pin stays.
- FLUID adding repeat counts or lower-case symbols to masks: the decode gains them, under a new key.
- Profiles publishing pattern counts per linked row: a linked prefix could then use section 2's
  allocation and its full capacity.
