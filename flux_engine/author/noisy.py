# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A noisy statistic is a point estimate with a variance: what ``fluxsim init`` reads of one.

PROFILE-SPEC.md section 3a. A profile released under differential privacy writes the counts of a
noisy statistic as ``noisyCounts``: decimal strings that can be negative or fractional, each with
the variance its ``noise`` block gives. ``fluxsim init`` reads such a statistic as it reads an
exact one, through its **point estimate**: the noisy values made a valid distribution by the one
rule of :func:`flux_stats.noise.point_estimate` (non-negative, adding up to the noisy total,
every negative cell paid for by the cells above it, equal shares when the total is not above
zero), spelled as whole counts that add up to the estimated record count. It records the variance
each one carries, so a person reading the bundle's ledger can see how much noise stands behind a
number.

Everything here is post-processing of numbers the profiler released: nothing reads a record, and
nothing draws.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Tuple

from flux_engine.author.basis import Basis
from flux_engine.author.forms import grouped
from flux_stats.cliques import CLIQUE_DEFINITION
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM
from flux_stats.noise import noise_floor, point_estimate, variance_of
from flux_stats.spelling import render_exact

__all__ = ["NoisyNote", "cite_noisy", "point_estimates"]

_COUNT_KEYS = {
    LEVEL_HISTOGRAM.identifier: "levelCounts",
    MARGINAL_HISTOGRAM.identifier: "binCounts",
}


@dataclass(frozen=True)
class NoisyNote:
    """What a noisy statistic of the profile carries, for the ledger."""

    tokens: Tuple[str, ...]
    mechanism: str
    scale: str
    #: The variance of each noisy cell, exact (``numerator/denominator`` when it does not terminate).
    variance: str
    #: The definition, and the records the point estimate counts: of an ungrouped statistic, or of
    #: a whole clique.
    definition: str = ""
    records: Optional[int] = None
    #: Of an ungrouped histogram, whether each cell's noisy count stands clear of the noise
    #: (:func:`flux_stats.noise.noise_floor`, ADR 0019, amendment of 2026-11-25); ``None`` else.
    clear: Optional[Tuple[bool, ...]] = None

    @property
    def sentence(self) -> str:
        return (
            f"released with {self.mechanism} noise of scale {self.scale} (variance {self.variance} "
            "per cell), and read as a point estimate: negative cells clipped by projection, "
            "normalised, as whole counts"
        )

    def as_document(self) -> Dict[str, str]:
        return {"mechanism": self.mechanism, "scale": self.scale, "variance": self.variance}


def point_estimates(
    profile: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], Dict[Tuple[str, ...], NoisyNote]]:
    """The profile with each noisy statistic read as its point estimate, and what each one carried.

    The result has the shape of the document: a noisy statistic stays at its place in its column's
    list, so a pointer into the profile still reaches it. Its groups become ordinary count groups
    (``k`` the estimated records, ``levelCounts`` or ``binCounts`` the whole counts). A profile
    with no noisy statistic is returned as it is. The notes are keyed by the statistic's tokens.
    """
    notes: Dict[Tuple[str, ...], NoisyNote] = {}
    tables = profile.get("tables")
    if not isinstance(tables, Mapping) or not _has_noise(profile):
        return profile, notes
    view = copy.deepcopy(dict(profile))
    for expose, table in view["tables"].items():
        cliques = table.get("cliques") if isinstance(table, dict) else None
        for index, clique in enumerate(cliques if isinstance(cliques, list) else ()):
            at: Tuple[str, ...] = ("tables", str(expose), "cliques", str(index))
            noise = clique["noise"]
            variance = variance_of(str(noise["mechanism"]), Fraction(noise["scale"]))
            notes[at] = NoisyNote(
                tokens=at,
                mechanism=str(noise["mechanism"]),
                scale=str(noise["scale"]),
                variance=render_exact(variance),
                definition=str(clique.get("definition", CLIQUE_DEFINITION)),
                records=point_estimate([Fraction(text) for text in clique["noisyCounts"]]).records,
            )
        columns = table.get("columns") if isinstance(table, dict) else None
        for name, column in (columns.items() if isinstance(columns, dict) else ()):
            statistics = column.get("statistics") if isinstance(column, dict) else None
            for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
                tokens = ("tables", str(expose), "columns", str(name), "statistics", str(index))
                _estimate(statistic, tokens, notes)
    # A relationship's noisy histogram of children per parent, grouped or not (profile format
    # 1-4-0), is read as a column's is.
    relationships = view.get("relationships")
    for number, entry in enumerate(relationships if isinstance(relationships, list) else ()):
        listed = entry.get("statistics") if isinstance(entry, dict) else None
        for index, statistic in enumerate(listed if isinstance(listed, list) else ()):
            _estimate(statistic, ("relationships", str(number), "statistics", str(index)), notes)
    return view, notes


def _estimate(
    statistic: Any, tokens: Tuple[str, ...], notes: Dict[Tuple[str, ...], NoisyNote]
) -> None:
    """Replace a noisy histogram's cells by their point estimate, in place, and note its noise."""
    if not isinstance(statistic, dict) or "noise" not in statistic:
        return
    key = _COUNT_KEYS[statistic["definition"]]
    noise = statistic.pop("noise")
    records = None
    clear = None
    for group in statistic["groups"]:
        noisy = [Fraction(text) for text in group.pop("noisyCounts")]
        estimate = point_estimate(noisy)
        group["k"] = estimate.records
        group[key] = list(estimate.counts)
        records = estimate.records if not grouped(statistic) else None
        if not grouped(statistic) and statistic["definition"] == MARGINAL_HISTOGRAM.identifier:
            floor = noise_floor(str(noise["mechanism"]), Fraction(noise["scale"]), len(noisy))
            clear = tuple(value > floor for value in noisy)
    variance = variance_of(str(noise["mechanism"]), Fraction(noise["scale"]))
    notes[tokens] = NoisyNote(
        tokens=tokens,
        mechanism=str(noise["mechanism"]),
        scale=str(noise["scale"]),
        variance=render_exact(variance),
        definition=str(statistic["definition"]),
        records=records,
        clear=clear,
    )


def _has_noise(profile: Mapping[str, Any]) -> bool:
    tables = profile.get("tables")
    for entry in profile.get("relationships") or ():
        statistics = entry.get("statistics") if isinstance(entry, Mapping) else None
        if any(isinstance(item, Mapping) and "noise" in item for item in statistics or ()):
            return True
    for table in tables.values() if isinstance(tables, Mapping) else ():
        if isinstance(table, Mapping) and table.get("cliques"):
            return True
        columns = table.get("columns") if isinstance(table, Mapping) else None
        for column in columns.values() if isinstance(columns, Mapping) else ():
            statistics = column.get("statistics") if isinstance(column, Mapping) else None
            if any(isinstance(item, Mapping) and "noise" in item for item in statistics or ()):
                return True
    return False


def cite_noisy(basis: Basis, notes: Mapping[Tuple[str, ...], NoisyNote]) -> Basis:
    """*basis* with a citation of a noisy statistic's counts pointing at the profile's own words.

    The point estimate has ``levelCounts``, ``binCounts`` and ``k`` where the document has
    ``noisyCounts``, so a pointer into the counts of a noisy statistic is rewritten to the
    ``noisyCounts`` list it was formed from (a cell stays the cell at its position).
    """
    if basis.statistic is None or not notes:
        return basis
    tokens, definition = basis.statistic
    for prefix in notes:
        size = len(prefix)
        if (
            tokens[:size] == prefix
            and tokens[size : size + 1] == ("groups",)
            and len(tokens) > size + 2
        ):
            leaf = tokens[size + 2]
            if leaf in ("levelCounts", "binCounts"):
                tokens = tokens[: size + 2] + ("noisyCounts",) + tokens[size + 3 :]
            elif leaf == "k":
                tokens = tokens[: size + 2] + ("noisyCounts",)
            return Basis(
                basis.source,
                statistic=(tokens, definition),
                contract=basis.contract,
                reason=basis.reason,
            )
    return basis


def variance_lines(notes: Mapping[Tuple[str, ...], NoisyNote]) -> List[str]:
    """One line for each noisy statistic: where it is and the noise it carries."""
    return [
        f"  - /{'/'.join(tokens)}: {note.mechanism}, scale {note.scale}, variance {note.variance}"
        for tokens, note in sorted(notes.items())
    ]
