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
git clone https://github.com/Agenticstiger/FLUX ../FLUX   # example bundles some tests read
uv sync --extra dev && make test-fast
```

Maintainers with write access can clone the repository directly and skip the
fork; read `origin` wherever this page says `upstream`.

`make test-fast` runs every test except the ones marked `slow`. It takes
about a minute on a 16-core workstation and a few minutes on a laptop.
`uv run pytest` runs the whole suite, slow tests included, and takes tens of
minutes, so leave that to CI unless you are chasing a slow-test failure.

Without the `../FLUX` checkout, the tests that read its example bundles are
skipped locally. CI refuses to skip them.

## Before you open a pull request

```bash
make check
```

That runs the static checks (ruff, black, mypy, import-linter, the
vocabulary lint, the naming guardrail, the licence-header check and the
proof-material guard) and the fast tests. CI runs more:
the slow shards, and a matrix of operating systems and Python versions.
`make ci-local` mirrors all of it and takes about half an hour.

## What a good change looks like

- **Tests with the code.** New behaviour comes with a test. A fix comes with
  a test that failed before it.
- **Determinism is the product.** The kernel packages (`core`, `world`,
  `runtime`, `expr`, `seam`) may not read the wall clock, draw random
  numbers or use process identity; the seed tree supplies every draw, and
  `make imports` enforces the rule. If a change moves a pinned digest under
  `tests/golden/`, that is a deliberate "golden move": regenerate the files
  and add a row to `GOLDEN-CHANGELOG.md` in the same commit. A test checks
  for the row.
- **Types and style.** ruff, black (line length 100) and mypy (every
  function in `flux_engine/` annotated) run in CI.
- **Vendored code stays vendored.** `flux_engine/_vendor/` is a pinned copy
  of the spec package. Change the specification in its own
  [repository](https://github.com/Agenticstiger/flux), not here.
  `tools/product_guardrail/` is synced from a shared source; only
  `profile.py` is written by hand.
- **Wording.** CI runs a vocabulary lint over the README, the docs, command
  help text and docstrings. When it flags a word, its message names the file
  and line. Reword and push again.
- **No proof-system backends here.** The engine ships the `ProofSystem` port
  and `NoProofSystem`, the no-op that fills it. Real proof-system backends are
  out of scope for this repository and live in a separate commercial plugin,
  so we can't take a pull request that adds one, however good it is.
  `make proof-guard` (part of `make check`, and one of the jobs the required
  `CI gate` waits for) fails on the backend material it recognises, so you
  find out before a reviewer has to say it.

## Sign your commits (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/)
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
- We squash-merge pull requests.
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
instead: Markdown, JSON, `uv.lock`, the vendored `flux_spec` copy, and the data
whose bytes feed a digest (`tests/fixtures`, `tests/golden` and
`flux_engine/starter`). A header inside one of those would move a pinned digest
or show on a rendered page. Add a path of that kind to `REUSE.toml`.

## Bugs and security problems

Open an issue for bugs and ideas; the templates ask for what we need. If you
think you found a security problem, do not open an issue: follow
[SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant 3.0](CODE_OF_CONDUCT.md).
