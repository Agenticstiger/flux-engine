---
title: "FLUX Simulation Engine"
description: "Documentation for the FLUX Simulation Engine: run a FLUX bundle deterministically against a FLUID contract, and pack the evidence."
---

# FLUX Simulation Engine

The FLUX Simulation Engine runs a [FLUX](https://agenticstiger.github.io/flux/) bundle, a
declared universe of synthetic customers, against the
[FLUID](https://open-data-protocol.github.io/fluid/) contract it refers to. The same
bundle and the same seed give the same bytes on every system the project's CI checks
(Linux and macOS, Python 3.11 to 3.13). A run that passes its checks can be packed into
an evidence archive. Anyone can check offline that the archive's files match its
manifest, and the report inside says plainly what the run does not show. The command is
`fluxsim`.

New here? Start with [First evidence pack in five
minutes](./tutorial/first-evidence-pack.md). It needs [uv](https://docs.astral.sh/uv/) and
nothing else.

## How these docs are organised

| You want to | Go to | It is |
|---|---|---|
| Learn by doing | [Tutorial](./tutorial/README.md) | A lesson that works the first time. |
| Get a job done | [How-to guides](./how-to/README.md) | Steps for one goal, for a reader who knows the basics. |
| Look something up | [Reference](./reference/README.md) | The command line, every error code, the Python API, the schemas. |
| Understand why | [Explanation](./explanation/README.md) | Background: the loop, determinism, the seam, the evidence pack. |

Every command and file on the tutorial, how-to and explanation pages is run by the
project's tests, so what you read is what the engine does. The one exception is the
install line, which needs the package to be published.

## What it is, and what it is not

It is a deterministic runtime: it takes a declared scenario and produces rows, run
records and an evidence pack, and it refuses to ship a bundle whose declared fidelity
checks fail.

It is not a claim that the simulated customers behave like real ones. Every evidence
pack ends with a section that says so, and in this release the engine always reports
`validity: not asserted`.
