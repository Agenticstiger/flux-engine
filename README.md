# FLUX Simulation Engine

Describe your customers and what they do in YAML, and this engine turns that into synthetic rows that follow a [FLUID](https://open-data-protocol.github.io/fluid/) data contract: the same rows every time, with a report that says plainly what the run does not show.

Documentation: <https://agenticstiger.github.io/flux/engine/>

The pages in this repository describe the `main` branch, and the latest release on PyPI can lag behind it. The documentation site above holds a copy of them taken at a pinned engine version, so it can be older still. A feature that `main` has and the latest release lacks is marked where a page first mentions it, with the release it came after, for example *(added after 0.6.0)*. To run exactly what the pages describe, install the engine from `main`:

```text
uv tool install git+https://github.com/Agenticstiger/flux-engine
```

[Which version these pages describe](docs/README.md#which-version-these-pages-describe) has the `pip` form.

## Try it

You need [uv](https://docs.astral.sh/uv/).

<!-- skip: next "installs the published package, so it cannot test this checkout; the First run workflow runs these same commands from a built wheel" -->
```bash
uvx --from flux-sim-engine fluxsim init demo && cd demo && uvx --from flux-sim-engine fluxsim run . --verify --ship -o out
```

That writes a small project, runs a 500-customer simulation, checks the result and packs an evidence archive into `out/`, in a few seconds. With the command installed (`uv tool install flux-sim-engine`, or `pip install flux-sim-engine` in a virtual environment), the same two steps print this:

```console
$ fluxsim init demo
created demo with a small FLUX bundle and its FLUID contract (10 files):
...
$ cd demo
$ fluxsim run . --verify --ship -o out
...
emitted payment_recovery_moment: 155 rows (scenario)
reproducible: not checked | validity: not asserted
...
shipped out/demo.flux.tgz
run ... complete; shipped to out
```

Open `.flux/runs/<runId>/EVIDENCE.md` first: its last section, "What Was Not Proven", lists what the run does not show. The [tutorial](docs/tutorial/first-evidence-pack.md) goes through each step.

## What it is

- **Repeatable.** The same bundle and the same seed give the same bytes on every system the project's CI checks (Linux on Python 3.11 to 3.14, and macOS on 3.11 to 3.13, with determinism checked on 3.12 for each change). `fluxsim replay` runs a recorded plan again and refuses unless it reaches the digest you give it.
- **Declarative.** A bundle is YAML documents for the world, the personas, the signals, the journeys and the treatments. It refers to a FLUID contract and never restates it, so the rows have the shape the contract expects.
- **Six steps, one command each.** `validate`, `plan`, `apply`, `run`, `verify`, `ship`. `ship` packs the bundle, the run and a manifest into one archive, writes two unsigned in-toto statements beside it, and the report inside always ends in a section called "What Was Not Proven".
- **One pass when you want one.** `fluxsim run . --verify` is the one-pass path: it plans, applies and verifies in a single command, so the rows are drawn once and `verify` reads each emit once. Run `plan`, `apply` and `verify` on their own to review a plan before it is applied: `plan` stages the run, and `apply --run-id` commits it without drawing the rows again.

## What it is not

- **Not evidence about the real world.** The engine reproduces the scenario you describe. Whether the scenario matches reality is a separate question, and in this release the report answers it `validity: not asserted`.
- **Not a privacy or fairness tool.** The rows are made from the numbers you declare. The pack claims no privacy beyond the guarantee its Privacy Grade section states, which only a P3 run has (a profile released under differential privacy, [ADR 0020](docs/adr/0020-privacy-grades.md)), and none about fairness or any model trained on the rows. It is still not a fairness tool.
- **Not a full model of how columns and rows depend on each other.** FLUX draws the dependence a profile publishes or a bundle declares, and nothing beyond it. In a table: categorical columns jointly from clique marginals through a junction tree ([ADR 0021](docs/adr/0021-clique-marginals-are-drawn-through-a-junction-tree.md)), cliques over number bins ([ADR 0036](docs/adr/0036-binned-number-columns-join-clique-marginals.md)), number columns together through a Gaussian copula of published rank correlations ([ADR 0035](docs/adr/0035-number-columns-are-drawn-through-a-gaussian-copula-of-published-rank-correlations.md)), and a column given another (`given`). Across tables: every key is unique, every foreign key names a row its parent holds ([KERNEL-SPEC.md](docs/spec/KERNEL-SPEC.md) section 8.2), a child reads its parent's values through declared links and conditions ([ADR 0022](docs/adr/0022-rows-read-their-linked-rows.md)), and children per parent can follow a parent column ([ADR 0037](docs/adr/0037-child-columns-and-child-counts-follow-their-parent-and-a-unit-can-follow-its-children.md)). A pattern that no published statistic or declaration describes is not reproduced.
- **Not finished.** The agentic tier, the sweep executor for `fidelity: stochastic`, calibration with `pyabc` and an OpenLineage transport are absent, and the engine says so instead of degrading quietly. `fluxsim describe --self` says whether the libraries behind three of them (`agentic`, `calibrate_abc`, `lineage_transport`) are installed, and a bundle that asks for `fidelity: stochastic` is refused with an error. Synthesis from real records is not part of the engine. On the roadmap: an optional plugin that trains inside the data owner's own environment under differential privacy (designed, [ADR 0003](docs/adr/0003-sdv-bsl-and-aggregate-only-rule.md)).

## Runs offline: your data and your model stay yours

- FLUX needs no internet to produce results.
- The network is used only for a target you name: an object store, a database, an Iceberg catalogue or Kafka brokers and a schema registry as a `--sink`, or an OpenLineage URL your own code sets. Everything else is local.
- An object store target with no credentials is refused unless you pass `--sink-opt instance-identity=true`, so a run never asks the cloud's instance metadata service by surprise.
- There is no telemetry, no update check and no LLM inside FLUX.
- The [agent skills](skills/README.md) are plain text that runs on the model you choose, a local one included.
- A generator plugin is your own code and is not sandboxed, so allow only plugins you trust.
- CI checks it three ways: sockets are blocked in every test run, a real command line journey runs with no network, and a source scan looks for network and model clients. [Runs offline](docs/explanation/offline.md) has the details and the limits.

## How FLUX works

FLUX makes rows from the numbers a bundle declares, never by fitting a model to records. Those numbers can come from a profile: aggregate statistics that a data owner computes with `fluid profile` where the data lives. `fluxsim init --contract --profile` writes a bundle from a contract and a profile, and labels every number by what it rests on ([write a bundle from a profile](docs/how-to/write-a-bundle-from-a-profile.md)). A run can then be scored against a profile that carries the statistics its bundle declares. From data-product-forge 0.31.0, `fluid profile` writes one: an ungrouped `mean/1` for each number column whose mean passes its output checks, so a `mean` property over such a column is scored. A property that reads any other statistic, such as a support, is refused with [FLX-5037](docs/reference/errors/FLX-5037.md); [score a run against a profile](docs/how-to/score-a-run-against-a-profile.md) shows the check with a profile written by hand. [Architecture and patterns](docs/explanation/architecture.md) draws the whole picture: what crosses over from the data, the parts you install, the packages inside the engine and the rules between them, the life of a run, and the design patterns behind it.

## Where to go next

- [Tutorial](docs/tutorial/README.md): your first evidence pack in five minutes.
- [How-to guides](docs/how-to/README.md): [model your customers](docs/how-to/model-customers.md), [add a journey](docs/how-to/add-a-journey.md), [use the FLUID seam](docs/how-to/use-the-fluid-seam.md), [calibrate from aggregates](docs/how-to/calibrate-from-aggregates.md), [draw a column given another](docs/how-to/draw-a-column-given-another.md), [ship with evidence](docs/how-to/ship-with-evidence.md), [score a run against a profile](docs/how-to/score-a-run-against-a-profile.md), [seal and score a prediction](docs/how-to/seal-and-score-a-prediction.md), [load a simulation into a database](docs/how-to/load-a-simulation-into-a-database.md).
- [Explanation](docs/explanation/README.md): how the parts fit together, the six-step loop, determinism and the seed tree, the seam, and what an evidence pack does not claim, and [how it runs offline](docs/explanation/offline.md).
- [Reference](docs/reference/README.md): every command, every error code, the Python API and the schemas.
- The FLUX specification: [Agenticstiger/flux](https://github.com/Agenticstiger/flux).

## Develop

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version:

```text
uv sync --extra dev
make check
```

`make check` runs the static checks and the fast tests; `make ci-local` runs every check in `.github/workflows/ci.yml`. The repository also releases [flux-stats](packages/flux-stats/README.md), the statistics the fidelity check compares, as a distribution of its own that installs without the engine; `uv sync --extra dev` sets up both. flux-stats runs on Python 3.10 or newer, the engine on 3.11 or newer. `make docs-test` runs every page of the documentation as a test, and `make docs-gen` rewrites the generated reference pages. The decisions behind the design are in [docs/adr/](docs/adr/README.md), and the specifications in [docs/spec/](docs/spec/README.md).

[The end-to-end harness](e2e/aws/README.md) under `e2e/aws/` checks FLUX the way a data owner would use it, against a relational source on AWS. It is a project of its own that no CI job runs. Its privacy judge measures the method on a TPC-DS-shaped dataset that holds no real people, and is not a privacy guarantee.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party components are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
