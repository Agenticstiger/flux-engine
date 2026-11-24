# ADR 0032: a generator plugin loads only from a declared entry point, its run is graded, and its bytes are stored

## Status

Proposed 2026-11-22, for review. Amends [ADR 0020](./0020-privacy-grades.md): `P4` is no longer
reserved, and `P5` is added. Kernel 0.2.0 only, and opt-in: a bundle that declares no plugin keeps
every byte and every digest.

**Amended 2026-11-24:** shipped in 0.10.0, and now Accepted.

## Context

The engine draws every row of a run itself, from its kernel's streams, so a run replays to the same
bytes anywhere. A learned model trained elsewhere is another kind of generator: its values do not
come from the kernel, and many models give the same bytes only in the environment that ran them.
ADR 0020 reserved `P4` for "a differentially private model from a plugin" and produced none.

The sink registry (`flux_engine/sinks/registry.py`) is the pattern to follow. Built-ins are names;
third parties register under an entry-point group; nothing is imported until it is selected; and
`fluxsim plugins` lists every entry point without importing it. A generator is more than a sink,
though. It decides the values of a run, so which code loads, what it may state about privacy, and
what happens to bytes that cannot be regenerated must be decided before any of it runs.

Borrow-before-build (2026-11-22):

- **`importlib.metadata`.** Fetched https://docs.python.org/3/library/importlib.metadata.html: the
  built-in finder discovers distribution metadata on the file system and in zip files along
  `sys.path`. Fetched https://packaging.python.org/en/latest/specifications/entry-points/: entry
  points live in a distribution's `entry_points.txt`, as a name and an object reference per group.
- **Which distribution answers.** Search "importlib.metadata entry_points security sys.path current
  directory dist-info plugin hijack": no source treats the hijack directly. The `entrypoints`
  package (maintenance only) states that when several copies of a distribution are on `sys.path`
  the first wins, and that there is no reliable way to tell which `.dist-info` folder matches the
  modules that import. A `.dist-info` directory anywhere on `sys.path` therefore advertises entry
  points.
- **The current directory.** Search "Python 3.11 PYTHONSAFEPATH -P option": Python 3.11 added `-P`
  and `PYTHONSAFEPATH` because `python -m module` puts the current working directory first on
  `sys.path`, and `python -c` an empty string, so a file there shadows an installed module.
- **Editable installs.** Fetched
  https://packaging.python.org/en/latest/specifications/direct-url-data-structure/: an installed
  distribution's `direct_url.json` records, for a local directory, `dir_info.editable`, true when it was
  installed in editable mode.
- **Autoloading.** Search "pytest PYTEST_DISABLE_PLUGIN_AUTOLOAD entry point plugins": pytest loads
  every plugin installed under its group at start unless autoloading is disabled, and then loads
  only the plugins named. Adopted here as the only mode: nothing loads unless a bundle names it.
- **The statement.** NIST SP 800-226, recorded in ADR 0020: a guarantee names its unit of privacy,
  its loss (epsilon and delta, or rho) and its accounting.

## Decision

### 1. The group and the contract

Plugins register under the entry-point group `flux_engine.generators`:

```text
[project.entry-points."flux_engine.generators"]
learned = "my_package.plugin:factory"
```

`factory(options)` returns a generator with two methods. `statement(request)` says, before any row,
what the run rests on (section 4). `generate(request, start, stop)` returns the values of rows `start`
to `stop` (section 5). A plugin never writes a file and never sees a path of the run.

### 2. A bundle names a plugin and pins it

```yaml
extensions:
  com.agentics.flux/1:
    generators:
      subscriber:
        plugin: learned
        distribution: my-package
        version: "1.2.0"
        grade: P4
        privacy:
          unit: one data subject with all of its records
          epsilon: "3"
          delta: "0.00001"
          accounting: a Renyi accountant over the training steps, converted to epsilon and delta
        model: {digest: "sha256:..."}
        reproducibility: environment
        options: {temperature: "1"}
```

- `plugin` is an entry-point name (`^[A-Za-z][A-Za-z0-9_.-]*$`), never a module path, a file or an
  object reference. `distribution` and `version` pin the distribution that must provide it, exactly.
- `grade` is `P4` or `P5`, and `validate` refuses any other value. `P0`, `P1` and `P3` say what a
  profile rests on, so a plugin cannot state them: a plugin that could state `P0` would clear every
  floor, `pii` included.
- For `P4`, `privacy` holds the account in NIST SP 800-226's terms (the unit of privacy, the loss as
  `epsilon` and `delta` or as `rho`, and the accounting), and `model.digest` is required. Both sit in
  the bundle, so `inputDigest` binds them, and the plugin's statement must repeat them exactly. For
  `P5`, `privacy` is refused and `model.digest` is optional.
- `reproducibility` is one of `bytes`, `environment` and `none` (section 6).
- `options` is plain data within the namespace's bounds: strings, exact decimals, booleans, lists and
  mappings. A key whose name names a credential (ADR 0031 section 5's rule) is refused, and so is a string
  value that is one (a URL with user information or a signature in its query, a `password=` pair, a
  bearer token, a JWT, an access key id, a private key block): a bundle never holds a credential. A key that ends in `-env` holds the name of
  an environment variable and is allowed, as it is for a sink option.
- The declaration is in the bundle, so `inputDigest` covers it; the expose's `H` binds it, only when
  declared. Kernel 0.1.0 refuses the key with `FLX-3105`, naming the kernel.

### 3. Which code loads

In this order, and each refusal comes before any plugin code runs:

1. **The declaration** is checked (section 2).
2. **The gate.** The governance gate checks every labelled column's minimum (ADR 0020) against the run's
   grade worked out from the declared grades (section 4). A bundle the gate refuses loads no plugin
   code.
3. **The person's word.** A command that would import a plugin (`plan`, `run`, and `replay` of a plugin
   stated `bytes` or `environment`) needs `--allow-generator NAME` on its command line for each plugin
   the bundle names, and refuses without it. There is no environment variable and no bundle key for it:
   a bundle is untrusted input, and any installed distribution, a transitive dependency among them, can
   register an entry point.
4. **Only a declared entry point.** The engine reads `importlib.metadata.entry_points(group=
   "flux_engine.generators")` and nothing else, and never imports a module that a bundle, the state
   directory or the current directory names. There must be one entry point of that name, from a
   distribution whose name (normalized) and version equal the declaration's. Two or more are refused,
   naming each distribution and version, rather than taking the first on `sys.path`.
5. **Installed where its code is.** The distribution's metadata directory lies under one of this
   interpreter's installation directories (`sysconfig`'s `purelib` and `platlib`, and the user site
   when it is enabled). The origin of the entry point's top-level module, found with
   `importlib.util.find_spec` (which imports nothing for a top-level name), lies under that same
   installation directory, and is listed in the distribution's `RECORD` (`dist.files`). After `load()`,
   the loaded module's `__file__` must still be that origin. So a `.dist-info` folder beside a bundle, a
   module that `python -m` finds first in the current directory, and a module swapped between the check
   and the import are refused.
6. **Editable installs.** A distribution whose `direct_url.json` records an editable install fails
   step 5, because its code lies in a source tree. It loads only because `--allow-generator` names it,
   as every plugin needs, and the engine then records its run as `none` whatever the declaration says,
   and `EVIDENCE.md` says why. Any other failure of step 5 is refused.
7. **The statement.** The entry point is loaded, the plugin's statement is read, and it must equal the
   declaration: the grade, the account, the model digest and the reproducibility. Anything else is
   refused before a row is drawn.

`fluxsim plugins` lists generators as it lists sinks, without importing them: the name, the object
reference, the distribution and its version, and "editable" or "outside an installation directory"
where step 5 or 6 applies. `--json` adds a `generators` member. Each refusal is a new code of the
namespace's family, and names what was found, never an option's value.

### 4. The statement and the grade

- **`P4`, a differentially private model.** The statement repeats the bundle's account. The engine checks
  its form (each part named, each loss finite and positive, delta below one), not the training.
- **`P5`, a learned model with no such account**, new: a model fitted to records whose privacy rests on
  empirical checks alone. It is the weakest pattern.
- **The order**, weakest first, becomes `P5`, `P1`, `P3`, `P4`, `P0`. `P4` ranks above `P3` as ADR 0020
  already orders them: its section 9 lets a bundle raise a label's minimum to `P4` above the `P3` floor,
  and `core/grades.py` orders `P1`, `P3`, `P4`, `P0`.
- **The run's grade** is the weakest of the profile's grade (ADR 0020) and every plugin's. The floors of
  ADR 0020 section 7, and a bundle's raised floors, apply unchanged. `P5` is below every built-in floor,
  so a labelled column with a floor is refused for a `P5` run with `FLX-4005`.
- `plan` records the run's grade as `privacyGrade`, as before, and `apply` and `ship` enforce it from the
  record. `EVIDENCE.md`'s "Privacy Grade" states each plugin's account as the bundle declares it and the
  plugin repeats it: the engine checks its form, not how the model was trained.

### 5. What a plugin generates

- The engine derives the expose's identifier and key columns as for any expose and draws its foreign
  keys. The plugin generates the expose's other columns, for rows `start` to `stop` of `n`, given a
  request that holds those columns with their types and constraints, `n`, the range, a 32-byte seed and
  the options.
- It returns one list of values per column. The engine checks each value against its column (its type,
  its nullability, its range, its length, its levels, and the contract's checks and rules), refuses on
  the first that fails, naming the plugin, the column and the row, and repairs nothing. The refusal comes
  at `plan` when it stages, as a plugin run always does there, and at `apply` otherwise (the single pass
  of `fluxsim run`). The engine renders every line itself, so the bytes have its canonical form.
- **The seed** is `sha256(b"flux-seed/2" + NUL + b"plugin" + NUL + root + NUL + label(("sim", sim_id,
  "gen", exposeId)))`, a new derivation in the form of KERNEL-SPEC-2 section 6.2, pinned in the kernel
  vectors. It reads the run root, so a sweep moves a plugin's draws as it moves the engine's.
- `--jobs` does not split a plugin's expose: the engine calls the plugin once for its rows, in its own
  process. `--shard I/N` hands the plugin the shard's range.
- **Ranges.** A plugin stated `bytes` gives the same values for a range however it is split:
  `generate(a, c)` equals `generate(a, b)` followed by `generate(b, c)`.

### 6. Its bytes are stored

`reproducibility` is one of `bytes` (the same values from the same request on any machine),
`environment` (the same values only in the same environment), or `none`. The environment is measured by
the engine, not stated by the plugin: its digest is the `jcs-document` digest of the interpreter (its
implementation and version), the platform (system and machine), and every installed distribution with its
version, sorted by normalized name.

- **Staged always.** A run with a plugin expose stages (ADR 0015): `plan --no-stage` is refused, and
  `apply --run-id` commits only from the stage. A missing or mismatched stage, another `--simulation-id`
  or `--zone`, `--strict-coverage` or `--shard I/N` is refused rather than generated again (the
  stage holds the whole run, and a share would ask the plugin for its rows). `fluxsim run` holds its
  execution in memory and writes it once, as before.
- **A committed run is kept.** Once the run is committed, its bytes are the record, and no command
  asks the plugin for them again or writes the run directory over them: `run`, `apply` with or without
  `--run-id` and the Python facade keep the run as it stands, say so in one line, and go on with what
  was asked (`--verify`, `--ship`, `--sink`) against it. `apply --run-id RUN_ID --sink` delivers its
  files without the plugin, which is how a delivery that failed after the commit is retried. The run's
  files are first checked against its receipt as `ship` checks them, and a run that no longer matches is
  refused (`FLX-2110`), never generated again. A new sample is a new run, for example with another seed.
- **Replay.** A replay verifies by generating again, and it never reports success without doing so.
  What a plugin promises is the bundle's declaration, and whether its install is editable is what the
  registry finds (nothing is imported); the engine measures the environment digest now. For `bytes`,
  `replay` generates again, into a scratch directory under the state directory that is removed
  afterwards, so the stored run is never written, and compares, as for the engine's own rows. For
  `environment`, it does so only when the digest it measures equals the recorded one. For `none`, an
  editable install, or an `environment` digest that differs, it cannot generate the rows again, so it is refused (`FLX-3123`)
  with a non-zero exit, saying that the bytes are stored, that they are the run of record, and that the
  replay verified nothing. It does not compare `--assert-digest` with the stored receipt.
- **What is stored is the bundle's say.** `plan.json` sits in the state directory, which can cross a
  trust boundary (ADR 0015), and its `planDigest` is an unkeyed hash, so a person who can write the
  directory can change its `generators` member, digest it again and rename the run. Whether a run's
  plugin bytes are stored is therefore decided from the bundle's declaration, whose bytes `apply` checks
  against `inputDigest`, and never from the plan. A `generators` record that is missing, or that differs
  from the declaration (the plugin, its distribution and version, the grade, the account and the model
  digest), is refused by `apply`, `replay` and `ship` (`FLX-3123`), by a message that quotes no value,
  so a forged record can make a command refuse and cannot make one skip a check.
- **Records.** `plan.json` gains a `generators` member, only when declared: per expose, the entry point,
  the distribution and its version, whether it is editable, the grade and account, the reproducibility
  recorded (`none` for an editable install), the environment digest and the model digest. `EVIDENCE.md`'s
  "Reproducibility" says which exposes were stored and not regenerated, from the declaration and the
  recorded environment digest, which must be `sha256:` and 64 hex digits; a recorded reproducibility
  that is none of `bytes`, `environment` and `none` is refused.

### 7. Tests this record asks for

- A bundle without `generators`: a golden computed with the code before this change pins its digests,
  under both kernels.
- A test plugin stated `bytes`: `generate(a, c)` against `generate(a, b)` and `generate(b, c)`, and the
  stored bytes under `--jobs 1` and `--jobs 4`, `--scale` below 1, and each `--shard I/N` against the
  whole run.
- Each refusal of sections 2 and 3, one test each: a plugin grade of `P0`, `P1` or `P3`; a `P4` without
  its account or its model digest; no `--allow-generator`; two entry points of one name; metadata outside
  an installation directory; an origin outside it or missing from `RECORD`; a module swapped after the
  check; a statement that differs from the declaration.
- An editable install under `--allow-generator`, recorded as `none`.
- A `plan.json` that was changed and digested again: a deleted or altered `generators` member is refused
  by `apply --run-id`, `--shard` and `--strict-coverage` before a plugin is imported; a reproducibility
  or an environment digest that is forged never lets `replay` exit 0 without generating again; a
  malformed record is refused by `ship`; an editable module shadowed from a directory of the run inside
  its source tree is refused, and a command run from the source root itself still works.

### 8. What this is not

There is no sandbox. A plugin runs in the engine's process with its rights, and installing one is
trusting it, as installing any dependency is. What the engine decides is which code loads, that it
loads only for a bundle the gate allows and a person names, what the plugin may state, and that its
output is checked and written by the engine.

## Consequences

- `P4` exists, and a run's grade can rest on a plugin's account as it can rest on a profile's. The
  account is the bundle's, so it is reviewed with the bundle and bound by `inputDigest`.
- A plugin's run is reproducible only as far as its record says, and the report says so. The stored
  bytes, checked against their receipt, are the run.
- Running a bundle that uses a plugin needs the plugin installed in the interpreter that runs the engine,
  at its pinned version, and named with `--allow-generator` on every command that imports it. A plugin
  on `PYTHONPATH`, or beside a bundle, fails section 3's checks and is refused. An editable install loads
  under the flag and is recorded as not reproducible.
- A bundle without `generators` is unchanged.

## What would change this decision

- A plugin interface that runs a model outside the engine's process: the request and the returned
  values would cross a process boundary, and the checks of section 5 would stay.
- Per-column grading (ADR 0020 section 6): a plugin's grade would then bind only the columns it
  generates.
