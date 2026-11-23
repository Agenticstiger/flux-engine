# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The skills check: the real tree passes, and every rule fails on a fixture that breaks it."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from flux_engine._links import DOCS_URL, ISSUES_URL
from tests.docs.harness import TESTED
from tests.skills.support import (
    DATA_SENTENCE,
    REPO_ROOT,
    SKILL,
    VERSION,
    check_skills,
    make_tree,
    rules,
    skill_text,
)

pytestmark = pytest.mark.unit

DOCS_BASE = f"https://github.com/Agenticstiger/flux-engine/blob/v{VERSION}/docs/tutorial/page.md"
MARKETPLACE_ADD = "claude plugin marketplace add Agenticstiger/flux-engine@vVERSION --sparse .claude-plugin skills"


def body_rules(tmp_path: Path, body: str) -> List[str]:
    return rules(make_tree(tmp_path, body))


def fence(language: str, *lines: str) -> str:
    return "\n".join([f"```{language}", *lines, "```", ""])


# ── the real tree ─────────────────────────────────────────────────────────


def test_the_real_tree_passes() -> None:
    findings = check_skills.check(REPO_ROOT)
    assert findings == [], "\n".join(str(f) for f in findings)


def test_missing_skills_lists_the_eight_minus_those_present() -> None:
    present = [n for n in check_skills.EXPECTED_SKILLS if (REPO_ROOT / "skills" / n).is_dir()]
    expected = [n for n in check_skills.EXPECTED_SKILLS if n not in present]
    assert check_skills.missing_skills(REPO_ROOT) == expected
    assert len(check_skills.EXPECTED_SKILLS) == 8


def test_the_tested_pages_are_pages_the_docs_tests_run() -> None:
    for page in check_skills.TESTED_PAGES:
        assert page in TESTED


def test_a_minimal_tree_is_clean(tmp_path: Path) -> None:
    assert check_skills.check(make_tree(tmp_path)) == []


def test_release_requires_all_eight(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    assert "skills" in rules(root, release=True)
    assert "skills" not in rules(root)


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_tree(tmp_path)
    assert check_skills.main(["--root", str(root)]) == 0
    (root / "skills" / SKILL / "SKILL.md").write_text(skill_text("`fluxsim --bogus`\n"))
    assert check_skills.main(["--root", str(root)]) == 1
    assert re.search(r"^skills/.*SKILL\.md:\d+: \w[\w-]*: ", capsys.readouterr().out, re.M)
    assert check_skills.main(["--root", str(tmp_path / "nowhere")]) == 2


# ── commands ──────────────────────────────────────────────────────────────


def test_an_abbreviated_option_is_refused(tmp_path: Path) -> None:
    assert "commands" in body_rules(tmp_path, fence("bash", "fluxsim plan bundle --sca 2"))


def test_an_option_with_an_equals_sign_passes(tmp_path: Path) -> None:
    assert body_rules(tmp_path, fence("bash", "fluxsim plan bundle --scale=2")) == []


@pytest.mark.parametrize(
    "body",
    [
        "    fluxsim plan bundle --bogus\n",
        "- fluxsim plan bundle --bogus\n",
        "1. fluxsim plan bundle --bogus\n",
        "> fluxsim plan bundle --bogus\n",
        fence("text", "fluxsim plan bundle --bogus"),
        "Run `fluxsim plan bundle --bogus` now.\n",
        "Run ``fluxsim plan bundle --bogus`` now.\n",
        fence("console", "$ fluxsim plan bundle --bogus"),
    ],
)
def test_a_bogus_option_is_caught_everywhere(tmp_path: Path, body: str) -> None:
    assert "commands" in body_rules(tmp_path, body)


def test_a_bare_bogus_option_in_prose_is_an_options_finding(tmp_path: Path) -> None:
    assert "options" in body_rules(tmp_path, "Pass --bogus to it.\n")


@pytest.mark.parametrize("info", ["", "sh", "python"])
def test_an_unnamed_or_foreign_fence_is_refused(tmp_path: Path, info: str) -> None:
    assert "blocks" in body_rules(tmp_path, f"```{info}\nhello\n```\n")


def test_a_tilde_fence_is_refused(tmp_path: Path) -> None:
    assert "blocks" in body_rules(tmp_path, "~~~text\nhello\n~~~\n")


def test_a_line_continuation_is_refused(tmp_path: Path) -> None:
    body = fence("bash", "fluxsim plan bundle \\", "  --scale 2")
    assert "commands" in body_rules(tmp_path, body)


@pytest.mark.parametrize("word", ["|", ">", ";", "&", "$HOME"])
def test_plain_words_only(tmp_path: Path, word: str) -> None:
    assert "commands" in body_rules(tmp_path, fence("bash", f"fluxsim plan bundle {word} x"))


def test_a_kernel_outside_the_choices_is_refused(tmp_path: Path) -> None:
    assert "commands" in body_rules(tmp_path, fence("bash", "fluxsim plan bundle --kernel 0.3.0"))


def test_key_and_sink_arguments_need_no_file(tmp_path: Path) -> None:
    body = fence(
        "bash",
        "fluxsim verify bundle --run-id RUN_ID --signing-key key.bin",
        "fluxsim apply bundle --run-id RUN_ID --sink parquet:OUT",
    )
    assert body_rules(tmp_path, body) == []


def test_a_sink_given_twice_is_not_a_check_failure(tmp_path: Path) -> None:
    # The once-only action became a plain store, so the check reads the line, not the run.
    body = fence("bash", "fluxsim apply bundle --run-id RUN_ID --sink parquet:A --sink parquet:B")
    assert body_rules(tmp_path, body) == []


def test_a_span_may_stop_before_a_required_argument(tmp_path: Path) -> None:
    assert body_rules(tmp_path, "Run `fluxsim plan` first.\n") == []


def test_a_line_must_parse_whole(tmp_path: Path) -> None:
    assert "commands" in body_rules(tmp_path, fence("bash", "fluxsim plan"))


def test_the_command_placeholder_takes_help_and_json_only(tmp_path: Path) -> None:
    assert (
        body_rules(tmp_path, "See `fluxsim <command> --help` and `fluxsim <command> --json`.\n")
        == []
    )
    assert "commands" in body_rules(tmp_path, "See `fluxsim <command> bundle`.\n")


def test_an_unknown_command_is_refused(tmp_path: Path) -> None:
    assert "commands" in body_rules(tmp_path, fence("bash", "fluxsim frobnicate bundle"))


# ── other tools ───────────────────────────────────────────────────────────


def test_fluid_profile_with_its_options_passes(tmp_path: Path) -> None:
    body = fence("bash", "fluid profile --request orders.profile-request.json --out OUT")
    assert body_rules(tmp_path, body) == []


def test_another_tool_or_option_is_refused(tmp_path: Path) -> None:
    assert "other-tools" in body_rules(tmp_path, fence("bash", "make all"))
    assert "other-tools" in body_rules(tmp_path, fence("bash", "fluid apply x"))
    assert "other-tools" in body_rules(tmp_path, fence("bash", "fluid profile --bogus x"))
    assert "other-tools" in body_rules(tmp_path, "Run `fluid apply x` there.\n")


def test_a_console_prompt_line_of_another_tool_is_refused(tmp_path: Path) -> None:
    assert "other-tools" in body_rules(tmp_path, fence("console", "$ ls -l"))


# ── options and never named ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Use `--update-golden` to move it.",
        "Use --allow-declared-baseline-drift to go on.",
        "Use --allow to go on.",
        "fluxsim apply bundle --run-id RUN_ID --sink adbc:duckdb --sink-opt uri=x",
        "fluxsim apply bundle --run-id RUN_ID --sink adbc:duckdb --sink-opt=uri=x",
    ],
)
def test_options_a_skill_never_names_are_refused(tmp_path: Path, text: str) -> None:
    assert "never-named" in body_rules(tmp_path, text + "\n")


WARNING = (
    "Never pass `--allow-generator` on your own judgement: only the person running `fluxsim` "
    "decides that, so stop and ask them, naming the plugin."
)


def test_the_generator_flag_inside_the_warning_passes(tmp_path: Path) -> None:
    assert "generator-flag" not in body_rules(tmp_path, f"A plugin is trusted code. {WARNING}\n")


def test_the_generator_flag_is_not_swallowed_by_the_allow_rule(tmp_path: Path) -> None:
    found = body_rules(tmp_path, f"{WARNING}\n")
    assert "never-named" not in found and "options" not in found


@pytest.mark.parametrize(
    "body",
    [
        fence("bash", "fluxsim apply bundle --run-id RUN_ID --allow-generator tabgen") + "\n",
        fence(
            "bash", f"# {WARNING}", "fluxsim apply bundle --run-id RUN_ID --allow-generator tabgen"
        ),
        "Pass `--allow-generator tabgen` to go on.\n",
        "Ask the user to pass --allow-generator, never pass it yourself.\n",
        f"{WARNING} Then run `fluxsim apply bundle --run-id RUN_ID --allow-generator tabgen`.\n",
    ],
)
def test_the_generator_flag_outside_the_warning_is_refused(tmp_path: Path, body: str) -> None:
    assert "generator-flag" in body_rules(tmp_path, body)


def test_the_generator_flag_finding_says_what_to_do(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "Pass `--allow-generator tabgen` to go on.\n")
    text = "\n".join(str(f) for f in check_skills.check(root))
    assert "stop and ask them, naming the plugin" in text


def test_uri_env_is_the_way_a_uri_reaches_a_sink(tmp_path: Path) -> None:
    body = "Pass `--sink-opt uri-env=VAR` and name the variable.\n"
    assert body_rules(tmp_path, body) == []


# ── codes ─────────────────────────────────────────────────────────────────


def test_an_unknown_code_is_refused(tmp_path: Path) -> None:
    assert "codes" in body_rules(tmp_path, "See FLX-0000.\n")


def test_a_catalogued_code_and_the_placeholder_pass(tmp_path: Path) -> None:
    assert body_rules(tmp_path, "See FLX-4005, or run `fluxsim explain FLX-nnnn`.\n") == []


# ── links ─────────────────────────────────────────────────────────────────


def test_a_link_at_the_release_passes(tmp_path: Path) -> None:
    assert body_rules(tmp_path, f"See [the page]({DOCS_BASE}).\n") == []


@pytest.mark.parametrize(
    "target",
    [
        DOCS_BASE.replace(VERSION, "0.0.1"),
        DOCS_URL,
        DOCS_BASE + "#top",
        DOCS_BASE + "?x=1",
        DOCS_BASE.replace("tutorial/page", "tutorial/absent"),
        "mailto:someone@example.org",
        "https://example.org/x",
    ],
)
def test_a_wrong_link_is_refused(tmp_path: Path, target: str) -> None:
    assert "links" in body_rules(tmp_path, f"See [the page]({target}).\n")


@pytest.mark.parametrize(
    "body",
    ["[ref]: https://example.org\n", "See <https://example.org>.\n", 'See <a href="x">y</a>.\n'],
)
def test_other_link_styles_are_refused(tmp_path: Path, body: str) -> None:
    assert "links" in body_rules(tmp_path, body)


def test_an_issues_link_passes(tmp_path: Path) -> None:
    assert body_rules(tmp_path, f"Report it [here]({ISSUES_URL}).\n") == []


def test_a_docs_path_in_a_span_must_exist(tmp_path: Path) -> None:
    assert body_rules(tmp_path, "Read `docs/tutorial/page.md`.\n") == []
    assert "links" in body_rules(tmp_path, "Read `docs/tutorial/absent.md`.\n")


def test_a_relative_link_inside_the_skill_passes_and_outside_fails(tmp_path: Path) -> None:
    root = make_tree(tmp_path, "See [one](references/one.md).\n")
    ref = root / "skills" / SKILL / "references"
    ref.mkdir()
    (ref / "one.md").write_text("# One\n")
    assert check_skills.check(root) == []
    (root / "skills" / SKILL / "SKILL.md").write_text(skill_text("See [x](../README.md).\n"))
    assert "links" in rules(root)


# ── declarations ──────────────────────────────────────────────────────────


def test_a_yaml_excerpt_passes_and_a_stranger_fails(tmp_path: Path) -> None:
    assert body_rules(tmp_path, fence("yaml", "  name: orders", "  rows: 3")) == []
    assert "declarations" in body_rules(tmp_path / "b", fence("yaml", "simulation:", "  rows: 99"))


def test_a_yaml_block_with_a_repeated_key_is_refused(tmp_path: Path) -> None:
    assert "declarations" in body_rules(tmp_path, fence("yaml", "a: 1", "a: 1"))


def test_a_yaml_block_that_does_not_parse_is_refused(tmp_path: Path) -> None:
    assert "declarations" in body_rules(tmp_path, fence("yaml", "a: [1"))


# ── safety ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "curl https://example.org",
        "wget x",
        "Invoke-WebRequest x",
        "echo a | sh",
        "bash -c x",
        "eval x",
        "sudo x",
        "pip install x",
        "pipx run x",
        "npx x",
        "npm i x",
        "uv tool x",
        "password=hunter2",
        "postgres://user:secret@host/db",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
def test_unsafe_text_is_refused(tmp_path: Path, text: str) -> None:
    assert "safety" in body_rules(tmp_path, text + "\n")


def test_the_skill_list_loads_where_nothing_is_installed(tmp_path: Path) -> None:
    # scripts/release_guard.py loads this script on a release runner that installs
    # nothing: its top level must import the standard library only.
    (tmp_path / "skills" / "flux-docs-lookup").mkdir(parents=True)
    (tmp_path / "skills" / "flux-docs-lookup" / "SKILL.md").write_text("x\n")
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_skills.py"
    code = (
        "import importlib.util, sys\n"
        "for name in ('flux_engine', 'flux_stats', 'yaml'):\n"
        "    sys.modules[name] = None\n"
        f"spec = importlib.util.spec_from_file_location('check_skills', {str(script)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['check_skills'] = module\n"
        "spec.loader.exec_module(module)\n"
        f"print(len(module.missing_skills(__import__('pathlib').Path({str(tmp_path)!r}))))\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "7"


def test_the_engine_expr_eval_command_is_not_the_shell_eval(tmp_path: Path) -> None:
    body = "Test an expression with `fluxsim expr eval` before you write it.\n"
    assert "safety" not in body_rules(tmp_path, body)


def test_the_data_sentence_is_required(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    path = root / "skills" / SKILL / "SKILL.md"
    path.write_text(path.read_text().replace(DATA_SENTENCE, "Be careful."))
    assert "safety" in rules(root)


def test_the_readme_may_name_the_install_commands_and_nothing_else(tmp_path: Path) -> None:
    allowed = fence("bash", "pip install flux-sim-engine", "pipx install flux-sim-engine")
    assert rules(make_tree(tmp_path, readme_body=allowed)) == []
    other = fence("bash", "pip install other")
    assert "safety" in rules(make_tree(tmp_path / "b", readme_body=other))


def test_a_skill_may_not_name_the_install_commands(tmp_path: Path) -> None:
    assert "safety" in body_rules(tmp_path, "pip install flux-sim-engine\n")


# ── claude code's own commands, README only ──────────────────────────────


def test_the_readme_may_add_the_marketplace_with_sparse(tmp_path: Path) -> None:
    assert rules(make_tree(tmp_path, readme_body=fence("bash", MARKETPLACE_ADD))) == []
    span = f"Run `{MARKETPLACE_ADD}` and then `/plugin install x`.\n"
    assert rules(make_tree(tmp_path / "b", readme_body=span)) == []


def test_a_skill_may_not_carry_the_same_line(tmp_path: Path) -> None:
    found = body_rules(tmp_path, fence("bash", MARKETPLACE_ADD))
    assert "other-tools" in found and "options" in found


def test_the_readme_still_gets_the_other_rules(tmp_path: Path) -> None:
    assert "commands" in rules(make_tree(tmp_path, readme_body="`fluxsim plan --bogus`\n"))
    assert "codes" in rules(make_tree(tmp_path / "b", readme_body="FLX-0000\n"))
    assert "blocks" in rules(make_tree(tmp_path / "c", readme_body="```\nx\n```\n"))
    assert "options" in rules(make_tree(tmp_path / "d", readme_body="Use --bogus.\n"))


def test_the_readme_bash_lines_are_not_commands(tmp_path: Path) -> None:
    assert rules(make_tree(tmp_path, readme_body=fence("bash", "make all"))) == []


# ── frontmatter ───────────────────────────────────────────────────────────

GOOD_FRONT = (
    'description: "Finds the docs. Use when the user asks where."\n'
    "license: Apache-2.0\n"
    'compatibility: "Needs the fluxsim command line."\n'
)


def front_rules(tmp_path: Path, front: str) -> List[str]:
    root = make_tree(tmp_path)
    (root / "skills" / SKILL / "SKILL.md").write_text(skill_text(front=front))
    return rules(root)


def test_good_frontmatter_passes(tmp_path: Path) -> None:
    assert front_rules(tmp_path, f"name: {SKILL}\n" + GOOD_FRONT) == []


def test_allowed_tools_is_refused_and_named(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    front = f"name: {SKILL}\n{GOOD_FRONT}allowed-tools: Bash(ls)\n"
    (root / "skills" / SKILL / "SKILL.md").write_text(skill_text(front=front))
    found = [f for f in check_skills.check(root) if f.rule == "frontmatter"]
    assert found and "allowed-tools" in found[0].message


@pytest.mark.parametrize(
    "front",
    [
        f"name: {SKILL}-\n" + GOOD_FRONT,
        f"name: {SKILL}--x\n" + GOOD_FRONT,
        "name: docs-lookup\n" + GOOD_FRONT,
        "name: flux-other\n" + GOOD_FRONT,
        f"name: Flux-Docs-Lookup\n{GOOD_FRONT}",
        GOOD_FRONT,
        f"name: {SKILL}\nname: {SKILL}\n" + GOOD_FRONT,
        f"name: {SKILL}\n" + GOOD_FRONT.replace("Use when", "Use if"),
        f"name: {SKILL}\n" + GOOD_FRONT.replace("Apache-2.0", "MIT"),
        f"name: {SKILL}\n" + GOOD_FRONT.replace("Needs the", "Needs engine 0.9 and the"),
        f"name: {SKILL}\n" + GOOD_FRONT + "extra: 1\n",
        f"name: {SKILL}\n" + GOOD_FRONT + "metadata:\n  n: 3\n",
        f"name: {SKILL}\n" + GOOD_FRONT.split("compatibility")[0],
        f"name: {SKILL}\n" + GOOD_FRONT.replace("Finds the docs.", "x" * 700),
    ],
)
def test_bad_frontmatter_is_refused(tmp_path: Path, front: str) -> None:
    assert "frontmatter" in front_rules(tmp_path, front)


def test_a_forge_cli_version_is_allowed_in_compatibility(tmp_path: Path) -> None:
    front = GOOD_FRONT.replace("Needs the", "Needs data-product-forge 0.38.0 and the")
    assert front_rules(tmp_path, f"name: {SKILL}\n" + front) == []


def test_a_file_without_frontmatter_is_refused(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "skills" / SKILL / "SKILL.md").write_text(f"# Title\n\n{DATA_SENTENCE}\n")
    assert "frontmatter" in rules(root)


# ── layout, skills, plugin root ───────────────────────────────────────────


def test_an_extra_file_or_folder_in_a_skill_is_refused(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "skills" / SKILL / "scripts").mkdir()
    assert "layout" in rules(root)


def test_a_skill_longer_than_the_limit_is_refused(tmp_path: Path) -> None:
    assert "layout" in body_rules(tmp_path, "line\n" * 150)


def test_a_reference_must_be_linked_and_short(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    ref = root / "skills" / SKILL / "references"
    ref.mkdir()
    (ref / "one.md").write_text("# One\n")
    assert "layout" in rules(root)
    (ref / "one.md").write_text("line\n" * 251)
    (root / "skills" / SKILL / "SKILL.md").write_text(skill_text("[one](references/one.md)\n"))
    assert "layout" in rules(root)


def test_a_skill_named_in_the_text_must_exist(tmp_path: Path) -> None:
    assert "layout" in body_rules(tmp_path, "The `flux-ghost` skill does it.\n")
    assert body_rules(tmp_path / "b", f"The `{SKILL}` skill does it.\n") == []


def test_a_folder_that_is_not_one_of_the_eight_is_refused(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "skills" / "flux-ghost").mkdir()
    assert "skills" in rules(root)


def test_the_plugin_root_holds_nothing_else(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / "skills" / ".mcp.json").write_text("{}")
    assert "plugin-root" in rules(root)
    (root / "skills" / ".mcp.json").unlink()
    (root / "skills" / ".claude-plugin" / "extra.json").write_text("{}")
    assert "plugin-root" in rules(root)


# ── manifests ─────────────────────────────────────────────────────────────


def edit_json(root: Path, rel: str, change: object) -> None:
    path = root / rel
    data = json.loads(path.read_text())
    assert callable(change)
    change(data)
    path.write_text(json.dumps(data))


PLUGIN = "skills/.claude-plugin/plugin.json"
MARKET = ".claude-plugin/marketplace.json"


@pytest.mark.parametrize(
    "rel, change",
    [
        (PLUGIN, lambda d: d.update(version="9.9.9")),
        (PLUGIN, lambda d: d.update(extra=1)),
        (PLUGIN, lambda d: d.update(name="Not_Kebab")),
        (PLUGIN, lambda d: d.update(license="MIT")),
        (PLUGIN, lambda d: d.update(homepage="https://example.org")),
        (PLUGIN, lambda d: d.update(author={"name": ""})),
        (PLUGIN, lambda d: d.update(skills="./skills")),
        (MARKET, lambda d: d.update(name="bad name")),
        (MARKET, lambda d: d.update(name="a..b")),
        (MARKET, lambda d: d.update(owner={})),
        (MARKET, lambda d: d.pop("description")),
        (MARKET, lambda d: d.update(extra=1)),
        (MARKET, lambda d: d.update(plugins=[])),
        (MARKET, lambda d: d["plugins"][0].update(version="1.2.3")),
        (MARKET, lambda d: d["plugins"][0].update(source="./")),
        (MARKET, lambda d: d["plugins"][0].update(name="other")),
    ],
)
def test_a_broken_manifest_is_refused(tmp_path: Path, rel: str, change: object) -> None:
    root = make_tree(tmp_path)
    edit_json(root, rel, change)
    assert "manifests" in rules(root)


def test_a_missing_or_repeating_manifest_is_refused(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    (root / MARKET).write_text('{"name": "a", "name": "b"}')
    assert "manifests" in rules(root)
    (root / MARKET).unlink()
    assert "manifests" in rules(root)


def test_a_link_in_a_skill_is_refused_and_never_read(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("a line from outside the repository\n")
    references = root / "skills" / SKILL / "references"
    references.mkdir(exist_ok=True)
    (references / "linked.md").symlink_to(outside)
    findings = check_skills.check(root)
    assert "layout" in {f.rule for f in findings}
    assert not any("outside the repository" in str(f) for f in findings)
