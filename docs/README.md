---
title: "FLUX Simulation Engine"
description: "Documentation for the FLUX Simulation Engine: run a FLUX bundle deterministically against a FLUID contract, and pack the evidence."
---

# FLUX Simulation Engine

The FLUX Simulation Engine runs a [FLUX](https://agenticstiger.github.io/flux/) bundle, a
declared universe of synthetic customers, against the
[FLUID](https://open-data-protocol.github.io/fluid/) contract it refers to. The same
bundle and the same seed give the same bytes on every system the project's CI checks
(Linux on Python 3.11 to 3.14, and macOS on 3.11 to 3.13, with determinism checked on 3.12 for each change). A run that passes its checks can be packed into
an evidence archive. Anyone can check offline that the archive's files match its
manifest, and the report inside says plainly what the run does not show. The command is
`fluxsim`.

New here? Start with [First evidence pack in five
minutes](./tutorial/first-evidence-pack.md). It needs [uv](https://docs.astral.sh/uv/) and
nothing else. For the whole picture first, read [Architecture and
patterns](./explanation/architecture.md): what crosses over from the data, the parts you
install, and how a run moves through them.

## How these docs are organised

| You want to | Go to | It is |
|---|---|---|
| Learn by doing | [Tutorial](./tutorial/README.md) | A lesson that works the first time. |
| Get a job done | [How-to guides](./how-to/README.md) | Steps for one goal, for a reader who knows the basics. |
| Look something up | [Reference](./reference/README.md) | The command line, every error code, the Python API, the schemas. |
| Understand why | [Explanation](./explanation/README.md) | Background: the architecture, the loop, determinism, the seam, the evidence pack, running offline. |
| Read the rules | [Specifications](./spec/README.md) | What the engine must do, rule by rule: the kernel, the seam, the expression languages, the profile and the scores. |
| See why it was decided | [Decision records](./adr/README.md) | One record per design decision, with what else was considered. |

Every command and file on the tutorial, how-to and explanation pages is run by the
project's tests, so what you read is what the engine does. The exceptions are the lines
that install the engine: they install the published package, or `main` from GitHub, so
they cannot test this checkout, and the First run workflow runs the tutorial's first
commands from a built wheel instead.

## Which version these pages describe

The docs site carries the pages of the latest release tag, so what it says matches the
release you install from PyPI. The `docs/` folder in the
[Agenticstiger/flux-engine](https://github.com/Agenticstiger/flux-engine) repository
describes the `main` branch, which can be ahead of the latest release. A feature that `main`
has and the latest release lacks is marked where a page first mentions it, with the release
it came after, for example *(added after 0.6.0)*. A build from `main` reports the version
number of the last release, so `fluxsim --version` does not tell the two apart.

To follow the pages on the site, install the latest release:

```text
pip install flux-sim-engine
```

To try what the repository's `docs/` folder describes, install `main` instead. With uv:

```text
uv tool install git+https://github.com/Agenticstiger/flux-engine
```

uv builds it with the `flux-stats` in the same repository, which `main` names as a member
of its uv workspace. pip does not read that, so with pip, install `flux-stats` from the
same repository first:

```text
pip install "flux-stats @ git+https://github.com/Agenticstiger/flux-engine#subdirectory=packages/flux-stats" "flux-sim-engine @ git+https://github.com/Agenticstiger/flux-engine"
```

## What it is, and what it is not

It is a deterministic runtime: it takes a declared scenario and produces rows, run
records and an evidence pack, and it refuses to ship a bundle whose declared fidelity
checks fail.

It is not a claim that the simulated customers behave like real ones. Every evidence
pack ends with a section that says so, and in this release the engine always reports
`validity: not asserted`.
