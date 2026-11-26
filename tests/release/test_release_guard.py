# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The release guard's own controls, on the pull-request path.

``scripts/release_guard.py --self-test`` builds a broken fixture for every
check it makes and requires each one to refuse. Running it here rather than
only from a pushed tag is the difference between a guard that was shown to
fire once and a guard that is shown to fire on every pull request.

``--check-tree`` runs the checks that need no tag against the REAL
tree, closing a drift class that has no other test:

*   ``pyproject.toml``'s ``[project] version`` and the hard-coded fallback
    in ``flux_engine/__init__.py``'s ``except PackageNotFoundError`` branch
    are two copies of one string. ``tests/test_cli.py`` compares
    ``fluxsim --version`` against the same imported ``__version__`` the CLI
    printed, and ``tests/cli/test_envelope_and_stage.py`` does the same, so
    every existing assertion over the version is a tautology that cannot
    see the two literals disagree.

The engine requires flux-stats by range, not by one release (ADR 0009, amended).
The guard's reading of that range is the other half of what is held here: it
refuses a requirement with no floor or no ceiling, and one that does not admit the
version in ``packages/flux-stats/pyproject.toml``. It orders versions itself,
because it runs before anything is installed, and the tests set its answer against
``packaging``'s over a grid of ranges and versions and over random ones.

The kernel version (``flux_engine/core/kernels.py``) is deliberately absent
from all of this. It is the semver of normative kernel behaviour, it is
hashed into ``inputDigest``, and it already has a far stronger guard: moving
the default kernel moves every pinned digest of a new plan and turns
``make test-digests`` red.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = REPO_ROOT / "scripts" / "release_guard.py"


def _load_guard() -> ModuleType:
    """Import the guard by path: scripts/ is not an installed package."""
    spec = importlib.util.spec_from_file_location("release_guard", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: One import for the tests that call the guard many times over.
_GUARD = _load_guard()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """The guard through its real entry point, not through an import."""
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


def test_the_guard_script_exists() -> None:
    assert GUARD.is_file(), f"no release guard at {GUARD}"


def test_every_check_refuses_a_fixture_built_to_break_it() -> None:
    """The self-test is the control. A check that stopped firing fails here."""
    result = _run("--self-test")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "checks all fired" in result.stdout


def test_this_tree_passes_the_checks_that_need_no_tag() -> None:
    result = _run("--check-tree")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "the package version reads" in result.stdout
    assert "which admits flux-stats" in result.stdout


def test_a_drifted_version_fallback_is_refused_without_any_tag(tmp_path: Path) -> None:
    """The negative arm of the check the test above runs green.

    A green check is not evidence on its own. This bends exactly one thing,
    the hard-coded fallback literal, and requires the same command that
    passed on the real tree to refuse.
    """
    guard = _load_guard()
    fixture, _ = guard.build_fixture(tmp_path / "drifted", REPO_ROOT, fallback="9.9.9")
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 1, f"the guard accepted a drifted fallback: {result.stdout}"
    assert "must move together" in result.stderr


@pytest.mark.parametrize("tag", ["v0.12.0.dev0", "v0.12.0.post1", "0.12.0", "release-0.12.0"])
def test_a_working_version_cannot_be_tagged_as_a_release(tag: str, tmp_path: Path) -> None:
    """The obvious way around a version mismatch: tag the dev string."""
    guard = _load_guard()
    fixture, head = guard.build_fixture(tmp_path / "shape", REPO_ROOT)
    result = _run("--tag", tag, "--commit", head, "--root", str(fixture))
    assert result.returncode == 1, f"the guard accepted {tag!r}: {result.stdout}"
    assert "is not a release tag" in result.stderr


def test_a_consistent_tree_and_tag_is_accepted(tmp_path: Path) -> None:
    """The green arm, so the guard is not one that only ever refuses."""
    guard = _load_guard()
    fixture, head = guard.build_fixture(tmp_path / "green", REPO_ROOT)
    output = tmp_path / "github-output.txt"
    result = _run(
        "--tag",
        "v0.12.0",
        "--commit",
        head,
        "--root",
        str(fixture),
        "--github-output",
        str(output),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert output.read_text(encoding="utf-8") == "version=0.12.0\nprerelease=false\n"


# ------------------------------------------------- the flux-stats requirement
#
# The engine requires flux-stats by range, and the guard asks one question of it:
# does the range admit the flux-stats version this tree builds? Each test below
# bends one thing in a copy of the real files and asks the guard through its real
# entry point, so a check that stops firing is a test that fails.


def _tree_with(tmp_path: Path, name: str, **bent: str) -> Path:
    """A copy of the real files with the keyword arguments of ``build_fixture`` bent."""
    fixture, _ = _load_guard().build_fixture(tmp_path / name, REPO_ROOT, **bent)
    return fixture


def test_a_range_that_holds_the_flux_stats_version_is_accepted_without_any_tag(
    tmp_path: Path,
) -> None:
    """The green arm of the range policy: a floor and a ceiling around the version."""
    fixture = _tree_with(tmp_path, "range", stats_requirement='"flux-stats>=0.12,<0.13"')
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert 'requires "flux-stats>=0.12,<0.13", which admits flux-stats "0.12.0"' in result.stdout


def test_a_flux_stats_release_of_the_line_need_not_be_the_engines_version(
    tmp_path: Path,
) -> None:
    """The point of versioning flux-stats by its format: 0.12.3 under an engine at 0.12.0."""
    fixture = _tree_with(
        tmp_path, "patch", stats="0.12.3", stats_requirement='"flux-stats>=0.12,<0.13"'
    )
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize(
    ("stats", "requirement"),
    [
        ("0.13.0", '"flux-stats>=0.12,<0.13"'),  # the next line, past the ceiling
        ("0.12.0", '"flux-stats>=0.11,<0.12"'),  # last line's pin
        ("0.11.9", '"flux-stats>=0.12,<0.13"'),  # the line before, under the floor
        ("0.12.0", '"flux-stats==0.11.9"'),  # an exact pin on another release
        ("0.13.0rc1", '"flux-stats>=0.12,<0.13"'),  # `<0.13` keeps out a pre-release of 0.13
        ("0.12.0rc1", '"flux-stats>=0.12,<0.13"'),  # a pre-release sorts below `>=0.12`
    ],
)
def test_a_range_that_leaves_the_flux_stats_version_out_is_refused_without_any_tag(
    tmp_path: Path, stats: str, requirement: str
) -> None:
    fixture = _tree_with(tmp_path, "outside", stats=stats, stats_requirement=requirement)
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 1, f"the guard accepted {requirement} for {stats}: {result.stdout}"
    assert "does not admit the flux-stats in this tree" in result.stderr


@pytest.mark.parametrize(
    ("requirement", "refusal"),
    [
        ('"flux-stats>=0.12"', "has no upper bound"),
        ('"flux-stats>0.8"', "has no upper bound"),
        ('"flux-stats<0.13"', "has no lower bound"),
        ('"flux-stats<=0.13"', "has no lower bound"),
        ('"flux-stats"', "names no version range"),
        ('"flux-stats~=0.12.0"', "is not a plain version range"),
        ('"flux-stats!=0.12.1,>=0.12,<0.13"', "is not a plain version range"),
        ('"flux-stats>=0.12.*,<0.13"', "is not a version this guard reads"),
        ('"flux-stats[yaml]>=0.12,<0.13"', "is not a plain version range"),
        ("\"flux-stats>=0.12,<0.13; python_version >= '3.11'\"", "is not a plain version range"),
        ('"flux-stats @ https://example.invalid/flux_stats.whl"', "is not a plain version range"),
        ('"flux-stats>=0.12,<0.13.dev0"', "is not a version this guard reads"),
        ('"flux-stats>=0.12,<0.13,"', "is not a plain version range"),
        ('"flux-stats>=0.12,\\u00a0<0.13"', "is not a plain version range"),  # a no-break space
        (
            '"flux-stats>=\\u0660.\\u0669,<0.13"',
            "is not a version this guard reads",
        ),  # Arabic-Indic
    ],
)
def test_a_requirement_that_is_not_a_bounded_plain_range_is_refused(
    tmp_path: Path, requirement: str, refusal: str
) -> None:
    fixture = _tree_with(tmp_path, "shape", stats_requirement=requirement)
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 1, f"the guard accepted {requirement}: {result.stdout}"
    assert refusal in result.stderr, result.stderr


def test_a_flux_stats_version_the_guard_cannot_order_is_refused(tmp_path: Path) -> None:
    """A dev release is refused by name rather than ordered wrongly."""
    fixture = _tree_with(
        tmp_path, "dev", stats="0.12.0.dev0", stats_requirement='"flux-stats>=0.8,<0.13"'
    )
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 1, result.stdout
    assert "is not a version this guard reads" in result.stderr


def test_a_tag_is_accepted_when_the_range_holds_the_flux_stats_version(tmp_path: Path) -> None:
    guard = _load_guard()
    fixture, head = guard.build_fixture(
        tmp_path / "tag", REPO_ROOT, stats_requirement='"flux-stats>=0.12,<0.13"'
    )
    output = tmp_path / "github-output.txt"
    result = _run(
        "--tag", "v0.12.0", "--commit", head, "--root", str(fixture), "--github-output", str(output)
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert output.read_text(encoding="utf-8") == "version=0.12.0\nprerelease=false\n"


def test_a_pre_release_tag_needs_a_range_that_admits_the_pre_release(tmp_path: Path) -> None:
    """`>=0.12` does not admit 0.12.0rc1, so a release candidate names it in the floor."""
    guard = _load_guard()
    holds, head = guard.build_fixture(
        tmp_path / "holds",
        REPO_ROOT,
        version="0.12.0rc1",
        changelog="0.12.0rc1",
        stats_requirement='"flux-stats>=0.12.0rc1,<0.13"',
    )
    result = _run("--tag", "v0.12.0rc1", "--commit", head, "--root", str(holds))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    leaves_out, head = guard.build_fixture(
        tmp_path / "leaves-out",
        REPO_ROOT,
        version="0.12.0rc1",
        changelog="0.12.0rc1",
        stats_requirement='"flux-stats>=0.12,<0.13"',
    )
    result = _run("--tag", "v0.12.0rc1", "--commit", head, "--root", str(leaves_out))
    assert result.returncode == 1, result.stdout
    assert "does not admit the flux-stats in this tree" in result.stderr


def test_a_tag_still_names_the_flux_stats_version_the_workflow_builds(tmp_path: Path) -> None:
    """The release workflow builds flux-stats's files at the tag's version.

    So a range that admits 0.12.3 is not enough at a tag: the files the workflow
    looks for are flux_stats-0.12.0, and the guard says so before the matrix runs.
    """
    guard = _load_guard()
    fixture, head = guard.build_fixture(
        tmp_path / "other", REPO_ROOT, stats="0.12.3", stats_requirement='"flux-stats>=0.12,<0.13"'
    )
    result = _run("--tag", "v0.12.0", "--commit", head, "--root", str(fixture))
    assert result.returncode == 1, result.stdout
    assert "flux-stats is built at the tag's version" in result.stderr


# The specifiers a release might write, and the versions a tree might hold.
_SPECIFIERS = (
    ">=0.11,<0.12",
    ">=0.11.0,<0.12.0",
    ">=0.11.0rc1,<0.12",
    ">=0.11,<0.12.0rc1",
    "==0.11.0",
    "==0.11",
    ">0.11,<=0.12",
    ">=0.11",
    "<0.12",
    ">0.11.0a1,<0.11.1",
    ">=0.11.0a1,<=0.11.0rc2",
    ">=1.0,<2",
    ">=0.0.1,<0.0.2",
    ">=0.11,<0.12,<0.11.5",
    ">=0.11,>=0.11.1,<0.12",
)
_VERSIONS = (
    "0",
    "0.0.0",
    "0.0.1",
    "0.0.1a1",
    "0.0.1rc1",
    "0.0.2rc1",
    "0.8.9",
    "0.11",
    "0.11.0",
    "0.11.0.0",
    "0.11.0a1",
    "0.11.0b2",
    "0.11.0rc1",
    "0.11.0rc2",
    "0.11.1",
    "0.11.1a1",
    "0.11.1rc1",
    "0.11.4",
    "0.11.5",
    "0.11.99",
    "0.12",
    "0.12.0",
    "0.12.0a1",
    "0.12.0rc1",
    "0.12.1",
    "1",
    "1.0",
    "1.0.0rc1",
    "1.0.0",
    "2.0.0",
)


@pytest.mark.parametrize("specifier", _SPECIFIERS)
def test_the_range_check_gives_the_answer_packaging_gives(specifier: str) -> None:
    """The guard orders versions itself, so `packaging` is the oracle, as PEP 440 is the rule.

    ``prereleases=True`` asks the question the guard asks: could this release
    be installed under the range with ``--pre``, by the ordering and the two
    exclusive-comparison rules alone.
    """
    guard = _load_guard()
    oracle = SpecifierSet(specifier)
    for version in _VERSIONS:
        expected = oracle.contains(Version(version), prereleases=True)
        assert guard.specifier_admits(specifier, version) is expected, (specifier, version)


_RELEASES = st.lists(st.integers(0, 12), min_size=1, max_size=4).map(
    lambda parts: ".".join(str(part) for part in parts)
)
_PRE_RELEASES = st.one_of(
    st.just(""),
    st.builds(
        lambda phase, number: f"{phase}{number}",
        st.sampled_from(["a", "b", "rc"]),
        st.integers(0, 3),
    ),
)
_GENERATED_VERSIONS = st.builds(lambda release, pre: release + pre, _RELEASES, _PRE_RELEASES)
_GENERATED_SPECIFIERS = st.lists(
    st.builds(
        lambda operator, version: operator + version,
        st.sampled_from([">=", ">", "<", "<=", "=="]),
        _GENERATED_VERSIONS,
    ),
    min_size=1,
    max_size=3,
).map(",".join)


@settings(max_examples=1500)
@given(specifier=_GENERATED_SPECIFIERS, version=_GENERATED_VERSIONS)
def test_the_range_check_agrees_with_packaging_on_any_range_and_version(
    specifier: str, version: str
) -> None:
    expected = SpecifierSet(specifier).contains(Version(version), prereleases=True)
    assert _GUARD.specifier_admits(specifier, version) is expected, (specifier, version)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "v0.11.0",
        "0.11.0.dev1",
        "0.11.0.post1",
        "1!0.11.0",
        "0.11.0+local",
        "0.12.*",
        "0.12.0\n",
        "0.12.0 ",
        "0.12.0\u00a0",
        "\u0660.\u0669.\u0660",  # Arabic-Indic digits, which pip refuses
        "\uff10.\uff19.\uff10",  # fullwidth digits
        "a.b",
    ],
)
def test_a_spelling_the_guard_does_not_read_is_refused_not_ordered(text: str) -> None:
    guard = _load_guard()
    with pytest.raises(guard.Refusal, match="is not a version this guard reads"):
        guard.parse_version(text)


@pytest.mark.parametrize("text", ["0.11" + "0" * 4000, "1" * 10 + ".0"])
def test_a_number_too_long_to_trust_is_refused_not_parsed(text: str) -> None:
    guard = _load_guard()
    with pytest.raises(guard.Refusal, match="is not a version this guard reads"):
        guard.parse_version(text)


def test_the_engine_requires_flux_stats_by_range_and_not_by_one_release() -> None:
    """The policy itself (ADR 0009, amended), held on the files that ship."""
    requirement, _ = _GUARD.engine_stats_requirement(REPO_ROOT)
    operators = {operator for operator, _ in _GUARD.read_stats_range(requirement)}
    assert {">=", "<"} <= operators, requirement
    assert "==" not in operators, requirement


# ------------------------------------------------------------ agent skills
#
# A release ships all eight agent skills (ADR 0033). ``--check-tree`` asks for
# them only with ``--require-skills``, because a pull-request tree may still be
# missing some while they land one by one; a tag always asks.

_ALL_SKILLS = (
    "flux-author-bundle",
    "flux-run-and-verify",
    "flux-privacy-and-profiling",
    "flux-scale-and-ship",
    "flux-relational-modeling",
    "flux-consumer-queries",
    "flux-forecasts",
    "flux-docs-lookup",
)


def test_a_tree_with_all_eight_skills_passes_the_skills_check(tmp_path: Path) -> None:
    fixture = _tree_with(tmp_path, "all-skills", skills=_ALL_SKILLS)
    result = _run("--check-tree", "--require-skills", "--root", str(fixture))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "all 8 agent skills are present" in result.stdout


def test_a_tree_missing_a_skill_is_refused_by_name(tmp_path: Path) -> None:
    fixture = _tree_with(tmp_path, "one-short", skills=_ALL_SKILLS[:-1])
    result = _run("--check-tree", "--require-skills", "--root", str(fixture))
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "flux-docs-lookup" in result.stderr
    assert "flux-author-bundle" not in result.stderr


def test_a_skill_folder_without_a_skill_file_counts_as_missing(tmp_path: Path) -> None:
    fixture = _tree_with(tmp_path, "empty-folder", skills=_ALL_SKILLS)
    (fixture / "skills" / "flux-forecasts" / "SKILL.md").unlink()
    result = _run("--check-tree", "--require-skills", "--root", str(fixture))
    assert result.returncode == 1
    assert "flux-forecasts" in result.stderr


def test_the_tree_check_alone_does_not_need_the_skills(tmp_path: Path) -> None:
    """Without the flag, a pull-request tree that is short of skills still passes."""
    fixture = _tree_with(tmp_path, "no-skills", skills=())
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_a_release_tag_on_a_tree_missing_a_skill_is_refused(tmp_path: Path) -> None:
    fixture, head = _load_guard().build_fixture(
        tmp_path / "tag-one-short", REPO_ROOT, skills=_ALL_SKILLS[:-1]
    )
    result = _run("--tag", "v0.12.0", "--commit", head, "--root", str(fixture))
    assert result.returncode == 1, f"{result.stdout}\n{result.stderr}"
    assert "flux-docs-lookup" in result.stderr
