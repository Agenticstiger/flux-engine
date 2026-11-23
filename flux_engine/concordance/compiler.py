# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Compile a validated fidelity block into digests and margins -- §3.

SEAM-FIDELITY-SPEC §3 in full:

* ``H`` = ``jcs-document`` digest of ``{"contract": <parsed .fluid.yml>,
  "emit": <emits[] entry>, "extension": <the emit's entry in the
  document-level ``extensions["com.agentics.flux/1"].emits`` map, where
  the fidelity block lives>, "columns": [declared column names in
  order]}``. The ``extension`` key is present only when the emit has such
  an entry, so a bundle that declares none keeps the ``H`` it always had;
* ``G`` = the same over the emit entry with the **excluded set removed**:
  every ``tolerance``, every ``minVariance``, ``budget.maxStateAge``.
  ``G ⊂ H``. A change that leaves ``G`` unchanged keeps the committed
  statistic reusable;
* ``ε`` per property, computed from ``encoding``, ``columns[].range``,
  ``records.min`` (and ``minVariance`` for correlations) by interval
  propagation; ``τ′ = τ − ε``; ``ε ≥ τ`` → ``FLX-5002`` at plan time;
  ``τ′`` rounded onto the grid in the tightening direction;
* both ``H`` and ``G`` and every ``ε`` are written to ``plan.json``.

Two readings this module fixes, each argued at its code site:

* **``G`` covers the contract too.** §3 says ``G`` is "the same over the
  emit entry with the excluded set removed", which read narrowly would
  make ``G`` a digest of the emit entry alone. §8's table then says "any
  other declaration in the block **or the contract** → ``G`` moves",
  which that narrow reading cannot deliver. So ``G`` is the same
  three-key document as ``H``, with the excluded set stripped from the
  emit entry's fidelity block -- which is also the reading under which
  "``G ⊂ H``" is a statement about the material each digest covers.
* **the capacity check is per property.** KERNEL-SPEC §12 bounds
  "intermediate magnitudes ... from the declared ranges and maximum
  record count"; the intermediates a comparison *forms* depend on which
  comparison it is, so a mean over a timestamp column is not refused for
  the width of a correlation product it never computes.

Nothing here reads a clock or the environment: the caller supplies the
parsed documents, and every mapping is iterated by sorted key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.errors import FLX_5030
from flux_engine.concordance.predicate import (
    ExposeSchema,
    FidelityBlock,
    PropertyDeclaration,
    PropertyKind,
    ToleranceKind,
    parse_fidelity_block,
)
from flux_engine.core.canonical import nfc
from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.seam.emit import (
    reusable_extension,
)
from flux_engine.seam.emit import (
    strip_excluded as seam_strip_excluded,
)
from flux_engine.seam.hidden import fidelity_without_hidden
from flux_stats.fixedpoint import (
    DEFAULT_FIELD_CAPACITY_BITS,
    FLX_5001,
    Grid,
    Interval,
    effective_tolerance,
    epsilon_correlation,
    epsilon_marginal_histogram,
    epsilon_mean_absolute,
    epsilon_mean_relative,
    epsilon_quantile,
    epsilon_support,
    magnitude_bits,
)
from flux_stats.spelling import parse_exact, render_exact

__all__ = [
    "CompiledFidelity",
    "CompiledProperty",
    "EXCLUDED_FIELD_NAMES",
    "EXTENSION_TRANSPORT_KEY",
    "FIDELITY_BLOCK_KEY",
    "compile_fidelity",
    "parse_exact",
    "property_magnitudes",
    "render_exact",
    "seam_document",
    "strip_excluded",
]

#: The field names §3 removes to form ``G``, taken off the ``properties[]``
#: entries §2 declares them on -- **not** wherever a key of that name
#: appears. A depth-blind removal erased the whole ``fidelity.columns``
#: entry of a contract with a *column* named ``tolerance``, so editing that
#: column's declared range left ``G`` unmoved. See
#: :func:`flux_engine.seam.emit.strip_excluded_from_fidelity_block`, which
#: this module now delegates to so the two cannot disagree.
EXCLUDED_FIELD_NAMES = ("minVariance", "tolerance")

#: The one path §3 excludes that is not a property field: ``budget.maxStateAge``.
EXCLUDED_BUDGET_FIELD = "maxStateAge"

#: The one key the block is declared under inside the extension entry.
#: SEAM-FIDELITY-SPEC §2 gives it one public name and refuses a second
#: spelling for the same reason it refuses a bare support list: two
#: spellings of one declaration would be two digests of one contract.
FIDELITY_BLOCK_KEY = "fidelity"

#: How the extension block reaches this module through the run directory.
#:
#: ``apply`` writes ``run/fidelity-input-<exposeId>.json`` and ``ship`` and
#: ``verify`` read it back. The document's ``emit`` member is the emit
#: entry as authored plus this one reserved key holding the extension
#: block, so a reader that has the file has everything ``H`` is taken over
#: in one object. :func:`compile_fidelity` lifts it back out before
#: hashing, so the digest is over the same ``{contract, emit, extension,
#: columns}`` document the run computed -- the transport shape never
#: reaches a pre-image.
#:
#: The ``$`` prefix marks it as this artifact's own framing rather than
#: something an author wrote: no FLUX emit field is spelled that way.
EXTENSION_TRANSPORT_KEY = "$extension"


# ---------------------------------------------------------------------------
# H and G
# ---------------------------------------------------------------------------


def seam_document(
    *,
    contract: Mapping[str, Any],
    emit: Mapping[str, Any],
    columns: Sequence[str],
    extension: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The document ``H`` is taken over (§3, KERNEL-SPEC §8).

    The column list is the expose's *declared order*, not a sorted one:
    KERNEL-SPEC §8 emits rows as arrays aligned to that order, so a
    reordering of the expose is a change to the wire format and must move
    the digest.

    ``extension`` is the emit's entry in the document-level
    ``extensions["com.agentics.flux/1"].emits`` map, where the fidelity
    block lives. The key is present only when the emit has such an entry,
    so ``H`` for a bundle that declares none is byte-identical to what it
    was before the block existed. This function and
    :func:`flux_engine.seam.emit.seam_digest` build the same document, and
    a test pins them against each other -- which is the only thing keeping
    the run's ``H`` and the ship gate's ``H`` one number.
    """
    document: Dict[str, Any] = {
        "contract": dict(contract),
        "emit": dict(emit),
        "columns": [nfc(str(name)) for name in columns],
    }
    if extension is not None:
        document["extension"] = dict(extension)
    return document


def strip_excluded(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """Return *entry* with §3's excluded set removed, as the compiler's ``G`` takes it.

    The seam's :func:`flux_engine.seam.emit.strip_excluded` does the removal, so the compiler
    and the seam cannot disagree about what ``G`` is blind to: the fidelity block's excluded
    fields, and the links, bridges, counts, copies and overlay members. A ``tolerance`` key outside the
    fidelity block is a different declaration and stays. The ``facts`` member stays too: the
    compiler's ``G`` has covered it since 0.9.0 and committed states written then are bound to it.
    """
    return seam_strip_excluded(entry, keep_facts=True)


# ---------------------------------------------------------------------------
# Per-property compilation
# ---------------------------------------------------------------------------

#: Which of :func:`~flux_stats.fixedpoint.magnitude_bits`'s named
#: intermediates each comparison actually forms (SEAM-FIDELITY-SPEC §5).
#: A support check forms none: it compares sorted member lists and counts
#: non-members, and encodes no value. A level histogram encodes no value
#: either, so of a histogram's two it forms only the cross-multiplied
#: total-variation inequality.
_FORMED_INTERMEDIATES: Mapping[PropertyKind, Tuple[str, ...]] = MappingProxyType(
    {
        PropertyKind.MEAN: ("value", "sum", "meanComparison"),
        PropertyKind.MARGINAL_HISTOGRAM: ("value", "totalVariationComparison"),
        PropertyKind.LEVEL_HISTOGRAM: ("totalVariationComparison",),
        PropertyKind.SUPPORT: (),
        PropertyKind.QUANTILE: ("value",),
        # A rank correlation forms exact integers (``S`` or ``A4``) in unbounded precision and
        # compares two rationals, so it needs no fixed-width intermediate (ADR 0035 decision 7).
        PropertyKind.RANK_CORRELATION: (),
        PropertyKind.CORRELATION: (
            "value",
            "sum",
            "sumOfProducts",
            "centredSecondMoment",
            "correlationNumerator",
            "correlationComparison",
        ),
    }
)


@dataclass(frozen=True)
class CompiledProperty:
    """One property's compiled margin and the bound behind it.

    Attributes
    ----------
    epsilon
        The encoding-error bound ``ε``, propagated by interval arithmetic
        from the declared grid, range and variance floor.
    margin
        ``τ′ = τ − ε`` rounded onto the grid in the tightening direction.
        ``None`` for a support check, whose tolerance is an integer count
        of non-members rather than a value-domain margin, and for which
        ``ε`` is exactly zero.
    margin_units
        ``τ′`` as an integer number of grid steps, for the two comparisons
        whose margin carries the column's units (an absolute mean, a
        quantile). ``None`` for the dimensionless ones.
    max_non_members
        A support check's integer margin.
    magnitudes
        The bit width of every intermediate this comparison forms.
    edges
        The declared bin edges on the block's grid, for a histogram and for
        a quantile that declares bins (``quantile/2``, ADR 0012); empty for
        every other property.
    """

    declaration: PropertyDeclaration
    epsilon: Fraction
    margin: Optional[Fraction]
    margin_units: Optional[int] = None
    max_non_members: Optional[int] = None
    magnitudes: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    edges: Tuple[int, ...] = ()

    @property
    def property_id(self) -> str:
        """The declaration's stable key inside its block."""
        return self.declaration.property_id


def property_magnitudes(
    *,
    block: FidelityBlock,
    declaration: PropertyDeclaration,
    margin: Optional[Fraction],
) -> Dict[str, int]:
    """Bound the intermediates *declaration*'s comparison forms.

    KERNEL-SPEC §12 bounds intermediate magnitudes "from the declared
    ranges and maximum record count". For a two-column property the wider
    of the two declared ranges is used, since either column can be the one
    whose encoded magnitude drives the product.
    """
    formed = _FORMED_INTERMEDIATES[declaration.kind]
    if not formed:
        return {}
    if declaration.kind is PropertyKind.LEVEL_HISTOGRAM:
        # A support column has no range to bound an encoded value by, and
        # the one intermediate a level histogram forms, ``2*d*k**2``, does
        # not read one: the zero interval stands for "no value is
        # encoded", so the bound comes from the record ceiling and the
        # margin alone, through the same formula a histogram's does.
        bounds = magnitude_bits(
            grid=block.grid,
            value_range=Interval(Fraction(0), Fraction(0)),
            max_records=block.records.maximum,
            tolerance=margin,
        )
        return {name: bounds[name] for name in formed}
    interval = block.column(declaration.columns[0]).interval
    for extra in declaration.columns[1:]:
        other = block.column(extra).interval
        if other.magnitude > interval.magnitude:
            interval = other
    bounds = magnitude_bits(
        grid=block.grid,
        value_range=interval,
        max_records=block.records.maximum,
        tolerance=margin,
    )
    return {name: bounds[name] for name in formed}


def _epsilon_of(block: FidelityBlock, declaration: PropertyDeclaration) -> Fraction:
    grid: Grid = block.grid
    kind = declaration.kind
    if kind is PropertyKind.RANK_CORRELATION:
        # Both sides are the statistic of the grid values, formed by one function, and a tie is a
        # tie on the grid (ADR 0035 decision 7): the grid is part of the definition, so it adds
        # no error to bound.
        return epsilon_support()
    if kind is PropertyKind.SUPPORT or kind is PropertyKind.LEVEL_HISTOGRAM:
        # Neither encodes a value: a support check compares member lists,
        # a level histogram counts records per declared level. A level is
        # read as the string it is, so the grid moves no record across a
        # cell and the bound is zero for the reason a support check's is.
        return epsilon_support()
    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        return epsilon_marginal_histogram(grid, declaration.bins)
    if kind is PropertyKind.QUANTILE:
        if declaration.bins:
            # A quantile that declares bins puts its edges where a histogram's
            # go: on the grid, so that encoding never moves the order statistic
            # across one. An off-grid edge is refused as a histogram's is. The
            # bound is the quantile's own, as the witness moves by the same
            # less than one grid step either way (ADR 0012).
            epsilon_marginal_histogram(grid, declaration.bins)
        return epsilon_quantile(grid)
    if kind is PropertyKind.CORRELATION:
        assert declaration.min_variance is not None  # guaranteed by the predicate parser
        return epsilon_correlation(grid, declaration.min_variance)
    if declaration.tolerance.kind is ToleranceKind.RELATIVE:
        return epsilon_mean_relative(
            grid,
            block.column(declaration.columns[0]).interval,
            declaration.tolerance.magnitude,
        )
    return epsilon_mean_absolute(grid)


def _compile_property(block: FidelityBlock, declaration: PropertyDeclaration) -> CompiledProperty:
    epsilon = _epsilon_of(block, declaration)
    if declaration.kind is PropertyKind.SUPPORT:
        # A count of non-members is not a value-domain margin: there is
        # nothing for the grid to round and nothing for epsilon to eat, so
        # the declared maximum is carried through as the integer it is.
        maximum = declaration.tolerance.magnitude
        compiled = CompiledProperty(
            declaration=declaration,
            epsilon=epsilon,
            margin=None,
            max_non_members=int(maximum),
        )
        return compiled

    margin = effective_tolerance(block.grid, declaration.tolerance.magnitude, epsilon)
    units: Optional[int] = None
    if declaration.kind is PropertyKind.QUANTILE or (
        declaration.kind is PropertyKind.MEAN
        and declaration.tolerance.kind is ToleranceKind.ABSOLUTE
    ):
        units = block.grid.quantise(margin, subject=f"{declaration.property_id} margin")
    magnitudes = property_magnitudes(block=block, declaration=declaration, margin=margin)
    return CompiledProperty(
        declaration=declaration,
        epsilon=epsilon,
        margin=margin,
        margin_units=units,
        magnitudes=MappingProxyType(dict(sorted(magnitudes.items()))),
        edges=tuple(block.grid.quantise(edge, subject="bin edge") for edge in declaration.bins),
    )


# ---------------------------------------------------------------------------
# The compiled block
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledFidelity:
    """A fidelity block with its digests, margins and magnitude bounds."""

    block: FidelityBlock
    h: DigestRef
    g: DigestRef
    properties: Tuple[CompiledProperty, ...]
    capacity_bits: int = DEFAULT_FIELD_CAPACITY_BITS

    def property_by_id(self, property_id: str) -> CompiledProperty:
        """Return the compiled property with *property_id*."""
        for compiled in self.properties:
            if compiled.property_id == property_id:
                return compiled
        raise FluxError(FLX_5030, f"no compiled property {property_id!r}")

    def plan_document(self) -> Dict[str, Any]:
        """The fragment §3 says goes into ``plan.json``.

        "Both ``H`` and ``G`` and every ``ε`` are written to
        ``plan.json``." The margin and the magnitude bounds travel with
        them: a reader checking a record months later needs the number the
        comparison actually used, not only the two it was derived from.
        ``fluxsim plan`` writes it into the emit's entry, as
        ``emits.<exposeId>.fidelity``.
        """
        return {
            "exposeId": self.block.expose_id,
            "H": self.h.value,
            "G": self.g.value,
            "encoding": {
                "fractionalBits": self.block.encoding.fractional_bits,
                "rounding": self.block.encoding.rounding,
            },
            "fieldCapacityBits": self.capacity_bits,
            "properties": [
                {
                    "propertyId": compiled.property_id,
                    "property": compiled.declaration.kind.value,
                    "columns": list(compiled.declaration.columns),
                    **(
                        {"groupBy": compiled.declaration.group_by}
                        if compiled.declaration.group_by is not None
                        else {}
                    ),
                    **(
                        {
                            "groupByBins": {
                                "column": compiled.declaration.group_by_bins.column,
                                "bins": [
                                    render_exact(edge)
                                    for edge in compiled.declaration.group_by_bins.bins
                                ],
                            }
                        }
                        if compiled.declaration.group_by_bins is not None
                        else {}
                    ),
                    "epsilon": render_exact(compiled.epsilon),
                    "margin": (
                        render_exact(compiled.margin) if compiled.margin is not None else None
                    ),
                    "maxNonMembers": compiled.max_non_members,
                    "magnitudeBits": dict(sorted(compiled.magnitudes.items())),
                }
                for compiled in self.properties
            ],
        }


def compile_fidelity(
    *,
    contract: Mapping[str, Any],
    emit: Mapping[str, Any],
    expose: ExposeSchema,
    extension: Optional[Mapping[str, Any]] = None,
    block: Optional[FidelityBlock] = None,
    capacity_bits: int = DEFAULT_FIELD_CAPACITY_BITS,
) -> CompiledFidelity:
    """Compile the fidelity block of *emit* against *expose*.

    The block lives in the emit's *extension* entry
    (``extensions["com.agentics.flux/1"].emits[<exposeId>].fidelity``).
    Pass it as *extension*; a caller reading a run's
    ``fidelity-input-<exposeId>.json`` may instead leave it inside *emit*
    under :data:`EXTENSION_TRANSPORT_KEY`, which this function lifts out
    before anything is hashed -- so the file and the in-process call
    produce the same ``H``.

    Parses the block when one is not supplied, takes ``H`` and ``G``, then
    compiles every property's ``ε`` and ``τ′`` and bounds the
    intermediates its comparison forms. Raises ``FLX-5002`` when an
    epsilon is not smaller than its tolerance and ``FLX-5001`` when an
    intermediate does not fit *capacity_bits* -- both at compile time,
    before any statistic exists to compare.
    """
    if EXTENSION_TRANSPORT_KEY in emit:
        carried = emit[EXTENSION_TRANSPORT_KEY]
        if extension is None and isinstance(carried, Mapping):
            extension = carried
        emit = {key: value for key, value in emit.items() if key != EXTENSION_TRANSPORT_KEY}

    if block is None:
        raw = (extension or {}).get(FIDELITY_BLOCK_KEY)
        if raw is None:
            raise FluxError(
                FLX_5030,
                "this emit declares no fidelity block under "
                "extensions['com.agentics.flux/1'].emits[<exposeId>]",
            )
        # A value that is not a mapping is a block nobody can read, not a
        # missing one: the parser refuses it as what it is.
        block = parse_fidelity_block(fidelity_without_hidden(raw, extension), expose=expose)

    columns = tuple(nfc(str(name)) for name in expose.column_names)
    h = digest_of_document(
        seam_document(contract=contract, emit=emit, columns=columns, extension=extension),
        subject=f"H:{block.expose_id}",
    )
    g = digest_of_document(
        seam_document(
            contract=contract,
            emit=strip_excluded(emit),
            columns=columns,
            # ``G`` has covered the facts member since 0.9.0 and committed states written then
            # are bound to it, so it stays in here though the plan-side ``G`` drops it.
            extension=reusable_extension(extension, keep_facts=True),
        ),
        subject=f"G:{block.expose_id}",
    )

    compiled: List[CompiledProperty] = []
    for declaration in block.properties:
        entry = _compile_property(block, declaration)
        for name, width in sorted(entry.magnitudes.items()):
            if width > capacity_bits:
                raise FluxError(
                    FLX_5001,
                    f"{declaration.property_id}: {name} needs {width} bits, "
                    f"capacity is {capacity_bits}",
                )
        compiled.append(entry)

    return CompiledFidelity(
        block=block,
        h=h,
        g=g,
        properties=tuple(compiled),
        capacity_bits=capacity_bits,
    )
