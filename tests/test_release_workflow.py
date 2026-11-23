# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The release workflow keeps code that installs packages apart from signing and publishing.

A job that holds ``id-token: write``, ``attestations: write`` or ``contents: write``,
or that deploys to the PyPI environment, can mint a signature or publish a file. If
that job also installs a package, a bad release of that package runs with those
permissions. So the job that builds and smoke-tests the distributions installs
everything, from hash-checked files, and holds ``contents: read`` only. The jobs that
attest and publish run pinned actions and the ``gh`` CLI and install nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
MAKEFILE = ROOT / "Makefile"

#: Anything that installs, resolves, builds or runs project or third-party Python code.
_INSTALL_OR_RUN = re.compile(
    r"\bpip3?\"?\s+install\b|-m\s+pip\b|\buv\"?\s+(?:sync|pip|run|build|export|tool|add)\b"
    r"|\bmake\b|-m\s+build\b"
)
_WRITE_SCOPES = ("id-token", "attestations", "contents")


def _jobs() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = document["jobs"]
    assert isinstance(jobs, dict) and jobs
    return jobs


def _commands(job: dict[str, Any]) -> list[str]:
    """Every logical shell line of a job: continuations joined, heredoc bodies left out."""
    lines: list[str] = []
    for step in job.get("steps", []):
        run = step.get("run")
        if run:
            run = re.sub(r"<<'?(\w+)'?\n.*?\n\s*\1\n", "\n", run + "\n", flags=re.S)
            lines.extend(re.sub(r"\\\n\s*", " ", run).splitlines())
    return lines


def _is_privileged(job: dict[str, Any]) -> bool:
    permissions = job.get("permissions") or {}
    writes = isinstance(permissions, dict) and any(
        permissions.get(scope) == "write" for scope in _WRITE_SCOPES
    )
    return writes or "environment" in job


def _privileged() -> dict[str, dict[str, Any]]:
    found = {name: job for name, job in _jobs().items() if _is_privileged(job)}
    assert {"release", "publish-pypi"} <= set(found), sorted(found)
    return found


def test_no_job_with_a_write_permission_or_the_pypi_environment_installs_or_builds() -> None:
    for name, job in _privileged().items():
        for command in _commands(job):
            assert not _INSTALL_OR_RUN.search(command), f"{name} runs: {command}"
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            assert not uses.startswith("actions/setup-python@"), f"{name} sets up Python"
            assert "scripts/" not in step.get("run", ""), f"{name} runs a repository script"


def test_the_build_job_is_read_only_and_every_install_in_it_is_checked_against_hashes() -> None:
    build = _jobs()["build"]
    assert build["permissions"] == {"contents": "read"}
    assert "environment" not in build
    installs = [c for c in _commands(build) if re.search(r"\bpip3?\"?\s+install\b", c)]
    assert len(installs) >= 4, installs
    for command in installs:
        hashed = "--require-hashes" in command
        # A wheel this job built a minute ago, installed with nothing resolved.
        local = "--no-index" in command and "--no-deps" in command
        assert hashed or local, f"build installs without hashes: {command}"
    for command in _commands(build):
        if re.search(r"\buv\"?\s+(?:sync|pip)\b", command):
            assert re.search(r"--(?:locked|frozen)\b", command), command
        if re.search(r"\buv\"?\s+export\b", command):
            assert re.search(r"--(?:locked|frozen)\b", command), command


def test_the_build_job_runs_the_smoke_test_in_its_locked_form() -> None:
    commands = _commands(_jobs()["build"])
    assert any(
        c.startswith("make packaging-smoke") and "PKG_LOCK_DIR=" in c for c in commands
    ), commands
    assert any(re.search(r"\buv\"?\s+export\b.*--locked", c) for c in commands), commands


def test_the_locked_branch_of_the_packaging_target_installs_only_from_hashes() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    locked = text.split("else\nPKG_TOOLS_INSTALL", 1)[1].split("endif", 1)[0]
    installs = [
        part for part in re.split(r"&&|\n", "PKG_TOOLS_INSTALL" + locked) if "pip install" in part
    ]
    assert len(installs) == 3, installs
    for part in installs:
        assert "--require-hashes" in part or ("--no-index" in part and "--no-deps" in part), part
    assert "--no-isolation" in locked


def test_the_hash_files_the_build_reads_exist_and_every_requirement_has_a_hash() -> None:
    for name in ("build-tools.txt", "sbom-tool.txt"):
        text = (ROOT / ".github" / "release" / name).read_text(encoding="utf-8")
        entries = re.findall(r"^[A-Za-z0-9][^\s#]*==\S+.*?(?=^[A-Za-z0-9]|\Z)", text, re.M | re.S)
        assert entries, name
        for entry in entries:
            assert "--hash=sha256:" in entry, f"{name}: {entry.splitlines()[0]}"


_RELEASE = ROOT / ".github" / "release"


def _requires_python_floor() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'^requires-python\s*=\s*"[^"]*?>=\s*(\d+\.\d+)', text, re.M)
    assert found, "requires-python does not start with >="
    return found.group(1)


def test_the_sbom_tool_lock_pins_typing_extensions_with_a_hash() -> None:
    """``cyclonedx-python-lib`` needs it below Python 3.13, and the release job runs 3.12.

    ``pip --require-hashes`` refuses an install whose dependency is not pinned with ``==`` and a
    hash, so a lock compiled on a newer Python than the job's (which drops it) fails the SBOM step
    of a release, after the tag.
    """
    text = (_RELEASE / "sbom-tool.txt").read_text(encoding="utf-8")
    entries = re.findall(r"^typing[-_]extensions==[^\n]*(?:\n[ \t]+[^\n]*)*", text, re.M | re.I)
    assert len(entries) == 1, entries
    assert "--hash=sha256:" in entries[0], entries[0]
    marker = re.search(r";\s*python_full_version\s*<\s*'(\d+\.\d+)'", entries[0])
    release_python = "3.12"
    assert marker is None or tuple(map(int, release_python.split("."))) < tuple(
        map(int, marker.group(1).split("."))
    ), f"typing-extensions is locked out of Python {release_python}: {entries[0].splitlines()[0]}"


@pytest.mark.parametrize("name", ["sbom-tool", "build-tools"])
def test_a_release_lock_is_compiled_for_the_lowest_python_the_project_supports(name: str) -> None:
    """Without ``--python-version`` uv resolves for the interpreter it runs on and drops the rest."""
    source = (_RELEASE / f"{name}.in").read_text(encoding="utf-8")
    command = " ".join(
        line.lstrip("# ").rstrip("\\ ")
        for line in source.splitlines()
        if line.startswith("#") and ("uv pip compile" in line or "--" in line)
    )
    assert f"--python-version {_requires_python_floor()}" in command, command


def test_the_jobs_are_chained_and_the_artifact_names_connect() -> None:
    jobs = _jobs()
    assert {"guard", "ci"} <= set(jobs["build"]["needs"])
    assert {"guard", "ci", "build"} <= set(jobs["release"]["needs"])
    assert {"guard", "build", "release"} <= set(jobs["publish-pypi"]["needs"])

    def artifact_names(job: dict[str, Any], action: str) -> set[str]:
        return {
            step["with"]["name"]
            for step in job.get("steps", [])
            if step.get("uses", "").startswith(f"actions/{action}@")
        }

    uploaded = artifact_names(jobs["build"], "upload-artifact")
    assert uploaded == {"dist", "release-extras"}
    assert artifact_names(jobs["release"], "download-artifact") == uploaded
    assert artifact_names(jobs["publish-pypi"], "download-artifact") == {"dist"}
    # The digests the later jobs check against come from the build job.
    assert {"dist_sha256", "sbom_sha256", "notes_sha256"} <= set(jobs["build"]["outputs"])
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "needs.build.outputs.dist_sha256" in text
    # The release body is checked against its recorded digest, not only for size.
    assert "needs.build.outputs.notes_sha256" in text
    assert 'NOTES_SHA256" notes.md | sha256sum -c -' in text
    assert "needs.release.outputs" not in text
    for ref in re.findall(r"needs\.(\w+)\.outputs\.(\w+)", text):
        job, output = ref
        assert output in jobs[job]["outputs"], ref


def test_the_attestation_is_written_by_the_release_job_over_the_downloaded_files() -> None:
    jobs = _jobs()
    attesting = [
        name
        for name, job in jobs.items()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("actions/attest-build-provenance@")
    ]
    assert attesting == ["release"]
