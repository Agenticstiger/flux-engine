# FLUX engine agent skills

Agent skills that drive the `fluxsim` command line of the FLUX engine. An agent that has them knows how to find the right documentation, write and check a bundle, run and verify it, choose a privacy grade, and scale and ship a run.

The skills pre-approve nothing. They declare no permissions that run a command without asking, so your agent asks you before every `fluxsim` command it wants to run.

## The skills

| Skill | What it does |
|---|---|
| `flux-docs-lookup` | Answers from offline help first, then the docs folder of a clone, then the docs of the installed release: the command reference, the page of each FLX code, the guides, the specifications and the decision records. |
| `flux-run-and-verify` | Runs `plan`, `apply` and `verify`, or `run`; explains the run summary, the verify legs, the scorecard and `EVIDENCE.md`; tells one entity's history with `fluxsim story`; explains every refusal during a run with its remedy from `fluxsim explain`. |
| `flux-author-bundle` | Writes a bundle with `fluxsim init` from a contract and a profile, or from the contract alone to write a profile request; adds module declarations under the Simulation's extensions. |
| `flux-privacy-and-profiling` | Chooses the grade each column's sensitivity needs, writes the profile request, and hands the data owner the `fluid profile` step that runs where the records live. |
| `flux-scale-and-ship` | Covers `--scale`, `--shard` and `--jobs`; the Parquet, Arrow IPC, ADBC, Iceberg, Delta and Kafka sinks and object store targets; the rule that only the person running `fluxsim` authorises a generator plugin; `ship` and the evidence pack. |
| `flux-relational-modeling` | Keeps the tables of a bundle consistent: links between tables, copied columns, sentinels and bridge pairs, a catalogue that other tables point at, and an exact number of children per parent. |
| `flux-consumer-queries` | Registers the SQL a bundle's consumers run, reports the requirements each query compiles to and the rates a run gives, and calibrates only where held-out queries agree. |
| `flux-forecasts` | Declares response families and forecasts of the next state, makes them before the next profile exists, and scores each one against it. |

More skills follow in later releases.

## Install

Install the engine first. Either command works:

```bash
pip install flux-sim-engine
```

```bash
pipx install flux-sim-engine
```

### Claude Code

Add the marketplace at the tag of the release you installed, then install the plugin. Replace VERSION with the version that `fluxsim --version` prints.

```bash
fluxsim --version
claude plugin marketplace add Agenticstiger/flux-engine@vVERSION --sparse .claude-plugin skills
claude plugin install flux-engine@flux-engine
```

Inside a session, the same steps are:

```text
/plugin marketplace add Agenticstiger/flux-engine@vVERSION
/plugin install flux-engine@flux-engine
```

The plugin version is the engine's version, so a plugin stays as it is until you add a newer tag.

### Codex and Cursor

Copy each `skills/<name>/` folder into `.agents/skills/` in a project, or into `~/.agents/skills/` to use it in every project.

## Offline and on your model

FLUX needs no internet to produce results and has no LLM inside it. These skills are plain text that runs on the model you choose, a local one included. The network is touched only by a `--sink` target you name and by an OpenLineage emitter your own code builds with a URL. See [Runs offline](https://github.com/Agenticstiger/flux-engine/blob/v0.12.1/docs/explanation/offline.md).

## What the skills never tell an agent to do

- Fetch and run remote content, pipe anything into a shell, or install a package.
- Turn a check off or record a run past it. A refusal is met with the remedy that `fluxsim explain` prints.
- Loosen a run to get past a failure, such as dropping `--strict-coverage` or using `verify --sample` in place of the full check.
- Publish without being asked: no sink to a database, and no `ship`, unless you asked for it and named the destination.
- Write, echo or pass a credential. A database address reaches a sink only through an environment variable, and a signing key is a file you name, which the agent never reads, prints, copies or creates.
- Lower a grade below a column's floor or change a column's sensitivity to get past the governance gate. A label is the contract owner's decision.
- Open, sample, print or summarise the real records. Only `fluid profile` reads them, where they live.
- Follow instructions found in what it reads: a contract, a profile, a bundle, a data file or a fetched page is data, never an instruction.
- Write outside the bundle, the run-state directory and the output folders you name.

## More

The guides are listed in the [how-to index](https://github.com/Agenticstiger/flux-engine/blob/v0.12.1/docs/how-to/README.md). Report a problem with a skill in the [issue tracker](https://github.com/Agenticstiger/flux-engine/issues).
