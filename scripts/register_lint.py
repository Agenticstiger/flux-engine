#!/usr/bin/env python3

# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Register-discipline lint: bans a fixed vocabulary from public surfaces.

Also fails when a flag, positional or subcommand carries no help text --
the same argparse tree is the payload of `fluxsim describe --self --json`,
so an unhelped flag is an unlabeled control in every consumer's UI.

Scans every Markdown file at the repository root (README.md,
GOLDEN-CHANGELOG.md, THIRD_PARTY_LICENSES.md -- the public register is
every document a reader can open at the top of the repository, not one
special-cased filename), everything under docs/, every `fluxsim --help`
string (the top-level parser and every subcommand, walked recursively),
and every docstring and every ``register(...)``-call explanation string
under ``flux_engine/`` (module/class/function docstrings via
``ast.get_docstring``, plus the ``title``/``explanation`` arguments the
error catalog's own ``register()`` takes; see
:mod:`flux_engine.core.errors`) for a fixed set of banned words --
case-insensitive substring match. Public-facing prose holds these words
back until the maintainers lift the restriction, and uses "sign-off",
"credibility", and "fidelity check" instead.

The word list, the matcher and the dotted-module-path allowlist all come
from :mod:`flux_engine._vocabulary`, which is the single definition this
gate shares with the runtime refusal in
:mod:`flux_engine.evidence.report` that guards ``EVIDENCE.md`` before it
is written. A dotted reference to the ``flux_engine.concordance`` package
(an import, a ``:mod:``/``:func:`` role, or a bare module path in prose)
is not a prose use of the banned word -- it is the package's own name,
which this script cannot rename -- so those spans are masked out before
matching.

Exit 0 = clean. Exit 1 = at least one banned word found (reported with
file/subcommand + line). Exit 2 = a collection error (e.g. flux_engine is
not importable).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import List, Tuple, Union

try:
    from flux_engine._vocabulary import BANNED_PATTERN as _VOCABULARY_PATTERN
    from flux_engine._vocabulary import BANNED_WORDS as _VOCABULARY_BANNED_WORDS
    from flux_engine._vocabulary import mask_module_references as _mask_module_references
except ImportError as exc:  # pragma: no cover - environment misconfiguration
    # Same contract as main()'s own ImportError branch: exit 2 is "this
    # gate could not run", never "this gate passed".
    print(f"register_lint: could not import flux_engine: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The word list and the matcher over it live in
#: :mod:`flux_engine._vocabulary` -- one definition, shared with the
#: runtime refusal in :mod:`flux_engine.evidence.report` that guards
#: ``EVIDENCE.md``. They used to be separate literal tuples in this
#: script, in that module and in three test modules, with nothing
#: asserting they were equal, so a word added to this gate left the
#: shipped, customer-facing surface blind to it.
BANNED_WORDS = list(_VOCABULARY_BANNED_WORDS)

_PATTERN = _VOCABULARY_PATTERN

#: Calls whose string-literal arguments are a public surface this repo
#: ships (a wire-format explanation, not internal commentary): the error
#: catalog's own ``register(code, title, explanation)`` (core/errors.py).
_SCANNED_CALL_NAMES = {"register"}


def _scan_text(label: str, text: str) -> List[str]:
    hits: List[str] = []
    masked = _mask_module_references(text)
    for lineno, (raw_line, masked_line) in enumerate(
        zip(text.splitlines(), masked.splitlines(), strict=True), start=1
    ):
        match = _PATTERN.search(masked_line)
        if match:
            hits.append(f"{label}:{lineno}: {match.group(0)!r} in: {raw_line.strip()}")
    return hits


def _scan_docs() -> List[str]:
    hits: List[str] = []
    # Every Markdown file at the repository root, not just README.md.
    # GOLDEN-CHANGELOG.md carried a banned word past this gate for exactly
    # as long as the gate special-cased one filename: the public register
    # is every document a reader can open at the top of the repository.
    for path in sorted(REPO_ROOT.glob("*.md")):
        if path.is_file():
            hits.extend(_scan_text(str(path.relative_to(REPO_ROOT)), path.read_text()))
    docs_dir = REPO_ROOT / "docs"
    if docs_dir.is_dir():
        # docs/spec/ holds the normative specifications. They name engine
        # modules by design, among them one whose name is a reserved word, so
        # the lint leaves them out. Every other docs/ file is a public-facing
        # surface.
        exempt_dirs = (docs_dir / "spec",)
        for path in sorted(docs_dir.rglob("*")):
            if any(exempt in path.parents for exempt in exempt_dirs):
                continue
            # HTML is scanned RAW, tags and all, rather than stripped to
            # its text first. The gate's job is to stop a reserved word
            # reaching a reader, and in HTML a word reaches one from prose,
            # from a `title` or `alt` attribute, and from a `<meta>`
            # description alike; stripping tags would hide exactly those.
            # Scanning raw also keeps every offset intact, so a hit still
            # reports the line a person can go and edit. The cost, recorded
            # rather than hidden: a word split across tags (`pro<b>ves</b>`)
            # is not matched. HTML is here at all because docs/ is the surface
            # most likely to be published and HTML is the most publishable
            # thing in it, so excluding it left the gate scanning the formats
            # least likely to reach a reader.
            if path.is_file() and path.suffix in {".md", ".rst", ".txt", ".html"}:
                hits.extend(_scan_text(str(path.relative_to(REPO_ROOT)), path.read_text()))
    return hits


def _iter_help_texts(parser: argparse.ArgumentParser, prefix: str) -> List[Tuple[str, str]]:
    """Recursively collect ``format_help()`` for *parser* and every subparser.

    Uses the same argparse-introspection idiom fluid_build.describe uses
    (``_SubParsersAction.choices``) -- read-only, no side effects.
    """
    texts = [(prefix, parser.format_help())]
    for action in parser._actions:  # noqa: SLF001 - standard argparse introspection idiom
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, subparser in action.choices.items():
                texts.extend(_iter_help_texts(subparser, f"{prefix} {name}"))
    return texts


def _scan_help_completeness() -> List[str]:
    """Every flag, positional and subparser must carry its own help text.

    The same argparse tree this script already walks is the payload of
    ``fluxsim describe --self --json``, whose stated purpose is to let a
    consumer render command/flag UI without lagging the CLI -- so a flag
    with ``help=""`` is an unlabeled control in that UI, not merely a
    terse ``--help``. This check is here rather than in the test suite
    because this script is the one place that already walks every
    subparser, and because a gap that has come back once should be caught
    by the gate that runs on every push.
    """
    from flux_engine.cli import _build_parser

    problems: List[str] = []

    def walk(parser: argparse.ArgumentParser, label: str) -> None:
        if not (parser.description or "").strip():
            problems.append(f"`{label}`: subcommand has no description")
        if not getattr(parser, "examples", ()):
            problems.append(
                f"`{label}`: subcommand has no worked example "
                "(add it to _EXAMPLES in flux_engine/cli/__init__.py)"
            )
        for action in parser._actions:  # noqa: SLF001 - argparse introspection idiom
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for name, subparser in action.choices.items():
                    walk(subparser, f"{label} {name}")
            elif isinstance(action, argparse._HelpAction):  # noqa: SLF001
                continue
            elif not (action.help or "").strip():
                names = "/".join(action.option_strings) or action.dest
                problems.append(f"`{label}`: {names} has no help string")

    walk(_build_parser(), "fluxsim")
    return problems


def _scan_help() -> List[str]:
    from flux_engine.cli import _build_parser

    hits: List[str] = []
    for label, text in _iter_help_texts(_build_parser(), "fluxsim"):
        hits.extend(_scan_text(f"`{label} --help`", text))
    return hits


#: Exactly the node types :func:`ast.get_docstring` accepts. Named so the
#: annotation below is as narrow as the docstring already promises: the wider
#: ``ast.AST`` type-checks while handing ``get_docstring`` a node it rejects.
_DocstringNode = Union[ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef]


def _docstring_nodes(tree: ast.Module) -> List[_DocstringNode]:
    """Every module/class/function node in *tree* that can carry a
    docstring, per :func:`ast.get_docstring`'s own accepted types.
    """
    nodes: List[_DocstringNode] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.append(node)
    return nodes


def _docstring_lineno(node: ast.AST) -> int:
    """The source line the docstring *starts* on, for a node whose body's
    first statement is the ``Expr(Constant(str))`` ``ast.get_docstring``
    reads -- ``node.lineno`` itself would point at ``def``/``class``, one
    or more lines above a multi-line docstring's own first line.
    """
    body = getattr(node, "body", None)
    if body:
        return int(body[0].lineno)
    return int(getattr(node, "lineno", 1))


def _register_call_strings(tree: ast.Module) -> List[Tuple[int, str]]:
    """Every string-literal argument to a ``register(...)`` call in *tree*.

    Matches by call name only (``register(...)`` or ``x.register(...)``)
    rather than binding to the real :func:`flux_engine.core.errors.register`
    import -- this script does not execute the module under scan, only
    parses it, so there is no live binding to check against; the name is
    distinctive enough in this codebase (grepped: the only ``register``
    calls under ``flux_engine/`` are the error catalog's).
    """
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _SCANNED_CALL_NAMES:
            continue
        for arg in (*node.args, *(kw.value for kw in node.keywords)):
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((int(arg.lineno), arg.value))
    return found


def _scan_source() -> List[str]:
    """Docstrings and ``register()`` explanation strings under
    ``flux_engine/`` -- two of the three surfaces the lint used to miss
    (docstrings, error-catalog explanation strings) that this script can
    reach without executing untrusted code; the third, log lines, has no
    instances to scan (grepped: no ``logging``/``logger`` call sites exist
    under ``flux_engine/`` today -- this function starts covering them the
    day one is added, since it walks every ``Call`` node's string
    literals the same way for ``register()``, just scoped to that one
    call name so an ordinary f-string in application code is not
    flagged as a false positive).
    """
    hits: List[str] = []
    engine_root = REPO_ROOT / "flux_engine"
    for path in sorted(engine_root.rglob("*.py")):
        if "_vendor" in path.parts or "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - defensive, no such file today
            hits.append(f"{path.relative_to(REPO_ROOT)}: could not parse ({exc})")
            continue
        label = str(path.relative_to(REPO_ROOT))
        strings: List[Tuple[int, str]] = []
        for node in _docstring_nodes(tree):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                strings.append((_docstring_lineno(node), doc))
        strings.extend(_register_call_strings(tree))
        for base_lineno, text in strings:
            masked = _mask_module_references(text)
            for offset, (raw_line, masked_line) in enumerate(
                zip(text.splitlines(), masked.splitlines(), strict=True)
            ):
                match = _PATTERN.search(masked_line)
                if match:
                    hits.append(
                        f"{label}:{base_lineno + offset}: {match.group(0)!r} in: {raw_line.strip()}"
                    )
    return hits


def main() -> int:
    try:
        hits = _scan_docs() + _scan_help() + _scan_source()
        help_gaps = _scan_help_completeness()
    except ImportError as exc:  # pragma: no cover - environment misconfiguration
        print(f"register_lint: could not import flux_engine: {exc}", file=sys.stderr)
        return 2

    if help_gaps:
        print("CLI help lint FAILED -- undocumented parser surface:", file=sys.stderr)
        for gap in help_gaps:
            print(f"  {gap}", file=sys.stderr)

    if hits:
        print("Register-discipline lint FAILED -- banned word(s) found:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        return 1

    if help_gaps:
        return 1

    print(
        "Register-discipline lint passed: no banned vocabulary in repository-root "
        "Markdown, docs/, --help, "
        "or flux_engine/ source (docstrings + register() explanations); "
        "every flag, positional and subcommand carries help."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
