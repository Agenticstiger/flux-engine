# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The parser every ``fluxsim`` command is built on.

:class:`_FluxArgumentParser` renders a usage error as a JSON envelope under ``--json``
and leads its help with worked :class:`Example` invocations and a reference page.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple

from flux_engine.cli._output import _failure_pointer, _print_result


def _stage_name_for_prog(prog: str) -> str:
    """``"fluxsim expr eval"`` -> ``"expr-eval"``; the bare program -> ``"fluxsim"``.

    The discriminator a usage error reports has to be the same one the
    command reports when it succeeds, and argparse's ``prog`` is the only
    thing a subparser knows about itself at ``error()`` time.
    """
    parts = prog.split()[1:]
    return "-".join(parts) if parts else "fluxsim"


@dataclass(frozen=True)
class Example:
    """One worked invocation, shown under ``examples:`` in ``--help``.

    *command* is the text to type. A line that ends in a backslash continues on
    the next, so a long example stays copy-and-paste; separate lines are
    separate commands. *note* is one line of comment shown above it.
    """

    command: str
    note: str = ""

    def one_line(self) -> str:
        """The first command, continuations joined, as one line."""
        first: List[str] = []
        for line in self.command.split("\n"):
            first.append(line.rstrip("\\").strip())
            if not line.rstrip().endswith("\\"):
                break
        return " ".join(part for part in first if part)


class _HelpFormatter(argparse.HelpFormatter):
    """argparse's formatter, minus the ``<command>`` header over the list of commands.

    The commands are already under a ``commands:`` heading; the metavar line above
    them said the same thing a second time.
    """

    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return "".join(
                self._format_action(sub) for sub in action._get_subactions()  # noqa: SLF001
            )
        return super()._format_action(action)


def _render_examples(examples: Sequence[Example]) -> str:
    """The ``examples:`` block, verbatim: argparse's own text wrapping would break a command."""
    lines = ["examples:"]
    for position, example in enumerate(examples):
        if position:
            lines.append("")
        if example.note:
            lines.append(f"  # {example.note}")
        continued = False
        for line in example.command.split("\n"):
            lines.append("      " + line.strip() if continued else "  " + line.rstrip())
            continued = line.rstrip().endswith("\\")
    return "\n".join(lines)


#: How argparse words an argument's refusal that carries a catalogued code: ``argument --scale: FLX-3405: ...``.
_CODED_ARGUMENT = re.compile(r"argument [^:]+: (FLX-[0-9]{4}): ")


class _FluxArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors honour ``--json`` too, and whose help leads with examples.

    argparse's own ``error()`` prints a usage block to stderr and exits 2
    with **zero bytes on stdout** -- so a wrapper that always passes
    ``--json`` and always parses stdout crashed on every flag typo, which
    is exactly the case where a structured reason is most useful. The
    envelope this emits is the same shape every other command prints, with
    ``exitCode: 2`` and the stage the failing subparser belongs to.

    ``json_requested`` is set once, in :func:`main`, from the raw argv --
    the parse has by definition not happened yet when ``error()`` runs, so
    the namespace cannot answer the question.

    **Help follows clig.dev.** Examples come first, straight after the
    description, because they are what people read; they are printed verbatim,
    since wrapping a command at the terminal width breaks it. The reference page
    for the command follows the options, and the top level adds where to report
    a problem. A usage error says what to type instead: a near miss on a command
    name names the command, anything else shows the first example.
    """

    #: Set by :func:`main` before parsing; subparsers inherit the class,
    #: so setting it on the class reaches every parser in the tree.
    json_requested: bool = False

    #: Worked invocations, shown first in ``--help``. ``scripts/register_lint.py``
    #: fails a command that has none.
    examples: Tuple[Example, ...] = ()
    #: The reference page for this command, printed at the foot of ``--help``.
    docs_url: Optional[str] = None
    #: Further lines at the very end of ``--help``.
    footer: Tuple[str, ...] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _HelpFormatter)
        super().__init__(*args, **kwargs)
        self._positionals.title = "arguments"

    def format_help(self) -> str:
        head = self._get_formatter()
        head.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        head.add_text(self.description)
        sections = [head.format_help().rstrip("\n")]
        if self.examples:
            sections.append(_render_examples(self.examples))
        body = self._get_formatter()
        for group in self._action_groups:
            body.start_section(group.title)
            body.add_text(group.description)
            body.add_arguments(group._group_actions)  # noqa: SLF001 - argparse idiom
            body.end_section()
        sections.append(body.format_help().rstrip("\n"))
        foot = ([f"docs: {self.docs_url}"] if self.docs_url else []) + list(self.footer)
        if foot:
            sections.append("\n".join(foot))
        return "\n\n".join(section for section in sections if section) + "\n"

    def parse_args(  # type: ignore[override]
        self,
        args: Optional[Sequence[str]] = None,
        namespace: Optional[argparse.Namespace] = None,
    ) -> argparse.Namespace:
        """``parse_args``, reporting leftover arguments from the parser they were typed to.

        argparse hands what no parser claimed back to the ROOT, which then
        complained in its own voice: the root usage line, and (once examples
        exist) the root's example, for a flag typo on ``fluxsim validate``. The
        command that was reached is the one whose usage and example help.
        """
        parsed, extras = self.parse_known_args(args, namespace)
        if extras:
            leaf = self._leaf_parser(parsed)
            message = f"unrecognized arguments: {' '.join(extras)}"
            homes = self._sibling_homes(leaf, extras)
            if homes:
                message += f" ({'; '.join(homes)})"
            leaf.error(message)
        return parsed

    def _sibling_homes(self, leaf: "_FluxArgumentParser", extras: Sequence[str]) -> List[str]:
        """Where a flag the command does not take belongs, when a sibling command takes it.

        ``fluxsim plan --shard 1/2`` is a typo of nothing: the flag exists, on ``apply``. Each
        flag of *extras* that a command beside *leaf* takes is named with that command.
        """
        siblings = _children(self)
        if siblings.get(leaf.prog.split()[-1]) is not leaf:
            return []
        found: List[str] = []
        for token in extras:
            if not token.startswith("--"):
                continue
            flag = token.split("=", 1)[0]
            owners = [
                f"`{child.prog}`"
                for child in siblings.values()
                if child is not leaf and flag in child._option_string_actions  # noqa: SLF001
            ]
            if owners:
                found.append(f"{flag} belongs to {' and '.join(dict.fromkeys(owners))}")
        return found

    def _leaf_parser(self, parsed: argparse.Namespace) -> "_FluxArgumentParser":
        """The deepest parser the command line reached."""
        node: _FluxArgumentParser = self
        while True:
            chosen: Optional[argparse.ArgumentParser] = None
            for action in node._actions:  # noqa: SLF001 - argparse introspection idiom
                if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                    chosen = action.choices.get(str(getattr(parsed, action.dest, None)))
                    break
            if not isinstance(chosen, _FluxArgumentParser):
                return node
            node = chosen

    def _suggestion(self, message: str) -> str:
        """What to type instead of what was typed, as lines to append to the error."""
        near = re.search(r"invalid choice: '([^']*)'", message)
        if near and "maybe you meant" not in message:  # Python 3.14 suggests by itself
            names: List[str] = []
            for action in self._actions:
                if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                    names.extend(action.choices)
            close = difflib.get_close_matches(near.group(1), names, n=1)
            if close:
                return f"did you mean `{self.prog} {close[0]}`?\n"
        if self.examples:
            return f"for example: {self.examples[0].one_line()}\n"
        return ""

    def error(self, message: str) -> "NoReturn":
        from flux_engine.api.stages import StageResult

        # An argument's own check (--scale, --shard) refuses with a catalogued code, which
        # argparse folds into the text of its error as "argument --scale: FLX-3405: ...".
        coded = _CODED_ARGUMENT.match(message)
        result = StageResult(
            stage=_stage_name_for_prog(self.prog),
            exit_code=2,
            stderr=f"{self.prog}: {message}",
            error_code=coded.group(1) if coded else None,
        )
        if type(self).json_requested:
            _print_result(result, as_json=True)
            raise SystemExit(2)
        pointer = _failure_pointer(result)
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: {message}\n{self._suggestion(message)}"
            + (f"{pointer}\n" if pointer else ""),
        )


def _children(parser: argparse.ArgumentParser) -> Dict[str, argparse.ArgumentParser]:
    """The subparsers of *parser*, by name; empty when it has none."""
    found: Dict[str, argparse.ArgumentParser] = {}
    for action in parser._actions:  # noqa: SLF001 - argparse introspection idiom
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            found.update(action.choices)
    return found
