# Changelog

What changed in the **flux-sim-engine package**, release by release.

This is not `GOLDEN-CHANGELOG.md`, which sits beside it at the repository
root and records something else entirely: every deliberate move of a pinned
digest or golden baseline. The two are easy to confuse and answer different
questions. Ask `GOLDEN-CHANGELOG.md` why a digest changed. Ask this file what
a given released version contains.

`scripts/release_guard.py` refuses a tag whose version has no `## X.Y.Z`
section here, so the entry lands in the same pull request as the version
bump and is reviewed like any other change. See `docs/RELEASING.md`.

Versions here are **package** versions. They are not `KERNEL_VERSION`,
`SEED_TREE_VERSION` or `TRACE_VERSION`, which version normative engine
behaviour, are hashed into `inputDigest`, and never move as part of cutting
a release.

Headings follow `## X.Y.Z - YYYY-MM-DD`.

## 0.2.0 - 2026-11-11

The command is now `fluxsim`, the distribution is now `flux-sim-engine`, and
the licence is Apache-2.0. The engine also gets documentation and an accuracy
program, which found four defects; all four are fixed here. No pinned digest
moved: `GOLDEN-CHANGELOG.md` has no new row, and `KERNEL_VERSION`,
`SEED_TREE_VERSION` and `TRACE_VERSION` keep their values.

- Renamed the distribution from `flux-simulation-engine` to `flux-sim-engine`.
  The import package stays `flux_engine`, and the product keeps its name, the
  FLUX Simulation Engine.
- Changed the licence to Apache-2.0, declared with PEP 639 metadata (`license`
  and `license-files`). `LICENSE`, `NOTICE` and the notice of the vendored
  `flux_spec` copy go into the wheel and the sdist.
- The engine now depends on `flux-spec>=0.5,<0.6` from PyPI instead of a pinned
  git commit, so the package can be installed from an index. The pin-drift
  alert in CI went with the pin.
- Renamed the command from `flux` to `fluxsim`, with no alias. `flux` is the
  command line of Flux CD, and the two collide on a shared `PATH`. The console
  script, `--help`, error messages, the docs and the examples all say
  `fluxsim`. The `FLUX_*` environment variables, the `.flux/` state directory
  and the `flux_engine` package keep their names: they name the engine and the
  specification, not the command. Nothing digest-bearing changed, so no golden
  moved. Text that names the command moved with it, so a host that matches on
  it should expect that: the `stage` of a `--json` envelope for a usage error
  at the top level, before any subcommand, is now `fluxsim`; the
  `error.message` of a usage error starts `fluxsim ...:` instead of
  `flux ...:`; the remediation text in error messages and in `fluxsim explain`
  entries says `fluxsim` where it said `flux`; and so do the `help` strings in
  the command tree of `fluxsim describe --self --json`. The `FLX-nnnn` codes
  themselves did not change.
- Added `fluxsim init <dir>`. It writes a small FLUX bundle (nine documents)
  and its FLUID contract into a directory that is missing or empty, and never
  overwrites anything, so a first run needs no sibling checkout of the
  specification. `fluxsim run . --verify --ship -o out` inside it gives a
  verified run and an evidence pack in seconds. The starter ships as package
  data (`flux_engine/starter/`), and its result is pinned by
  `tests/starter/test_starter_bundle.py`.
- Added `make first-run` and a `First run` workflow. They build the wheel and
  run that path through `uvx` from outside the checkout, with an empty uv
  cache, then check the evidence pack and record the time to first success.
  Linux runs on every pull request; macOS runs weekly. The job is advisory: it
  is not one of the jobs `CI gate` waits for.
  `make packaging-smoke` now also runs `init` and the run from the installed
  wheel.
- Fixed the evidence report naming no bundle when the bundle was given as `.`:
  it began `# Evidence: ` with a blank where the name goes.
- Fixed `-o` pointing at a directory inside the bundle (`fluxsim run . --ship
  -o out` from the bundle directory): the next archive packed the previous
  one, and every re-run grew it. The output directory is left out of the
  staged copy.
- Removed the `calibrate` and `synth` extras. Nothing in the engine imported
  what they installed, except that the default experiment readout used `gbstats`
  whenever it happened to be installed, so the same run could read out
  differently on two machines. `GbstatsReadout` is gone. The readout is now
  always the exact two-proportion interval, with no switch that depends on which
  packages are installed.
- Fixed: `accuracy score` read a registered interval bound of the JSON
  number `0` (or `0.0`) as no bound, so the prediction was scored
  `not-scored` instead of `within-interval` or `outside-interval`. It
  now reads the bounds the way `accuracy register` does.
- Fixed: `accuracy record` published, for every prediction with an
  interval, a digest that was not the `sealDigest` `accuracy register`
  returned, so the record could not be matched to its seals. It now
  rebuilds each prediction with its interval before digesting it.
- Fixed: a run read the caller's ambient `decimal` context in three
  places (the arrival gap rate, a scalar Persona trait's quantisation
  and the equal Persona share). A library caller at a precision other
  than 28 got different events and a run digest that did not replay,
  and at precision 5 the run raised. Those sites now use the kernel's
  pinned context, and `stages.pipeline.execute` runs the whole pipeline
  inside it. Under the default context nothing moves.
- Fixed: a draw whose value or bounds need more digits than the kernel's
  50-digit working precision at the declared scale (for example a
  Persona trait `{dist: normal, stddev: 1.25e43}`) raised a bare
  `decimal.InvalidOperation`. It is now refused with `FLX-5021`.
- Reworked `--help` after clig.dev. Examples come first, printed verbatim (a
  long one continues with a backslash, so it can be pasted), then the options,
  then a link to the command's reference page; the top level also says where to
  report a problem. A typo in a command name suggests the command, a missing
  argument shows that command's worked example, and leftover arguments are now
  reported by the command they were typed to rather than by the program: the
  `stage` of a `--json` usage error for `fluxsim validate . --bogus` is
  `validate`, not `fluxsim`. The command tree of `describe --self --json` is
  unchanged except for one full stop that became a semicolon in the help of
  `--committed-slice`.
- A failing command now says where to read more. A catalogued failure prints
  `more: fluxsim explain FLX-nnnn` and the address of that code's page; any
  other failure says how to get the traceback (`FLUX_DEBUG=1`) and where to
  report it. `fluxsim explain FLX-nnnn` ends with the same address. The
  `--json` envelope is unchanged. **The addresses point into the engine's
  documentation, which is published with the specification's site at the
  first public release; until then they do not resolve.**
- On a terminal, a command in the six-step loop now ends with `next:` and the
  command to run after it, built from the bundle and run id it just used, with
  the flags the next command needs again (`--simulation-id`, `--zone`,
  `--committed-slice`). The lines go to stderr only, never under `--json`, and
  `FLUX_NO_PROGRESS` silences them like the progress lines.
- Added the reference pages for the command line and for every `FLX-nnnn` code
  under `docs/reference/`, generated from the argparse tree and the error
  catalog (`make docs-gen`). `tests/docs/test_generated_docs.py` fails when a
  page is stale, when a code has no page, and when an address the CLI prints
  names a page that does not exist.
- `scripts/register_lint.py` now requires a worked example for every command,
  where it used to require an epilog.
- Added the rest of the documentation under `docs/`, in the Diátaxis layout: a
  tutorial, five how-to guides and four explanations, beside the reference. The
  README is now a front door that points into them. The examples in the
  documentation and in the README run as tests in CI (Sybil), so a page that
  stops working fails the build.
- Added the accuracy program under `tests/accuracy/`: nine families of tests
  that measure whether what the engine does is right, not only whether it
  repeats itself. They cover fidelity metrics on aggregates, goodness of fit for
  every sampler and declared distribution, the fidelity gate's own accuracy,
  metamorphic and schema-driven properties, seed-tree stream quality, journey
  conformance, a determinism matrix and speed, and the last family renders the
  report. A pull-request tier runs at fixed seeds; a nightly tier runs at fresh
  ones. The design is `docs/accuracy/DESIGN.md` and the committed report is
  `docs/accuracy/REPORT.md`. The program is test-only and changes nothing at
  runtime. It found the four engine defects fixed in this release.
- Added a CycloneDX SBOM of the installed wheel to the release assets, and a
  release job that publishes to PyPI through Trusted Publishing, with
  attestations. It runs only when the repository variable `PYPI_PUBLISH` is
  `true`. Every action in the workflows is pinned by commit, and
  `tests/test_workflow_pins.py` keeps it that way.
- Added the community files (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue and pull request templates), a DCO sign-off check and
  `make check`, a quick local run of the checks a pull request goes through.

## 0.1.0 - 2026-11-08

First release.

- Added a release pipeline: `.github/workflows/release.yml`,
  `scripts/release_guard.py` with its self-test, `tests/release/` and
  `docs/RELEASING.md`.
- Removed the `tags: ["v*"]` push trigger from `.github/workflows/ci.yml`.
  A tag now reaches the six-cell whole-suite matrix through that file's
  `workflow_call` hook, which the release workflow calls, so one tag runs
  the matrix once rather than twice.
