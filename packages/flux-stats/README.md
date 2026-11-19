# flux-stats

The statistics of FLUX, as a package of their own. Every statistic the fidelity check of the FLUX Simulation Engine (`flux-sim-engine`) compares has one definition here. A profiler that runs where the data lives imports this package to form the same numbers, without installing the engine.

It holds these modules:

- `flux_stats.registry` and `flux_stats.definitions`: one definition per statistic (`mean/1`, `marginalHistogram/1`, `levelHistogram/1`, `support/1`, `correlation/1` and `quantile/1`), and a second one for a quantile that declares bins (`quantile/2`, which publishes the bin that holds the order statistic and not the statistic itself). `definition_for_property` picks a declared property's definition. Each definition names the quantities it keeps, how it forms them from values on the grid, how they are spelled and how two of them compare.
- `flux_stats.edge_counts`: the output checks for the counts a profile publishes between a column's edges: histograms over bins or levels, `quantile/2` and `quantile/1` (ADR 0012). `check_edge_counts` decides which groups and cells a profiler publishes, spells each `F` or `C` with the reason a withheld one is withheld for the profiler's report, and rounds a published quantile group's counts with the profiler's own rounding, so a profiler implements no rule of its own.
- `flux_stats.column_metrics`: the seven column metrics a profile carries.
- `flux_stats.spelling`: the exact decimal and rational spellings those documents use for numbers.
- `flux_stats.reading`: how the fidelity check reads a record, so that a profiler reads it the same way. A number becomes the exact rational it spells, an RFC 3339 instant integer milliseconds, a category its NFC string. It applies each column's null policy, forms the groups, and refuses with `FLX-5005`, `FLX-5022` or `FLX-5031` what it cannot read.
- `flux_stats.fidelity`: the grammar of a Simulation's fidelity block. `parse_fidelity_block` reads the declared block against the expose it names and refuses what the specification refuses with `FLX-5030` to `FLX-5034`, so a profiler reads the declaration with the parser the check uses. `measure_group` forms one group of one declared property from rows, the way the check does.
- `flux_stats.profile`: the reader of the profile document ([PROFILE-SPEC](https://github.com/Agenticstiger/flux-engine/blob/main/docs/spec/PROFILE-SPEC.md)). It reads a profile whole, or refuses it with `FLX-5036`.
- `flux_stats.fixedpoint`: the fixed-point grid every statistic is formed and compared on. Every comparison is an exact integer inequality ([KERNEL-SPEC](https://github.com/Agenticstiger/flux-engine/blob/main/docs/spec/KERNEL-SPEC.md) §12).
- `flux_stats.tails`: exact decisions about binomial tails, such as whether an empty cell or a count is too unlikely under a share, decided from directed-rounding bounds and, where those cannot tell, the exact `Fraction`.
- `flux_stats.sampling`: estimates from a sample of whole blocks of records, the one-stage cluster ratio estimator with a 95 % interval, every number exact and every bound widened, never narrowed.
- `flux_stats.integers`: a test for a count, an ordinal or a size: an `int`, never a `bool`, and not below a floor.
- `flux_stats.canonical`: the canonical form a FLUX digest is taken over (KERNEL-SPEC §2.1). Every string is NFC, every mapping key a coerced string, every mapping sorted, every `Decimal` its string spelling, and no float is allowed. The engine serialises it with RFC 8785 and hashes it, so a reader without the engine forms the very structure the engine hashes.
- `flux_stats.exact_yaml`, with the `yaml` extra: the exact read of a FLUX bundle's YAML text. A float is the `Decimal` of its source text and a timestamp the text written. The read is bounded: the values and the characters a document writes out once its aliases and merge keys are written out, its nesting, the mappings its merge keys name, the digits a number needs, and the pairs merge keys copy in one read. Past a bound it refuses with `FLX-3105`, naming the file and the line. It reads text and opens no file.
- `flux_stats.errors`: the `FLX-nnnn` code catalog its refusals are numbered in (KERNEL-SPEC §13).

The package depends on the Python standard library alone and runs on Python 3.10 or newer. The one exception is `flux_stats.exact_yaml`, which imports PyYAML when it first reads a document. It reads no clock, draws no randomness and opens no connection. Its tests import it in a fresh interpreter and check that nothing else loads.

## Install

```text
pip install flux-stats
```

To read a bundle's YAML with `flux_stats.exact_yaml`, install the `yaml` extra, which adds PyYAML:

```text
pip install 'flux-stats[yaml]'
```

Without the extra it has no dependencies. Releases start with the first flux-sim-engine release that depends on flux-stats; the 0.0.1 on PyPI only reserves the name and holds no code. Before that release, install it from a checkout of this repository with `pip install ./packages/flux-stats`.

## Use

Form a statistic over values already on the grid, and compare two of them under a declared tolerance:

```python
>>> from flux_stats import Check, Parameters, definition_for_kind
>>> mean = definition_for_kind("mean")
>>> mean.identifier
'mean/1'
>>> reference = mean.measure("", [20, 20], Parameters())
>>> synthetic = mean.measure("", [21, 21], Parameters())
>>> (reference.k, reference.sum_x, synthetic.sum_x)
(2, 40, 42)
>>> mean.compare(synthetic, reference, Check(subject="p0", tolerance="absolute", margin_units=1))
True
```

Read a profile document, from the root of this repository:

```python
>>> from pathlib import Path
>>> from flux_stats.profile import load_profile_text
>>> text = Path("docs/spec/profile-example.json").read_text(encoding="utf-8")
>>> profile, document = load_profile_text(text)
>>> sorted(profile.tables)
['payment', 'subscriber']
>>> profile.tables["subscriber"].row_count
1200
```

Read values the way the fidelity check reads them:

```python
>>> from flux_stats.reading import exact, nfc, parse_rfc3339_ms
>>> exact("1.25", "column 'amount' row 0")
Fraction(5, 4)
>>> parse_rfc3339_ms("2024-02-29T12:00:00Z")
1709208000000
>>> nfc("e\u0301") == "\u00e9"
True
```

`tests/test_readme.py` runs all three blocks.

## Known limits

Two limits of the record reader remain (ADR 0009, amendment of 2026-11-17):

- `nfc` uses the running Python's Unicode database, so a string holding a character newer than one Python knows can be normalised differently on two Pythons.
- A number is read when it needs at most 1000 digits written out, a leading `0.` not counted (`digits_written_out`), the bound a fidelity block's numbers have. A profile document's plain decimal counts that `0` too (the pattern in `profile-1.schema.json`, which `flux_stats.spelling` applies), so a value at the very edge, such as `1E-1000`, is read but cannot be published as a minimum or a maximum, and a sum of values near the bound can need more digits than a document allows. Either way the document is refused whole and nothing is written. The length of the text a cell arrives as is the caller's to bound: reading a cell costs time and memory in proportion to it.

## Versions

flux-stats is released with flux-sim-engine, from the same tag and at the same version, and the engine depends on exactly that version. A profile formed with one release is read and compared by the same definitions in the engine of that release.

## Development

The package lives in the flux-engine repository, as a member of its uv workspace. From the repository root, `make test-stats-alone` builds this package, installs the wheel into a clean environment that has no flux-sim-engine, and runs `packages/flux-stats/tests` there. The engine's own test run collects the same tests.

## License

Apache-2.0. See [LICENSE](https://github.com/Agenticstiger/flux-engine/blob/main/packages/flux-stats/LICENSE) and [NOTICE](https://github.com/Agenticstiger/flux-engine/blob/main/packages/flux-stats/NOTICE).
