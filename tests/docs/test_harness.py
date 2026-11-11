# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The documentation harness itself: what a block means and what makes it fail."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import List

import pytest

from .harness import (
    PageFailure,
    all_markdown,
    fence_languages,
    matches,
    run_page,
    skips,
    split_console,
)

pytestmark = pytest.mark.unit


def _page(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "page.md"
    path.write_text(body, encoding="utf-8")
    return path


def _run(tmp_path: Path, body: str):
    scratch = Path(tempfile.mkdtemp(prefix="scratch-", dir=tmp_path))
    return run_page(_page(tmp_path, body), scratch)


def _lines(text: str) -> List[str]:
    return text.splitlines()


# --- reading a console block -------------------------------------------------------


def test_a_console_block_is_commands_each_followed_by_its_output() -> None:
    steps = split_console("$ echo one\none\n$ echo two\ntwo\nthree\n")
    assert [(s.command, s.expected) for s in steps] == [
        ("echo one", ["one"]),
        ("echo two", ["two", "three"]),
    ]


def test_a_command_can_continue_on_the_next_line_with_a_backslash() -> None:
    (step,) = split_console("$ echo a \\\n    b\na b\n")
    assert step.command == "echo a \\\n    b"
    assert step.expected == ["a b"]


def test_a_command_that_is_meant_to_fail_says_so_in_a_comment() -> None:
    plain, failing = split_console("$ true\n$ false   # exit 1\n")
    assert (plain.expected_exit, failing.expected_exit) == (0, 1)


def test_a_here_document_belongs_to_its_command_and_is_not_output() -> None:
    steps = split_console(
        "$ cat > f <<'EOF'\nline one\nline two\nEOF\n$ cat f\nline one\nline two\n"
    )
    assert [s.expected for s in steps] == [[], ["line one", "line two"]]
    assert steps[0].command == "cat > f <<'EOF'\nline one\nline two\nEOF"


def test_a_here_document_that_is_never_closed_is_an_error() -> None:
    with pytest.raises(ValueError, match="never closed"):
        split_console("$ cat <<EOF\nline\n")


def test_output_before_the_first_command_is_an_error() -> None:
    with pytest.raises(ValueError, match="before any"):
        split_console("stray output\n$ true\n")


# --- comparing output ----------------------------------------------------------------


@pytest.mark.parametrize(
    "expected,actual",
    [
        ("a\nb", "a\nb"),
        ("a\nb\n\n", "\n\na\nb  \n"),
        ("run id ... done", "run id 8dec done"),
        ("a\n...\nz", "a\nb\nc\nz"),
        ("a\n...\nz", "a\nz"),
        ("...", ""),
        ("a\n...", "a\nb\nc"),
        ("...\nz", "x\ny\nz"),
        ("x ... y ... z", "x 1 y 2 z"),
    ],
)
def test_output_that_matches(expected: str, actual: str) -> None:
    assert matches(_lines(expected), _lines(actual))


@pytest.mark.parametrize(
    "expected,actual",
    [
        ("a\nb", "a\nc"),
        ("a\nb", "a"),
        ("a", "a\nb"),
        ("a\n...\nz", "a\nb\ny"),
        ("run id ... done", "run id 8dec"),
        ("a\n\nb", "a\nb"),
    ],
)
def test_output_that_does_not_match(expected: str, actual: str) -> None:
    assert not matches(_lines(expected), _lines(actual))


# --- running a page --------------------------------------------------------------------


def test_a_page_whose_blocks_do_what_it_says_passes(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        "# T\n\n```console\n$ echo hello\nhello\n$ printf 'a\\nb\\n'\na\n...\n```\n",
    )
    assert report.ran == 1 and report.skipped == []


def test_a_block_whose_output_differs_fails_with_the_line_and_a_diff(tmp_path: Path) -> None:
    with pytest.raises(PageFailure) as failure:
        _run(tmp_path, "# T\n\n```console\n$ echo hello\nHELLO\n```\n")
    text = str(failure.value)
    assert "page.md:3" in text and "-HELLO" in text and "+hello" in text


def test_a_command_that_fails_unannounced_fails_the_page(tmp_path: Path) -> None:
    with pytest.raises(PageFailure, match="exited 3"):
        _run(tmp_path, "```console\n$ sh -c 'exit 3'\n```\n")


def test_a_command_that_fails_as_announced_passes(tmp_path: Path) -> None:
    _run(tmp_path, "```console\n$ sh -c 'echo no; exit 1'   # exit 1\nno\n```\n")


def test_a_command_announced_to_fail_that_succeeds_fails_the_page(tmp_path: Path) -> None:
    with pytest.raises(PageFailure, match="exited 0, the page says 1"):
        _run(tmp_path, "```console\n$ true   # exit 1\n```\n")


def test_stderr_is_part_of_the_output(tmp_path: Path) -> None:
    _run(tmp_path, "```console\n$ echo oops >&2\noops\n```\n")


def test_a_here_document_writes_its_file(tmp_path: Path) -> None:
    _run(
        tmp_path,
        '```console\n$ cat > slice.ndjson <<\'EOF\'\n{"a": 1}\nEOF\n$ cat slice.ndjson\n{"a": 1}\n```\n',
    )


def test_a_cd_in_one_block_holds_in_the_next(tmp_path: Path) -> None:
    _run(
        tmp_path,
        "```console\n$ mkdir demo\n$ cd demo\n```\n\n" '```console\n$ basename "$PWD"\ndemo\n```\n',
    )


def test_a_yaml_block_that_names_a_file_is_written_there(tmp_path: Path) -> None:
    _run(
        tmp_path,
        "```console\n$ mkdir demo\n$ cd demo\n```\n\n"
        "```yaml\n# persona-x.flux.yml\nkind: Persona\n```\n\n"
        "```console\n$ cat persona-x.flux.yml\n# persona-x.flux.yml\nkind: Persona\n```\n",
    )


def test_a_yaml_block_without_a_file_name_is_only_parsed(tmp_path: Path) -> None:
    _run(tmp_path, "```yaml\nkind: Persona\n```\n\n```console\n$ ls\n```\n")
    (scratch,) = [p for p in tmp_path.iterdir() if p.name.startswith("scratch-")]
    assert list(scratch.iterdir()) == []


def test_a_yaml_block_that_does_not_parse_fails_the_page(tmp_path: Path) -> None:
    with pytest.raises(PageFailure, match="does not parse"):
        _run(tmp_path, "```yaml\na: [unclosed\n```\n")


def test_a_json_block_must_parse(tmp_path: Path) -> None:
    _run(tmp_path, '```json\n{"ok": true}\n```\n')
    with pytest.raises(PageFailure, match="does not parse"):
        _run(tmp_path, "```json\n{nope}\n```\n")


def test_a_python_block_runs_and_keeps_its_variables(tmp_path: Path) -> None:
    _run(tmp_path, "```python\nx = 40\n```\n\n```python\nassert x + 2 == 42\n```\n")
    with pytest.raises(PageFailure):
        _run(tmp_path, "```python\nassert 1 == 2\n```\n")


def test_python_blocks_count_as_blocks_that_ran(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        "<!-- invisible-code-block: python\ny = 2\n-->\n\n```python\nassert y == 2\n```\n",
    )
    assert report.ran == 2


def test_a_page_that_changes_directory_leaves_the_process_where_it_was(tmp_path: Path) -> None:
    before = os.getcwd()
    _run(tmp_path, "```python\nimport os\nos.chdir(workdir)\n```\n")
    assert os.getcwd() == before


def test_the_directory_is_put_back_when_the_page_fails(tmp_path: Path) -> None:
    before = os.getcwd()
    with pytest.raises(PageFailure):
        _run(tmp_path, "```python\nimport os\nos.chdir(workdir)\nassert False\n```\n")
    assert os.getcwd() == before


def test_a_skipped_block_is_left_out_and_reported_with_its_reason(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        '<!-- skip: next "needs the network" -->\n```console\n$ false\n```\n\n```console\n$ true\n```\n',
    )
    assert report.ran == 1
    assert report.skipped == [(2, "needs the network")]


def test_a_text_block_is_not_run(tmp_path: Path) -> None:
    report = _run(tmp_path, "```text\n$ false\n```\n")
    assert report.ran == 0


def test_a_page_runs_in_its_own_directory_with_fluxsim_on_the_path(tmp_path: Path) -> None:
    _run(
        tmp_path,
        "```console\n$ fluxsim --version\n0.2.1\n$ fluxsim init demo   # exit 0\n...\n$ ls demo | head -1\n"
        "detector.flux.yml\n```\n",
    )


def test_fluxsim_variables_of_the_developer_do_not_leak_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUX_DEBUG", "1")
    _run(tmp_path, '```console\n$ echo "[${FLUX_DEBUG:-unset}]"\n[unset]\n```\n')


# --- which fences a page may use ---------------------------------------------------------


def test_fence_languages_lists_each_opening_fence() -> None:
    text = "# T\n\n```console\n$ x\n```\n\ntext\n\n````text\n```inner\n````\n\n```\nbare\n```\n"
    assert fence_languages(text) == ["console", "text", ""]


def test_fence_languages_sees_indented_quoted_and_tilde_fences() -> None:
    """Sybil runs a fenced block wherever it stands, so the language check must see it too."""
    text = "- step\n\n    ```sh\n    $ false\n    ```\n\n> ```shell\n> x\n> ```\n\n~~~zsh\nx\n~~~\n"
    assert fence_languages(text) == ["sh", "shell", "zsh"]


def test_a_fence_closes_on_its_own_character_and_a_longer_fence_holds_shorter_ones() -> None:
    text = "````md\n~~~\n```\n```python\n````\n\n```console\n```\n"
    assert fence_languages(text) == ["md", "console"]


# --- what the hunt for false passes found ------------------------------------------------


@pytest.mark.parametrize(
    "info",
    ['console title="x"', "console ", "bash {.x}", "sh", "Console", "shell", "py"],
)
def test_a_fence_that_sybil_would_not_run_comes_back_whole_so_it_is_flagged(info: str) -> None:
    assert fence_languages(f"```{info}\n$ false\n```\n") == [info]


def test_a_longer_closing_fence_closes_the_block() -> None:
    assert fence_languages("```text\nx\n````\n\n```sh\nfalse\n```\n") == ["text", "sh"]


def test_a_pipeline_fails_when_its_first_stage_does(tmp_path: Path) -> None:
    with pytest.raises(PageFailure, match="exited 1"):
        _run(tmp_path, "```console\n$ false | cat\n```\n")
    with pytest.raises(PageFailure, match="exited 1"):
        _run(tmp_path, "```bash\nfalse | cat\n```\n")


def test_a_here_string_is_not_a_here_document(tmp_path: Path) -> None:
    _run(tmp_path, "```console\n$ cat <<< hi\nhi\n```\n")


def test_a_line_with_many_wildcards_is_matched_in_linear_time() -> None:
    started = time.monotonic()
    assert not matches(["... a ... a ... a ... a ... b"], ["a " * 400 + "c"])
    assert matches(["... a ... a ... a ... a ... b"], ["a " * 400 + "b"])
    assert time.monotonic() - started < 1.0


def test_a_long_expected_block_does_not_hit_the_recursion_limit() -> None:
    lines = [f"line {number}" for number in range(3000)]
    assert matches(["..."] + lines[-5:], lines)
    assert matches(lines, lines)
    assert not matches(lines, lines[:-1])


def test_wildcards_cannot_share_the_same_text(tmp_path: Path) -> None:
    assert matches(["ab...ba"], ["aba"]) is False
    assert matches(["ab...ba"], ["abba"]) is True


def test_a_yaml_block_with_a_key_twice_fails_the_page(tmp_path: Path) -> None:
    with pytest.raises(PageFailure, match="twice"):
        _run(tmp_path, "```yaml\na: 1\na: 2\n```\n")


def test_a_json_block_with_nan_fails_the_page(tmp_path: Path) -> None:
    with pytest.raises(PageFailure, match="not JSON"):
        _run(tmp_path, '```json\n{"a": NaN}\n```\n')


def test_a_python_block_writes_relative_paths_into_the_scratch_directory(tmp_path: Path) -> None:
    before = set(os.listdir(os.getcwd()))
    _run(tmp_path, "```python\nopen('leak-probe.txt', 'w').write('x')\n```\n")
    (scratch,) = [p for p in tmp_path.iterdir() if p.name.startswith("scratch-")]
    assert (scratch / "leak-probe.txt").is_file()
    assert set(os.listdir(os.getcwd())) == before


def test_an_environment_variable_a_page_sets_does_not_reach_the_next_page(tmp_path: Path) -> None:
    _run(tmp_path, "```python\nimport os\nos.environ['DOCS_LEAK_PROBE'] = '1'\n```\n")
    assert "DOCS_LEAK_PROBE" not in os.environ


def test_markdown_in_any_case_or_extension_or_behind_a_link_is_found(tmp_path: Path) -> None:
    (tmp_path / "docs" / "how-to").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    for name in ("a.md", "B.MD", "c.markdown", "d.mdx", "notes.txt"):
        (tmp_path / "docs" / "how-to" / name).write_text("# x\n")
    (tmp_path / "elsewhere" / "e.md").write_text("# x\n")
    (tmp_path / "docs" / "linked").symlink_to(tmp_path / "elsewhere")
    found = sorted(p.relative_to(tmp_path).as_posix() for p in all_markdown(tmp_path))
    assert found == [
        "docs/how-to/B.MD",
        "docs/how-to/a.md",
        "docs/how-to/c.markdown",
        "docs/how-to/d.mdx",
        "docs/linked/e.md",
    ]


# --- skip directives, read the way Sybil reads them ----------------------------------------


def test_a_skip_directive_is_read_in_every_spelling_sybil_accepts() -> None:
    text = (
        '<!-- skip: next "needs the network here" -->\n```console\n$ x\n```\n\n'
        '<!-- skip next "no colon in this one" -->\n```console\n$ y\n```\n\n'
        '<!--- skip: next "a -> b needs net" --->\n```console\n$ z\n```\n'
    )
    found = skips(text)
    assert [(s.action, s.reason, s.before) for s in found] == [
        ("next", "needs the network here", "console"),
        ("next", "no colon in this one", "console"),
        ("next", "a -> b needs net", "console"),
    ]


def test_a_skip_that_is_not_one_next_block_with_a_reason_is_visible() -> None:
    text = (
        '<!-- skip: start "leaves out many blocks" -->\n```console\n$ x\n```\n'
        "<!-- skip: next if(True, reason='a condition') -->\n```console\n$ y\n```\n"
    )
    first, second = skips(text)
    assert (first.action, first.reason) == ("start", None)
    assert second.reason is None


def test_a_skip_before_a_text_block_or_prose_does_not_count_as_adjacent() -> None:
    text = (
        '<!-- skip: next "hops over the text" -->\n```text\n$ x\n```\n\n```console\n$ y\n```\n\n'
        '<!-- skip: next "prose in between" -->\n\nsome words\n\n```console\n$ z\n```\n'
    )
    first, second = skips(text)
    assert first.before == "text"
    assert second.before is None
