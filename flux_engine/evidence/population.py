# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``EVIDENCE.md``'s "Population" section: the run's histograms drawn against the profile's.

Each histogram property the fidelity check scored (a ``marginalHistogram/1`` or a
``levelHistogram/1``) gets one Mermaid XY chart, the profile's shares as bars and the run's as a
line, and the same numbers in a table under it (ADR 0031). The cells are the ones the score was
computed over, so a cell the profile withheld is not drawn. Everything that comes from the data
(a title, a label) is written through :func:`safe_text`, so no value can close the code block or
the ``<details>`` element, start a Mermaid directive or comment, or carry markup.

The numbers reach this module from the pass that scores the properties; the scorecard document
is not touched by any of it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, localcontext
from typing import List, Optional, Sequence, Tuple

from flux_engine._vocabulary import find_banned_words
from flux_engine.core.canonical import nfc, sha256_hex

__all__ = [
    "MAX_CELLS",
    "MAX_CHARTS",
    "PopulationCell",
    "PopulationGroup",
    "PopulationProperty",
    "render_population_section",
    "safe_text",
    "share_percent",
]

#: The most charts one report draws, in property order.
MAX_CHARTS = 32

#: The most cells one chart holds; a property with more is listed with its score and no chart.
MAX_CELLS = 40

#: The longest a title or a label can be.
_MAX_TEXT = 40

_ASCII_KEPT = re.compile(r"[A-Za-z0-9 _.,:;/()+=\-]")

#: Unicode general categories whose characters are kept: letters and numbers of any script.
_KEPT_CATEGORIES = ("L", "N")

#: Combining marks (``Mn``, ``Mc``, ``Me``) are kept only after a kept letter, number or mark, since
#: many scripts write a syllable with them and nothing else puts one at the start of a label.
_MARK_CATEGORY = "M"

#: The Default_Ignorable_Code_Point property as a list of inclusive ranges, from Unicode 16.0.0
#: ``DerivedCoreProperties.txt`` (the section "Derived Property: Default_Ignorable_Code_Point").
#: The standard library has no such property. A character in it can render as nothing, so a label
#: of such characters would print blank and look like another label. A later Unicode version adds
#: only unassigned code points, which are replaced already.
_DEFAULT_IGNORABLE: Tuple[Tuple[int, int], ...] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

#: The tail a label gets when the text it prints is not the text it was given: ``_`` and eight
#: lowercase hexadecimal digits of the SHA-256 of the given text in NFC and UTF-8.
_TAIL = re.compile(r"_[0-9a-f]{8}$")
_TAIL_LENGTH = 9

_NO_PROFILE = "There is no target to draw: this run was shipped without a profile."

_NOTHING_SCORED = (
    "No histogram property was scored against the profile, so there is nothing to draw."
)

_INTRO = (
    "Each chart draws the population of the profile the run was scored against as bars and this "
    "run's as a line, over the cells the fidelity score was computed over. A cell the profile withheld is left out."
)


@dataclass(frozen=True)
class PopulationCell:
    """One cell: its label and the records each side put in it."""

    label: str
    profile: int
    run: int


@dataclass(frozen=True)
class PopulationGroup:
    """One group of a property: the cells its shares are taken over.

    ``label`` is empty for an ungrouped property.
    """

    label: str
    cells: Tuple[PopulationCell, ...]


@dataclass(frozen=True)
class PopulationProperty:
    """One property the scorecard scores, as the report draws it.

    A property with no ``groups`` is one the report has nothing to draw for (a relationship's
    integrity, a mean, a histogram whose cells the profile withheld): it is counted as left out.
    """

    expose_id: str
    column: str
    score: str
    groups: Tuple[PopulationGroup, ...]
    #: What the report calls the property when it is not ``exposeId.column`` (a relationship's
    #: children per parent is ``children:invoice.account``).
    label: str = ""
    #: ``distance 0.24, tolerance 0.1`` for a property scored outside its tolerance, else empty.
    outside: str = ""

    @property
    def cell_count(self) -> int:
        """How many cells a chart of this property would hold."""
        return sum(len(group.cells) for group in self.groups)


def _kept(character: str, after_kept: bool) -> bool:
    """Whether *character* is written as it is: an allowed ASCII one, or a letter or number of any script."""
    if character.isascii():
        return _ASCII_KEPT.fullmatch(character) is not None
    code = ord(character)
    if any(first <= code <= last for first, last in _DEFAULT_IGNORABLE):
        return False
    category = unicodedata.category(character)
    if category[0] in _KEPT_CATEGORIES:
        return True
    return category[0] == _MARK_CATEGORY and after_kept


def safe_text(text: str) -> str:
    """*text* in NFC with each character that is not safe to print replaced by ``_``, at most 40 characters.

    Kept: the ASCII letters, digits, space and ``_.,:;/()+=-``, and the letters and numbers of any
    script (with the combining marks that follow them). Everything else becomes ``_``: control and
    format characters (the bidirectional controls and the joiners among them), line and paragraph
    separators, spaces other than the plain one, private-use and unassigned code points, and every
    punctuation and symbol that is not in the ASCII set above, so no label can start a heading, a
    link, a table cell, a Mermaid directive or markup.

    A label that comes out as something other than what it was given (a character replaced, or cut
    at 40) ends in ``_`` and eight hexadecimal digits of the SHA-256 of the given text, so two
    different labels never print the same. A label that prints as it was given is unchanged. The
    result depends on the text alone, on every platform.
    """
    given = nfc(text)
    printed: List[str] = []
    after_kept = False
    for character in given:
        keep = _kept(character, after_kept)
        printed.append(character if keep else "_")
        after_kept = keep and unicodedata.category(character)[0] in "LNM"
    body = "".join(printed)
    if body == given and len(body) <= _MAX_TEXT and _TAIL.search(body) is None:
        return body
    digest = sha256_hex(given.encode("utf-8"))[:8]
    return f"{body[: _MAX_TEXT - _TAIL_LENGTH]}_{digest}"


def _shown(text: str, fallback: str) -> str:
    """*text* through :func:`safe_text`, or *fallback* when what it would print holds a reserved word.

    The whole report is refused (``FLX-8030``) when it holds a reserved word, and a level or a column
    is named by the data, so one such name would stop a run from shipping. The Provenance section
    withholds those names for the same reason (``quoted_name``) and this one does the same with the
    same matcher: the data keeps the real label, the report prints a position or a generic name.
    """
    printed = safe_text(text)
    return fallback if find_banned_words(printed) else printed


def share_percent(count: int, total: int) -> Decimal:
    """*count* over *total* as a percentage, rounded half to even to one decimal."""
    if total <= 0:
        return Decimal("0.0")
    with localcontext() as context:
        # Wide enough that a quotient of two integers is never near a tie but a true one.
        context.prec = 120
        return (Decimal(count) * 100 / Decimal(total)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_EVEN
        )


def _y_max(shares: Sequence[Decimal]) -> int:
    """The first multiple of 10 at or above the largest share, and at least 10."""
    largest = max(shares, default=Decimal(0))
    tens = (largest / 10).to_integral_value(rounding=ROUND_CEILING)
    return max(10, int(tens) * 10)


def _name(prop: PopulationProperty) -> str:
    """What the report calls *prop*: ``exposeId.column``, a part held back when it holds a reserved word."""
    if prop.label:
        return _shown(prop.label, f"{_shown(prop.expose_id, 'expose')} property")
    return f"{_shown(prop.expose_id, 'expose')}.{_shown(prop.column, 'column')}"


def _outside(prop: PopulationProperty) -> str:
    """``outside its tolerance (distance 0.24, tolerance 0.1)`` for a property scored outside, else empty."""
    return f"outside its tolerance ({safe_text(prop.outside)})" if prop.outside else ""


def _rows(prop: PopulationProperty) -> List[Tuple[str, Decimal, Decimal]]:
    rows: List[Tuple[str, Decimal, Decimal]] = []
    for group in prop.groups:
        profile_total = sum(cell.profile for cell in group.cells)
        run_total = sum(cell.run for cell in group.cells)
        prefix = f"{_shown(group.label, 'group')}: " if group.label else ""
        for cell in group.cells:
            rows.append(
                (
                    _shown(prefix + cell.label, f"{prefix}cell {len(rows) + 1}"),
                    share_percent(cell.profile, profile_total),
                    share_percent(cell.run, run_total),
                )
            )
    return rows


def _chart(prop: PopulationProperty) -> str:
    rows = _rows(prop)
    title = _name(prop) + ": bars profile, line run"
    labels = ", ".join(f'"{label}"' for label, _, _ in rows)
    ymax = _y_max([value for _, profile, run in rows for value in (profile, run)])
    table = "\n".join(f"| {label} | {profile} | {run} |" for label, profile, run in rows)
    return (
        "```mermaid\n"
        "xychart-beta\n"
        f'  title "{title}"\n'
        f"  x-axis [{labels}]\n"
        f'  y-axis "share (percent)" 0 --> {ymax}\n'
        f"  bar [{', '.join(str(profile) for _, profile, _ in rows)}]\n"
        f"  line [{', '.join(str(run) for _, _, run in rows)}]\n"
        "```\n\n"
        "<details><summary>Numbers</summary>\n\n"
        "| cell | profile | run |\n"
        "| --- | --- | --- |\n"
        f"{table}\n\n"
        "</details>"
    )


def _too_large_line(prop: PopulationProperty, outside: str) -> str:
    """The line for a property with more cells than :data:`MAX_CELLS`: its score, listed but not drawn."""
    return (
        f"{_name(prop)}: score {safe_text(prop.score)}, "
        f"not drawn ({prop.cell_count} cells, more than {MAX_CELLS})"
        + (f", {outside}." if outside else ".")
    )


def _charted_block(prop: PopulationProperty, outside: str) -> str:
    """A property's score line followed by its chart."""
    head = f"{_name(prop)}: score {safe_text(prop.score)}" + (f", {outside}" if outside else "")
    return f"{head}.\n\n{_chart(prop)}"


def render_population_section(properties: Optional[Sequence[PopulationProperty]]) -> str:
    """The body of the "Population" section.

    ``None`` is a run shipped without a profile: one sentence. Otherwise one chart per property that
    has cells, in order, at most :data:`MAX_CHARTS`, none for a property of more than
    :data:`MAX_CELLS` cells (that one is listed with its score). Every scored property that is not
    charted is counted in the closing line against the total the scorecard scores, and one that is
    outside its tolerance is named there with its distance and tolerance, so a failing property is
    never hidden by being left out.
    """
    if properties is None:
        return _NO_PROFILE
    if not properties:
        return _NOTHING_SCORED
    parts: List[str] = [_INTRO if any(prop.groups for prop in properties) else _NOTHING_SCORED]
    drawn = 0
    left_out = 0
    for prop in properties:
        outside = _outside(prop)
        if prop.groups and prop.cell_count > MAX_CELLS:
            left_out += 1
            parts.append(_too_large_line(prop, outside))
        elif prop.groups and drawn < MAX_CHARTS:
            drawn += 1
            parts.append(_charted_block(prop, outside))
        else:
            left_out += 1
            if outside:
                parts.append(f"{_name(prop)}: {outside}, not drawn.")
    parts.append(f"Properties left out of the charts: {left_out} of {len(properties)}.")
    return "\n\n".join(parts)
