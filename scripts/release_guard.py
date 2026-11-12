#!/usr/bin/env python3

# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Refuse a release whose version strings disagree with the tag.

There are TWO copies of this package's version in the tree, and nothing in
the test suite can see them drift apart:

    pyproject.toml          [project] version
    flux_engine/__init__.py the hard-coded literal in the
                            ``except PackageNotFoundError`` branch

The except branch never executes in CI, because every job pip-installs the
package first and ``importlib.metadata`` always wins. The one test that
touches ``__version__`` compares ``fluxsim --version`` against that same
imported value, so it is a tautology that cannot observe a mismatch. A text
check over the two literals is the only thing that can, and that is what
this script is.

What this script deliberately does NOT check is ``KERNEL_VERSION``
(flux_engine/stages/pipeline.py). That constant is the semver of normative
kernel behaviour, it is hashed into ``inputDigest``, and it is NOT the
package version. It needs no check here because it already has a stronger
one: moving it moves every pinned digest, which turns ``make test-digests``
red on every cell of the matrix a release waits on. Adding a string check
for it would duplicate a stronger existing check and invite someone to
treat the string check as the real gate.

The two constants beside it, ``SEED_TREE_VERSION`` and ``TRACE_VERSION``,
ARE checked, for the opposite reason: pipeline.py re-declares both as bare
literals that flux_engine/core/seedtree.py and flux_engine/core/trace.py
also declare, and nothing in tests/ compares the copies. That check is not
about the package version at all; it rides along because it is a
cross-file literal agreement with no other home.

Four modes:

    --self-test
        Build a broken fixture per check and require each one to refuse,
        plus green arms that must pass. A check that has silently stopped
        firing fails here rather than blessing a release. Same shape as
        ``tools/product_guardrail/check.py --self-test``.

    --check-tree
        The checks that need no tag: the two literals agree with each
        other, and the mirrored constants agree with the modules that own
        them. Cheap enough for the pull-request suite.

    --tag vX.Y.Z --commit SHA [--github-output FILE]
        The release check. Tag shape, both version literals, a CHANGELOG
        section, the mirrored constants, and containment in main. Writes
        ``version`` and ``prerelease`` to GITHUB_OUTPUT.

    --check-installed --version X.Y.Z --describe describe.json
        Reads built-and-installed metadata rather than repository text:
        the version the freshly installed wheel reports about itself.

Standard library only, so it runs before any dependency is installed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A release tag. ``v`` then three dot-separated numbers, with an optional
#: PEP 440 pre-release suffix. A ``.devN`` or ``.postN`` tag is refused
#: here, which closes the obvious way around a version mismatch: tagging
#: the working version string instead of bumping it.
TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)$")

#: The suffix that makes a release a pre-release on GitHub.
PRERELEASE_RE = re.compile(r"(?:a|b|rc)\d+$")

#: A CHANGELOG heading naming a version, with or without a leading ``v``
#: and with or without a trailing date.
HEADING_RE = re.compile(r"^##\s+v?(?P<version>\d[^\s]*)")

PYPROJECT = "pyproject.toml"
INIT = "flux_engine/__init__.py"
PIPELINE = "flux_engine/stages/pipeline.py"
SEEDTREE = "flux_engine/core/seedtree.py"
TRACE = "flux_engine/core/trace.py"
CHANGELOG = "CHANGELOG.md"

#: The constants pipeline.py re-declares, and the module that owns each.
MIRRORED = (("SEED_TREE_VERSION", SEEDTREE), ("TRACE_VERSION", TRACE))

#: Every file a fixture needs a real copy of.
FIXTURE_FILES = (PYPROJECT, INIT, PIPELINE, SEEDTREE, TRACE)


class Refusal(Exception):
    """A check refused. The message is what the log prints."""


# --------------------------------------------------------------- readers


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        raise Refusal(f"REFUSED: {rel} does not exist under {root}.")
    return path.read_text(encoding="utf-8")


def pyproject_version(root: Path) -> Tuple[str, int]:
    """``[project] version`` and the line it is declared on."""
    text = _read(root, PYPROJECT)
    try:
        declared = tomllib.loads(text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise Refusal(f"REFUSED: {PYPROJECT} has no readable [project] version ({exc}).") from exc
    if not isinstance(declared, str):
        raise Refusal(f"REFUSED: {PYPROJECT} [project] version is not a string: {declared!r}.")
    needle = f'version = "{declared}"'
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip() == needle:
            return declared, number
    return declared, 0


def fallback_version(root: Path) -> Tuple[str, int]:
    """The hard-coded literal in ``__init__.py``'s except branch.

    Found with ``ast`` rather than a regex because the try branch assigns a
    Call and the except branch assigns a Constant. The AST separates the
    two; a regex would have to guess which assignment it had found.
    """
    text = _read(root, INIT)
    tree = ast.parse(text, filename=INIT)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            for statement in handler.body:
                if not isinstance(statement, ast.Assign):
                    continue
                if not isinstance(statement.value, ast.Constant):
                    continue
                if not isinstance(statement.value.value, str):
                    continue
                for target in statement.targets:
                    if isinstance(target, ast.Name) and target.id == "__version__":
                        return statement.value.value, statement.lineno
    raise Refusal(
        f"REFUSED: {INIT} has no hard-coded __version__ fallback in an except branch. "
        "If the fallback was removed on purpose, remove this check with it."
    )


def module_constant(root: Path, rel: str, name: str) -> Tuple[str, int]:
    """A module-level ``NAME = "literal"`` assignment and its line."""
    tree = ast.parse(_read(root, rel), filename=rel)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not isinstance(statement.value, ast.Constant):
            continue
        if not isinstance(statement.value.value, str):
            continue
        for target in statement.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return statement.value.value, statement.lineno
    raise Refusal(f"REFUSED: {rel} declares no module-level string constant {name}.")


# ---------------------------------------------------------------- checks


def check_tag_shape(tag: str) -> str:
    """The tag is a release tag, and return the version it names."""
    match = TAG_RE.match(tag)
    if match is None:
        raise Refusal(
            f'REFUSED: "{tag}" is not a release tag. A release tag is '
            "v<major>.<minor>.<patch> with an optional a/b/rc pre-release suffix, "
            'so a working version such as "v0.3.0.dev0" cannot be released.'
        )
    return match.group("version")


def check_pyproject(root: Path, version: str) -> str:
    declared, line = pyproject_version(root)
    if declared != version:
        raise Refusal(
            f'REFUSED: {PYPROJECT}:{line} declares version "{declared}", '
            f'but the tag names "{version}".'
        )
    return f'{PYPROJECT}:{line} declares "{declared}"'


def check_fallback(root: Path, version: str) -> str:
    declared, line = fallback_version(root)
    if declared != version:
        raise Refusal(
            f'REFUSED: {INIT}:{line} falls back to "{declared}", ' f'but the tag names "{version}".'
        )
    return f'{INIT}:{line} falls back to "{declared}"'


def check_literals_agree(root: Path) -> str:
    """The two copies of the package version agree with EACH OTHER.

    Needs no tag, so it runs on every pull request through ``--check-tree``
    rather than only when someone pushes a tag. Without it, a version bump
    that forgets the fallback sits green on main until release day.
    """
    declared, declared_line = pyproject_version(root)
    fallback, fallback_line = fallback_version(root)
    if declared != fallback:
        raise Refusal(
            f'REFUSED: {PYPROJECT}:{declared_line} declares version "{declared}" '
            f'but {INIT}:{fallback_line} falls back to "{fallback}". '
            "The two copies of the package version must move together."
        )
    return f'the package version reads "{declared}" in both {PYPROJECT} and {INIT}'


def check_changelog(root: Path, version: str) -> str:
    path = root / CHANGELOG
    if not path.is_file():
        raise Refusal(f"REFUSED: {CHANGELOG} does not exist. A release needs a record.")
    found: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING_RE.match(line.strip())
        if match is not None:
            found.append(match.group("version"))
    if version not in found:
        raise Refusal(
            f"REFUSED: {CHANGELOG} has no `## {version}` section. "
            f"Sections found: {found or 'none'}."
        )
    return f"{CHANGELOG} has a `## {version}` section"


def check_mirrored_constants(root: Path) -> List[str]:
    """pipeline.py's copies agree with the modules that own them."""
    report: List[str] = []
    for name, owner in MIRRORED:
        mirrored, mirrored_line = module_constant(root, PIPELINE, name)
        owned, owned_line = module_constant(root, owner, name)
        if mirrored != owned:
            raise Refusal(
                f'REFUSED: {PIPELINE}:{mirrored_line} declares {name} = "{mirrored}" '
                f'but {owner}:{owned_line} declares "{owned}". '
                "pipeline.py re-declares this constant rather than importing it, "
                "and nothing else in the tree compares the copies."
            )
        report.append(f'{name} = "{owned}" in both {PIPELINE} and {owner}')
    return report


def check_ancestry(commit: str, root: Path, base: str = "origin/main") -> str:
    """The tagged commit is contained in *base*.

    The in-repository stand-in for tag protection the repository ruleset
    does not provide: it constrains what a tag may RELEASE, not what a tag
    may become afterwards.
    """
    resolved = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{base}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if resolved.returncode != 0:
        raise Refusal(
            f"REFUSED: cannot resolve {base} in {root}. "
            f"git said: {resolved.stderr.strip() or resolved.stdout.strip()}"
        )
    contained = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, base],
        capture_output=True,
        text=True,
        check=False,
    )
    if contained.returncode != 0:
        raise Refusal(
            f"REFUSED: commit {commit} is not contained in {base}. "
            "A release is cut from a commit that went through the merge gate, "
            f"not from a side branch. git said: "
            f"{contained.stderr.strip() or f'exit {contained.returncode}'}"
        )
    return f"commit {commit} is contained in {base}"


def check_installed(describe_path: Path, version: str) -> List[str]:
    """The freshly installed wheel describes itself as the tagged version."""
    if not describe_path.is_file():
        raise Refusal(f"REFUSED: no describe output at {describe_path}.")
    try:
        artifacts = json.loads(describe_path.read_text(encoding="utf-8"))["artifacts"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise Refusal(
            f"REFUSED: {describe_path} is not describe --self --json output ({exc})."
        ) from exc
    if not isinstance(artifacts, dict):
        raise Refusal(
            f"REFUSED: {describe_path} has an 'artifacts' that is not an object: "
            f"{type(artifacts).__name__}."
        )
    reported = artifacts.get("version")
    if reported != version:
        raise Refusal(
            f"REFUSED: the installed wheel reports version {reported!r}, "
            f"but the tag names {version!r}. The artifact about to be published "
            "disagrees with the tag it would be published under."
        )
    source = artifacts.get("spec_source")
    if source != "flux_spec":
        raise Refusal(
            f"REFUSED: the installed wheel resolved its spec through {source!r} rather than "
            "the declared flux_spec distribution, so it would ship a different "
            "enforcement decision than the one that was tested."
        )
    return [
        f"the installed wheel reports version {reported!r}",
        f"spec {artifacts.get('spec_version')} via {source}",
    ]


# ------------------------------------------------------------- run modes


def run_tree_checks(root: Path) -> List[str]:
    """Everything that needs no tag."""
    report = [check_literals_agree(root)]
    report.extend(check_mirrored_constants(root))
    return report


def run_release_checks(
    root: Path, tag: str, commit: str, base: str = "origin/main"
) -> Tuple[str, bool, List[str]]:
    """Every check a tag must pass. Returns (version, prerelease, report)."""
    version = check_tag_shape(tag)
    report = [f'the tag "{tag}" names version "{version}"']
    report.append(check_pyproject(root, version))
    report.append(check_fallback(root, version))
    report.append(check_changelog(root, version))
    report.extend(check_mirrored_constants(root))
    report.append(check_ancestry(commit, root, base=base))
    return version, PRERELEASE_RE.search(version) is not None, report


# ------------------------------------------------------------- fixtures


def _substitute(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"fixture edit found nothing to replace in {path}: {old!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def build_fixture(
    dest: Path,
    root: Path,
    *,
    version: str = "0.3.0",
    fallback: Optional[str] = None,
    changelog: Optional[str] = "0.3.0",
    seed_tree: Optional[str] = None,
    trace: Optional[str] = None,
) -> Tuple[Path, str]:
    """A copy of the REAL files, with exactly one thing bent.

    Copies rather than synthesises, so the parsers are exercised against the
    shapes that actually ship: the try/except in ``__init__.py``, the
    ``__all__`` list in pipeline.py that names the same constants it
    assigns, and pyproject.toml's several other ``*version*`` keys.

    Every fixture is a real git repository with its single commit reachable
    from ``origin/main``, so the ancestry check PASSES here. That is not a
    convenience: without it, a fixture built to break the pyproject check
    would be refused by the ancestry check instead, and the self-test would
    record the pyproject check as firing when it had not run at all.

    Returns (path, the commit on main).
    """
    dest.mkdir(parents=True, exist_ok=True)
    for rel in FIXTURE_FILES:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, target)

    real_version, _ = pyproject_version(root)
    real_fallback, _ = fallback_version(root)

    _substitute(dest / PYPROJECT, f'version = "{real_version}"', f'version = "{version}"')
    _substitute(
        dest / INIT,
        f'__version__ = "{real_fallback}"',
        f'__version__ = "{version if fallback is None else fallback}"',
    )
    for name, override in (("SEED_TREE_VERSION", seed_tree), ("TRACE_VERSION", trace)):
        if override is None:
            continue
        real, _ = module_constant(root, PIPELINE, name)
        _substitute(dest / PIPELINE, f'{name} = "{real}"', f'{name} = "{override}"')
    if changelog is not None:
        (dest / CHANGELOG).write_text(
            f"# Changelog\n\n## {changelog}\n\n- The first release.\n",
            encoding="utf-8",
        )

    _git(dest, "init", "--quiet")
    _git(dest, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(dest, "add", "--all")
    _git(dest, "commit", "--quiet", "-m", "fixture")
    on_main = _git(dest, "rev-parse", "HEAD")
    _git(dest, "update-ref", "refs/remotes/origin/main", on_main)
    return dest, on_main


def _git(repo: Path, *args: str) -> str:
    environment = dict(os.environ)
    # A developer's global config can carry a commit template, a signing
    # key or a hooks path, any of which would make this throwaway repository
    # behave differently on their machine than on a runner.
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=release guard self-test",
            "-c",
            "user.email=release-guard@invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return result.stdout.strip()


def build_git_fixture(dest: Path) -> Tuple[Path, str, str]:
    """A throwaway repository with one commit on main and one off it.

    Returns (repo, commit_on_main, commit_off_main).
    """
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "--quiet")
    _git(dest, "symbolic-ref", "HEAD", "refs/heads/main")
    (dest / "file.txt").write_text("one\n", encoding="utf-8")
    _git(dest, "add", "file.txt")
    _git(dest, "commit", "--quiet", "-m", "on main")
    on_main = _git(dest, "rev-parse", "HEAD")
    # The remote-tracking ref the real check reads. Created directly rather
    # than by cloning, so the fixture needs no second repository.
    _git(dest, "update-ref", "refs/remotes/origin/main", on_main)
    _git(dest, "checkout", "--quiet", "-b", "side")
    (dest / "file.txt").write_text("two\n", encoding="utf-8")
    _git(dest, "add", "file.txt")
    _git(dest, "commit", "--quiet", "-m", "off main")
    off_main = _git(dest, "rev-parse", "HEAD")
    _git(dest, "checkout", "--quiet", "main")
    return dest, on_main, off_main


# ------------------------------------------------------------- self-test


def self_test(root: Path) -> int:
    """Every check refuses a fixture built to break it, and passes one built to pass.

    A check that has silently stopped firing fails HERE, loudly, rather
    than blessing the next release. A green arm is in the list for the same
    reason in the other direction: a check that only ever refuses is as
    useless as one that only ever passes.

    Each refusing arm also names a fragment its refusal must contain. A
    Refusal on its own is not evidence that the check under test fired:
    several checks run in sequence over one fixture, so a later one
    refusing would otherwise be recorded as the earlier one working.
    """
    broken: List[str] = []
    passed = 0

    with tempfile.TemporaryDirectory(prefix="release-guard-selftest-") as raw:
        scratch = Path(raw)
        git_repo, on_main_only, off_main = build_git_fixture(scratch / "git")

        mismatched, mismatched_head = build_fixture(
            scratch / "mismatched-pyproject", root, version="0.3.0.dev0"
        )
        stale, stale_head = build_fixture(scratch / "stale-fallback", root, fallback="0.3.0.dev0")
        no_changelog, no_changelog_head = build_fixture(
            scratch / "no-changelog", root, changelog=None
        )
        wrong_changelog, wrong_changelog_head = build_fixture(
            scratch / "wrong-changelog", root, changelog="0.0.9"
        )
        bent_trace, _ = build_fixture(scratch / "bent-trace", root, trace="flux-trace/2")
        bent_seed, _ = build_fixture(scratch / "bent-seed", root, seed_tree="flux-seed/2")
        green, green_head = build_fixture(scratch / "green", root)
        prerelease, prerelease_head = build_fixture(
            scratch / "prerelease", root, version="0.3.0rc1", changelog="0.3.0rc1"
        )
        output = scratch / "github-output.txt"

        must_refuse: Sequence[Tuple[str, str, Callable[[], object]]] = (
            (
                "a .dev tag is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("v0.3.0.dev0"),
            ),
            (
                "a bare number is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("0.3.0"),
            ),
            (
                "a .post tag is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("v0.3.0.post1"),
            ),
            (
                "pyproject.toml disagreeing with the tag",
                'declares version "0.3.0.dev0"',
                lambda: run_release_checks(mismatched, "v0.3.0", mismatched_head),
            ),
            (
                "a stale __version__ fallback",
                'falls back to "0.3.0.dev0"',
                lambda: run_release_checks(stale, "v0.3.0", stale_head),
            ),
            (
                "the two version literals disagreeing, with no tag in sight",
                "must move together",
                lambda: run_tree_checks(stale),
            ),
            (
                "no CHANGELOG.md at all",
                "A release needs a record",
                lambda: run_release_checks(no_changelog, "v0.3.0", no_changelog_head),
            ),
            (
                "a CHANGELOG.md with no section for this version",
                "has no `## 0.3.0` section",
                lambda: run_release_checks(wrong_changelog, "v0.3.0", wrong_changelog_head),
            ),
            (
                "a TRACE_VERSION copy that disagrees",
                'TRACE_VERSION = "flux-trace/2"',
                lambda: run_tree_checks(bent_trace),
            ),
            (
                "a SEED_TREE_VERSION copy that disagrees",
                'SEED_TREE_VERSION = "flux-seed/2"',
                lambda: run_tree_checks(bent_seed),
            ),
            (
                "a commit that never went through main",
                "is not contained in origin/main",
                lambda: check_ancestry(off_main, git_repo),
            ),
            (
                "a describe.json whose version is not the tagged one",
                "the installed wheel reports version '0.3.0.dev0'",
                lambda: check_installed(_describe_fixture(scratch, "0.3.0.dev0"), "0.3.0"),
            ),
            (
                "a describe.json running the vendored spec fallback",
                "resolved its spec through 'vendored'",
                lambda: check_installed(
                    _describe_fixture(scratch, "0.3.0", source="vendored"), "0.3.0"
                ),
            ),
        )

        for name, expect, call in must_refuse:
            try:
                call()
            except Refusal as refusal:
                if expect in str(refusal):
                    passed += 1
                else:
                    broken.append(
                        f"BROKEN {name}: refused, but for another reason. "
                        f"Expected a message containing {expect!r}, got: {refusal}"
                    )
            else:
                broken.append(f"BROKEN {name}: the check PASSED a fixture built to fail it")

        must_pass: Sequence[Tuple[str, Callable[[], object]]] = (
            (
                "a consistent tree and tag, classified as a full release",
                lambda: _expect_outputs(green, "v0.3.0", green_head, output, "false"),
            ),
            ("a consistent tree with no tag", lambda: run_tree_checks(green)),
            ("a commit that is on main", lambda: check_ancestry(on_main_only, git_repo)),
            (
                "a describe.json that agrees with the tag",
                lambda: check_installed(_describe_fixture(scratch, "0.3.0"), "0.3.0"),
            ),
            (
                "a pre-release tag, classified as a pre-release",
                lambda: _expect_outputs(prerelease, "v0.3.0rc1", prerelease_head, output, "true"),
            ),
        )

        for name, call in must_pass:
            try:
                call()
            except Refusal as exc:
                broken.append(
                    f"BROKEN {name}: the check REFUSED a fixture built to pass it ({exc})"
                )
            else:
                passed += 1

    if broken:
        for line in broken:
            print(line)
        print(f"self-test: {len(broken)} check(s) did not fire")
        return 1
    print(f"self-test: {passed} checks all fired, each against a fixture built for it")
    return 0


def _describe_fixture(scratch: Path, version: str, *, source: str = "flux_spec") -> Path:
    path = scratch / f"describe-{version}-{source}.json"
    path.write_text(
        json.dumps(
            {
                "ok": True,
                "exitCode": 0,
                "artifacts": {
                    "version": version,
                    "spec_version": "0.5.0",
                    "spec_source": source,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _expect_outputs(root: Path, tag: str, commit: str, output: Path, prerelease: str) -> None:
    """The green path writes the outputs the release workflow consumes.

    The release job reads ``needs.guard.outputs.version`` and
    ``needs.guard.outputs.prerelease``. If the guard stopped writing them,
    the workflow would build a wheel at an empty version and publish it
    with no pre-release flag, so this arm checks the file, not just the
    return code.
    """
    version, is_prerelease, _ = run_release_checks(root, tag, commit)
    output.write_text("", encoding="utf-8")
    write_github_output(output, version, is_prerelease)
    written = output.read_text(encoding="utf-8")
    expected = f"version={version}\nprerelease={prerelease}\n"
    if written != expected:
        raise Refusal(f"REFUSED: {tag} wrote {written!r} to GITHUB_OUTPUT, expected {expected!r}")


# ---------------------------------------------------------------- output


def write_github_output(path: Path, version: str, prerelease: bool) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"version={version}\n")
        handle.write(f"prerelease={'true' if prerelease else 'false'}\n")


# ------------------------------------------------------------------ main


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release_guard.py",
        description="Refuse a release whose version strings disagree with the tag.",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="the tree to check")
    parser.add_argument("--self-test", action="store_true", help="every check refuses a fixture")
    parser.add_argument("--check-tree", action="store_true", help="the checks that need no tag")
    parser.add_argument("--tag", help="the release tag, e.g. v0.3.0")
    parser.add_argument("--commit", help="the commit the tag points at")
    parser.add_argument("--base", default="origin/main", help="the ref a release must come from")
    parser.add_argument("--github-output", type=Path, help="append version/prerelease here")
    parser.add_argument("--check-installed", action="store_true", help="read describe.json")
    parser.add_argument("--version", help="the version --check-installed expects")
    parser.add_argument("--describe", type=Path, help="fluxsim describe --self --json output")
    args = parser.parse_args(argv)

    root = args.root.resolve()

    try:
        if args.self_test:
            return self_test(root)

        if args.check_installed:
            if not args.version or args.describe is None:
                parser.error("--check-installed needs --version and --describe")
            for line in check_installed(args.describe, args.version):
                print(line)
            return 0

        if args.check_tree:
            for line in run_tree_checks(root):
                print(line)
            return 0

        if not args.tag or not args.commit:
            parser.error("pass --self-test, --check-tree, --check-installed, or --tag and --commit")

        version, prerelease, report = run_release_checks(root, args.tag, args.commit, args.base)
        for line in report:
            print(line)
        if args.github_output is not None:
            write_github_output(args.github_output, version, prerelease)
        print(f"release {version} may be cut (prerelease={'true' if prerelease else 'false'})")
        return 0
    except Refusal as refusal:
        print(refusal, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
