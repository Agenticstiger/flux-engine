# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Run the Markdown pages as tests.

The documentation is held to one promise: what a page shows is what the engine
does. [Sybil](https://sybil.readthedocs.io/) finds the fenced blocks in a page;
this module says what each kind of block means and runs it.

* ```` ```console ````: a terminal session. A line that starts with ``$ `` is a
  command (a trailing backslash continues it, and a here-document belongs to the
  command that opens it); the lines after it, up to the next ``$ `` line, are the
  output it must print, stdout and stderr together. A line that is only ``...``
  stands for any number of lines, and ``...`` inside a line for any text on that
  line. A command is expected to succeed; one that is meant to fail ends its last
  line with ``# exit 1`` (or the status it should return). A command with no output
  lines (or only blank ones) is run and only its status is checked. A pipeline fails
  when any stage of it does (``pipefail``).
* ```` ```bash ````: commands with no output to compare; every one must succeed.
* ```` ```yaml ````: when the first line is a comment that names a document,
  ``# persona.flux.yml``, the block is written to that file in the project
  directory before the next block runs. Any other yaml block must still parse, and
  a mapping with the same key twice is refused.
* ```` ```python ````: executed in the test process, with the page's variables kept
  between blocks. The process starts in the page's temporary directory, ``workdir``
  names it, and the directory and environment this process had are put back when the
  page ends. A ``<!-- invisible-code-block: python ... -->`` comment runs the same
  way and is not shown, for setting a page up.
* ```` ```json ````: must parse (``NaN`` is not JSON). ```` ```text ````: not run,
  on purpose.
* ``<!-- skip: next "reason" -->`` immediately before a block leaves that block out.
  A page's own steps share one working directory, so ``cd demo`` in one block holds
  in the next.

Fences are not nested: Sybil takes every fence line it sees, so a console example
written inside another fence would run. A fence whose line holds anything but the
language (a title, a trailing space) is not run by Sybil, and the tests flag it.

A page starts in an empty temporary directory with the ``fluxsim`` of the
interpreter running the tests on ``PATH``, so it must create what it uses.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from sybil import Document, Example, Sybil
from sybil.evaluators.python import PythonEvaluator
from sybil.parsers.markdown import CodeBlockParser, PythonCodeBlockParser, SkipParser
from sybil.parsers.markdown.lexers import RawFencedCodeBlockLexer

__all__ = [
    "ACCOUNTED_LANGUAGES",
    "LEFT_OUT",
    "OWNED_ELSEWHERE",
    "PageFailure",
    "PageReport",
    "RUN_LANGUAGES",
    "Skip",
    "Step",
    "TESTED",
    "all_markdown",
    "fence_languages",
    "matches",
    "run_page",
    "skips",
    "split_console",
    "tested_pages",
]

#: The fence languages a tested page may use. Anything else is a block nobody runs
#: or checks, which is what this harness exists to prevent.
ACCOUNTED_LANGUAGES = frozenset({"console", "bash", "python", "yaml", "yml", "json", "text"})

#: The ones of those that are evaluated. ``text`` is accounted for and left alone.
RUN_LANGUAGES = ACCOUNTED_LANGUAGES - {"text"}

#: Pages and folders (relative to the repository root) that are tested. Every other
#: page of ``README.md`` and ``docs/`` is in :data:`LEFT_OUT` or :data:`OWNED_ELSEWHERE`
#: with its reason, and ``test_every_page_is_tested_or_left_out_on_purpose`` makes that
#: a list, not a gap: a new page has to be run or explained.
TESTED = (
    "README.md",
    "docs/README.md",
    "docs/tutorial",
    "docs/how-to",
    "docs/explanation",
    "docs/reference/README.md",
    "docs/reference/python-api.md",
    "docs/reference/schemas.md",
)

#: Why each other page or folder is not run.
LEFT_OUT = {
    "docs/reference/cli": "generated from the argparse tree; scripts/gen_docs.py --check holds it",
    "docs/reference/errors": "generated from the error catalog; scripts/gen_docs.py --check holds it",
    "docs/adr": "decision records, written as of their date",
    "docs/spec": "the kernel, expression and seam specifications; reference text, not a walkthrough",
    "docs/RELEASING.md": "the release procedure for maintainers",
}

#: Folders that another owner keeps, with checks of its own. They are left out
#: whether or not they exist yet, so their owner can add pages without editing this
#: file, and a missing folder is not an error here.
OWNED_ELSEWHERE = {
    "docs/accuracy": "the accuracy record's pages are held by its own checks",
}

COMMAND_TIMEOUT_SECONDS = 300

_FILE_COMMENT = re.compile(r"^#\s+([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:flux|fluid)\.ya?ml)\s*$")
_EXPECTED_EXIT = re.compile(r"\s#\s*exits?\s+(\d+)\s*$")


class PageFailure(AssertionError):
    """A block on a page did not do what the page says."""


@dataclass
class Step:
    """One command of a console block, and the output the page says it prints."""

    command: str
    expected: List[str]
    expected_exit: int = 0


@dataclass
class PageReport:
    ran: int = 0
    skipped: List[Tuple[int, str]] = field(default_factory=list)


@dataclass
class _Page:
    scratch: Path
    cwd: Path
    env: Dict[str, str]
    report: PageReport = field(default_factory=PageReport)


# --------------------------------------------------------------------------- console blocks


_HEREDOC = re.compile(
    r"(?<!<)<<(?!<)-?\s*(?P<quote>['\"]?)(?P<tag>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)"
)


def split_console(source: str) -> List[Step]:
    """The commands in a console block, each with the output that follows it.

    A command may continue onto the next line with a trailing backslash, and a
    here-document belongs to the command that opens it: its lines, up to the
    terminator, are part of the command and not output.
    """
    steps: List[Step] = []
    lines = source.splitlines()
    position = 0
    while position < len(lines):
        line = lines[position]
        position += 1
        if not line.startswith("$ "):
            if steps:
                steps[-1].expected.append(line)
            elif line.strip():
                raise ValueError(f"output before any `$ ` command: {line!r}")
            continue
        command = [line[2:]]
        while command[-1].rstrip().endswith("\\"):
            if position >= len(lines):
                raise ValueError("a command ends with a backslash and nothing follows it")
            command.append(lines[position])
            position += 1
        for opened in _HEREDOC.finditer("\n".join(command)):
            terminator = opened.group("tag")
            while True:
                if position >= len(lines):
                    raise ValueError(f"a here-document <<{terminator} is never closed")
                body = lines[position]
                position += 1
                command.append(body)
                if body.strip() == terminator:
                    break
        steps.append(_step("\n".join(command)))
    return steps


def _step(command: str) -> Step:
    exit_marker = _EXPECTED_EXIT.search(command.splitlines()[-1])
    return Step(
        command=command, expected=[], expected_exit=int(exit_marker.group(1)) if exit_marker else 0
    )


def _line_matches(expected: str, actual: str) -> bool:
    """One output line against one expected line, where ``...`` stands for any text.

    The pieces between the wildcards must appear in order, the first at the start and
    the last at the end. Taking each middle piece at its first occurrence is enough, so
    this is linear in the length of the line however many wildcards it has.
    """
    pieces = expected.split("...")
    if len(pieces) == 1:
        return expected == actual
    first, last = pieces[0], pieces[-1]
    if not actual.startswith(first) or len(first) + len(last) > len(actual):
        return False
    position, limit = len(first), len(actual) - len(last)
    for piece in pieces[1:-1]:
        found = actual.find(piece, position, limit)
        if found < 0:
            return False
        position = found + len(piece)
    return actual.endswith(last)


def matches(expected: List[str], actual: List[str]) -> bool:
    """Whether *actual* is what *expected* describes.

    Lines are compared with trailing space ignored and blank lines at either end
    dropped. An expected line of ``...`` matches any number of lines; ``...``
    inside an expected line matches any text within that line.
    """
    want = _trim([line.rstrip() for line in expected])
    have = _trim([line.rstrip() for line in actual])
    # fits[i][j]: want[i:] describes have[j:]
    fits = [[False] * (len(have) + 1) for _ in range(len(want) + 1)]
    fits[len(want)][len(have)] = True
    for i in range(len(want) - 1, -1, -1):
        if want[i].strip() == "...":
            reachable = False
            for j in range(len(have), -1, -1):
                reachable = reachable or fits[i + 1][j]
                fits[i][j] = reachable
        else:
            for j in range(len(have) - 1, -1, -1):
                fits[i][j] = fits[i + 1][j + 1] and _line_matches(want[i], have[j])
    return fits[0][0]


def _trim(lines: List[str]) -> List[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


# --------------------------------------------------------------------------- running


def _environment() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLUX_", "FLUXSIM_"))}
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    env.update(
        FLUX_NO_PROGRESS="1",
        NO_COLOR="1",
        COLUMNS="100",
        # Pages show `ls` and `sort` output: one collation, on every machine.
        LC_ALL="C",
        LANG="C",
        # macOS tar adds AppleDouble `._name` entries unless told not to.
        COPYFILE_DISABLE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
    )
    for name in ("FORCE_COLOR", "PYTHONSTARTUP", "PYTHONWARNINGS", "PYTHONHOME", "PYTHONINSPECT"):
        env.pop(name, None)
    return env


def _run_shell(script: str, page: _Page) -> Tuple[int, str]:
    """Run *script* in the page's working directory; the directory it ends in is kept."""
    marker = page.scratch / ".cwd"
    # `pipefail`: without it a failing first stage of a pipeline (`false | cat`) is hidden.
    wrapped = (
        f"set -o pipefail\n{script}\n__status=$?\npwd > {shlex.quote(str(marker))}\nexit $__status"
    )
    try:
        done = subprocess.run(
            ["bash", "-c", wrapped],
            cwd=page.cwd,
            env=page.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as expired:
        raise AssertionError(f"`{script}` did not finish in {expired.timeout:.0f} s") from None
    if marker.is_file():
        page.cwd = Path(marker.read_text(encoding="utf-8").strip())
        marker.unlink()
    return done.returncode, done.stdout


def _page_of(example: Example) -> _Page:
    page = example.namespace["__page__"]
    assert isinstance(page, _Page)
    return page


def _console(example: Example) -> None:
    page = _page_of(example)
    for step in split_console(str(example.parsed)):
        status, output = _run_shell(step.command, page)
        shown = step.command.splitlines()[0]
        if status != step.expected_exit:
            raise AssertionError(
                f"`{shown}` exited {status}, the page says {step.expected_exit}.\n"
                f"--- output ---\n{output}"
            )
        if any(line.strip() for line in step.expected) and not matches(
            step.expected, output.splitlines()
        ):
            diff = "\n".join(
                difflib.unified_diff(
                    [line.rstrip() for line in step.expected],
                    [line.rstrip() for line in output.splitlines()],
                    "the page",
                    "the engine",
                    lineterm="",
                )
            )
            raise AssertionError(f"`{shown}` printed something else than the page shows:\n{diff}")
    page.report.ran += 1


def _bash(example: Example) -> None:
    page = _page_of(example)
    status, output = _run_shell("set -e\n" + str(example.parsed), page)
    if status != 0:
        raise AssertionError(f"the block exited {status}:\n{output}")
    page.report.ran += 1


class _StrictLoader(yaml.SafeLoader):
    """``yaml.safe_load`` that refuses a mapping with the same key twice.

    PyYAML keeps the last of two equal keys without a word, and a bundle document
    with a doubled key is a document the author did not mean to write.
    """


def _mapping_without_repeats(loader: _StrictLoader, node: Any, deep: bool = False) -> Any:
    loader.flatten_mapping(node)
    pairs = loader.construct_pairs(node, deep=deep)
    keys = [key for key, _value in pairs]
    repeated = [key for index, key in enumerate(keys) if key in keys[:index]]
    if repeated:
        raise yaml.constructor.ConstructorError(
            "while constructing a mapping",
            node.start_mark,
            f"found the key {repeated[0]!r} twice",
            node.start_mark,
        )
    return dict(pairs)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_repeats
)


def _not_json(constant: str) -> Any:
    raise ValueError(f"{constant} is not JSON")


def _yaml(example: Example) -> None:
    page = _page_of(example)
    source = str(example.parsed)
    first = source.splitlines()[0] if source.strip() else ""
    named = _FILE_COMMENT.match(first)
    try:
        list(yaml.load_all(source, Loader=_StrictLoader))
    except yaml.YAMLError as error:
        raise AssertionError(f"the yaml block does not parse: {error}") from None
    if named:
        (page.cwd / named.group(1)).write_text(source, encoding="utf-8", newline="\n")
    page.report.ran += 1


def _json(example: Example) -> None:
    try:
        json.loads(str(example.parsed), parse_constant=_not_json)
    except ValueError as error:
        raise AssertionError(f"the json block does not parse: {error}") from None
    _page_of(example).report.ran += 1


def _sybil() -> Sybil:
    return Sybil(
        parsers=[
            CodeBlockParser("console", evaluator=_console),
            CodeBlockParser("bash", evaluator=_bash),
            CodeBlockParser("yaml", evaluator=_yaml),
            CodeBlockParser("yml", evaluator=_yaml),
            CodeBlockParser("json", evaluator=_json),
            PythonCodeBlockParser(),
            SkipParser(),
        ]
    )


_SYBIL = _sybil()


def run_page(path: Path, scratch: Path) -> PageReport:
    """Run every block of *path* in order, in *scratch*; raise :class:`PageFailure` at the first that fails.

    A python block runs inside this process, so a relative path in it means the page's
    scratch directory (the process starts there), and an ``os.chdir`` or an
    ``os.environ`` change it makes would otherwise reach the next test in the same
    worker. The directory and the environment this process had are put back
    afterwards, pass or fail.
    """
    document = _SYBIL.parse(path)
    page = _Page(scratch=scratch, cwd=scratch, env=_environment())
    document.namespace["__page__"] = page
    document.namespace["workdir"] = scratch
    started_in = os.getcwd()
    saved_environment = dict(os.environ)
    os.chdir(scratch)
    try:
        for example in document:
            try:
                example.evaluate()
            except unittest.SkipTest as skipped:
                page.report.skipped.append((example.line, str(skipped)))
            except PageFailure:
                raise
            except Exception as error:  # noqa: BLE001 - every failure is reported with its place
                raise PageFailure(f"{path}:{example.line}\n{error}") from None
            else:
                if isinstance(example.region.evaluator, PythonEvaluator):
                    page.report.ran += 1
    finally:
        os.chdir(started_in)
        os.environ.clear()
        os.environ.update(saved_environment)
    return page.report


# --------------------------------------------------------------------------- which pages


_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdx"})


def _markdown_under(base: Path) -> List[Path]:
    """Every Markdown file below *base*, in a stable order, through symlinks, in any case.

    ``rglob("*.md")`` misses ``NOTES.MD``, ``page.markdown``, ``page.mdx`` and a folder
    reached by a link, and a page the scan cannot see is a page nobody tests.
    """
    found: List[Path] = []
    for directory, subdirectories, names in os.walk(base, followlinks=True):
        subdirectories.sort()
        found += [
            Path(directory) / name
            for name in sorted(names)
            if Path(name).suffix.lower() in _MARKDOWN_SUFFIXES
        ]
    return found


def tested_pages(root: Path) -> List[Path]:
    """The Markdown pages that are run, in a stable order."""
    pages: List[Path] = []
    for entry in TESTED:
        path = root / entry
        if path.is_dir():
            pages += _markdown_under(path)
        elif path.is_file():
            pages.append(path)
    return pages


# Fences and skips are read with Sybil's own lexers, not a second set of patterns, so what
# is counted here is exactly what Sybil will find and act on.

_EVERY_FENCE = RawFencedCodeBlockLexer(info_pattern=re.compile(r"(?P<info>[^\n]*)(?:\n|\Z)"))


@dataclass(frozen=True)
class _Fence:
    start: int
    end: int
    info: str


def _fences(document: Document) -> List[_Fence]:
    found = [
        _Fence(region.start, region.end, str(region.lexemes["info"]))
        for region in _EVERY_FENCE(document)
    ]
    return sorted(found, key=lambda fence: fence.start)


def fence_languages(text: str) -> List[str]:
    """The info string of every fenced block Sybil sees in *text*, in order.

    For a plain block that is its language (an empty string for a bare fence). A fence
    with anything else on the line, even a trailing space or a title, comes back whole,
    which is how a caller notices it: Sybil runs a block only when the language is the
    whole line, so ```` ```console title="x" ```` would otherwise look like a console
    block and never run. Fences count wherever they stand: indented in a list, inside
    a block quote, and fenced with tildes as well as backticks.
    """
    return [fence.info for fence in _fences(Document(text, "<page>"))]


@dataclass(frozen=True)
class Skip:
    """One ``<!-- skip: ... -->`` directive, as Sybil reads it."""

    line: int
    action: str
    arguments: str
    #: The info string of the fence that follows it with nothing between, or ``None``.
    before: Optional[str]

    @property
    def reason(self) -> Optional[str]:
        """The quoted reason of a ``skip: next "reason"``, or ``None`` for any other form."""
        quoted = re.fullmatch(r'"([^"]*)"', self.arguments.strip())
        return quoted.group(1) if self.action == "next" and quoted else None


def skips(text: str) -> List[Skip]:
    """Every skip directive in *text* that Sybil will act on."""
    document = Document(text, "<page>")
    fences = _fences(document)
    found = []
    for region in SkipParser()(document):
        action, arguments = region.parsed
        following = next((fence for fence in fences if fence.start >= region.end), None)
        # Sybil's region stops before the closing `-->`, so allow that between them.
        adjacent = following is not None and bool(
            re.fullmatch(r"\s*(?:--+>)?\s*", text[region.end : following.start])
        )
        found.append(
            Skip(
                line=text.count("\n", 0, region.start) + 1,
                action=action,
                arguments=arguments or "",
                before=following.info if adjacent and following is not None else None,
            )
        )
    return found


def all_markdown(root: Path) -> Iterable[Path]:
    """The Markdown this harness answers for: ``README.md`` and everything under ``docs/``.

    The other files at the repository root (the changelogs, the licence list, the
    community files) have owners and checks of their own and are not pages a reader
    follows step by step, so a new one there is not this harness's business.
    """
    readme = root / "README.md"
    if readme.is_file():
        yield readme
    yield from _markdown_under(root / "docs")
