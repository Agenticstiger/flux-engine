# Releasing

A release is a `vX.Y.Z` tag on a commit contained in `main`, plus a GitHub
Release carrying three assets: the wheel and the sdist that
`make packaging-smoke` builds, and a CycloneDX SBOM of the wheel installed
into a clean environment.

A release also goes to PyPI as `flux-sim-engine`. The workflow's `publish-pypi`
job uploads it through Trusted Publishing, so no API token is stored anywhere.
The job runs only while the repository variable `PYPI_PUBLISH` is `true` (it
is), and each upload waits for a maintainer to approve the `pypi` environment.
See "Publishing to PyPI" below.

## The three version strings that are NOT the package version

Read this part before you bump anything.

| Constant | Where | What it is |
|---|---|---|
| `KERNEL_VERSION` | `flux_engine/stages/pipeline.py` | The semver of normative kernel behaviour. **Hashed into `inputDigest`.** |
| `SEED_TREE_VERSION` | `flux_engine/core/seedtree.py` | The seed-tree derivation wire version. |
| `TRACE_VERSION` | `flux_engine/core/trace.py` | The trace wire version. |

None of the three moves as part of cutting a release. Moving `KERNEL_VERSION`
moves every pinned digest in the repository, which is a deliberate golden move
recorded in `GOLDEN-CHANGELOG.md`, not a release step. If you bump it by
habit while bumping the package version, `make test-digests` goes red on every
cell of the matrix and no release is built.

The package version is the pair below, and only that pair.

## Procedure

1. **One pull request** that changes:
   - `pyproject.toml`, `[project] version`, to `X.Y.Z`.
   - `flux_engine/__init__.py`, the hard-coded literal in the
     `except PackageNotFoundError` branch, to the same `X.Y.Z`. These are two
     copies of one string and the guard refuses a tag if they disagree.
   - `uv.lock`: the `flux-sim-engine` entry records the project's own version,
     and `uv lock --check` fails while it differs from `pyproject.toml`.
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
   first, then checks the two literals agree with each other.
3. **Merge to `main`** through the normal gate. The required status check is
   `CI gate`.
4. **Sign the tag on the merge commit and push it** (see "Signed tags"):

   ```
   git tag -s vX.Y.Z -m "flux-sim-engine X.Y.Z" <the merge commit>
   git tag -v vX.Y.Z
   git push origin vX.Y.Z
   ```

   `.github/workflows/release.yml` takes it from there: it refuses a tag that
   disagrees with the tree, runs the whole suite on six cells, builds and
   attests the distribution, checks its own attestation refuses an altered
   wheel, and publishes the GitHub Release. The PyPI upload follows once a
   reviewer signs off on the `pypi` environment (see "Publishing to PyPI").

## Tag shape

`vX.Y.Z`, with an optional `aN`, `bN` or `rcN` suffix, which publishes as a
GitHub pre-release. A `.devN` or `.postN` tag is refused, because tagging the
working version string is the obvious way around the version checks.

## Signed tags

Release tags are signed, so anyone can check that a tag came from a
maintainer. Set signing up once. An SSH key is the least work:

```
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global tag.gpgSign true
git config --global gpg.ssh.allowedSignersFile ~/.ssh/allowed_signers
echo "<your git email> $(cat ~/.ssh/id_ed25519.pub)" >> ~/.ssh/allowed_signers
```

Add the same public key to your GitHub account as a **signing key**
(Settings, SSH and GPG keys). Then tag with `-s` and check the result before
you push, as in step 4 above.

The allowed-signers file is what lets `git tag -v` verify an SSH signature, on
your machine and on anyone else's, so they need a line for your key too. GitHub
shows "Verified" next to a signed tag whose key is registered on the tagger's
account; with GPG the key goes in the keyring instead.

The release workflow does not check the signature. It checks that the tag
agrees with the tree and sits on `main`. The signature is for the people who
consume the tag.

## Publishing to PyPI

The `publish-pypi` job uses PyPI Trusted Publishing, so no API token is stored
anywhere. It runs only while the repository variable `PYPI_PUBLISH` is `true`,
and each upload waits for approval of the `pypi` environment.

PyPI refuses a distribution whose dependencies contain a direct reference such
as `flux-spec @ git+https://...`, so every dependency stays a version range.

The setup below is done for this repository. A fork, or a renamed project,
repeats it:

1. On pypi.org, open your account's Publishing page and add a pending publisher
   on the GitHub Actions tab:
   - PyPI project name: `flux-sim-engine`
   - Owner: `Agenticstiger`
   - Repository name: `flux-engine`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   A pending publisher does not reserve the name, so register it before
   anyone else registers the project. It becomes an ordinary publisher on the
   first upload.
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
