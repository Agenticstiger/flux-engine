# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §1's draw path, and the stream table that serves it.

The test that matters most here is the one recording a **gap**:
KERNEL-SPEC §3's canonical path table has eleven rows and §1 needs a
twelfth, so :meth:`flux_engine.core.seedtree.SeedTree.stream` refuses the
generator's own address. That is pinned deliberately -- when the row lands
in the kernel table this test fails, which is the signal to delete
:class:`GeneratorStreams`' local derivation and pass the real tree in.
"""

from __future__ import annotations

from typing import Any, List, Sequence

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.core.errors import FluxError
from flux_engine.core.rng import uniform53
from flux_engine.core.seedtree import (
    CANONICAL_PATH_SHAPES,
    SeedTree,
    SeedTreeRoot,
    match_shape,
    path_label,
    seed_tree_digest,
    stream_entropy,
)
from flux_engine.generator.paths import (
    GENERATOR_PATH_SHAPE,
    GeneratorStreams,
    generator_column_path,
    matches_generator_shape,
    resolve_stream_source,
)

from .conftest import EXPOSE_ID, SIM_ID, root_for

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The path
# ---------------------------------------------------------------------------


def test_the_path_is_the_one_the_spec_writes() -> None:
    """``("sim", sim_id, "gen", exposeId, "row", rowIndex, column)``."""
    assert generator_column_path(SIM_ID, EXPOSE_ID, 7, "amount") == (
        "sim",
        SIM_ID,
        "gen",
        EXPOSE_ID,
        "row",
        7,
        "amount",
    )


def test_the_path_matches_its_declared_shape() -> None:
    assert matches_generator_shape(generator_column_path(SIM_ID, EXPOSE_ID, 0, "c"))


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("sim", SIM_ID, "gen", EXPOSE_ID, "row", 0),
        ("sim", SIM_ID, "gen", EXPOSE_ID, "row", 0, "c", "extra"),
        ("world", SIM_ID, "gen", EXPOSE_ID, "row", 0, "c"),
        ("sim", SIM_ID, "generate", EXPOSE_ID, "row", 0, "c"),
        ("sim", SIM_ID, "gen", EXPOSE_ID, "rows", 0, "c"),
        ("sim", SIM_ID, "gen", EXPOSE_ID, "row", -1, "c"),
        ("sim", SIM_ID, "gen", EXPOSE_ID, "row", True, "c"),
        ("sim", SIM_ID, "gen", EXPOSE_ID, "row", "0", "c"),
        ("sim", SIM_ID, "gen", EXPOSE_ID, "row", 0, ""),
        ("sim", 7, "gen", EXPOSE_ID, "row", 0, "c"),
    ],
)
def test_a_path_that_is_not_a_generator_path_does_not_match(path: Sequence[Any]) -> None:
    assert matches_generator_shape(path) is False


@pytest.mark.parametrize(
    ("sim_id", "expose_id", "row_index", "column"),
    [
        ("", EXPOSE_ID, 0, "c"),
        (SIM_ID, "", 0, "c"),
        (SIM_ID, EXPOSE_ID, -1, "c"),
        (SIM_ID, EXPOSE_ID, True, "c"),
        (SIM_ID, EXPOSE_ID, 0, ""),
    ],
)
def test_building_a_malformed_path_is_refused(
    sim_id: Any, expose_id: Any, row_index: Any, column: Any
) -> None:
    with pytest.raises(FluxError) as error:
        generator_column_path(sim_id, expose_id, row_index, column)
    assert error.value.code.code == "FLX-2202"


# ---------------------------------------------------------------------------
# The gap in KERNEL-SPEC §3's canonical table
# ---------------------------------------------------------------------------


def test_the_generator_path_is_not_yet_in_the_kernels_canonical_table(
    root: SeedTreeRoot,
) -> None:
    """A recorded gap, not an accident.

    When ``sim.generator`` lands in :data:`CANONICAL_PATH_SHAPES`, this
    test fails -- which is the signal that :class:`GeneratorStreams`'
    local derivation can be replaced by the kernel's own
    :class:`~flux_engine.core.seedtree.SeedTree`.
    """
    assert GENERATOR_PATH_SHAPE not in CANONICAL_PATH_SHAPES
    assert GENERATOR_PATH_SHAPE.name not in {shape.name for shape in CANONICAL_PATH_SHAPES}

    path = generator_column_path(SIM_ID, EXPOSE_ID, 0, "amount")
    assert match_shape(path) is None
    with pytest.raises(FluxError) as error:
        SeedTree(root=root).stream(path)
    assert error.value.code.code == "FLX-2201"


def test_the_local_derivation_is_the_kernels_own(root: SeedTreeRoot) -> None:
    """Bit-identical to what the canonical table would produce.

    :class:`GeneratorStreams` derives from the root with
    :func:`~flux_engine.core.seedtree.stream_entropy` -- the same function
    :class:`~flux_engine.core.seedtree.SeedTree` uses -- so adding the row
    to the table later moves no value.
    """
    path = generator_column_path(SIM_ID, EXPOSE_ID, 3, "amount")
    streams = GeneratorStreams(root=root)
    assert streams.entropy_of(path) == stream_entropy(root, path)


# ---------------------------------------------------------------------------
# The stream table
# ---------------------------------------------------------------------------


def test_a_stream_is_memoised_so_two_requests_continue_one_stream(
    streams: GeneratorStreams,
) -> None:
    path = generator_column_path(SIM_ID, EXPOSE_ID, 0, "amount")
    first = streams.stream(path)
    first_draw = uniform53(first)
    second = streams.stream(path)
    assert second is first
    assert uniform53(second) != first_draw or first.position == 2


def test_two_paths_are_two_streams(streams: GeneratorStreams) -> None:
    left = streams.stream(generator_column_path(SIM_ID, EXPOSE_ID, 0, "a"))
    right = streams.stream(generator_column_path(SIM_ID, EXPOSE_ID, 0, "b"))
    assert left is not right
    assert uniform53(left) != uniform53(right)


def test_a_non_generator_path_is_refused(streams: GeneratorStreams) -> None:
    with pytest.raises(FluxError) as error:
        streams.stream(("world", "w", "cohorts"))
    assert error.value.code.code == "FLX-2201"


def test_the_table_records_what_it_materialised(streams: GeneratorStreams) -> None:
    paths = [
        generator_column_path(SIM_ID, EXPOSE_ID, index, column)
        for index in (1, 0)
        for column in ("b", "a")
    ]
    for path in paths:
        streams.stream(path)
    materialised = streams.materialised_paths
    assert len(materialised) == len(paths)
    assert list(materialised) == sorted(materialised, key=path_label)
    assert streams.digest() == seed_tree_digest(paths)


def test_the_digest_is_blind_to_the_order_paths_were_touched() -> None:
    """§3: streams are addressed by path, never by spawn order."""
    paths = [generator_column_path(SIM_ID, EXPOSE_ID, index, "c") for index in range(4)]
    forward = GeneratorStreams(root=root_for())
    backward = GeneratorStreams(root=root_for())
    for path in paths:
        forward.stream(path)
    for path in reversed(paths):
        backward.stream(path)
    assert forward.digest() == backward.digest()


def test_a_different_root_gives_different_streams() -> None:
    path = generator_column_path(SIM_ID, EXPOSE_ID, 0, "c")
    first = GeneratorStreams(root=root_for(seed=1)).stream(path)
    second = GeneratorStreams(root=root_for(seed=2)).stream(path)
    assert uniform53(first) != uniform53(second)


@given(
    row=st.integers(min_value=0, max_value=1000),
    column=st.text(min_size=1, max_size=8).filter(
        lambda value: "\x1f" not in value and "\x1e" not in value
    ),
)
def test_every_cell_gets_its_own_entropy(row: int, column: str) -> None:
    root = root_for()
    path = generator_column_path(SIM_ID, EXPOSE_ID, row, column)
    assert stream_entropy(root, path) == stream_entropy(root, path)
    other = generator_column_path(SIM_ID, EXPOSE_ID, row + 1, column)
    assert stream_entropy(root, path) != stream_entropy(root, other)


# ---------------------------------------------------------------------------
# Choosing a stream source
# ---------------------------------------------------------------------------


def test_a_caller_can_pass_its_own_stream_source(root: SeedTreeRoot) -> None:
    mine = GeneratorStreams(root=root)
    assert resolve_stream_source(source=mine) is mine


def test_a_root_gets_a_table_built_over_it(root: SeedTreeRoot) -> None:
    source = resolve_stream_source(root=root)
    assert isinstance(source, GeneratorStreams)
    assert source.root is root


@pytest.mark.parametrize(
    "kwargs", [{}, {"root": root_for(), "source": GeneratorStreams(root=root_for())}]
)
def test_exactly_one_of_root_and_source_is_required(kwargs: Any) -> None:
    with pytest.raises(FluxError) as error:
        resolve_stream_source(**kwargs)
    assert error.value.code.code == "FLX-2202"


def test_the_shape_is_written_in_the_kernels_own_vocabulary() -> None:
    """So the diff that adds it to the canonical table is a one-line insert."""
    assert GENERATOR_PATH_SHAPE.rooted_at == "root"
    literals: List[str] = [part for part in GENERATOR_PATH_SHAPE.pattern if isinstance(part, str)]
    assert literals == ["sim", "gen", "row"]


def test_asking_for_the_entropy_of_a_non_generator_path_is_refused(
    streams: GeneratorStreams,
) -> None:
    """The shape check guards the entropy accessor as it guards the stream.

    Otherwise a caller could read the entropy of a path the table refuses
    to materialise, and derive the very stream the shape check exists to
    keep out of the generator's ``seedTreeDigest``.
    """
    with pytest.raises(FluxError) as error:
        streams.entropy_of(("world", "w", "market", 0))
    assert error.value.code.code == "FLX-2201"
