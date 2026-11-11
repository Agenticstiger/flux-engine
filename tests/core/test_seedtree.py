# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §3: the seed tree.

The properties pinned here are the ones §3 states in prose:

* **insertion stability** -- "adding an unreferenced document to the bundle, or a new
  consumer that uses a new path, changes no existing stream";
* **seed sensitivity** -- and contract sensitivity: "Changing the contract changes every
  stream";
* **path addressing** -- a stream's words depend on its address and on nothing else, so
  materialising streams in a different order changes none of them;
* **``FLX-2201``** -- a run that touches a path the plan did not anticipate stops.

``assert_insertion_stability`` and ``assert_streams_are_path_addressed`` take the tree
factory under test, so ``test_negative_kernels_streams.py`` can point them at a
spawn-ordered tree and show they fire.

Regenerating ``tests/vectors/seed-vectors.json``: every value in that file is computed by
:mod:`flux_engine.core.seedtree` from ids, digests and seeds that the shipped
telco-payment-recovery example declares. To rebuild it, re-run the derivations this module
asserts (``derive_root``, ``path_label``, ``stream_entropy``, the first four
``next_raw()`` words per path, and ``seed_paths_bytes``/``seed_paths_digest`` over the
``streams`` paths) and write them back. A value that moves is a
``kernelVersion`` bump with a ``GOLDEN-CHANGELOG.md`` row -- never a quiet regeneration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Tuple

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from flux_engine.core import seedtree as st_mod
from flux_engine.core.errors import FluxError
from flux_engine.core.rng import DrawStream

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "seed-vectors.json"

TreeFactory = Callable[[st_mod.SeedTreeRoot], st_mod.SeedTree]


def load_seed_vectors() -> Dict[str, object]:
    return json.loads(VECTORS.read_text())


VECTOR_DOCUMENT = load_seed_vectors()
SOURCE = VECTOR_DOCUMENT["source"]
assert isinstance(SOURCE, dict)

CONTRACT_DIGEST = str(SOURCE["contractDigest"])
WORLD_ID = str(SOURCE["worldId"])
SIM_ID = str(SOURCE["simulationId"])
EXPERIMENT_ID = str(SOURCE["experimentId"])
SIM_SEED = int(SOURCE["simulationSeed"])
WORLD_SEED = int(SOURCE["worldSeed"])
ASSIGNMENT_SEED = int(SOURCE["assignmentSeed"])


def telco_root() -> st_mod.SeedTreeRoot:
    """The root the shipped example derives: one emit, effective seed 42."""
    seam = st_mod.seam_bytes([CONTRACT_DIGEST])
    return st_mod.derive_root(seam, st_mod.effective_seed(SIM_SEED, WORLD_SEED))


def first_words(tree: st_mod.SeedTree, path: Sequence[object], count: int = 4) -> List[int]:
    stream = tree.stream(tuple(path))
    return [stream.next_raw() for _ in range(count)]


# ---------------------------------------------------------------------------
# Root derivation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_effective_seed_prefers_the_simulation_over_the_world() -> None:
    assert st_mod.effective_seed(11, 22) == 11
    assert st_mod.effective_seed(None, 22) == 22


@pytest.mark.unit
def test_effective_seed_refuses_what_it_cannot_derive_from() -> None:
    for bad in (None, True, 2**63, -(2**63) - 1):
        with pytest.raises(FluxError) as caught:
            st_mod.effective_seed(bad, None)  # type: ignore[arg-type]
        assert caught.value.code.code == "FLX-2202"


@pytest.mark.unit
def test_h_bytes_is_the_digest_itself_for_one_emit_and_a_fold_for_several() -> None:
    single = st_mod.seam_bytes([CONTRACT_DIGEST])
    assert single == st_mod.digest_bytes(CONTRACT_DIGEST)
    assert len(single) == 32

    other = "sha256:" + "11" * 32
    folded = st_mod.seam_bytes([CONTRACT_DIGEST, other])
    assert len(folded) == 32
    assert folded != single
    # emits[] order is part of the derivation.
    assert folded != st_mod.seam_bytes([other, CONTRACT_DIGEST])


@pytest.mark.unit
def test_a_malformed_digest_reference_is_refused() -> None:
    for bad in ("762fdf58", "sha256:" + "zz" * 32, "sha512:" + "ab" * 32, "sha256:abcd"):
        with pytest.raises(FluxError) as caught:
            st_mod.digest_bytes(bad)
        assert caught.value.code.code == "FLX-2202"
    with pytest.raises(FluxError):
        st_mod.seam_bytes([])


@pytest.mark.unit
def test_the_root_is_the_documented_construction() -> None:
    """Rebuild §3's ``root_label`` here and check the module derives the same root."""
    from flux_engine.core.canonical import sha256_hex

    seam = st_mod.seam_bytes([CONTRACT_DIGEST])
    seed = st_mod.effective_seed(SIM_SEED, WORLD_SEED)
    label = b"flux-seed/1" + b"\x00" + seam + b"\x00" + seed.to_bytes(8, "big", signed=True)
    expected = sha256_hex(label)
    root = st_mod.derive_root(seam, seed)
    assert root.hex == expected
    assert root.value == int.from_bytes(bytes.fromhex(expected), "big")


@pytest.mark.property
@given(seed=st.integers(min_value=-(2**63), max_value=2**63 - 1))
def test_every_seed_gives_its_own_root(seed: int) -> None:
    seam = st_mod.seam_bytes([CONTRACT_DIGEST])
    other = seed + 1 if seed < 2**63 - 1 else seed - 1
    assert st_mod.derive_root(seam, seed).hex != st_mod.derive_root(seam, other).hex


@pytest.mark.property
@given(
    byte_index=st.integers(min_value=0, max_value=31),
    delta=st.integers(min_value=1, max_value=255),
)
def test_changing_the_contract_changes_the_root(byte_index: int, delta: int) -> None:
    """§3: "Changing the contract changes every stream"."""
    seam = bytearray(st_mod.seam_bytes([CONTRACT_DIGEST]))
    original = st_mod.derive_root(bytes(seam), 42)
    seam[byte_index] = (seam[byte_index] + delta) % 256
    assert st_mod.derive_root(bytes(seam), 42).hex != original.hex


@pytest.mark.unit
def test_reseed_keeps_the_contract_and_moves_only_the_seed() -> None:
    root = telco_root()
    assignment = root.reseed(ASSIGNMENT_SEED)
    assert assignment.seam_bytes == root.seam_bytes
    assert assignment.effective_seed == ASSIGNMENT_SEED
    assert assignment.hex != root.hex


@pytest.mark.unit
def test_sweep_seeds_walk_the_base_seed() -> None:
    assert st_mod.sweep_effective_seed(42, 0) == 42
    assert st_mod.sweep_effective_seed(42, 3) == 45
    with pytest.raises(FluxError):
        st_mod.sweep_effective_seed(42, -1)
    with pytest.raises(FluxError):
        st_mod.sweep_effective_seed(2**63 - 1, 1)


# ---------------------------------------------------------------------------
# Path labels
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_label_is_the_documented_join() -> None:
    path = st_mod.market_path(WORLD_ID, 7)
    assert st_mod.path_label(path) == b"\x1f".join(
        [b"world", WORLD_ID.encode("utf-8"), b"market", b"7"]
    )


@pytest.mark.unit
def test_the_two_spellings_of_one_trait_name_address_one_stream() -> None:
    """§3's label applies NFC to every part, so one trait name is one stream."""
    composed = "caf\u00e9Score"
    decomposed = "cafe\u0301Score"
    assert composed != decomposed
    assert st_mod.path_label(st_mod.subject_trait_path(WORLD_ID, 0, composed)) == st_mod.path_label(
        st_mod.subject_trait_path(WORLD_ID, 0, decomposed)
    )


@pytest.mark.unit
def test_an_empty_path_has_no_label() -> None:
    with pytest.raises(FluxError):
        st_mod.path_label(())


@pytest.mark.property
@given(
    ordinal_a=st.integers(min_value=0, max_value=10**6),
    ordinal_b=st.integers(min_value=0, max_value=10**6),
)
def test_distinct_ordinals_address_distinct_streams(ordinal_a: int, ordinal_b: int) -> None:
    assume(ordinal_a != ordinal_b)
    root = telco_root()
    assert st_mod.stream_entropy(root, st_mod.market_path(WORLD_ID, ordinal_a)) != (
        st_mod.stream_entropy(root, st_mod.market_path(WORLD_ID, ordinal_b))
    )


# ---------------------------------------------------------------------------
# The canonical path table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_canonical_table_has_the_eleven_rows_the_spec_lists() -> None:
    names = [shape.name for shape in st_mod.CANONICAL_PATH_SHAPES]
    assert len(names) == len(set(names)) == 11
    assert {
        "world.cohorts",
        "world.market",
        "world.subject.trait",
        "world.subject.products",
        "sim.signal",
        "sim.journey",
        "sim.campaign",
        "sim.temperature",
        "experiment.assignment",
        "sweep.run",
        "sim.agentic",
    } == set(names)


@pytest.mark.unit
def test_the_two_re_rooted_rows_say_so() -> None:
    rooted = {shape.name: shape.rooted_at for shape in st_mod.CANONICAL_PATH_SHAPES}
    assert rooted["experiment.assignment"] == "Experiment.spec.assignment.seed"
    assert rooted["sweep.run"] == "baseSeed + run_index"
    assert [name for name, at in rooted.items() if at != "root"] == [
        "experiment.assignment",
        "sweep.run",
    ]


@pytest.mark.unit
def test_every_constructor_builds_a_path_the_table_matches() -> None:
    built = [
        st_mod.world_cohorts_path(WORLD_ID),
        st_mod.market_path(WORLD_ID, 0),
        st_mod.subject_trait_path(WORLD_ID, 0, "monthlySpend"),
        st_mod.subject_products_path(WORLD_ID, 0),
        st_mod.signal_path(SIM_ID, "sig-payment-aborted", 0, 0),
        st_mod.journey_path(SIM_ID, "jr-payment-recovery", 0, 0),
        st_mod.campaign_path(SIM_ID, "cam-payment-recovery", 0),
        st_mod.temperature_path(SIM_ID, "engagement", 0, 0),
        st_mod.experiment_assignment_path(EXPERIMENT_ID),
        st_mod.sweep_path(0),
        st_mod.agentic_path(SIM_ID, "journey-improviser", 0, 0),
    ]
    matched = [st_mod.match_shape(path) for path in built]
    assert all(shape is not None for shape in matched)
    assert len({shape.name for shape in matched if shape is not None}) == len(
        st_mod.CANONICAL_PATH_SHAPES
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        ("world", "w"),
        ("world", "w", "cohort"),
        ("world", "w", "market", -1),
        ("world", "w", "market", "0"),
        ("planet", "w", "cohorts"),
        ("sim", "s", "signal", "sig", "subject", 0),
        ("sweep", -1),
        (),
    ],
)
def test_a_path_outside_the_table_matches_nothing(path: Tuple[object, ...]) -> None:
    assert st_mod.match_shape(path) is None


@pytest.mark.unit
def test_a_constructor_refuses_a_part_it_cannot_address() -> None:
    with pytest.raises(FluxError):
        st_mod.market_path("", 0)
    with pytest.raises(FluxError):
        st_mod.market_path(WORLD_ID, -1)
    with pytest.raises(FluxError):
        st_mod.subject_trait_path(WORLD_ID, 0, "")


# ---------------------------------------------------------------------------
# The tree: the properties §3 states
# ---------------------------------------------------------------------------


def assert_streams_are_path_addressed(make_tree: TreeFactory) -> None:
    """A stream's words depend on its address, not on when the tree reached it.

    Materialising the same path first, last, or after a hundred others must give the same
    words. A tree that derived its streams by spawning them from a parent in traversal
    order would fail this the moment two runs traversed differently.
    """
    root = telco_root()
    target = st_mod.subject_trait_path(WORLD_ID, 3, "monthlySpend")

    alone = first_words(make_tree(root), target)

    crowded_tree = make_tree(root)
    for ordinal in range(100):
        first_words(crowded_tree, st_mod.market_path(WORLD_ID, ordinal), count=1)
    crowded = first_words(crowded_tree, target)

    reversed_tree = make_tree(root)
    for ordinal in reversed(range(100)):
        first_words(reversed_tree, st_mod.market_path(WORLD_ID, ordinal), count=1)
    reversed_order = first_words(reversed_tree, target)

    assert alone == crowded == reversed_order


def assert_insertion_stability(make_tree: TreeFactory) -> None:
    """§3: a new consumer on a new path changes no existing stream.

    The "new consumer" here is every other row of the canonical table: a tree that has
    materialised all of them must still give the original words for the original path.
    """
    root = telco_root()
    existing = [
        st_mod.market_path(WORLD_ID, 0),
        st_mod.subject_trait_path(WORLD_ID, 0, "monthlySpend"),
        st_mod.signal_path(SIM_ID, "sig-payment-aborted", 0, 0),
    ]
    before = {path: first_words(make_tree(root), path) for path in existing}

    newcomers = [
        st_mod.world_cohorts_path(WORLD_ID),
        st_mod.subject_products_path(WORLD_ID, 0),
        st_mod.journey_path(SIM_ID, "jr-payment-recovery", 0, 0),
        st_mod.campaign_path(SIM_ID, "cam-payment-recovery", 0),
        st_mod.temperature_path(SIM_ID, "engagement", 0, 0),
        st_mod.agentic_path(SIM_ID, "journey-improviser", 0, 0),
    ]
    after_tree = make_tree(root)
    for path in newcomers:
        first_words(after_tree, path, count=2)
    after = {path: first_words(after_tree, path) for path in existing}

    assert before == after


@pytest.mark.unit
def test_streams_are_addressed_by_path() -> None:
    assert_streams_are_path_addressed(lambda root: st_mod.SeedTree(root=root))


@pytest.mark.unit
def test_adding_a_consumer_changes_no_existing_stream() -> None:
    assert_insertion_stability(lambda root: st_mod.SeedTree(root=root))


@pytest.mark.property
@given(seed=st.integers(min_value=-1000, max_value=1000))
def test_the_same_path_under_two_seeds_gives_two_streams(seed: int) -> None:
    """Seed sensitivity, at the level of the drawn words rather than the root digest."""
    seam = st_mod.seam_bytes([CONTRACT_DIGEST])
    path = st_mod.subject_trait_path(WORLD_ID, 0, "monthlySpend")
    words = first_words(st_mod.SeedTree(root=st_mod.derive_root(seam, seed)), path, count=2)
    other = first_words(st_mod.SeedTree(root=st_mod.derive_root(seam, seed + 1)), path, count=2)
    assert words != other


@pytest.mark.unit
def test_a_repeated_request_continues_one_stream_rather_than_restarting_it() -> None:
    tree = st_mod.SeedTree(root=telco_root())
    path = st_mod.market_path(WORLD_ID, 0)
    first = tree.stream(path)
    words = [first.next_raw(), first.next_raw()]
    again = tree.stream(path)
    assert again is first
    assert again.next_raw() not in words
    assert again.position == 3


@pytest.mark.unit
def test_the_tree_records_every_path_it_materialised_in_sorted_order() -> None:
    """Sorted by the line each path is written as, which is the order the file is in."""
    tree = st_mod.SeedTree(root=telco_root())
    for ordinal in (5, 1, 3):
        tree.stream(st_mod.market_path(WORLD_ID, ordinal))
    recorded = tree.materialised_paths
    assert len(recorded) == 3
    assert list(recorded) == sorted(recorded, key=st_mod.render_seed_path)
    assert tree.seed_paths_bytes() == st_mod.seed_paths_bytes(recorded)


@pytest.mark.unit
def test_the_seed_tree_digest_is_over_the_set_of_paths_not_their_order() -> None:
    paths = [st_mod.market_path(WORLD_ID, ordinal) for ordinal in range(5)]
    assert st_mod.seed_tree_digest(paths) == st_mod.seed_tree_digest(list(reversed(paths)))
    assert st_mod.seed_tree_digest(paths) == st_mod.seed_tree_digest(paths + paths)
    assert st_mod.seed_tree_digest(paths) != st_mod.seed_tree_digest(paths[:-1])
    assert st_mod.seed_tree_digest(paths).startswith("sha256:")


@pytest.mark.unit
def test_a_part_carrying_a_separator_is_refused_rather_than_silently_merged() -> None:
    """Without this refusal two distinct paths would share one label -- and one stream."""
    collide_one = ("world", "a\x1fb", "cohorts")
    collide_two = ("world", "a", "b", "cohorts")
    with pytest.raises(FluxError) as caught:
        st_mod.path_label(collide_one)
    assert caught.value.code.code == "FLX-2202"
    # The path the collision would have been with is still perfectly addressable.
    assert st_mod.path_label(collide_two)
    with pytest.raises(FluxError):
        st_mod.path_label(("world", "a\x1eb", "cohorts"))
    with pytest.raises(FluxError):
        st_mod.seed_tree_digest([collide_one])


# ---------------------------------------------------------------------------
# FLX-2201
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_path_outside_the_canonical_table_stops_the_run() -> None:
    tree = st_mod.SeedTree(root=telco_root())
    with pytest.raises(FluxError) as caught:
        tree.stream(("world", WORLD_ID, "cohort"))
    assert caught.value.code.code == "FLX-2201"


@pytest.mark.unit
def test_a_path_the_plan_did_not_anticipate_stops_the_run() -> None:
    planned = [st_mod.market_path(WORLD_ID, 0)]
    tree = st_mod.SeedTree.for_plan(telco_root(), planned)
    assert tree.stream(planned[0]) is not None
    with pytest.raises(FluxError) as caught:
        tree.stream(st_mod.market_path(WORLD_ID, 1))
    assert caught.value.code.code == "FLX-2201"
    assert "planned path set" in str(caught.value)


@pytest.mark.unit
def test_a_planning_tree_accepts_every_canonical_path() -> None:
    """With no planned set, the tree is discovering one -- that is what plan time is for."""
    tree = st_mod.SeedTree(root=telco_root())
    tree.stream(st_mod.market_path(WORLD_ID, 999))
    assert tree.digest().startswith("sha256:")


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def assert_identifiers_are_reproducible(
    make_identifier: Callable[[st_mod.SeedTreeRoot, Sequence[object]], str],
) -> None:
    """An id is a function of the root and the path, and of nothing else."""
    root = telco_root()
    paths = [st_mod.signal_path(SIM_ID, "sig-payment-aborted", ordinal, 0) for ordinal in range(8)]
    first = [make_identifier(root, path) for path in paths]
    second = [make_identifier(root, path) for path in paths]
    assert first == second
    assert len(set(first)) == len(first)


@pytest.mark.unit
def test_identifiers_are_derived_not_drawn() -> None:
    assert_identifiers_are_reproducible(st_mod.derive_identifier)


@pytest.mark.unit
def test_identifiers_move_with_the_contract_and_the_seed() -> None:
    root = telco_root()
    path = st_mod.signal_path(SIM_ID, "sig-payment-aborted", 0, 0)
    assert st_mod.derive_identifier(root, path) != st_mod.derive_identifier(
        root.reseed(root.effective_seed + 1), path
    )
    assert len(st_mod.derive_identifier(root, path, length=8)) == 8
    with pytest.raises(FluxError):
        st_mod.derive_identifier(root, path, length=0)


# ---------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_root_derivation_vectors_are_reproduced() -> None:
    for vector in VECTOR_DOCUMENT["rootDerivation"]:  # type: ignore[union-attr]
        seam = st_mod.seam_bytes(vector["emitDigests"])
        assert seam.hex() == vector["hBytesHex"], vector["name"]
        root = st_mod.derive_root(seam, vector["effectiveSeed"])
        assert root.hex == vector["rootHex"], vector["name"]


@pytest.mark.golden
def test_stream_vectors_are_reproduced() -> None:
    root = telco_root()
    assert root.hex == VECTOR_DOCUMENT["rootDerivation"][0]["rootHex"]  # type: ignore[index]
    for vector in VECTOR_DOCUMENT["streams"]:  # type: ignore[union-attr]
        path = tuple(vector["path"])
        shape = st_mod.match_shape(path)
        assert shape is not None and shape.name == vector["shape"]
        assert st_mod.path_label(path).hex() == vector["labelHex"]
        assert str(st_mod.stream_entropy(root, path)) == vector["entropy"]
        words = first_words(st_mod.SeedTree(root=root), path)
        assert [str(word) for word in words] == vector["firstRawWords"], path


@pytest.mark.golden
def test_the_re_rooted_stream_vectors_are_reproduced() -> None:
    root = telco_root()
    roots = {
        "experiment.assignment": root.reseed(ASSIGNMENT_SEED),
        "sweep.run": root.reseed(st_mod.sweep_effective_seed(root.effective_seed, 3)),
    }
    for vector in VECTOR_DOCUMENT["reRootedStreams"]:  # type: ignore[union-attr]
        this_root = roots[vector["shape"]]
        assert this_root.hex == vector["rootHex"]
        path = tuple(vector["path"])
        assert st_mod.path_label(path).hex() == vector["labelHex"]
        words = first_words(st_mod.SeedTree(root=this_root), path)
        assert [str(word) for word in words] == vector["firstRawWords"], path


@pytest.mark.golden
def test_the_seed_tree_digest_vector_is_reproduced() -> None:
    paths = [tuple(vector["path"]) for vector in VECTOR_DOCUMENT["streams"]]  # type: ignore
    assert st_mod.seed_tree_digest(paths) == VECTOR_DOCUMENT["seedTreeDigest"]


@pytest.mark.golden
def test_the_seed_paths_file_vector_is_reproduced_line_for_line() -> None:
    """The file beside the plan is a wire format, so its bytes are pinned, not just its digest."""
    vector = VECTOR_DOCUMENT["seedPathsFile"]
    assert isinstance(vector, dict)
    paths = [tuple(path) for path in vector["paths"]]
    data = st_mod.seed_paths_bytes(paths)
    assert data.decode("utf-8").splitlines() == vector["lines"]
    assert st_mod.seed_paths_digest(data) == VECTOR_DOCUMENT["seedTreeDigest"]
    assert st_mod.parse_seed_paths(data) == tuple(sorted(paths, key=st_mod.render_seed_path))


@pytest.mark.golden
def test_the_vector_file_covers_every_canonical_shape() -> None:
    covered = {vector["shape"] for vector in VECTOR_DOCUMENT["streams"]}  # type: ignore
    covered |= {vector["shape"] for vector in VECTOR_DOCUMENT["reRootedStreams"]}  # type: ignore
    assert covered == {shape.name for shape in st_mod.CANONICAL_PATH_SHAPES}


@pytest.mark.golden
def test_the_vector_file_declares_the_version_it_pins() -> None:
    assert VECTOR_DOCUMENT["seedTreeVersion"] == st_mod.SEED_TREE_VERSION == "flux-seed/1"


# ---------------------------------------------------------------------------
# Determinism of the whole tree
# ---------------------------------------------------------------------------


@pytest.mark.property
@settings(max_examples=20)
@given(ordinals=st.lists(st.integers(min_value=0, max_value=200), min_size=1, max_size=12))
def test_two_trees_over_one_root_draw_one_sequence(ordinals: List[int]) -> None:
    root = telco_root()

    def sequence(order: List[int]) -> Dict[int, List[int]]:
        tree = st_mod.SeedTree(root=root)
        return {
            ordinal: first_words(tree, st_mod.market_path(WORLD_ID, ordinal), count=2)
            for ordinal in order
        }

    forward = sequence(ordinals)
    backward = sequence(list(reversed(ordinals)))
    assert forward == backward


@pytest.mark.unit
def test_the_tree_hands_out_draw_streams() -> None:
    stream = st_mod.SeedTree(root=telco_root()).stream(st_mod.market_path(WORLD_ID, 0))
    assert isinstance(stream, DrawStream)
    assert stream.position == 0


@pytest.mark.unit
def test_an_empty_h_bytes_has_no_root() -> None:
    with pytest.raises(FluxError) as caught:
        st_mod.derive_root(b"", 42)
    assert caught.value.code.code == "FLX-2202"


@pytest.mark.unit
def test_a_name_position_holding_a_number_matches_no_shape() -> None:
    """A path is matched on the kind of every part, not only on its length."""
    assert st_mod.match_shape(("world", 5, "cohorts")) is None
    assert st_mod.match_shape(("world", "", "cohorts")) is None
    assert st_mod.match_shape(("world", "w", "cohorts")) is not None


@pytest.mark.unit
def test_entropy_can_be_read_without_materialising_a_stream() -> None:
    tree = st_mod.SeedTree(root=telco_root())
    path = st_mod.market_path(WORLD_ID, 0)
    entropy = tree.entropy(path)
    assert entropy == st_mod.stream_entropy(telco_root(), path)
    assert tree.materialised_paths == ()  # reading the entropy is not touching the path
