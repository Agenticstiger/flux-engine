# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Record the ``fluxsim`` command surface that ``tests/cli/test_help_snapshots.py`` holds.

Two kinds of snapshot, under ``tests/cli/help-snapshots/``:

* ``structure.json``: every parser in the tree and every argument it takes -- the
  flags, the destination, the default, the type, the choices, the help string, the
  worked examples, the reference page and the stage each command reports. None of it
  depends on the interpreter, so one file serves every Python.
* ``py3.N/<command>.txt``: the help text each command prints, rendered at 80
  columns with colour off. argparse lays help out differently from one minor version
  of Python to the next (``gen_docs.py`` builds the CLI pages from the structure for
  the same reason), so there is one set per minor version the CI matrix runs.

After a deliberate change to the surface, ``make help-snapshots`` runs it under each
of those versions, in throwaway environments (``uv run --isolated --frozen --python 3.N``)
that leave the development virtualenv alone. Each run rewrites ``structure.json``
and its own version's directory. The pattern is
pytest-snapshot's and syrupy's (a committed file the test compares against and a
command that rewrites it), kept as plain files so the test needs no plugin.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

from flux_engine.cli import _children

SNAPSHOTS = Path(__file__).resolve().parent / "help-snapshots"

#: The environment help is rendered in: a fixed width, and no colour (Python 3.14
#: colours help on a terminal that allows it).
RENDER_ENVIRONMENT = {"COLUMNS": "80", "NO_COLOR": "1", "PYTHON_COLORS": "0"}


def minor_version_directory() -> Path:
    """The directory of rendered help for the running interpreter."""
    return SNAPSHOTS / f"py{sys.version_info.major}.{sys.version_info.minor}"


@contextlib.contextmanager
def rendering_environment() -> Iterator[None]:
    """Set :data:`RENDER_ENVIRONMENT`, and drop what would turn colour back on."""
    saved = {name: os.environ.get(name) for name in (*RENDER_ENVIRONMENT, "FORCE_COLOR")}
    os.environ.update(RENDER_ENVIRONMENT)
    os.environ.pop("FORCE_COLOR", None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _parsers(
    parser: argparse.ArgumentParser, path: str = "fluxsim"
) -> Iterator[Tuple[str, argparse.ArgumentParser]]:
    """Every parser in the tree with its command path, in the order ``--help`` lists them."""
    yield path, parser
    for name, child in _children(parser).items():
        yield from _parsers(child, f"{path} {name}")


def _name(value: Any) -> Any:
    """A type or callable by name, so the snapshot does not depend on where it is defined."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return getattr(value, "__qualname__", None) or repr(value)


def _action(action: argparse.Action) -> Dict[str, Any]:
    described: Dict[str, Any] = {
        "class": type(action).__name__,
        "option_strings": list(action.option_strings),
        "dest": action.dest,
        "nargs": action.nargs,
        "const": repr(action.const),
        "default": repr(action.default),
        "type": _name(action.type),
        "required": action.required,
        "help": action.help,
        "metavar": list(action.metavar) if isinstance(action.metavar, tuple) else action.metavar,
    }
    if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
        described["choices"] = list(action.choices)
        described["commands"] = [
            {"name": choice.dest, "help": choice.help}
            for choice in action._get_subactions()  # noqa: SLF001
        ]
    else:
        described["choices"] = None if action.choices is None else [str(c) for c in action.choices]
    return described


def structure(root: argparse.ArgumentParser) -> Dict[str, Any]:
    """Every parser and every argument, as plain data."""
    tree: Dict[str, Any] = {}
    for path, parser in _parsers(root):
        tree[path] = {
            "class": type(parser).__name__,
            "prog": parser.prog,
            "usage": parser.usage,
            "description": parser.description,
            "epilog": parser.epilog,
            "formatter_class": _name(parser.formatter_class),
            "defaults": {
                key: repr(value) for key, value in sorted(parser._defaults.items())
            },  # noqa: SLF001
            "examples": [
                {"command": example.command, "note": example.note}
                for example in getattr(parser, "examples", ())
            ],
            "docs_url": getattr(parser, "docs_url", None),
            "footer": list(getattr(parser, "footer", ())),
            "actions": [_action(action) for action in parser._actions],  # noqa: SLF001
        }
    return tree


def help_texts(root: argparse.ArgumentParser) -> Dict[str, str]:
    """Each command's rendered help, keyed by the file name it is recorded under."""
    with rendering_environment():
        return {path.replace(" ", "_"): parser.format_help() for path, parser in _parsers(root)}


def _write(directory: Path, texts: Dict[str, str]) -> List[str]:
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.txt"):
        if stale.stem not in texts:
            stale.unlink()
    for name, text in texts.items():
        (directory / f"{name}.txt").write_text(text, encoding="utf-8", newline="\n")
    return sorted(texts)


def main() -> int:
    from flux_engine.cli import build_parser

    root = build_parser()
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    (SNAPSHOTS / "structure.json").write_text(
        json.dumps(structure(root), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    written = _write(minor_version_directory(), help_texts(build_parser()))
    print(
        f"wrote structure.json and {len(written)} help texts to "
        f"{minor_version_directory().relative_to(SNAPSHOTS.parent)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
