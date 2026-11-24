# Contributing

Thanks for helping build the FLUX Simulation Engine. Bug reports, fixes,
tests and documentation are all welcome. This page takes you from a fresh
clone to a pull request.

## Quick start

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), git and GNU
Make (on Windows, use WSL). Fork the repository on GitHub, then:

```bash
git clone https://github.com/<you>/flux-engine
cd flux-engine
git remote add upstream https://github.com/Agenticstiger/flux-engine
uv sync --extra dev && make test-fast
```

Maintainers with write access can clone the repository directly and skip the
fork; read `origin` wherever this page says `upstream`.

`make test-fast` runs every test except the ones marked `slow`. It takes
about a minute on a 16-core workstation and a few minutes on a laptop.
`uv run pytest` runs the whole suite, slow tests included, and takes tens of
minutes, so leave that to CI unless you are chasing a slow-test failure.

The FLUX specification repository is optional. Only the tests that read its
example bundles need it: clone it next to this repository
(`git clone https://github.com/Agenticstiger/flux ../FLUX`) to run them. Without
it they are skipped locally. CI and `make ci-local` refuse to skip them.

Those tests validate the examples with the `flux_spec` package from the same
checkout, as CI does, because an example can need a newer specification than
the released one (the towers example pins FLUID 0.7.6). Install it over the
released package with `uv pip install --no-deps ../FLUX`.

## Two distributions in one repository

This repository releases two distributions, from one tag and at one version
([ADR 0009](docs/adr/0009-flux-stats-distribution.md)):

- `flux-sim-engine`, the engine (`flux_engine/`, at the root), on Python
  3.11 or newer;
- `flux-stats` (`packages/flux-stats/`, import `flux_stats`), on Python 3.10
  or newer: the statistic
  registry, the record reader, the profile document reader, the fixed-point
  grid and the `FLX-nnnn` catalog. A profiler installs it where the data lives, without the
  engine, so it imports the standard library and nothing else, and
  `make imports` refuses an import of the engine from it.

They are one uv workspace, so the quick start's `uv sync --extra dev`
installs both. With pip, name the
member first: `pip install -e packages/flux-stats -e ".[dev]"`. The engine's
test run collects `packages/flux-stats/tests` too, and `make test-stats-alone`
runs those tests where no engine is installed, as CI's `flux-stats alone` job
does.

## Before you open a pull request

```bash
make check
```

That runs the static checks (ruff, black, mypy, import-linter, the
vocabulary lint, the naming guardrail, the licence-header check, the
skills drift check and the proof-material guard) and the fast tests. CI runs more:
the slow shards, and a matrix of operating systems and Python versions.
`make ci-local` mirrors all of it and takes about half an hour. It also runs
the lineage, publishing and Kafka sink tests against the real libraries, so
install those extras first: `uv sync --extra dev --extra lineage --extra
publish --extra kafka`, then `uv pip install "sqlalchemy>=2.0.18,<3"`.

No CI job runs [the end-to-end harness](e2e/aws/README.md) under `e2e/aws/`.
It is a uv project of its own that checks FLUX the way a data owner would use
it, against a relational source on AWS, and is run by hand. Its privacy judge
measures the method on a TPC-DS-shaped dataset that holds no real people, and
is not a privacy guarantee.

A second harness, [`e2e/roundtrip`](e2e/roundtrip/README.md), needs no cloud. It
generates a truth, profiles it with `fluid profile`, writes a bundle back
from the profile with `fluxsim init`, runs it, and judges the copy against the
truth. It is a uv project of its own too, is run by hand, and keeps its score
history in `history.jsonl`.

## What a good change looks like

- **Tests with the code.** New behaviour comes with a test. A fix comes with
  a test that failed before it.
- **Determinism is the product.** The kernel packages (`core`, `world`,
  `runtime`, `expr`, `expr2`, `seam`) and `flux_stats` may not read the wall clock,
  draw random numbers or use process identity; the seed tree supplies every draw, and
  `make imports` enforces the rule. If a change moves a pinned digest under
  `tests/golden/`, that is a deliberate "golden move": regenerate the files
  and add a row to `GOLDEN-CHANGELOG.md` in the same commit. A test checks
  for the row.
- **Types and style.** ruff, black (line length 100) and mypy (every
  function in `flux_engine/` and `flux_stats/` annotated) run in CI.
- **Vendored code stays vendored.** `tools/product_guardrail/` is synced
  from a shared source; only `profile.py` is written by hand. The
  specification is the flux-spec package: change it in its own
  [repository](https://github.com/Agenticstiger/flux), not here.
- **Wording.** CI runs a vocabulary lint over the README, the docs, command
  help text and docstrings. When it flags a word, its message names the file
  and line. Reword and push again.
- **No proof-system backends here.** The engine ships the `ProofSystem` port
  and `NoProofSystem`, the no-op that fills it. Real proof-system backends are
  out of scope for this repository and live in a separate commercial plugin,
  so the maintainers can't take a pull request that adds one, however good it is.
  `make proof-guard` (part of `make check`, and one of the jobs the required
  `CI gate` waits for) fails on the backend material it recognises, so you
  find out before a reviewer has to say it.

## Sign your commits (DCO)

The project uses the [Developer Certificate of Origin](https://developercertificate.org/)
instead of a CLA. If you contribute from outside the organisation that
maintains this project, sign off every commit:

```bash
git commit -s -m "fix: describe the change"
```

That adds a `Signed-off-by: Your Name <you@example.com>` line. The name and
email must match the commit's author. The check on your pull request fails
when a commit has no matching sign-off. If you edit in GitHub's web editor,
add the line yourself as the last line of the commit message. To repair a
branch:

```bash
git fetch upstream
git rebase --signoff upstream/main
git push --force-with-lease
```

Pull requests opened by the organisation's owners and members skip the check.
Their work is covered by the organisation's own ownership, so a sign-off adds
nothing. A member whose pull request is still checked should ask a maintainer to
add their login to `.github/workflows/dco.yml`.

## Commits and pull requests

- Start the pull request title and each commit title with a Conventional
  Commits type (`feat:`, `fix:`, `docs:`, `chore:`), like the existing history.
- Keep a pull request to one logical change.
- The maintainers merge pull requests by rebase, so every commit of a pull request lands on `main` as it is. Make each commit one task: one change, one Conventional Commits title, and its own sign-off (`git commit -s`). Fix a mistake in the commit it belongs to rather than adding a commit that mends it.
- CI must be green. The required check is `CI gate`.

## Licensing

By signing off you confirm that you have the right to submit the work under
the project's licence, which is in `LICENSE`.

Every file also states its licence and copyright holder, in the format of the
[REUSE](https://reuse.software/spec/) specification, and `make reuse` (part of
`make check`) fails on a file that does not. A new Python file, script or
workflow starts with this header, which `reuse annotate` writes:

```
# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0
```

You keep the copyright in what you write, so in a file you create, name
yourself or your employer on the copyright line. The licence line stays as it
is:

```bash
.venv/bin/reuse annotate --license Apache-2.0 --copyright "Your Name" \
  --year 2026 --copyright-prefix string path/to/new_file.py
```

Files that cannot take a header, or must not, are listed in `REUSE.toml`
instead: Markdown, JSON, `uv.lock`, and the data
whose bytes feed a digest (`tests/fixtures`, `tests/golden` and
`flux_engine/starter`). A header inside one of those would move a pinned digest
or show on a rendered page. Add a path of that kind to `REUSE.toml`.

## Bugs and security problems

Open an issue for bugs and ideas; the templates ask for what the maintainers need. If you
think you found a security problem, do not open an issue: follow
[SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant 3.0](CODE_OF_CONDUCT.md).
