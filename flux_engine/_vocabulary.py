# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The one reserved-vocabulary list, and the one matcher over it.

A fixed set of words is held back from every public-facing surface this
project ships -- ``README.md``, repository-root Markdown, ``docs/``
outside the internal specification directories, every ``fluxsim --help``
string, the docstrings and error-catalog explanations under
``flux_engine/``, and the generated ``EVIDENCE.md``. Public prose uses
"sign-off", "credibility" and "fidelity check" instead.

This module is the single definition. It used to be five hand-copied
tuples -- ``scripts/register_lint.py``, ``flux_engine.evidence.report``
and three test modules -- with nothing asserting they were equal, so
adding a word in one place left four surfaces unenforced. Every consumer
now imports from here: the CI gate script, the runtime refusal that
guards ``EVIDENCE.md`` before it is written, and the tests over both.

A reference to the ``flux_engine.concordance`` package (an import, a
Sphinx ``:mod:``/``:func:`` role, a slash-separated path, or a package
table's monospaced cell) is the package's own name, not a prose use of
the banned word, so :func:`mask_module_references` blanks those spans
before matching. Only that package: the mask is anchored to names that
exist, because a rule keyed on the SHAPE of a reference exempts a path
that names nothing real just as readily.
Kept here rather than in either consumer for the same reason the word
list is: one allowlist, or the two gates disagree about what a violation
is.
"""

from __future__ import annotations

import re
from typing import Tuple

__all__ = [
    "BANNED_PATTERN",
    "BANNED_WORDS",
    "find_banned_words",
    "mask_module_references",
]

#: The reserved words, case-insensitive substring match. Public prose holds
#: them back until the maintainers lift the restriction.
BANNED_WORDS: Tuple[str, ...] = (
    "concordance",
    "zero-knowledge",
    "tolerance predicate",
    "clear-statistic",
    "practise",
    "mathematically",
    "proves",
    "legally admissible",
    "certified",
    "FLUX Simulator",
)

#: The compiled matcher every gate uses. Built from :data:`BANNED_WORDS`
#: so a gate cannot compile a pattern from a stale copy of the list.
BANNED_PATTERN = re.compile("|".join(re.escape(word) for word in BANNED_WORDS), re.IGNORECASE)

#: The packages of this engine whose own names are reserved words. There is
#: exactly one today, and anchoring on it is the whole design. An earlier
#: version of this mask matched any ``flux_engine.*`` dotted reference and any
#: ``flux_engine/*`` or ``tests/*`` path, on the reasoning that those shapes
#: are module references. An adversarial pass showed what that let through:
#: ``tests/certified_output.log`` and ``flux_engine.report.legally_certified``
#: are that shape, name nothing real, and carried a reserved word straight past
#: the gate. A reference is exempt because it names a package that exists, not
#: because it is punctuated like one. Add a name here only when a real package
#: takes it; an unlisted collision then fails closed, which is the right way
#: round.
_RESERVED_WORD_PACKAGES = ("concordance",)

_PACKAGE = "|".join(re.escape(name) for name in _RESERVED_WORD_PACKAGES)

#: One of those packages named as a module path, dotted or slashed.
_MODULE_REFERENCE = re.compile(
    rf"flux_engine\.(?:{_PACKAGE})(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    rf"|(?:flux_engine|tests)/(?:{_PACKAGE})(?:/[A-Za-z_][A-Za-z0-9_.-]*)*"
)

#: One of those packages named alone as the whole content of a markup element,
#: which is how an architecture table writes a package:
#: ``<td class="mono">concordance</td>``, in a table whose every other row is
#: ``core``, ``api``, ``cli`` and so on.
#:
#: Scoped to the package names above, NOT to any single identifier-shaped
#: token. The unscoped version of this rule was tried and is a hole: it exempts
#: ``<span>certified</span>``, which is a reserved word delivered whole to a
#: reader, and it made a markup element the easiest place in the repository to
#: carry one past the gate. The tag names are matched case-insensitively
#: because HTML tag names are; the package name is not, because a capitalised
#: word at the start of a cell is prose.
_PACKAGE_NAME_ELEMENT = re.compile(
    rf"(<(?i:code|td|th|span)\b[^>]*>)((?:{_PACKAGE}))(</(?i:code|td|th|span)>)"
)


def mask_module_references(text: str) -> str:
    """Blank out references to this engine's own packages in *text*.

    Replaces each span with the same number of ``#`` characters, so line and
    column offsets into the masked text still address the original.

    A document names a package three ways and all three are the name rather
    than a use of the word: dotted (``flux_engine.concordance.record``), as a
    path (``flux_engine/concordance/record.py``, ``tests/concordance``), and
    alone as the content of a markup element
    (``<td class="mono">concordance</td>``).

    Every shape is anchored to a package that exists. Prose is not exempt
    for being punctuated like a path, and it is not exempt for sitting inside
    a markup element. A path naming no real package, a reserved word standing
    alone in a ``<span>``, and a whole sentence inside ``<code>`` are all left
    for the matcher to find, and each has a test.
    """

    def blank(match: "re.Match[str]") -> str:
        return "#" * len(match.group(0))

    def blank_content(match: "re.Match[str]") -> str:
        return match.group(1) + "#" * len(match.group(2)) + match.group(3)

    return _PACKAGE_NAME_ELEMENT.sub(blank_content, _MODULE_REFERENCE.sub(blank, text))


def find_banned_words(text: str, *, mask_module_paths: bool = False) -> Tuple[str, ...]:
    """Return every reserved word found in *text*: lower-cased, sorted, deduplicated.

    *mask_module_paths* is off by default: generated customer-facing prose
    (``EVIDENCE.md``) has no business naming an internal module either
    way, so the runtime refusal stays strict. The static gate over source
    and docs turns it on, where a dotted package path is an ordinary
    cross-reference.
    """
    if mask_module_paths:
        text = mask_module_references(text)
    return tuple(sorted({match.group(0).lower() for match in BANNED_PATTERN.finditer(text)}))
