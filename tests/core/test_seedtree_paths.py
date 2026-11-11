# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §3's path list: ``seed-paths.ndjson``, its digest, and the count table.

§3 keeps the addresses a run materialises *beside* the plan rather than inside it: "one
canonical path per line; raw-bytes digest = ``seedTreeDigest``", with ``plan.json`` carrying
only that digest and a per-shape count table. Three properties follow, and this module pins
each of them directly rather than through a plan:

* the bytes are a pure function of the **set** of paths -- order, repetition and the shape
  of the caller's iterable cannot reach them;
* the digest of those bytes is a plain ``sha256`` of the file, so a reviewer with
  ``sha256sum`` and no engine can check a plan's ``seedTreeDigest``;
* the round trip is exact -- what :func:`parse_seed_paths` reads back is what
  ``FLX-2201`` will check a run's streams against, so a path that does not survive the
  round trip would be a stream the plan silently forbade.

``tests/stages/test_plan_apply.py`` covers the same rules through ``fluxsim plan``; these tests
own the format itself.
"""

from __future__ import annotations

from typing import List, Sequence, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core import seedtree as st_mod
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

WORLD_ID = "q3-retention-world"
SIM_ID = "sim-payment-recovery"


def sample_paths() -> List[st_mod.SeedPath]:
    """A spread over the canonical table, deliberately not in sorted order."""
    return [
        st_mod.subject_trait_path(WORLD_ID, 2, "arpu"),
        st_mod.market_path(WORLD_ID, 10),
        st_mod.market_path(WORLD_ID, 2),
        st_mod.world_cohorts_path(WORLD_ID),
        st_mod.signal_path(SIM_ID, "sig-payment-aborted", 2, 0),
        st_mod.subject_products_path(WORLD_ID, 2),
        st_mod.campaign_path(SIM_ID, "cam-payment-recovery", 2),
    ]


# ---------------------------------------------------------------------------
# One line per path
# ---------------------------------------------------------------------------


def test_a_line_is_the_canonical_json_of_the_path_array() -> None:
    assert st_mod.render_seed_path(("world", WORLD_ID, "market", 0)) == (
        b'["world","q3-retention-world","market",0]'
    )


def test_an_ordinal_stays_a_json_integer_and_a_name_stays_a_string() -> None:
    """The two are different paths, and the file must not spell them the same way."""
    as_int = st_mod.render_seed_path(("sweep", 3))
    as_text = st_mod.render_seed_path(("sweep", "3"))
    assert as_int == b'["sweep",3]'
    assert as_text == b'["sweep","3"]'
    assert as_int != as_text


def test_a_part_carrying_a_separator_is_refused_by_the_renderer_too() -> None:
    """The file records stream addresses; a part that cannot be labelled is not one."""
    with pytest.raises(FluxError) as caught:
        st_mod.render_seed_path(("world", "a\x1fb", "cohorts"))
    assert caught.value.code.code == "FLX-2202"


def test_a_line_round_trips_through_the_parser() -> None:
    for path in sample_paths():
        assert st_mod.parse_seed_path(st_mod.render_seed_path(path)) == path


@pytest.mark.parametrize(
    "line",
    ['{"world": 1}', "[]", "not json", '["world", 1.5]', '["world", true]', '["world", null]'],
)
def test_a_line_that_is_not_a_path_is_refused(line: str) -> None:
    with pytest.raises(FluxError) as caught:
        st_mod.parse_seed_path(line)
    assert caught.value.code.code == "FLX-2202"


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_the_file_is_sorted_and_newline_terminated() -> None:
    data = st_mod.seed_paths_bytes(sample_paths())
    lines = data.decode("utf-8").splitlines()
    assert data.endswith(b"\n")
    assert len(lines) == len(sample_paths())
    assert lines == sorted(lines)


def test_the_bytes_are_a_function_of_the_set_not_of_the_order_or_the_repetition() -> None:
    paths = sample_paths()
    once = st_mod.seed_paths_bytes(paths)
    assert st_mod.seed_paths_bytes(reversed(paths)) == once
    assert st_mod.seed_paths_bytes(paths + paths) == once
    assert st_mod.seed_paths_bytes(paths[:-1]) != once


def test_two_paths_sharing_one_label_contribute_one_line() -> None:
    """``("sweep", 1)`` and ``("sweep", "1")`` address one stream, so the file has one line.

    The line kept is the lexicographically first of the two, not the first one recorded --
    a tie-break by arrival order would make the bytes depend on the order the run happened
    to reach the two spellings, which is exactly what this file must not depend on.
    """
    both = [("sweep", 1), ("sweep", "1")]
    data = st_mod.seed_paths_bytes(both)
    assert data.decode("utf-8").splitlines() == ['["sweep","1"]']
    assert st_mod.seed_paths_bytes(reversed(both)) == data


def test_the_digest_is_a_plain_sha256_of_the_file_bytes() -> None:
    """A reviewer with sha256sum and no engine can check a plan's seedTreeDigest."""
    import hashlib

    data = st_mod.seed_paths_bytes(sample_paths())
    assert st_mod.seed_paths_digest(data) == "sha256:" + hashlib.sha256(data).hexdigest()
    assert st_mod.seed_tree_digest(sample_paths()) == st_mod.seed_paths_digest(data)


def test_parsing_the_file_recovers_every_path() -> None:
    paths = sample_paths()
    recovered = st_mod.parse_seed_paths(st_mod.seed_paths_bytes(paths))
    assert set(recovered) == set(paths)
    assert st_mod.seed_paths_bytes(recovered) == st_mod.seed_paths_bytes(paths)


def test_a_blank_line_is_skipped_rather_than_read_as_a_path() -> None:
    data = st_mod.seed_paths_bytes(sample_paths())
    assert st_mod.parse_seed_paths(b"\n" + data + b"\n") == st_mod.parse_seed_paths(data)


# ---------------------------------------------------------------------------
# The count table
# ---------------------------------------------------------------------------


def test_the_count_table_is_keyed_by_canonical_shape_name_and_sorted() -> None:
    counts = st_mod.shape_counts(sample_paths())
    assert counts == {
        "sim.campaign": 1,
        "sim.signal": 1,
        "world.cohorts": 1,
        "world.market": 2,
        "world.subject.products": 1,
        "world.subject.trait": 1,
    }
    assert list(counts) == sorted(counts)
    assert sum(counts.values()) == len(sample_paths())
    assert set(counts) <= {shape.name for shape in st_mod.CANONICAL_PATH_SHAPES}


def test_a_path_outside_the_closed_table_is_refused_rather_than_counted() -> None:
    with pytest.raises(FluxError) as caught:
        st_mod.shape_counts([("world", WORLD_ID, "cohort")])
    assert caught.value.code.code == "FLX-2201"


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_recording_one_path_twice_records_it_once() -> None:
    ledger = st_mod.SeedPathLedger()
    first = ledger.record(st_mod.market_path(WORLD_ID, 0))
    second = ledger.record(st_mod.market_path(WORLD_ID, 0))
    assert first == second
    assert len(ledger) == 1
    assert first in ledger


def test_the_ledger_answers_the_same_digest_before_and_after_a_re_record() -> None:
    """The memoised view is invalidated on a real addition and only on one."""
    ledger = st_mod.SeedPathLedger()
    for path in sample_paths():
        ledger.record(path)
    digest = ledger.digest()
    assert ledger.digest() == digest
    ledger.record(sample_paths()[0])
    assert ledger.digest() == digest
    ledger.record(st_mod.market_path(WORLD_ID, 999))
    assert ledger.digest() != digest


def test_the_ledger_matches_the_free_functions_it_stands_for() -> None:
    ledger = st_mod.SeedPathLedger()
    for path in sample_paths():
        ledger.record(path)
    assert ledger.ndjson_bytes() == st_mod.seed_paths_bytes(sample_paths())
    assert ledger.digest() == st_mod.seed_tree_digest(sample_paths())
    assert ledger.shape_counts() == st_mod.shape_counts(sample_paths())
    assert ledger.paths == tuple(sorted(sample_paths(), key=st_mod.render_seed_path))


def test_a_tree_records_what_it_derives_and_nothing_it_only_priced() -> None:
    root = st_mod.derive_root(bytes(range(32)), 42)
    tree = st_mod.SeedTree(root=root)
    tree.entropy(st_mod.market_path(WORLD_ID, 7))
    assert tree.seed_paths_bytes() == b""
    assert tree.shape_counts() == {}
    tree.stream(st_mod.market_path(WORLD_ID, 7))
    assert tree.shape_counts() == {"world.market": 1}
    assert tree.digest() == st_mod.seed_tree_digest([st_mod.market_path(WORLD_ID, 7)])


# ---------------------------------------------------------------------------
# The round trip, over arbitrary path sets
# ---------------------------------------------------------------------------


@st.composite
def path_sets(draw: st.DrawFn) -> Sequence[Tuple[object, ...]]:
    ordinals = draw(st.lists(st.integers(min_value=0, max_value=5000), min_size=1, max_size=25))
    kind = draw(st.sampled_from(["market", "trait", "products"]))
    if kind == "market":
        return [st_mod.market_path(WORLD_ID, ordinal) for ordinal in ordinals]
    if kind == "products":
        return [st_mod.subject_products_path(WORLD_ID, ordinal) for ordinal in ordinals]
    return [st_mod.subject_trait_path(WORLD_ID, ordinal, "arpu") for ordinal in ordinals]


@pytest.mark.property
@settings(max_examples=30)
@given(paths=path_sets())
def test_writing_then_reading_a_path_set_preserves_it_and_its_digest(
    paths: Sequence[Tuple[object, ...]],
) -> None:
    data = st_mod.seed_paths_bytes(paths)
    recovered = st_mod.parse_seed_paths(data)
    assert set(recovered) == set(paths)
    assert st_mod.seed_paths_bytes(recovered) == data
    assert st_mod.seed_tree_digest(recovered) == st_mod.seed_paths_digest(data)
