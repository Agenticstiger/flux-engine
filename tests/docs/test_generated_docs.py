# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The reference pages generated from the code, and the addresses the CLI prints for them.

``scripts/gen_docs.py`` writes ``docs/reference/cli/`` from the argparse tree and
``docs/reference/errors/`` from the error catalog. Nothing in those directories
is written by hand, so the one thing worth holding is that they are current. A
page that disagrees with the code is a wrong page, and a failing test is how it
gets noticed: this file is the CI drift gate.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import List

import pytest

from flux_engine._links import DOCS_URL, command_url, error_url
from flux_engine.api.stages import StageResult
from flux_engine.cli import _print_result, build_parser, main
from flux_engine.core.errors import catalog, ensure_core_codes_loaded

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

pytestmark = pytest.mark.unit


def _load_generator() -> ModuleType:
    path = REPO_ROOT / "scripts" / "gen_docs.py"
    spec = importlib.util.spec_from_file_location("gen_docs_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen_docs = _load_generator()
ensure_core_codes_loaded()
CODES = [entry.code for entry in catalog()]


# --- the drift gate -----------------------------------------------------------


def test_the_generated_reference_pages_are_current() -> None:
    """Run in a clean interpreter: the error catalog is process-global, and any test
    that registered a code of its own would otherwise show up here as a missing page."""
    done = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "gen_docs.py"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, (
        "docs/reference is out of date with the code; run `make docs-gen` and commit the result:\n"
        + done.stderr
    )


def test_the_drift_gate_notices_a_page_that_was_edited_by_hand(tmp_path: Path) -> None:
    gen_docs.write(tmp_path)
    assert gen_docs.check(tmp_path) == []
    page = tmp_path / "reference" / "errors" / "FLX-1001.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nA hand-written paragraph.\n")
    assert gen_docs.check(tmp_path) == [
        "differs from what the code generates: reference/errors/FLX-1001.md"
    ]


def test_the_drift_gate_notices_a_missing_page_and_a_stray_one(tmp_path: Path) -> None:
    gen_docs.write(tmp_path)
    (tmp_path / "reference" / "cli" / "run.md").unlink()
    (tmp_path / "reference" / "errors" / "FLX-9999.md").write_text("left over from a deleted code")
    assert gen_docs.check(tmp_path) == [
        "missing: reference/cli/run.md",
        "stale, nothing generates it: reference/errors/FLX-9999.md",
    ]


def test_write_removes_a_stray_page_and_is_idempotent(tmp_path: Path) -> None:
    gen_docs.write(tmp_path)
    stray = tmp_path / "reference" / "errors" / "FLX-9999.md"
    stray.write_text("left over")
    assert gen_docs.write(tmp_path) == ["reference/errors/FLX-9999.md (removed)"]
    assert gen_docs.write(tmp_path) == []


# --- the schemas page ----------------------------------------------------------


def test_the_schemas_page_names_the_pinned_version_and_every_flux_file() -> None:
    from flux_engine.describe import VENDORED_SPEC_VERSION

    text = (DOCS / "reference" / "schemas.md").read_text(encoding="utf-8")
    assert f"**FLUX {VENDORED_SPEC_VERSION}**" in text
    assert f"[flux-schema-{VENDORED_SPEC_VERSION}.json](" in text
    for stem in gen_docs._FLUX_SCHEMA_PURPOSE:
        assert re.search(rf"\[{re.escape(stem)}-\d+\.\d+\.\d+\.json\]\(", text), stem
    assert text.count("fluid-schema-") >= 3


def test_each_schema_family_links_its_newest_file_at_or_below_the_pin(tmp_path: Path) -> None:
    """A release republishes only what it changed; the page must not invent a file."""
    for name in (
        "flux-x-0.4.1.json",
        "flux-x-0.5.0.json",
        "flux-x-0.6.0.json",
        "flux-x-latest.json",
    ):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert gen_docs._current_schema_file(tmp_path, "flux-x", "0.5.1") == "flux-x-0.5.0.json"
    assert gen_docs._current_schema_file(tmp_path, "flux-x", "0.6.0") == "flux-x-0.6.0.json"
    assert gen_docs._current_schema_file(tmp_path, "flux-x", "0.4.0") is None


def test_a_schema_the_specification_no_longer_ships_stops_the_generator(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A page that quietly lost a row would still match its own output and pass the drift gate."""
    monkeypatch.setitem(gen_docs._FLUX_SCHEMA_PURPOSE, "flux-nonesuch", "A file nobody ships.")
    with pytest.raises(gen_docs.GenerationError, match="flux-nonesuch"):
        gen_docs.schemas_page()
    assert gen_docs.main(["--check"]) == 1
    assert "flux-nonesuch" in capsys.readouterr().err


# --- one page per code ---------------------------------------------------------


def test_every_code_in_the_catalog_has_a_page_and_every_page_has_a_code() -> None:
    pages = {p.stem for p in (DOCS / "reference" / "errors").glob("FLX-*.md")}
    assert pages == set(CODES)
    assert len(CODES) > 80


@pytest.mark.parametrize("entry", catalog(), ids=CODES)
def test_a_code_page_carries_the_catalog_title_and_explanation(entry) -> None:
    text = (DOCS / "reference" / "errors" / f"{entry.code}.md").read_text(encoding="utf-8")
    assert f"# {entry.code}: " in text
    assert entry.title.split()[0] in text
    assert f"$ fluxsim explain {entry.code}" in text
    assert f"(the {entry.band} band)" in text


def test_the_error_index_links_every_code_once() -> None:
    index = (DOCS / "reference" / "errors" / "README.md").read_text(encoding="utf-8")
    for code in CODES:
        assert index.count(f"[{code}](./{code}.md)") == 1, code


# --- the addresses the command line prints --------------------------------------


def _page_for(url: str) -> Path:
    assert url.startswith(DOCS_URL), url
    relative = url[len(DOCS_URL) :]
    if relative.endswith("/"):
        return DOCS / relative / "README.md"
    assert relative.endswith(".html"), url
    return DOCS / (relative[: -len(".html")] + ".md")


@pytest.mark.parametrize("code", CODES)
def test_the_address_printed_for_a_code_is_a_page_that_exists(code: str) -> None:
    assert _page_for(error_url(code)).is_file()


def test_the_address_printed_for_each_command_is_a_page_that_exists() -> None:
    names = ["", *build_parser()._subparsers._group_actions[0].choices]  # type: ignore[union-attr]
    for name in names:
        assert _page_for(command_url(name)).is_file(), name


def test_every_help_screen_names_the_page_for_its_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv, url in (
        ([], command_url()),
        (["run"], command_url("run")),
        (["accuracy", "score"], command_url("accuracy")),
        (["expr", "eval"], command_url("expr")),
    ):
        with pytest.raises(SystemExit):
            main([*argv, "--help"])
        out = capsys.readouterr().out
        assert f"\ndocs: {url}\n" in out, argv


# --- the addresses are public -----------------------------------------------------


def test_the_published_addresses_do_not_move() -> None:
    """Every released version prints these, and a printed address cannot be recalled.

    The literals are the point. ``_page_for`` above only checks that an address names
    a page in this tree, and a change to the base or to the layout would satisfy that
    by moving the page and the address together. A person following the address in a
    version already installed would find nothing.
    """
    site = "https://agenticstiger.github.io/flux/engine/"
    assert DOCS_URL == site
    assert command_url() == f"{site}reference/cli/"
    assert command_url("run") == f"{site}reference/cli/run.html"
    assert error_url("FLX-2109") == f"{site}reference/errors/FLX-2109.html"


@pytest.mark.parametrize("code", CODES)
def test_a_failure_with_this_code_ends_with_its_explanation_and_its_page(
    code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result = StageResult(stage="verify", exit_code=1, stderr=f"{code}: it failed", error_code=code)
    assert _print_result(result, as_json=False) == 1
    err = capsys.readouterr().err
    assert err.endswith(f"more: fluxsim explain {code}  {error_url(code)}\n")


@pytest.mark.parametrize("code", CODES)
def test_explaining_this_code_ends_with_its_page(
    code: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["explain", code]) == 0
    assert capsys.readouterr().out.endswith(f"\n\n{error_url(code)}\n")


# --- the pages are safe to publish -----------------------------------------------


def _outside_code(text: str) -> str:
    """*text* without fenced blocks, inline code spans and HTML comments."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def _generated_pages() -> List[Path]:
    return sorted(
        path
        for directory in gen_docs.GENERATED_DIRECTORIES
        for path in (DOCS / directory).glob("*.md")
    )


@pytest.mark.parametrize("path", _generated_pages(), ids=lambda p: p.relative_to(DOCS).as_posix())
def test_a_generated_page_holds_nothing_a_vue_template_would_read(path: Path) -> None:
    """VuePress compiles each page as a Vue template: a bare ``<tag>`` or ``{{ }}`` breaks the build."""
    prose = _outside_code(path.read_text(encoding="utf-8").split("---\n", 2)[2])
    assert "{{" not in prose and "}}" not in prose
    assert "<" not in prose.replace("&lt;", "")


def test_the_escape_keeps_code_spans_and_neutralises_everything_else() -> None:
    assert gen_docs.prose("under `<state_dir>/golden/`, run <id> {{x}}") == (
        "under `<state_dir>/golden/`, run &lt;id&gt; &#123;&#123;x&#125;&#125;"
    )


def test_every_option_of_every_command_is_documented() -> None:
    parser = build_parser()
    top = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    for name, command in top.items():
        page = (DOCS / "reference" / "cli" / f"{name}.md").read_text(encoding="utf-8")
        for node in [command, *gen_docs._subparsers(command).values()]:
            for option in gen_docs._options(node):
                assert f"`{option.flags}`" in page, (name, option.flags)


# --- the parts of the index that are written in the generator, held to the code --------


def test_every_environment_variable_the_index_lists_is_read_by_the_code() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "flux_engine").rglob("*.py")
        if "_vendor" not in path.parts
    )
    for name, _effect in gen_docs._ENVIRONMENT:
        assert f'"{name}"' in sources or f"'{name}'" in sources, name


def test_the_exit_codes_the_index_lists_are_the_ones_the_cli_states() -> None:
    import flux_engine.cli as cli

    documented = {code for code, _meaning in gen_docs._EXIT_CODES}
    assert documented == {"0", "1", "2", "3"}
    assert "2 for a usage error" in (cli.__doc__ or "")
    assert "1 for a command that ran and failed" in (cli.__doc__ or "")
    assert "``fluxsim gate decide``" in (cli.__doc__ or "")
    assert "returns 3" in (cli.__doc__ or "")


def test_the_envelope_the_index_shows_has_the_keys_of_a_real_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.starter import scaffold

    index = (DOCS / "reference" / "cli" / "README.md").read_text(encoding="utf-8")
    block = index.split("```json\n", 1)[1].split("\n```", 1)[0]
    shown = json.loads(block)

    scaffold(tmp_path / "demo")
    assert main(["validate", str(tmp_path / "demo"), "--json"]) == 0
    real = json.loads(capsys.readouterr().out)
    assert set(shown) == set(real)
