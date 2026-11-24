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

flux-stats, the second distribution this repository releases (ADR 0009), is
versioned by its own format and API, and the engine requires a RANGE of it
(``flux-stats>=0.11,<0.12``), not one release. Two more places, then, that have
to agree:

    packages/flux-stats/pyproject.toml  [project] version
    pyproject.toml                      the ``flux-stats`` requirement

The requirement has to be a plain version range with a floor and a ceiling, and
it has to admit that version. A tag adds one constraint of its own: the release
workflow builds flux-stats's files at the tag's version, so at a tag the
version in ``packages/flux-stats/pyproject.toml`` is the tag's.

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
        other, and the engine's requirement on flux-stats is a plain range
        that admits flux-stats's version. Cheap enough for the pull-request
        suite. With ``--require-skills`` it also refuses a tree that lacks
        any of the eight agent skills (ADR 0033); every tag check requires
        them whatever the flag.

    --tag vX.Y.Z --commit SHA [--github-output FILE]
        The release check. Tag shape, both version literals, flux-stats's
        version and the requirement, a CHANGELOG section, and containment in
        main. Writes ``version`` and ``prerelease`` to GITHUB_OUTPUT.

    --check-installed --version X.Y.Z --describe describe.json
        Reads built-and-installed metadata rather than repository text:
        the version the freshly installed wheel reports about itself.

Standard library only, so it runs before any dependency is installed; the skills check
loads scripts/check_skills.py, whose top level is standard library only too. That is
why it orders versions itself, for the subset of PEP 440 a release uses: a
version is ``N.N.N`` with an optional ``aN``, ``bN`` or ``rcN``, and a range is
comparisons joined by commas (``>=``, ``>``, ``<``, ``<=``, ``==``). Every other
spelling is refused by name, never ordered wrongly. ``packaging`` is the
reference implementation and the oracle in tests/release/test_release_guard.py,
which sets this script's answers against its own; it is never imported here.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Callable, List, NamedTuple, Optional, Sequence, Tuple

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
#: name that starts with it (``flux-stats-extras``).
STATS_REQUIREMENT_RE = re.compile(r"^\s*flux[-_.]+stats(?![\w.-])", re.IGNORECASE)

#: A version this guard orders: a release segment of dot-separated numbers, then
#: an optional ``aN``, ``bN`` or ``rcN`` pre-release. Every number is at most
#: nine digits, which no release comes near and which keeps ``int()`` cheap.
#: ASCII only, because ``\d`` alone takes any Unicode decimal digit and pip does
#: not. Matched with ``fullmatch``, never ``match``: ``$`` would let a trailing
#: newline through.
VERSION_RE = re.compile(
    r"(?P<release>\d{1,9}(?:\.\d{1,9})*)(?:(?P<phase>a|b|rc)(?P<number>\d{1,9}))?", re.ASCII
)

#: The pre-release phases in the order PEP 440 sorts them, and the place a final
#: release takes after every one of them.
PHASES = {"a": 0, "b": 1, "rc": 2}
FINAL = 3

#: The comparisons a requirement on flux-stats may use, and which of them bound
#: a range from below and from above. ``==`` bounds both. ``~=``, ``!=`` and
#: ``===`` are PEP 440 too, and a release has no use for them here, so they are
#: refused rather than half read.
OPERATORS = frozenset({">=", ">", "<", "<=", "=="})
FLOORS = frozenset({">=", ">", "=="})
CEILINGS = frozenset({"<", "<=", "=="})

#: Every file a fixture needs a real copy of.
FIXTURE_FILES = (PYPROJECT, INIT, STATS_PYPROJECT)

#: The requirement on flux-stats a fixture carries unless it bends it: a floor and
#: a ceiling around the fixtures' own version, as the real tree's has.
FIXTURE_STATS_REQUIREMENT = '"flux-stats>=0.11,<0.12"'


SKILLS_CHECK = Path(__file__).resolve().with_name("check_skills.py")


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


def engine_stats_requirement(root: Path) -> Tuple[str, int]:
    """The engine's one requirement on flux-stats, as pyproject.toml writes it, and its line.

    Refuses a tree that declares it other than once. What the requirement
    says is read by :func:`read_stats_range`.
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
    for number, line in enumerate(text.splitlines(), start=1):
        if f'"{requirement}"' in line:
            return requirement, number
    return requirement, 0


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


# ------------------------------------------------- versions and ranges
#
# The subset of PEP 440 a release uses, ordered here because this script runs
# before anything is installed. The rules are the PyPA "Version specifiers"
# specification's: the release segment is compared as if padded with zeros
# (0.11 is 0.11.0), a pre-release sorts below its release, and the exclusive
# comparison ``<V`` does not admit a pre-release of V unless V is itself one.
# ``packaging`` (PyPA) is the reference implementation; the tests set this
# code's answers against its own and never import it here.


class Version(NamedTuple):
    """A version as this guard reads it."""

    #: The release segment with its trailing zeros dropped, so 0.11 and 0.11.0 are one key.
    release: Tuple[int, ...]
    #: ``(phase, number)`` of an ``aN``, ``bN`` or ``rcN`` pre-release; ``None`` for a final release.
    pre: Optional[Tuple[int, int]]

    @property
    def order(self) -> Tuple[Tuple[int, ...], Tuple[int, int]]:
        """The key that sorts versions: the release, then the pre-release, a final one last."""
        return self.release, (FINAL, 0) if self.pre is None else self.pre


def _shown(text: str) -> str:
    """*text* for a refusal: quoted, with control characters visible, and not unbounded."""
    return repr(text if len(text) <= 60 else text[:57] + "...")


def parse_version(text: str) -> Version:
    """Read *text* as a version, or refuse it by name.

    Anything outside ``N.N.N`` with an optional ``aN``, ``bN`` or ``rcN`` is
    refused (a ``.devN`` or ``.postN`` release, an epoch, a local label, a
    wildcard, a longer number), because ordering a spelling this code does not
    understand is how a range ends up admitting what it should keep out.
    """
    match = VERSION_RE.fullmatch(text)
    if match is None:
        raise Refusal(
            f"REFUSED: {_shown(text)} is not a version this guard reads. It reads N.N.N with "
            "an optional aN, bN or rcN suffix, each number at most nine digits, and refuses "
            "every other spelling rather than order it wrongly."
        )
    release = [int(part) for part in match.group("release").split(".")]
    while release and release[-1] == 0:
        release.pop()
    phase = match.group("phase")
    pre = None if phase is None else (PHASES[phase], int(match.group("number")))
    return Version(tuple(release), pre)


def parse_specifier(text: str) -> List[Tuple[str, Version]]:
    """The clauses of a plain version range such as ``>=0.11,<0.12``, or a refusal."""
    clauses: List[Tuple[str, Version]] = []
    for part in text.split(","):
        # PEP 508 allows spaces and tabs around a comparison, and nothing else.
        clause = part.strip(" \t")
        match = re.fullmatch(r"(?P<operator>[<>=!~]*)(?P<version>.*)", clause, re.DOTALL)
        if match is None or match.group("operator") not in OPERATORS:
            raise Refusal(
                f"REFUSED: {_shown(text)} is not a plain version range: {_shown(clause)} is "
                "not one of >=, >, <, <= or == followed by a version."
            )
        clauses.append(
            (match.group("operator"), parse_version(match.group("version").strip(" \t")))
        )
    return clauses


def clause_admits(operator: str, bound: Version, version: Version) -> bool:
    """Whether *version* satisfies one comparison with *bound*, as pip reads it with ``--pre``."""
    if operator == "==":
        return version.order == bound.order
    if operator == ">=":
        return version.order >= bound.order
    if operator == "<=":
        return version.order <= bound.order
    if operator == ">":
        return version.order > bound.order
    if operator != "<":
        raise AssertionError(f"parse_specifier let {operator!r} through")
    if version.order >= bound.order:
        return False
    # PEP 440: <V does not admit a pre-release of V itself, unless V is one. Without
    # this, 0.12.0rc1 would sit under "<0.12" and a tree at 0.12.0rc1 would pass a
    # requirement pip would never resolve it under.
    return not (version.pre is not None and bound.pre is None and version.release == bound.release)


def specifier_admits(specifier: str, version: str) -> bool:
    """Whether *version* satisfies every clause of *specifier*."""
    candidate = parse_version(version)
    return all(
        clause_admits(operator, bound, candidate) for operator, bound in parse_specifier(specifier)
    )


def read_stats_range(requirement: str) -> List[Tuple[str, Version]]:
    """The clauses of the engine's requirement on flux-stats, or a refusal.

    The requirement is a plain range with a floor and a ceiling. A floor says
    which flux-stats releases have what the engine uses. A ceiling keeps out
    the next line, which the engine has not been tested with: without one, an
    install would pair the engine with statistics it never saw. An extra, a
    marker, a URL or parentheses are refused, because each changes what
    installs without changing a version.
    """
    name = STATS_REQUIREMENT_RE.match(requirement)
    if name is None:
        raise Refusal(f"REFUSED: {_shown(requirement)} is not a requirement on flux-stats.")
    rest = requirement[name.end() :].strip(" \t")
    if not rest:
        raise Refusal(
            f'REFUSED: "{requirement}" names no version range. The engine requires a floor and '
            "a ceiling on flux-stats, as flux-stats>=X.Y,<X.Z."
        )
    if re.search(r"[\[\];@()]", rest):
        raise Refusal(
            f'REFUSED: "{requirement}" is not a plain version range: an extra, a marker, a URL '
            "and parentheses are refused."
        )
    clauses = parse_specifier(rest)
    operators = {operator for operator, _ in clauses}
    if not operators & FLOORS:
        raise Refusal(
            f'REFUSED: "{requirement}" has no lower bound, so it admits releases older than '
            "the ones the engine uses."
        )
    if not operators & CEILINGS:
        raise Refusal(
            f'REFUSED: "{requirement}" has no upper bound, so it admits releases of a later '
            "line, which the engine has not been tested with."
        )
    return clauses


# ---------------------------------------------------------------- checks


def check_tag_shape(tag: str) -> str:
    """The tag is a release tag, and return the version it names."""
    match = TAG_RE.match(tag)
    if match is None:
        raise Refusal(
            f'REFUSED: "{tag}" is not a release tag. A release tag is '
            "v<major>.<minor>.<patch> with an optional a/b/rc pre-release suffix, "
            'so a working version such as "v0.11.0.dev0" cannot be released.'
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
    """At a tag, flux-stats's version is the tag's.

    flux-stats is versioned by its own format, and the engine requires a range
    of it, so the two numbers need not agree between releases. A tag still
    releases both distributions, and the release workflow looks for
    flux_stats-<tag version> files, so a flux-stats at another version would
    fail after forty minutes of matrix. This refuses it before.
    """
    declared, line = stats_version(root)
    return _agrees_with_tag(
        declared,
        version,
        f'REFUSED: {STATS_PYPROJECT}:{line} declares version "{declared}", '
        f'but the tag names "{version}". flux-stats is built at the tag\'s version: the '
        "release workflow builds, attests and uploads flux_stats files named by the tag.",
        f'{STATS_PYPROJECT}:{line} declares "{declared}"',
    )


def check_stats_range(root: Path) -> str:
    """The engine's requirement on flux-stats is a plain range that admits flux-stats's version.

    Needs no tag, so a version bump that moves flux-stats out of the range it
    is required under is refused on the pull request that makes it rather than
    on release day. The engine's verdicts are computed by flux-stats's
    definitions, so the range has to hold the flux-stats this tree builds.
    """
    requirement, line = engine_stats_requirement(root)
    declared, declared_line = stats_version(root)
    try:
        clauses = read_stats_range(requirement)
        version = parse_version(declared)
    except Refusal as refusal:
        raise Refusal(
            f"{refusal} The requirement is at {PYPROJECT}:{line}, and flux-stats's version is "
            f"at {STATS_PYPROJECT}:{declared_line}."
        ) from refusal
    if not all(clause_admits(operator, bound, version) for operator, bound in clauses):
        raise Refusal(
            f'REFUSED: {PYPROJECT}:{line} requires "{requirement}", which does not admit the '
            f'flux-stats in this tree: {STATS_PYPROJECT}:{declared_line} declares "{declared}". '
            "The engine's verdicts are computed by flux-stats's definitions, so its "
            "requirement has to hold the flux-stats this tree builds."
        )
    return f'{PYPROJECT}:{line} requires "{requirement}", which admits flux-stats "{declared}"'


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


def _skills_module() -> Any:
    """``scripts/check_skills.py`` beside this script, imported by path (scripts/ is not a package).

    It is imported only when the skills check runs. Its top level imports the
    standard library only, so this works where nothing is installed.
    """
    spec = importlib.util.spec_from_file_location("check_skills", SKILLS_CHECK)
    if spec is None or spec.loader is None:
        raise Refusal(f"cannot read {SKILLS_CHECK}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_skills", module)
    spec.loader.exec_module(module)
    return module


def check_skills_present(root: Path) -> str:
    """A release ships all eight agent skills of ADR 0033 section 1."""
    module = _skills_module()
    missing = list(module.missing_skills(root))
    if missing:
        raise Refusal(
            f"{len(missing)} of the {len(module.EXPECTED_SKILLS)} agent skills have no "
            f"skills/<name>/SKILL.md: {', '.join(missing)}"
        )
    return f"all {len(module.EXPECTED_SKILLS)} agent skills are present under skills/"


def run_tree_checks(root: Path, require_skills: bool = False) -> List[str]:
    """Everything that needs no tag.

    *require_skills* adds the check that all eight agent skills exist. A
    release always requires them; a pull-request tree may still be missing
    some while they land one by one (ADR 0033 section 8), so ``--check-tree``
    asks for them only with ``--require-skills``.
    """
    report = [check_literals_agree(root), check_stats_range(root)]
    if require_skills:
        report.append(check_skills_present(root))
    return report


def run_release_checks(
    root: Path, tag: str, commit: str, base: str = "origin/main"
) -> Tuple[str, bool, List[str]]:
    """Every check a tag must pass. Returns (version, prerelease, report)."""
    version = check_tag_shape(tag)
    report = [f'the tag "{tag}" names version "{version}"']
    report.append(check_pyproject(root, version))
    report.append(check_fallback(root, version))
    report.append(check_stats_version(root, version))
    report.append(check_stats_range(root))
    report.append(check_changelog(root, version))
    report.append(check_skills_present(root))
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
    version: str = "0.11.0",
    fallback: Optional[str] = None,
    changelog: Optional[str] = "0.11.0",
    stats: Optional[str] = None,
    stats_requirement: Optional[str] = None,
    drop_stats_pyproject: bool = False,
    skills: Optional[Sequence[str]] = None,
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

    *stats_requirement* is the whole quoted TOML string, or several joined by commas.
    *skills* names the skill folders the fixture holds, each with a SKILL.md;
    by default all eight, so a fixture bent for another check passes the skills check.

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
    real_requirement, _ = engine_stats_requirement(root)

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
        f'"{real_requirement}"',
        stats_requirement or FIXTURE_STATS_REQUIREMENT,
    )
    if drop_stats_pyproject:
        (dest / STATS_PYPROJECT).unlink()
    for name in _skills_module().EXPECTED_SKILLS if skills is None else skills:
        (dest / "skills" / name).mkdir(parents=True, exist_ok=True)
        (dest / "skills" / name / "SKILL.md").write_text("# fixture\n", encoding="utf-8")
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
            scratch / "mismatched-pyproject", root, version="0.11.0.dev0"
        )
        stale, stale_head = build_fixture(scratch / "stale-fallback", root, fallback="0.11.0.dev0")
        no_changelog, no_changelog_head = build_fixture(
            scratch / "no-changelog", root, changelog=None
        )
        wrong_changelog, wrong_changelog_head = build_fixture(
            scratch / "wrong-changelog", root, changelog="0.0.9"
        )
        stats_behind, stats_behind_head = build_fixture(
            scratch / "stats-behind", root, stats="0.10.9"
        )
        stats_ahead, _ = build_fixture(scratch / "stats-ahead", root, stats="0.12.0")
        stats_pre_ceiling, _ = build_fixture(scratch / "stats-pre-ceiling", root, stats="0.12.0rc1")
        stats_pre_floor, _ = build_fixture(scratch / "stats-pre-floor", root, stats="0.11.0rc1")
        stats_dev, _ = build_fixture(scratch / "stats-dev", root, stats="0.11.0.dev0")
        stats_patch, stats_patch_head = build_fixture(scratch / "stats-patch", root, stats="0.11.3")
        range_elsewhere, range_elsewhere_head = build_fixture(
            scratch / "range-elsewhere", root, stats_requirement='"flux-stats==0.10.9"'
        )
        range_no_ceiling, _ = build_fixture(
            scratch / "range-no-ceiling", root, stats_requirement='"flux-stats>=0.11"'
        )
        range_no_floor, _ = build_fixture(
            scratch / "range-no-floor", root, stats_requirement='"flux-stats<0.12"'
        )
        range_bare, _ = build_fixture(
            scratch / "range-bare", root, stats_requirement='"flux-stats"'
        )
        range_tilde, _ = build_fixture(
            scratch / "range-tilde", root, stats_requirement='"flux-stats~=0.11.0"'
        )
        range_extra, _ = build_fixture(
            scratch / "range-extra", root, stats_requirement='"flux-stats[yaml]>=0.11,<0.12"'
        )
        range_exact, _ = build_fixture(
            scratch / "range-exact", root, stats_requirement='"flux-stats==0.11.0"'
        )
        range_missing, _ = build_fixture(
            scratch / "range-missing", root, stats_requirement='"flux-stats-extras>=0.11,<0.12"'
        )
        range_twice, _ = build_fixture(
            scratch / "range-twice",
            root,
            stats_requirement='"flux-stats>=0.11,<0.12", "flux_stats>=0.11,<0.12"',
        )
        no_stats, _ = build_fixture(scratch / "no-stats-pyproject", root, drop_stats_pyproject=True)
        no_skill, no_skill_head = build_fixture(
            scratch / "no-skill", root, skills=["flux-author-bundle"]
        )
        green, green_head = build_fixture(scratch / "green", root)
        prerelease, prerelease_head = build_fixture(
            scratch / "prerelease",
            root,
            version="0.11.0rc1",
            changelog="0.11.0rc1",
            stats_requirement='"flux-stats>=0.11.0rc1,<0.12"',
        )
        output = scratch / "github-output.txt"

        must_refuse: Sequence[Tuple[str, str, Callable[[], object]]] = (
            (
                "a .dev tag is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("v0.11.0.dev0"),
            ),
            (
                "a bare number is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("0.11.0"),
            ),
            (
                "a .post tag is not a release tag",
                "is not a release tag",
                lambda: check_tag_shape("v0.11.0.post1"),
            ),
            (
                "pyproject.toml disagreeing with the tag",
                'declares version "0.11.0.dev0"',
                lambda: run_release_checks(mismatched, "v0.11.0", mismatched_head),
            ),
            (
                "a stale __version__ fallback",
                'falls back to "0.11.0.dev0"',
                lambda: run_release_checks(stale, "v0.11.0", stale_head),
            ),
            (
                "the two version literals disagreeing, with no tag in sight",
                "must move together",
                lambda: run_tree_checks(stale),
            ),
            (
                "flux-stats's version disagreeing with the tag",
                "flux-stats is built at the tag's version",
                lambda: run_release_checks(stats_behind, "v0.11.0", stats_behind_head),
            ),
            (
                "a flux-stats release of the line that is not the tag's, though the range admits it",
                "flux-stats is built at the tag's version",
                lambda: run_release_checks(stats_patch, "v0.11.0", stats_patch_head),
            ),
            (
                "a requirement that leaves flux-stats's version out, at a tag",
                "does not admit the flux-stats in this tree",
                lambda: run_release_checks(range_elsewhere, "v0.11.0", range_elsewhere_head),
            ),
            (
                "flux-stats's version under the floor, with no tag in sight",
                "does not admit the flux-stats in this tree",
                lambda: run_tree_checks(stats_behind),
            ),
            (
                "flux-stats's version above the ceiling, with no tag in sight",
                "does not admit the flux-stats in this tree",
                lambda: run_tree_checks(stats_ahead),
            ),
            (
                "a pre-release of the ceiling's version, which <V keeps out",
                "does not admit the flux-stats in this tree",
                lambda: run_tree_checks(stats_pre_ceiling),
            ),
            (
                "a pre-release of the floor's version, which sorts below >=V",
                "does not admit the flux-stats in this tree",
                lambda: run_tree_checks(stats_pre_floor),
            ),
            (
                "an exact requirement on another release",
                "does not admit the flux-stats in this tree",
                lambda: run_tree_checks(range_elsewhere),
            ),
            (
                "a flux-stats version this guard cannot order",
                "is not a version this guard reads",
                lambda: run_tree_checks(stats_dev),
            ),
            (
                "a range with a floor and no ceiling",
                "has no upper bound",
                lambda: run_tree_checks(range_no_ceiling),
            ),
            (
                "a range with a ceiling and no floor",
                "has no lower bound",
                lambda: run_tree_checks(range_no_floor),
            ),
            (
                "a requirement with no version at all",
                "names no version range",
                lambda: run_tree_checks(range_bare),
            ),
            (
                "a compatible-release requirement, which this guard does not read",
                "is not a plain version range",
                lambda: run_tree_checks(range_tilde),
            ),
            (
                "a requirement with an extra",
                "is not a plain version range",
                lambda: run_tree_checks(range_extra),
            ),
            (
                "no flux-stats requirement, only a longer name that starts with it",
                "declares flux-stats 0 times",
                lambda: run_tree_checks(range_missing),
            ),
            (
                "flux-stats required twice, in two spellings",
                "declares flux-stats 2 times",
                lambda: run_tree_checks(range_twice),
            ),
            (
                "no packages/flux-stats/pyproject.toml",
                f"{STATS_PYPROJECT} does not exist",
                lambda: run_tree_checks(no_stats),
            ),
            (
                "no CHANGELOG.md at all",
                "A release needs a record",
                lambda: run_release_checks(no_changelog, "v0.11.0", no_changelog_head),
            ),
            (
                "a CHANGELOG.md with no section for this version",
                "has no `## 0.11.0` section",
                lambda: run_release_checks(wrong_changelog, "v0.11.0", wrong_changelog_head),
            ),
            (
                "a commit that never went through main",
                "is not contained in origin/main",
                lambda: check_ancestry(off_main, git_repo),
            ),
            (
                "a describe.json whose version is not the tagged one",
                "the installed wheel reports version '0.11.0.dev0'",
                lambda: check_installed(_describe_fixture(scratch, "0.11.0.dev0"), "0.11.0"),
            ),
            (
                "a describe.json running the vendored spec fallback",
                "resolved its spec through 'vendored'",
                lambda: check_installed(
                    _describe_fixture(scratch, "0.11.0", source="vendored"), "0.11.0"
                ),
            ),
        )

        must_refuse = (
            *must_refuse,
            (
                "a tree missing skills is refused when skills are required",
                "flux-run-and-verify",
                lambda: run_tree_checks(no_skill, require_skills=True),
            ),
            (
                "a release tag on a tree missing skills",
                "agent skills have no",
                lambda: run_release_checks(no_skill, "v0.11.0", no_skill_head),
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
                lambda: _expect_outputs(green, "v0.11.0", green_head, output, "false"),
            ),
            ("a consistent tree with no tag", lambda: run_tree_checks(green)),
            (
                "a tree with all eight skills when skills are required",
                lambda: run_tree_checks(green, require_skills=True),
            ),
            (
                "a tree missing skills when none are required",
                lambda: run_tree_checks(no_skill),
            ),
            (
                "a flux-stats release of the line that is not the engine's version, with no tag",
                lambda: run_tree_checks(stats_patch),
            ),
            ("an exact requirement that admits flux-stats", lambda: run_tree_checks(range_exact)),
            ("a commit that is on main", lambda: check_ancestry(on_main_only, git_repo)),
            (
                "a describe.json that agrees with the tag",
                lambda: check_installed(_describe_fixture(scratch, "0.11.0"), "0.11.0"),
            ),
            (
                "a pre-release tag, classified as a pre-release",
                lambda: _expect_outputs(prerelease, "v0.11.0rc1", prerelease_head, output, "true"),
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
    parser.add_argument(
        "--require-skills", action="store_true", help="--check-tree also needs all eight skills"
    )
    parser.add_argument("--tag", help="the release tag, e.g. v0.11.0")
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
            for line in run_tree_checks(root, args.require_skills):
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
