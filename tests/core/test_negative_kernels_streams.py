# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The broken kernels of KERNEL-SPEC §14 that live in the streams area.

§14 names six deliberately broken kernels and requires that each "MUST fail the property
suite". Four of them are defects of the modules covered here:

======================================  ====================================================
Broken kernel                           The property that catches it
======================================  ====================================================
spawn-ordered seed tree                 ``test_seedtree.assert_streams_are_path_addressed``
                                        and ``assert_insertion_stability``
float-accumulating LRM                  ``test_lrm.assert_lrm_contract``
set-iterating scheduler                 ``test_clock.assert_pops_in_key_order``
uuid4 event id                          ``test_seedtree.assert_identifiers_are_reproducible``
======================================  ====================================================

The remaining two -- a wall-clock timestamp in a traced op, and a sorted-key (object) row
serialiser -- are defects of ``core/trace.py`` and ``seam/emit.py``, and belong with those
modules' own suites.

Every case here is stated twice: the property **passes** for the real kernel and **fails**
for the broken one. A negative test that only showed the failure would not distinguish a
property that catches the defect from a property that fails for everything.
"""

from __future__ import annotations

import math
import uuid
from decimal import Decimal
from typing import Any, Callable, Dict, Mapping, Sequence, Set, Tuple

import pytest
from numpy.random import PCG64, Generator, SeedSequence

from flux_engine.core.canonical import nfc
from flux_engine.core.clock import Phase, ScheduledItem, Scheduler
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.rng import DrawStream
from flux_engine.core.seedtree import SeedPath, SeedTree, SeedTreeRoot
from tests.core.test_clock import assert_pops_in_key_order
from tests.core.test_lrm import TELCO_LIFECYCLE_MIX, TELCO_POPULATION, assert_lrm_contract

# Package-qualified, never the bare module name: the bare spelling works
# only because pytest puts each non-package test directory on sys.path,
# so `from test_clock import ...` resolves by collection accident and any
# other directory adding a module of that name shadows it.
from tests.core.test_negative_kernels import CORE_NEGATIVE_KERNELS, NegativeKernel
from tests.core.test_negative_kernels import CORRECT_KERNELS as CORE_CORRECT_KERNELS
from tests.core.test_seedtree import (
    assert_identifiers_are_reproducible,
    assert_insertion_stability,
    assert_streams_are_path_addressed,
    telco_root,
)

# ---------------------------------------------------------------------------
# 1. Spawn-ordered seed tree
# ---------------------------------------------------------------------------


class SpawnOrderedSeedTree(SeedTree):
    """**BROKEN.** Derives each stream by spawning the next child of one parent sequence.

    The defect KERNEL-SPEC §3 forbids in one line: "Stream derivation is by **path**, never
    by spawn order." This tree is perfectly reproducible for a fixed traversal -- which is
    why the defect survives a naive determinism test -- but a stream's words now depend on
    how many streams were materialised before it, so adding one consumer moves every stream
    that comes after it.
    """

    def __init__(self, root: SeedTreeRoot) -> None:
        super().__init__(root=root)
        self._parent = SeedSequence(root.value)

    def _new_stream(self, path: SeedPath, label: bytes) -> DrawStream:
        child = self._parent.spawn(1)[0]
        return DrawStream(Generator(PCG64(child)), label=label.decode("utf-8", "replace"))


@pytest.mark.unit
def test_the_real_tree_is_path_addressed() -> None:
    assert_streams_are_path_addressed(lambda root: SeedTree(root=root))
    assert_insertion_stability(lambda root: SeedTree(root=root))


@pytest.mark.unit
def test_a_spawn_ordered_tree_fails_the_path_addressing_property() -> None:
    with pytest.raises(AssertionError):
        assert_streams_are_path_addressed(SpawnOrderedSeedTree)


@pytest.mark.unit
def test_a_spawn_ordered_tree_fails_the_insertion_stability_property() -> None:
    with pytest.raises(AssertionError):
        assert_insertion_stability(SpawnOrderedSeedTree)


@pytest.mark.unit
def test_the_spawn_ordered_tree_is_broken_in_the_way_the_docstring_claims() -> None:
    """It is deterministic for one traversal -- which is exactly why it needs a property."""
    root = telco_root()
    from flux_engine.core.seedtree import market_path

    def traverse(order: Sequence[int]) -> Dict[int, int]:
        """The word each subject ordinal ends up with, under one traversal order."""
        tree = SpawnOrderedSeedTree(root)
        return {i: tree.stream(market_path("q3-retention-world", i)).next_raw() for i in order}

    # Reproducible for one traversal -- which is why a naive determinism test passes it.
    assert traverse([0, 1, 2]) == traverse([0, 1, 2])
    # And wrong across two: subject 0 gets whichever child the traversal reached first.
    assert traverse([0, 1, 2]) != traverse([2, 1, 0])


# ---------------------------------------------------------------------------
# 2. Float-accumulating LRM
# ---------------------------------------------------------------------------


def float_largest_remainder(n: int, weights: Mapping[str, Decimal]) -> Dict[str, int]:
    """**BROKEN.** KERNEL-SPEC §5's algorithm with ``float`` where it says exact rationals.

    Structurally identical to the real one -- floors, residual to the largest fractional
    parts, NFC tie-break -- and different in one place: ``n * float(weight)`` instead of
    ``n * Fraction(weight)``. That is enough. ``50 * 0.29`` is ``14.499999999999998`` in
    binary floating point rather than exactly ``14.5``, so a fractional part that should
    have tied loses, and a subject moves between cohorts.
    """
    floors: Dict[str, int] = {}
    fractional: Dict[str, float] = {}
    for key, weight in weights.items():
        scaled = n * float(weight)
        whole = math.floor(scaled)
        floors[key] = whole
        fractional[key] = scaled - whole
    residual = n - sum(floors.values())
    order = sorted(fractional.items(), key=lambda item: (-item[1], nfc(item[0]), item[0]))
    for key, _ in order[:residual]:
        floors[key] += 1
    return {key: floors[key] for key in weights}


#: The input that separates the two. Found by exhausting the two-decimal weight grid for
#: populations up to 600, not chosen by hand -- and it is a real declaration shape: two
#: shares of a population, as ``experiment.flux.yml`` declares its variants.
_FLOAT_LRM_SEPARATOR = (50, {"alpha": Decimal("0.29"), "beta": Decimal("0.71")})


@pytest.mark.unit
def test_the_real_lrm_satisfies_the_contract_on_the_separating_input() -> None:
    n, weights = _FLOAT_LRM_SEPARATOR
    assert_lrm_contract(largest_remainder, n, weights)
    assert_lrm_contract(largest_remainder, TELCO_POPULATION + 1, TELCO_LIFECYCLE_MIX)


@pytest.mark.unit
def test_a_float_accumulating_lrm_fails_the_contract() -> None:
    n, weights = _FLOAT_LRM_SEPARATOR
    with pytest.raises(AssertionError):
        assert_lrm_contract(float_largest_remainder, n, weights)


@pytest.mark.unit
def test_the_float_lrm_moves_a_subject_between_two_declared_cohorts() -> None:
    """Named precisely, so the failure is a fact about counts and not about a tolerance."""
    n, weights = _FLOAT_LRM_SEPARATOR
    exact = largest_remainder(n, weights)
    broken = float_largest_remainder(n, weights)
    assert sum(exact.values()) == sum(broken.values()) == n  # the sum alone hides it
    assert exact != broken
    assert exact["alpha"] - broken["alpha"] == 1


# ---------------------------------------------------------------------------
# 3. Set-iterating scheduler
# ---------------------------------------------------------------------------


class SetIteratingScheduler(Scheduler[str]):
    """**BROKEN.** A queue backed by a ``set`` instead of a binary heap over the key.

    KERNEL-SPEC §7: "The queue is a binary heap over that key". A set returns whatever its
    hash table reaches first, so the run's execution order becomes a function of the
    payloads' hashes and of the insertion history -- and, because string hashing is seeded
    per process, of the process itself.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pool: Set[ScheduledItem[str]] = set()

    def _store(self, item: ScheduledItem[str]) -> None:
        # Appended, not heap-pushed, so the inherited length bookkeeping still works.
        self._heap.append((item.key.as_tuple(), item))
        self._pool.add(item)

    def _take(self) -> ScheduledItem[str]:
        item = next(iter(self._pool))
        self._pool.discard(item)
        self._heap.pop()
        return item


_INSERTION_ORDER = list(range(54))


@pytest.mark.unit
def test_the_real_scheduler_pops_in_key_order() -> None:
    assert_pops_in_key_order(lambda: Scheduler(), _INSERTION_ORDER)
    assert_pops_in_key_order(lambda: Scheduler(), list(reversed(_INSERTION_ORDER)))


@pytest.mark.unit
def test_a_set_iterating_scheduler_fails_the_ordering_property() -> None:
    # Precondition: with this many items a set's iteration order is not the sorted order.
    # If this ever held, the test below would be vacuous, so it is asserted rather than
    # assumed -- a set that happened to iterate in order would fail here, loudly.
    scheduler = SetIteratingScheduler()
    for index in _INSERTION_ORDER:
        scheduler.schedule(
            t_ms=index * 1000,
            phase=Phase.SIGNAL,
            entity_ordinal=index % 5,
            stream_ordinal=index % 3,
            payload=f"item-{index}",
        )
    pooled = [item.key.as_tuple() for item in scheduler._pool]
    assert pooled != sorted(pooled), "the set happened to iterate in key order"

    with pytest.raises((AssertionError, FluxError)):
        assert_pops_in_key_order(SetIteratingScheduler, _INSERTION_ORDER)


@pytest.mark.unit
def test_the_real_scheduler_refuses_to_return_a_key_out_of_order() -> None:
    """The heap is the mechanism; this is the check that the mechanism is working."""
    scheduler = SetIteratingScheduler()
    for index in _INSERTION_ORDER:
        scheduler.schedule(
            t_ms=index * 1000,
            phase=Phase.EMIT,
            entity_ordinal=0,
            stream_ordinal=0,
            payload=f"item-{index}",
        )
    with pytest.raises(FluxError) as caught:
        while True:
            scheduler.pop()
    assert caught.value.code.code == "FLX-5022"


# ---------------------------------------------------------------------------
# 4. uuid4 event id
# ---------------------------------------------------------------------------


def uuid4_identifier(root: SeedTreeRoot, path: Sequence[object]) -> str:
    """**BROKEN.** An event id drawn from the operating system instead of from the tree.

    SEAM-FIDELITY-SPEC §1 requires identifiers "derived deterministically from
    ``(H, ordinal)`` -- never random", and KERNEL-SPEC §14 names ``uuid4`` as one of the six
    ways a kernel silently stops being reproducible. Every field of a run can be correct
    and every golden still fails, because the ids differ on every run and there is no seed
    anywhere that would bring them back.
    """
    return uuid.uuid4().hex


@pytest.mark.unit
def test_the_real_identifier_is_derived_from_the_tree() -> None:
    from flux_engine.core.seedtree import derive_identifier

    assert_identifiers_are_reproducible(derive_identifier)


@pytest.mark.unit
def test_a_uuid4_identifier_fails_the_reproducibility_property() -> None:
    with pytest.raises(AssertionError):
        assert_identifiers_are_reproducible(uuid4_identifier)


@pytest.mark.unit
def test_the_uuid4_identifier_takes_no_input_from_the_run_at_all() -> None:
    """Not merely different between runs: unrelated to the contract and the seed."""
    root = telco_root()
    path = ("world", "q3-retention-world", "cohorts")
    assert uuid4_identifier(root, path) != uuid4_identifier(root, path)

    from flux_engine.core.seedtree import derive_identifier

    assert derive_identifier(root, path) == derive_identifier(root, path)
    assert derive_identifier(root, path) != derive_identifier(root.reseed(43), path)


# ---------------------------------------------------------------------------
# 5. The registry, and the totality of KERNEL-SPEC §14
# ---------------------------------------------------------------------------
#
# Every one of the four kernels above is implemented and paired with a
# control, but until now nothing asserted that *four* were here, or that
# the two in ``test_negative_kernels.py`` plus these four are §14's six.
# The mapping lived in two module docstrings, so a §14 kernel whose
# registration was dropped in a refactor -- or a seventh that was never
# added -- was silently unproven. The repository already uses this
# totality idiom for the receipt fields, the gate's refusals and the
# ``explain`` catalog; §14 gets the same treatment.


def _holds(check: Callable[..., None], *arguments: Any) -> bool:
    """Run an ``assert_...`` property helper and report whether it held.

    The property helpers signal a failed property by raising -- either an
    ``AssertionError`` or, where the kernel refuses at its own boundary
    (the scheduler's ``FLX-5022``), a ``FluxError``. Both spellings are
    the property not holding, which is exactly what the per-kernel tests
    above assert with ``pytest.raises((AssertionError, FluxError))``. The
    registry needs the same fact as a boolean.
    """
    try:
        check(*arguments)
    except (AssertionError, FluxError):
        return False
    return True


STREAM_NEGATIVE_KERNELS: Tuple[NegativeKernel, ...] = (
    NegativeKernel(
        name="spawn-ordered-seed-tree",
        defect="streams derived by spawn order, so adding a consumer moves every later stream",
        spec="KERNEL-SPEC §3",
        property_name="streams are addressed by path, and insertion is stable",
        holds=lambda: _holds(assert_streams_are_path_addressed, SpawnOrderedSeedTree)
        and _holds(assert_insertion_stability, SpawnOrderedSeedTree),
    ),
    NegativeKernel(
        name="float-accumulating-lrm",
        defect="cohort sizes accumulated in binary floating point rather than on exact rationals",
        spec="KERNEL-SPEC §5",
        property_name="the largest-remainder contract holds for the separating input",
        holds=lambda: _holds(assert_lrm_contract, float_largest_remainder, *_FLOAT_LRM_SEPARATOR),
    ),
    NegativeKernel(
        name="set-iterating-scheduler",
        defect="the queue is backed by a set, so pop order is storage order and not key order",
        spec="KERNEL-SPEC §7",
        property_name="every pop is greater than the one before it, for any insertion order",
        holds=lambda: _holds(assert_pops_in_key_order, SetIteratingScheduler, _INSERTION_ORDER),
    ),
    NegativeKernel(
        name="uuid4-event-id",
        defect="event ids drawn from uuid4, so they are a function of nothing in the run",
        spec="KERNEL-SPEC §3",
        property_name="an id is a function of the root and the path, and of nothing else",
        holds=lambda: _holds(assert_identifiers_are_reproducible, uuid4_identifier),
    ),
)

STREAM_CORRECT_KERNELS: Tuple[NegativeKernel, ...] = (
    NegativeKernel(
        name="path-addressed-seed-tree",
        defect="none -- the control for the seed tree",
        spec="KERNEL-SPEC §3",
        property_name="streams are addressed by path, and insertion is stable",
        holds=lambda: _holds(assert_streams_are_path_addressed, lambda root: SeedTree(root=root))
        and _holds(assert_insertion_stability, lambda root: SeedTree(root=root)),
    ),
    NegativeKernel(
        name="exact-rational-lrm",
        defect="none -- the control for the LRM",
        spec="KERNEL-SPEC §5",
        property_name="the largest-remainder contract holds for the separating input",
        holds=lambda: _holds(assert_lrm_contract, largest_remainder, *_FLOAT_LRM_SEPARATOR),
    ),
    NegativeKernel(
        name="heap-scheduler",
        defect="none -- the control for the scheduler",
        spec="KERNEL-SPEC §7",
        property_name="every pop is greater than the one before it, for any insertion order",
        holds=lambda: _holds(assert_pops_in_key_order, lambda: Scheduler(), _INSERTION_ORDER),
    ),
    NegativeKernel(
        name="tree-derived-event-id",
        defect="none -- the control for the event id",
        spec="KERNEL-SPEC §3",
        property_name="an id is a function of the root and the path, and of nothing else",
        holds=lambda: _holds(assert_identifiers_are_reproducible, _derive_identifier()),
    ),
)

#: KERNEL-SPEC §14's closed list, as the six slugs the two registries use.
#: Left column is the slug, right is §14's own wording:
#:
#: * ``spawn-ordered-seed-tree``          -- "spawn-ordered seed tree"
#: * ``float-accumulating-lrm``           -- "float-accumulating LRM"
#: * ``set-iterating-scheduler``          -- "set-iterating scheduler"
#: * ``uuid4-event-id``                   -- "uuid4 event id"
#: * ``wall-clock-field-in-a-traced-op``  -- "wall-clock timestamp in a traced op"
#: * ``sorted-key-object-row-serialiser`` -- "sorted-key (object) row serialiser"
KERNEL_SPEC_14_KERNELS = frozenset(
    {
        "spawn-ordered-seed-tree",
        "float-accumulating-lrm",
        "set-iterating-scheduler",
        "uuid4-event-id",
        "wall-clock-field-in-a-traced-op",
        "sorted-key-object-row-serialiser",
    }
)


def _derive_identifier() -> Callable[..., str]:
    from flux_engine.core.seedtree import derive_identifier

    return derive_identifier


@pytest.mark.property
@pytest.mark.parametrize(
    "kernel", STREAM_NEGATIVE_KERNELS, ids=[kernel.name for kernel in STREAM_NEGATIVE_KERNELS]
)
def test_the_broken_kernel_fails_its_property(kernel: NegativeKernel) -> None:
    """Each §14 kernel in this area MUST fail the property it breaks."""
    assert not kernel.holds(), (
        f"{kernel.name} ({kernel.spec}) carries the defect {kernel.defect!r} but the "
        f"property {kernel.property_name!r} still holds: the property has stopped "
        "detecting it"
    )


@pytest.mark.property
@pytest.mark.parametrize(
    "kernel", STREAM_CORRECT_KERNELS, ids=[kernel.name for kernel in STREAM_CORRECT_KERNELS]
)
def test_the_correct_kernel_passes_the_same_property(kernel: NegativeKernel) -> None:
    """The control: a property nothing can pass would detect nothing."""
    assert kernel.holds(), f"{kernel.name} ({kernel.spec}) fails {kernel.property_name!r}"


@pytest.mark.unit
def test_the_two_registries_are_exactly_the_six_kernels_the_spec_names() -> None:
    """§14 is a closed list, so the registries must cover it exactly.

    Not "at least": a seventh entry is a kernel the specification does not
    name, and a missing one is a defect class nothing is proving is
    caught.
    """
    registered = {kernel.name for kernel in (*CORE_NEGATIVE_KERNELS, *STREAM_NEGATIVE_KERNELS)}
    assert registered == KERNEL_SPEC_14_KERNELS


@pytest.mark.unit
def test_every_registered_kernel_has_a_control_over_the_same_property() -> None:
    """A broken kernel with no passing control proves nothing about the property."""
    for broken, correct in (
        (CORE_NEGATIVE_KERNELS, CORE_CORRECT_KERNELS),
        (STREAM_NEGATIVE_KERNELS, STREAM_CORRECT_KERNELS),
    ):
        assert {kernel.property_name for kernel in broken} == {
            kernel.property_name for kernel in correct
        }
