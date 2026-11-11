# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Supply-chain hygiene for the workflows: pinned actions, explicit token scope.

Every third-party action is pinned to a commit SHA, with a `# vX.Y.Z` comment so
Dependabot can bump the pin and a reader can see which release it is. A tag
(`@v4`) can be moved after the fact; a SHA cannot. Every workflow also states
its token permissions at the top, so a job added later without its own block
inherits the narrow default instead of the repository's wide one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW_DIR = _ROOT / ".github" / "workflows"
# GitHub loads both spellings; a new file must not slip past the checks by using .yaml.
WORKFLOWS = sorted([*_WORKFLOW_DIR.glob("*.yml"), *_WORKFLOW_DIR.glob("*.yaml")])
# A composite action kept in this repository carries `uses:` steps too.
ACTIONS = sorted((_ROOT / ".github" / "actions").glob("**/action.y*ml"))
FILES = [*WORKFLOWS, *ACTIONS]

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", re.MULTILINE)
_SHA = re.compile(r"[0-9a-f]{40}")


def _uses_in(node: object) -> list[str]:
    """Every `uses` value in a parsed YAML document, whatever shape it is written in."""
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k == "uses" and isinstance(v, str)]
        for key, value in node.items():
            if key != "uses":
                found.extend(_uses_in(value))
        return found
    if isinstance(node, list):
        return [use for item in node for use in _uses_in(item)]
    return []


def test_the_workflow_directory_is_not_empty() -> None:
    assert WORKFLOWS, "no workflows found; the glob would let every check below pass vacuously"


@pytest.mark.unit
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_and_names_its_release(path: Path) -> None:
    for ref, rest in _USES.findall(path.read_text(encoding="utf-8")):
        if ref.startswith("./"):  # a reusable workflow in this repository
            continue
        _, _, pin = ref.partition("@")
        assert _SHA.fullmatch(pin), f"{path.name}: {ref} is not pinned to a commit SHA"
        assert re.search(r"#\s*v\d", rest), f"{path.name}: {ref} has no `# vX.Y.Z` comment"


@pytest.mark.unit
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_sets_its_token_permissions_at_the_top(path: Path) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "permissions" in document, f"{path.name} has no top-level `permissions:` block"


@pytest.mark.unit
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_the_line_scan_sees_every_uses_entry_the_yaml_has(path: Path) -> None:
    # The pin check above reads lines. This cross-check fails when a file holds a
    # `uses` the line scan cannot see (flow style, a quoted key), so a new shape
    # cannot pass the pin check by being invisible to it.
    text = path.read_text(encoding="utf-8")
    scanned = len(_USES.findall(text))
    parsed = len(_uses_in(yaml.safe_load(text)))
    assert scanned == parsed, f"{path.name}: {parsed} `uses` entries in the YAML, {scanned} seen"
