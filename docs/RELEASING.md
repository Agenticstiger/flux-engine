# Releasing

A release is a `vX.Y.Z` tag on a commit contained in `main`, plus a GitHub
Release carrying five assets: the wheel and the sdist of each of the two
distributions that `make packaging-smoke` builds, and a CycloneDX SBOM of the
engine's wheel installed into a clean environment.

The repository releases two distributions from one tag
([ADR 0009](adr/0009-flux-stats-distribution.md)): `flux-sim-engine`, and
`flux-stats` (`packages/flux-stats`), which is versioned by its own format and
which the engine requires as a range. The workflow builds flux-stats's files at
the tag's version, so at a release the two are at one version.

A release also goes to PyPI, flux-stats first and then the engine. The
workflow's `publish-pypi` job uploads them through Trusted Publishing, so no
API token is stored anywhere.
The job runs only while the repository variable `PYPI_PUBLISH` is `true` (it
is), and each upload waits for a maintainer to approve the `pypi` environment.
See "Publishing to PyPI" below.

## The three version strings that are NOT the package version

Read this part before you bump anything.

| Constant | Where | What it is |
|---|---|---|
| `DEFAULT_KERNEL`, `KERNELS` | `flux_engine/core/kernels.py` | The kernel a new plan takes, and every kernel this engine implements. A `kernelVersion` is the semver of normative kernel behaviour. **Hashed into `inputDigest`.** |
| `SEED_TREE_VERSION` | `flux_engine/core/seedtree.py` | The seed-tree derivation wire version. |
| `TRACE_VERSION` | `flux_engine/core/trace.py` | The trace wire version. |

None of the three moves as part of cutting a release. Moving `DEFAULT_KERNEL`
moves every pinned digest of a new plan in the repository, which is a deliberate
golden move recorded in `GOLDEN-CHANGELOG.md`, not a release step. A recorded
plan keeps its digests, because every stage runs the kernel it names. If you
move the default by habit while bumping the package version, `make test-digests`
goes red on every cell of the matrix and no release is built.

The package version is the set of strings below, and only that set.

## Procedure

1. **One pull request** that changes:
   - `pyproject.toml`, `[project] version`, to `X.Y.Z`.
   - `flux_engine/__init__.py`, the hard-coded literal in the
     `except PackageNotFoundError` branch, to the same `X.Y.Z`. These are two
     copies of one string and the guard refuses a tag if they disagree.
   - `packages/flux-stats/pyproject.toml`, `[project] version`, to the same
     `X.Y.Z`. The release workflow builds, attests and uploads flux-stats's
     files named by the tag, so at a tag flux-stats is at the tag's version, and
     the guard refuses a tag where it is not.
   - The engine's requirement on flux-stats, in `pyproject.toml`'s
     `[project] dependencies`, to a range of flux-stats's line that admits that
     version: `flux-stats>=X.Y,<X.Z`, for example `flux-stats>=0.10,<0.11` at
     0.10.0. flux-stats is versioned by its own format and the engine requires
     the line it was tested with, not one release
     ([ADR 0009](adr/0009-flux-stats-distribution.md), amended). The guard
     refuses a requirement that is not a plain range, one with no floor or no
     ceiling, and one that does not admit flux-stats's version, on a pull
     request and at a tag. A pre-release tag needs the pre-release in the floor
     (`flux-stats>=X.Y.ZrcN,<X.Z`), because `>=X.Y` sorts above `X.Y.ZrcN`.
   - `uv.lock`: the `flux-sim-engine` and `flux-stats` entries record the two
     projects' own versions, and `uv lock --check` fails while either differs
     from its `pyproject.toml`.
   - `CHANGELOG.md`, turning the `## Unreleased` section into a
     `## X.Y.Z - YYYY-MM-DD` section.
   - `CITATION.cff`: `version` to `X.Y.Z`, and `date-released` to the date in
     that changelog heading. A test compares both.
   - The two places the docs tests compare with the output of
     `fluxsim --version`: the console block in
     `docs/tutorial/first-evidence-pack.md` and its fixture in
     `tests/docs/test_harness.py`.

   Two more places hold the version only as an example. Nothing fails when they
   go stale, so move them too, for the next reader: the placeholder in
   `.github/ISSUE_TEMPLATE/bug_report.yml`, and the version the release guard's
   self-test fixtures use (`scripts/release_guard.py` and
   `tests/release/test_release_guard.py`).
2. **Run the guard locally**: `make release-guard`. It runs its own self-test
   first, then checks that the two literals agree with each other and that the
   engine's requirement on flux-stats is a plain range with a floor and a
   ceiling that admits flux-stats's version.
3. **Merge to `main`** through the normal gate. The required status check is
   `CI gate`.
4. **Tag the merge commit and push the tag** (see "Tags and attestations"):

   ```
   git tag -a --no-sign vX.Y.Z -m "flux-sim-engine and flux-stats X.Y.Z" <the merge commit>
   git push origin vX.Y.Z
   ```

   `.github/workflows/release.yml` takes it from there: it refuses a tag that
   disagrees with the tree, runs the whole suite on seven cells, builds and
   attests both distributions, checks its own attestation refuses an altered
   wheel, and publishes the GitHub Release. The PyPI upload follows once a
   reviewer signs off on the `pypi` environment (see "Publishing to PyPI").

## Tag shape

`vX.Y.Z`, with an optional `aN`, `bN` or `rcN` suffix, which publishes as a
GitHub pre-release. A `.devN` or `.postN` tag is refused, because tagging the
working version string is the obvious way around the version checks.

## Tags and attestations

Release tags are annotated and not signed. The maintainers decided this on
2026-11-22. A signature on a tag would only say that someone held a key, and
the people who consume a release need to know something else: which workflow
built the files they downloaded.

That is what the release's GitHub attestations prove. The release workflow
attests the build provenance of the four distribution files, and the
attestation names the workflow, the repository and the commit that produced
them. Check a downloaded file against it:

```
gh attestation verify <file> --repo Agenticstiger/flux-engine
```

The workflow also confirms, before it publishes anything, that it can read its
own attestation and that it refuses an altered wheel. An annotated tag still
carries the tagger's name, the date and the message, so the tag is not
anonymous. It is only unsigned. Tag with `--no-sign`, so that a global `tag.gpgSign`
setting does not sign it, and never with `-s`.

The release workflow does not look at who made the tag. It checks that the tag
agrees with the tree and that its commit sits on `main`.

## Publishing to PyPI

The `publish-pypi` job uses PyPI Trusted Publishing, so no API token is stored
anywhere. It runs only while the repository variable `PYPI_PUBLISH` is `true`,
and each upload waits for approval of the `pypi` environment.

PyPI refuses a distribution whose dependencies contain a direct reference such
as `flux-spec @ git+https://...`, so every dependency stays a version range.

The setup below is done for this repository. `flux-stats` was registered on
2026-11-16 with a placeholder, 0.0.1, that holds no code and only keeps the
name: a pending publisher reserves nothing, and the engine's dependency on it
made the name worth taking before that dependency was published. Before the
first release that carries flux-stats, check that its trusted publisher (the
project's own Publishing page on pypi.org) names `Agenticstiger`,
`flux-engine`, `release.yml` and `pypi`; without it the job's first upload is
refused and nothing reaches PyPI. A fork, or a renamed project, repeats all of
it, for each project:

1. On pypi.org, open your account's Publishing page and add a pending publisher
   on the GitHub Actions tab, once for each project:
   - PyPI project name: `flux-sim-engine`, and again `flux-stats`
   - Owner: `Agenticstiger`
   - Repository name: `flux-engine`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   One workflow and environment may publish several projects, but each project
   needs its own publisher entry. A pending publisher does not reserve the
   name, so register it before anyone else registers the project. It becomes an
   ordinary publisher on the first upload. To hold a name before the first
   release, upload a placeholder instead and add the publisher on the project's
   page, at a version no release will use (0.0.1): the flux-stats upload step
   skips a file PyPI already holds, so a placeholder at the release version
   would stand in for the attested file.
2. On GitHub, create the environment `pypi` (Settings, Environments), add yourself
   as a required reviewer so every upload waits for a human, and under deployment
   branches and tags allow only the tag pattern `v*`.
3. On GitHub, add the repository variable `PYPI_PUBLISH` with the value `true`
   (Settings, Secrets and variables, Actions, Variables).
4. Before the first tag, add a ruleset (Settings, Rules) that limits who may
   create `v*` tags. The "release tags are immutable" ruleset blocks deletion and
   force-moves, not creation. A tag runs the workflow file of the commit it points
   at, so the `pypi` environment's reviewer and this ruleset are what stop an
   unreviewed tag from publishing.

The upload carries PEP 740 attestations; the pypa action turns them on by
default.

flux-stats is uploaded in a step of its own, before the engine, because the
engine requires it: an engine on PyPI with no flux-stats release inside its
range would install nowhere. The flux-stats step skips a file PyPI already
holds, so a re-run after a failed engine upload does not stop at the flux-stats
files the first attempt uploaded. The engine's step does not skip: a file of its
name already on PyPI is not one this run attested, so the upload is refused, as
before.

If the upload fails, the GitHub Release already exists, so running the whole
workflow again is refused by the guard. Fix the cause (the publisher
registration, the environment, a classifier or dependency PyPI refuses) and use
"Re-run failed jobs" on the run. That works for 7 days, the time the `dist`
artifact is kept.

## When the guard refuses a tag you already pushed

Nothing has been published at that point, so the fix is to move the tag:

```
git push origin :refs/tags/vX.Y.Z
git tag -d vX.Y.Z
# fix the pull request, merge it, then tag again
```

That deletion works because a repository admin can bypass the tag ruleset,
and that is deliberate: a mistyped tag has to be removable.

Release tags are covered by a repository ruleset, "release tags are
immutable", which applies `deletion` and `non_fast_forward` to `refs/tags/v*`.
Read its limit honestly. An admin can bypass it, so it does not stop an admin;
what it stops is a workflow token, an automation, and an accidental
force-push, and it puts a bypass on the record when an admin does move a tag.
The workflow's ancestry check constrains what a tag may RELEASE; the ruleset
constrains what it may become afterwards, for everything that is not an admin.

## What a release does not claim

The wheel is built once, so it is not claimed to be byte-reproducible.
`build-system.requires` floats at `setuptools>=77.0.3`; an exact pin and a
cross-time rebuild check would have to land together before any such claim.
