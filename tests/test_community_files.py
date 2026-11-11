# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Guards for the community files a newcomer meets first.

These files are prose, so what is worth pinning is the part that can rot: the
commands CONTRIBUTING.md tells people to run must exist, the code of conduct
must be the filled-in Covenant text, the DCO check must keep its narrow bot
exemption, and no public file may carry a real email address (conduct and
security reports go through GitHub's private channels).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

# Placeholder domains documentation is allowed to use in examples.
_EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_PRIVATE_REPORTING = "https://github.com/Agenticstiger/flux-engine/security/advisories/new"
# The one shape the DCO job's `if:` may take: skip when GitHub says the author is
# an owner or member, or when the author is named in the list of members who keep
# their membership private (GitHub reports those as CONTRIBUTOR); run for everyone
# else, and for an event that carries neither field.
_DCO_MEMBER_EXEMPTION = re.compile(
    r"^\$\{\{ !\(contains\(fromJSON\('(?P<roles>\[[^\]]*\])'\), "
    r"github\.event\.pull_request\.author_association\) "
    r"\|\| contains\(fromJSON\('(?P<logins>\[[^\]]*\])'\), "
    r"github\.event\.pull_request\.user\.login\)\) \}\}$"
)
# GitHub's rule for a user name: letters, digits and single hyphens, not starting
# or ending with a hyphen, 39 characters at most. Excludes bots (`name[bot]`).
_GITHUB_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

_PROSE_FILES = (
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "MAINTAINERS.md",
    "SECURITY.md",
    "CITATION.cff",
    ".github/pull_request_template.md",
)
_LINKED_FILES = ("CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "MAINTAINERS.md", "SECURITY.md")
# What `make check` has to keep running: the static gates, then the fast tests.
_CHECK_PREREQUISITES = {
    "lint",
    "format-check",
    "typecheck",
    "imports",
    "register-lint",
    "guardrail",
    "test-fast",
}


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _github_files() -> list[str]:
    return sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / ".github").rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml", ".md"}
    )


@pytest.mark.unit
def test_citation_file_carries_the_required_keys() -> None:
    data = yaml.safe_load(_read("CITATION.cff"))
    assert data["cff-version"].startswith("1.2")
    assert data["title"] and data["message"]
    assert data["authors"] and all(author.get("name") for author in data["authors"])


@pytest.mark.unit
def test_code_of_conduct_is_the_covenant_with_every_placeholder_filled() -> None:
    text = _read("CODE_OF_CONDUCT.md")
    assert text.startswith("# Contributor Covenant 3.0 Code of Conduct")
    assert "adapted from the Contributor Covenant, version 3.0" in text
    assert "licensed under CC BY-SA 4.0" in text, "the Covenant's licence notice was dropped"
    assert "[NOTE" not in text and "[INSERT" not in text, "an adopter placeholder was left in"
    assert "+++" not in text, "the upstream front matter was left in"


@pytest.mark.unit
def test_security_policy_points_at_private_reporting_and_a_fallback_that_exists() -> None:
    text = " ".join(_read("SECURITY.md").split())  # the prose is wrapped across lines
    assert "do not report a security problem in a public issue" in text.lower()
    assert "Ask for a private security channel" in text
    form = yaml.safe_load(_read(".github/ISSUE_TEMPLATE/security_channel.yml"))
    assert form["name"] == "Ask for a private security channel"


@pytest.mark.unit
def test_maintainers_file_links_each_handle_to_its_own_profile() -> None:
    assert re.search(r"\[@([\w-]+)\]\(https://github\.com/\1\)", _read("MAINTAINERS.md"))


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative",
    ["CODE_OF_CONDUCT.md", "SECURITY.md", "MAINTAINERS.md", ".github/ISSUE_TEMPLATE/config.yml"],
)
def test_every_reporting_route_names_the_same_private_channel(relative: str) -> None:
    assert _PRIVATE_REPORTING in _read(relative)


@pytest.mark.unit
@pytest.mark.parametrize("relative", _LINKED_FILES)
def test_relative_links_resolve(relative: str) -> None:
    targets = re.findall(r"\]\(([^)\s]+)\)", _read(relative))
    local = [t.split("#")[0] for t in targets if not re.match(r"(https?:|mailto:|#)", t)]
    missing = [t for t in local if t and not (ROOT / t).exists()]
    assert not missing, f"{relative} links to files that do not exist: {missing}"


@pytest.mark.unit
@pytest.mark.parametrize("relative", [*_PROSE_FILES, *_github_files()])
def test_no_public_file_carries_a_real_email_address(relative: str) -> None:
    real = [
        match.group(0)
        for match in _EMAIL.finditer(_read(relative))
        if match.group(1).lower() not in _EXAMPLE_DOMAINS
    ]
    assert not real, f"{relative} carries {real}; use GitHub's private channels instead"


@pytest.mark.unit
@pytest.mark.skipif(shutil.which("make") is None, reason="GNU Make is not installed")
def test_contributing_names_only_make_targets_that_exist() -> None:
    named = set(re.findall(r"(?:^|`|&& )make ([a-z][a-z-]*)", _read("CONTRIBUTING.md"), re.M))
    assert {"test-fast", "check", "ci-local", "imports"} <= named

    # `make -n` prints a target's recipe without running it. A phony target with no
    # rule still exits 0 but prints "Nothing to be done", so both are checked.
    def can_make(target: str) -> bool:
        done = subprocess.run(["make", "-n", target], cwd=ROOT, capture_output=True, text=True)
        return (
            done.returncode == 0
            and done.stdout.strip() != ""
            and "Nothing to be done" not in done.stdout
        )

    missing = sorted(target for target in named if not can_make(target))
    assert not missing, f"CONTRIBUTING.md names make targets the Makefile cannot make: {missing}"


@pytest.mark.unit
def test_contributing_says_proof_system_backends_are_out_of_scope() -> None:
    text = " ".join(_read("CONTRIBUTING.md").split())  # the wrapped lines, read as one
    assert "proof-system backends are out of scope for this repository" in text
    assert "separate commercial plugin" in text and "`make proof-guard`" in text


@pytest.mark.unit
def test_the_check_gate_keeps_its_static_checks_and_is_phony() -> None:
    makefile = _read("Makefile")
    rule = re.search(r"^check:([^=\n]*)$", makefile, re.MULTILINE)
    assert rule, "no `check:` rule"
    assert _CHECK_PREREQUISITES <= set(rule.group(1).split())
    flat = re.sub(r"\\\n\s*", " ", makefile)
    phony = re.search(r"^\.PHONY:(.*)$", flat, re.MULTILINE)
    assert phony and "check" in phony.group(1).split(), "`check` must be listed in .PHONY"


@pytest.mark.unit
def test_quick_start_is_in_the_right_order() -> None:
    text = _read("CONTRIBUTING.md")
    assert text.index("cd flux-engine") < text.index(
        "../FLUX"
    ), "../FLUX is relative to the working directory; clone it after `cd flux-engine`"
    assert "git remote add upstream" in text, "the sign-off repair refers to `upstream`"


@pytest.mark.unit
def test_the_documented_install_extra_exists() -> None:
    extras = tomllib.loads(_read("pyproject.toml"))["project"]["optional-dependencies"]
    assert "dev" in extras and "uv sync --extra dev" in _read("CONTRIBUTING.md")


@pytest.mark.unit
def test_dependabot_waits_a_week_and_groups_minor_and_patch_updates() -> None:
    config = yaml.safe_load(_read(".github/dependabot.yml"))
    assert config["version"] == 2
    updates = {update["package-ecosystem"]: update for update in config["updates"]}
    assert set(updates) == {"uv", "github-actions"}
    for name, update in updates.items():
        assert update["cooldown"]["default-days"] >= 7, f"{name}: a release must be a week old"
        (group,) = update["groups"].values()
        assert group["update-types"] == ["minor", "patch"], f"{name}: majors keep their own PR"
    assert updates["uv"]["open-pull-requests-limit"] <= 3


@pytest.mark.unit
def test_dco_workflow_triggers_pins_and_bot_exemption() -> None:
    text = _read(".github/workflows/dco.yml")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) == {"pull_request"}, "pull_request_target would run with secrets"
    uses = re.findall(r"uses:\s*(\S+)(.*)", text)
    assert uses, "no action in dco.yml"
    for ref, rest in uses:
        assert re.search(r"@[0-9a-f]{40}$", ref) and re.search(r"#\s*v\d", rest), ref
    (job,) = workflow["jobs"].values()
    config = tomllib.loads(job["steps"][0]["with"]["config"])
    assert config == {"bot": {"policy": "allowlist", "allow": ["dependabot[bot]"]}}


@pytest.mark.unit
def test_dco_job_is_skipped_only_for_organisation_owners_and_members() -> None:
    text = _read(".github/workflows/dco.yml")
    (job,) = yaml.safe_load(text)["jobs"].values()
    condition = " ".join(job.get("if", "").split())  # the `if:` is a folded scalar
    match = _DCO_MEMBER_EXEMPTION.match(condition)
    assert match, f"the job's `if:` is not the exemption test: {condition!r}"
    assert json.loads(match["roles"]) == ["OWNER", "MEMBER"], (
        "only owners and members may skip the sign-off check; COLLABORATOR, CONTRIBUTOR, "
        "FIRST_TIME_CONTRIBUTOR and NONE are outside contributors"
    )
    logins = json.loads(match["logins"])
    assert logins, "an empty list would make the second half of the test dead code"
    assert all(
        isinstance(login, str) and _GITHUB_LOGIN.match(login) for login in logins
    ), f"the private-member list must hold plain GitHub user names, got {logins}"
    assert len({login.lower() for login in logins}) == len(logins), "a login is listed twice"
    assert "continue-on-error" not in job and all(
        "continue-on-error" not in step for step in job["steps"]
    ), "a failing sign-off check must fail the pull request"
    assert all("if" not in step for step in job["steps"]), "the exemption belongs on the job"


@pytest.mark.unit
def test_contributing_explains_the_sign_off() -> None:
    text = _read("CONTRIBUTING.md")
    assert "git commit -s" in text
    assert "git rebase --signoff" in text
    assert "git fetch upstream" in text
    assert "owners and members skip the check" in " ".join(text.split()), "say who skips"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["bug_report", "feature_request", "security_channel"])
def test_issue_forms_are_well_formed(name: str) -> None:
    form = yaml.safe_load(_read(f".github/ISSUE_TEMPLATE/{name}.yml"))
    assert form["name"] and form["description"] and form["body"]
    ids = [item["id"] for item in form["body"] if "id" in item]
    assert len(ids) == len(set(ids)), "issue form field ids must be unique"


@pytest.mark.unit
def test_issue_chooser_turns_off_blank_issues_and_keeps_the_private_channel() -> None:
    config = yaml.safe_load(_read(".github/ISSUE_TEMPLATE/config.yml"))
    assert config["blank_issues_enabled"] is False
    assert any("security/advisories/new" in link["url"] for link in config["contact_links"])


@pytest.mark.unit
def test_pull_request_template_asks_for_the_sign_off() -> None:
    template = " ".join(_read(".github/pull_request_template.md").split())
    assert "git commit -s" in template
    assert "owners and members can skip this" in template, "say who may skip the sign-off"
