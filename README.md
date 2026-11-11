# FLUX Simulation Engine

Describe your customers and what they do in YAML, and this engine turns that into synthetic rows that follow a [FLUID](https://open-data-protocol.github.io/fluid/) data contract: the same rows every time, with a report that says plainly what the run does not show.

## Try it

You need [uv](https://docs.astral.sh/uv/). (Until the package is on PyPI you also need a clone of this repository and `git`: run `uv build --wheel` in the clone and pass the wheel's path after `--from` instead of the package name.)

<!-- skip: next "the package is not on PyPI yet; the First run workflow runs these same commands from a built wheel" -->
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
emitted payment_recovery_moment: 154 rows (scenario)
reproducible: not checked | validity: not asserted
...
shipped out/demo.flux.tgz
run 8dec55892096169f complete; shipped to out
```

Open `.flux/runs/<runId>/EVIDENCE.md` first: its last section, "What Was Not Proven", lists what the run does not show. The [tutorial](docs/tutorial/first-evidence-pack.md) goes through each step.

## What it is

- **Repeatable.** The same bundle and the same seed give the same bytes on every system the project's CI checks (Linux and macOS, Python 3.11 to 3.13). `fluxsim replay` runs a recorded plan again and refuses unless it reaches the digest you give it.
- **Declarative.** A bundle is YAML documents for the world, the personas, the signals, the journeys and the treatments. It refers to a FLUID contract and never restates it, so the rows have the shape the contract expects.
- **Six steps, one command each.** `validate`, `plan`, `apply`, `run`, `verify`, `ship`. `ship` packs the bundle, the run and a manifest into one archive, writes two unsigned in-toto statements beside it, and the report inside always ends in a section called "What Was Not Proven".

## What it is not

- **Not evidence about the real world.** The engine reproduces the scenario you describe. Whether the scenario matches reality is a separate question, and in this release the report answers it `validity: not asserted`.
- **Not a privacy or fairness tool.** The rows are made from the numbers you declare. The pack makes no claim about privacy, fairness or any model trained on the rows.
- **Not finished.** The agentic tier, the sweep executor for `fidelity: stochastic`, calibration with `pyabc`, synthesis with `synthcity` and an OpenLineage transport are absent, and the engine says so instead of degrading quietly. `fluxsim describe --self` says whether the libraries behind four of them (`agentic`, `calibrate_abc`, `synth`, `lineage_transport`) are installed, and a bundle that asks for `fidelity: stochastic` is refused with an error.

## Where to go next

- [Tutorial](docs/tutorial/README.md): your first evidence pack in five minutes.
- [How-to guides](docs/how-to/README.md): model customers, add a journey, use the FLUID seam, calibrate from aggregates, ship with evidence.
- [Explanation](docs/explanation/README.md): the six-step loop, determinism and the seed tree, the seam, and what an evidence pack does not claim.
- [Reference](docs/reference/README.md): every command, every error code, the Python API and the schemas.
- The FLUX specification: [Agenticstiger/flux](https://github.com/Agenticstiger/flux).

## Develop

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version:

```text
uv sync --extra dev
make check
```

`make check` runs the static checks and the fast tests; `make ci-local` runs every check in `.github/workflows/ci.yml`. `make docs-test` runs every page of the documentation as a test, and `make docs-gen` rewrites the generated reference pages. The decisions behind the design are in [docs/adr/](docs/adr/).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Third-party components are listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
