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

flux-stats, the second distribution this repository releases (ADR 0009),
moves in lockstep with them: one tag releases both at one version, and the
engine pins exactly that release. Two more strings, then, that must read the
same version:

    packages/flux-stats/pyproject.toml  [project] version
    pyproject.toml                      the ``flux-stats==`` requirement

The except branch never executes in CI, because every job pip-installs the
package first and ``importlib.metadata`` always wins. The one test that
touches ``__version__`` compares ``fluxsim --version`` against that same
imported value, so it is a tautology that cannot observe a mismatch. A text
check over the two literals is the only thing that can, and that is what
this script is.

What this script deliberately does NOT check is the kernel version
(``DEFAULT_KERNEL`` and ``KERNELS`` in flux_engine/core/kernels.py). A
``kernelVersion`` is the semver of normative kernel behaviour, it is hashed
into ``inputDigest``, and it is NOT the package version. It needs no check
here because it already has a stronger one: moving the default kernel moves
every pinned digest of a new plan, which turns ``make test-digests`` red on
every cell of the matrix a release waits on. Adding a string check for it
would duplicate a stronger existing check and invite someone to treat the
string check as the real gate. ``SEED_TREE_VERSION`` and ``TRACE_VERSION``
are declared once each, in flux_engine/core/seedtree.py and
flux_engine/core/trace.py, and the kernel registry imports them, so there is
no copy of either for a string check to compare.

Four modes:

    --self-test
        Build a broken fixture per check and require each one to refuse,
        plus green arms that must pass. A check that has silently stopped
        firing fails here rather than blessing a release. Same shape as
        ``tools/product_guardrail/check.py --self-test``.

    --check-tree
        The checks that need no tag: the two literals agree with each
        other, and flux-stats's version and the engine's pin of it agree
        with the engine's. Cheap enough for the pull-request suite.

    --tag vX.Y.Z --commit SHA [--github-output FILE]
        The release check. Tag shape, both version literals, flux-stats's
        version and the pin, a CHANGELOG section, and containment in main.
        Writes ``version`` and ``prerelease`` to GITHUB_OUTPUT.

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
CHANGELOG = "CHANGELOG.md"
#: flux-stats's own pyproject.toml. Released from the same tag as the engine.
STATS_PYPROJECT = "packages/flux-stats/pyproject.toml"

#: The engine's requirement on flux-stats: the distribution name in any PEP 503
#: spelling (``flux-stats``, ``flux_stats``, ``Flux.Stats``), and not a longer
#: name that starts with it (``flux-stats-extras``), then for the pin an exact
#: ``==`` and nothing else.
STATS_REQUIREMENT_RE = re.compile(r"^\s*flux[-_.]+stats(?![\w.-])", re.IGNORECASE)
STATS_PIN_RE = re.compile(
    r"^\s*flux[-_.]+stats\s*==(?!=)\s*(?P<version>[^\s,;\[\]@=]+)\s*$", re.IGNORECASE
)

#: Every file a fixture needs a real copy of.
FIXTURE_FILES = (PYPROJECT, INIT, STATS_PYPROJECT)


class Refusal(Exception):
    """A check refused. The message is what the log prints."""


# --------------------------------------------------------------- readers


def _read(root: Path, rel: str) -> str:
    path = root / rel
    if not path.is_file():
        raise Refusal(f"REFUSED: {rel} does not exist under {root}.")
    return path.read_text(encoding="utf-8")


def _project_version(root: Path, rel: str) -> Tuple[str, int]:
    """``[project] version`` of the pyproject.toml at *rel*, and its line."""
    text = _read(root, rel)
    try:
        declared = tomllib.loads(text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise Refusal(f"REFUSED: {rel} has no readable [project] version ({exc}).") from exc
    if not isinstance(declared, str):
        raise Refusal(f"REFUSED: {rel} [project] version is not a string: {declared!r}.")
    needle = f'version = "{declared}"'
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip() == needle:
            return declared, number
    return declared, 0


def pyproject_version(root: Path) -> Tuple[str, int]:
    """The engine's ``[project] version`` and the line it is declared on."""
    return _project_version(root, PYPROJECT)


def stats_version(root: Path) -> Tuple[str, int]:
    """flux-stats's ``[project] version`` and the line it is declared on."""
    return _project_version(root, STATS_PYPROJECT)


def stats_pin(root: Path) -> Tuple[str, int]:
    """The flux-stats version the engine depends on, and the line that pins it.

    Refuses anything but one exact ``==`` pin: the engine computes its
    verdicts with flux-stats's definitions, so a range would let an install
    pair this engine with statistics it was never tested with.
    """
    text = _read(root, PYPROJECT)
    try:
        dependencies = tomllib.loads(text)["project"]["dependencies"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise Refusal(
            f"REFUSED: {PYPROJECT} has no readable [project] dependencies ({exc})."
        ) from exc
    requirements = [
        entry
        for entry in dependencies
        if isinstance(entry, str) and STATS_REQUIREMENT_RE.match(entry)
    ]
    if len(requirements) != 1:
        raise Refusal(
            f"REFUSED: {PYPROJECT} declares flux-stats {len(requirements)} times in "
            "[project] dependencies; the engine depends on it exactly once."
        )
    (requirement,) = requirements
    match = STATS_PIN_RE.match(requirement)
    if match is None:
        raise Refusal(
            f'REFUSED: {PYPROJECT} requires "{requirement}", which is not a bare exact pin '
            "(no range, extra, marker or URL). "
            "flux-stats is released in lockstep with the engine, and the engine "
            "depends on exactly the flux-stats released with it (flux-stats==X.Y.Z)."
        )
    for number, line in enumerate(text.splitlines(), start=1):
        if f'"{requirement}"' in line:
            return match.group("version"), number
    return match.group("version"), 0


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


# ---------------------------------------------------------------- checks


def check_tag_shape(tag: str) -> str:
    """The tag is a release tag, and return the version it names."""
    match = TAG_RE.match(tag)
    if match is None:
        raise Refusal(
            f'REFUSED: "{tag}" is not a release tag. A release tag is '
            "v<major>.<minor>.<patch> with an optional a/b/rc pre-release suffix, "
            'so a working version such as "v0.5.0.dev0" cannot be released.'
        )
    return match.group("version")


def _agrees_with_tag(found: str, version: str, refusal: str, reading: str) -> str:
    """Return *reading* when *found* is the tag's *version*, and refuse with *refusal* if not."""
    if found != version:
        raise Refusal(refusal)
    return reading


def check_pyproject(root: Path, version: str) -> str:
    declared, line = pyproject_version(root)
    return _agrees_with_tag(
        declared,
        version,
        f'REFUSED: {PYPROJECT}:{line} declares version "{declared}", '
        f'but the tag names "{version}".',
        f'{PYPROJECT}:{line} declares "{declared}"',
    )


def check_fallback(root: Path, version: str) -> str:
    declared, line = fallback_version(root)
    return _agrees_with_tag(
        declared,
        version,
        f'REFUSED: {INIT}:{line} falls back to "{declared}", but the tag names "{version}".',
        f'{INIT}:{line} falls back to "{declared}"',
    )


def check_stats_version(root: Path, version: str) -> str:
    declared, line = stats_version(root)
    return _agrees_with_tag(
        declared,
        version,
        f'REFUSED: {STATS_PYPROJECT}:{line} declares version "{declared}", '
        f'but the tag names "{version}". flux-stats is released with the engine, '
        "from the same tag and at the same version.",
        f'{STATS_PYPROJECT}:{line} declares "{declared}"',
    )


def check_stats_pin(root: Path, version: str) -> str:
    pinned, line = stats_pin(root)
    return _agrees_with_tag(
        pinned,
        version,
        f"REFUSED: {PYPROJECT}:{line} pins flux-stats=={pinned}, "
        f'but the tag names "{version}". The engine depends on exactly the '
        "flux-stats released with it.",
        f"{PYPROJECT}:{line} pins flux-stats=={pinned}",
    )


def check_lockstep(root: Path) -> str:
    """flux-stats's version and the engine's pin of it agree with the engine's version.

    Needs no tag, so a version bump that forgets flux-stats is refused on
    the pull request that makes it rather than on release day.
    """
    engine, engine_line = pyproject_version(root)
    stats, stats_line = stats_version(root)
    pinned, pin_line = stats_pin(root)
    if not engine == stats == pinned:
        raise Refusal(
            "REFUSED: flux-sim-engine and flux-stats are released in lockstep, but "
            f'{PYPROJECT}:{engine_line} declares "{engine}", '
            f'{STATS_PYPROJECT}:{stats_line} declares "{stats}" and '
            f"{PYPROJECT}:{pin_line} pins flux-stats=={pinned}. "
            "The three move together."
        )
    return f'flux-stats reads "{stats}" in {STATS_PYPROJECT}, and the engine pins it exactly'


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
    return [check_literals_agree(root), check_lockstep(root)]


def run_release_checks(
    root: Path, tag: str, commit: str, base: str = "origin/main"
) -> Tuple[str, bool, List[str]]:
    """Every check a tag must pass. Returns (version, prerelease, report)."""
    version = check_tag_shape(tag)
    report = [f'the tag "{tag}" names version "{version}"']
    report.append(check_pyproject(root, version))
    report.append(check_fallback(root, version))
    report.append(check_stats_version(root, version))
    report.append(check_stats_pin(root, version))
    report.append(check_changelog(root, version))
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
    version: str = "0.5.0",
    fallback: Optional[str] = None,
    changelog: Optional[str] = "0.5.0",
    stats: Optional[str] = None,
    stats_requirement: Optional[str] = None,
    drop_stats_pyproject: bool = False,
) -> Tuple[Path, str]:
    """A copy of the REAL files, with exactly one thing bent.

    Copies rather than synthesises, so the parsers are exercised against the
    shapes that actually ship: the try/except in ``__init__.py`` and
    pyproject.toml's several other ``*version*`` keys.

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
    real_stats, _ = stats_version(root)
    real_pin, _ = stats_pin(root)

    _substitute(dest / PYPROJECT, f'version = "{real_version}"', f'version = "{version}"')
    _substitute(
        dest / INIT,
        f'__version__ = "{real_fallback}"',
        f'__version__ = "{version if fallback is None else fallback}"',
    )
    # flux-stats follows the engine's version unless a fixture bends it.
    _substitute(
        dest / STATS_PYPROJECT,
        f'version = "{real_stats}"',
        f'version = "{version if stats is None else stats}"',
    )
    _substitute(
        dest / PYPROJECT,
        f'"flux-stats=={real_pin}"',
        stats_requirement or f'"flux-stats=={version}"',
    )
    if drop_stats_pyproject:
        (dest / STATS_PYPROJECT).unlink()
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
            scratch / "mismatched-pyproject", root, version="0.5.0.dev0"
        )
        stale, stale_head = build_fixture(scratch / "stale-fallback", root, fallback="0.5.0.dev0")
        no_changelog, no_changelog_head = build_fixture(
            scratch / "no-changelog", root, changelog=None
        )
        wrong_changelog, wrong_changelog_head = build_fixture(
            scratch / "wrong-changelog", root, changelog="0.0.9"
        )
        stats_behind, stats_behind_head = build_fixture(
            scratch / "stats-behind", root, stats="0.4.9"
        )
        pin_behind, pin_behind_head = build_fixture(
            scratch / "pin-behind", root, stats_requirement='"flux-stats==0.4.9"'
        )
        pin_range, pin_range_head = build_fixture(
            scratch / "pin-range", root, stats_requirement='"flux-stats>=0.5,<0.6"'
        )
        pin_missing, _ = build_fixture(
            scratch / "pin-missing", root, stats_requirement='"flux-stats-extras==0.5.0"'
        )
        pin_twice, _ = build_fixture(
            scratch / "pin-twice",
            root,
            stats_requirement='"flux-stats==0.5.0", "flux_stats==0.5.0"',
        )
        no_stats, _ = build_fixture(scratch / "no-stats-pyproject", root, drop_stats_pyproject=True)
        green, green_head = build_fixture(scratch / "green", root)
        prerelease, prerelease_head = build_fixture(
            scratch / "prerelease", root, version="0.5.0rc1", changelog="0.5.0rc1"
        )
        output = scratch / "github-output.txt"

        must_refuse: Sequence[Tuple[str, str, Callable[[], object]]] = (
            (
                "a .dev tag is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("v0.5.0.dev0"),
            ),
            (
                "a bare number is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("0.5.0"),
            ),
            (
                "a .post tag is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("v0.5.0.post1"),
            ),
            (
                "pyproject.toml disagreeing with the tag",
                'declares version "0.5.0.dev0"',
                lambda: run_release_checks(mismatched, "v0.5.0", mismatched_head),
            ),
            (
                "a stale __version__ fallback",
                'falls back to "0.5.0.dev0"',
                lambda: run_release_checks(stale, "v0.5.0", stale_head),
            ),
            (
                "the two version literals disagreeing, with no tag in sight",
                "must move together",
                lambda: run_tree_checks(stale),
            ),
            (
                "flux-stats's version disagreeing with the tag",
                "flux-stats is released with the engine",
                lambda: run_release_checks(stats_behind, "v0.5.0", stats_behind_head),
            ),
            (
                "the engine's flux-stats pin disagreeing with the tag",
                "pins flux-stats==0.4.9",
                lambda: run_release_checks(pin_behind, "v0.5.0", pin_behind_head),
            ),
            (
                "flux-stats out of lockstep, with no tag in sight",
                "are released in lockstep",
                lambda: run_tree_checks(stats_behind),
            ),
            (
                "the engine's flux-stats pin out of lockstep, with no tag in sight",
                "are released in lockstep",
                lambda: run_tree_checks(pin_behind),
            ),
            (
                "a flux-stats requirement that is a range rather than a pin",
                "is not a bare exact pin",
                lambda: run_release_checks(pin_range, "v0.5.0", pin_range_head),
            ),
            (
                "no flux-stats requirement, only a longer name that starts with it",
                "declares flux-stats 0 times",
                lambda: run_tree_checks(pin_missing),
            ),
            (
                "flux-stats required twice, in two spellings",
                "declares flux-stats 2 times",
                lambda: run_tree_checks(pin_twice),
            ),
            (
                "no packages/flux-stats/pyproject.toml",
                f"{STATS_PYPROJECT} does not exist",
                lambda: run_tree_checks(no_stats),
            ),
            (
                "no CHANGELOG.md at all",
                "A release needs a record",
                lambda: run_release_checks(no_changelog, "v0.5.0", no_changelog_head),
            ),
            (
                "a CHANGELOG.md with no section for this version",
                "has no `## 0.5.0` section",
                lambda: run_release_checks(wrong_changelog, "v0.5.0", wrong_changelog_head),
            ),
            (
                "a commit that never went through main",
                "is not contained in origin/main",
                lambda: check_ancestry(off_main, git_repo),
            ),
            (
                "a describe.json whose version is not the tagged one",
                "the installed wheel reports version '0.5.0.dev0'",
                lambda: check_installed(_describe_fixture(scratch, "0.5.0.dev0"), "0.5.0"),
            ),
            (
                "a describe.json running the vendored spec fallback",
                "resolved its spec through 'vendored'",
                lambda: check_installed(
                    _describe_fixture(scratch, "0.5.0", source="vendored"), "0.5.0"
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
                lambda: _expect_outputs(green, "v0.5.0", green_head, output, "false"),
            ),
            ("a consistent tree with no tag", lambda: run_tree_checks(green)),
            ("a commit that is on main", lambda: check_ancestry(on_main_only, git_repo)),
            (
                "a describe.json that agrees with the tag",
                lambda: check_installed(_describe_fixture(scratch, "0.5.0"), "0.5.0"),
            ),
            (
                "a pre-release tag, classified as a pre-release",
                lambda: _expect_outputs(prerelease, "v0.5.0rc1", prerelease_head, output, "true"),
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
    parser.add_argument("--tag", help="the release tag, e.g. v0.5.0")
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
