# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's kernel 0.2.0 held to the vector files (KERNEL-SPEC-2 section 11).

``tests/kernel2/reference.py`` wrote ``tests/vectors/kernel2-*-vectors.json``. This module
reads them and asks the engine's own code for every value: :mod:`flux_engine.core.kernel2`,
the seed tree's addressing, the samplers of :mod:`flux_engine.core.rng` on a counter
stream, ``permute/2``, and the generator's identifiers, keys and cell streams. It imports
nothing from the reference, so the engine is held to the files and not to that module.
"""

from __future__ import annotations

import json
import struct
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from flux_engine.core import kernel2, rng
from flux_engine.core.errors import FluxError
from flux_engine.core.kernel2 import CounterStream
from flux_engine.core.permute import permute2
from flux_engine.core.seedtree import (
    PathShape,
    SeedTreeRoot,
    counter_stream,
    derive_identifier,
    derive_plugin_seed,
    derive_root,
    key_label,
    match_shape,
    path_key,
)
from flux_engine.generator.paths import GENERATOR_PATH_SHAPE, CellStreams, matches_generator_shape
from flux_engine.generator.relations import (
    ForeignKey,
    ParentView,
    RelationReader,
    _Owner,
    layout_key,
)
from flux_engine.generator.rows import identifier_value

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
STREAMS: Dict[str, Any] = json.loads(
    (VECTORS / "kernel2-streams-vectors.json").read_text(encoding="utf-8")
)
TRANSFORMS: Dict[str, Any] = json.loads(
    (VECTORS / "kernel2-transforms-vectors.json").read_text(encoding="utf-8")
)
SEAM_DIGEST = "sha256:" + STREAMS["seam"]


def _words(hexes: Sequence[str]) -> Tuple[int, ...]:
    return tuple(int(word, 16) for word in hexes)


def _bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def _from_bits(pattern: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(pattern))[0]
    return value


def _root() -> SeedTreeRoot:
    return derive_root(
        bytes.fromhex(STREAMS["seam"]),
        int(STREAMS["seed"]),
        seed_tree_version=kernel2.SEED_TREE_VERSION,
    )


def _shape(path: Sequence[Any]) -> PathShape:
    if matches_generator_shape(path):
        return GENERATOR_PATH_SHAPE
    shape = match_shape(path)
    assert shape is not None, path
    return shape


def _stream(path: Sequence[Any]) -> CounterStream:
    return counter_stream(_root(), tuple(path), _shape(path))


class _Fixed(CounterStream):
    """A counter stream whose every draw is one given uniform53: the samplers' rules alone."""

    __slots__ = ("_u53",)

    def __init__(self, u53: int) -> None:
        super().__init__((0, 0), (0, 0))
        self._u53 = u53

    def block(self, draw: int, attempt: int = 0) -> Tuple[int, int, int, int]:
        return self._u53 << 11, 0, 0, 0


def test_the_versions_are_kernel_0_2_0s() -> None:
    assert STREAMS["kernelVersion"] == TRANSFORMS["kernelVersion"] == "0.2.0"
    assert STREAMS["seedTreeVersion"] == kernel2.SEED_TREE_VERSION


@pytest.mark.parametrize("case", STREAMS["philox4x64_10"], ids=lambda case: case["name"])
def test_philox_matches_random123(case: Dict[str, Any]) -> None:
    key = _words(case["key"])
    assert kernel2.philox_block(_words(case["counter"]), (key[0], key[1])) == _words(case["block"])


def test_the_root_is_section_2s() -> None:
    root = _root()
    assert root.digest.hex() == STREAMS["root"]
    assert root.counter_based and root.reseed(int(STREAMS["seed"])) == root


@pytest.mark.parametrize("case", STREAMS["paths"], ids=lambda case: str(case["path"]))
def test_a_paths_key_counter_and_blocks(case: Dict[str, Any]) -> None:
    path = tuple(case["path"])
    label, ordinals = key_label(path, _shape(path))
    assert label.hex() == case["keyLabel"]
    assert ordinals == tuple(int(value) for value in case["ordinals"])
    assert path_key(_root(), label) == _words(case["key"])
    stream = _stream(path)
    for draw in case["draws"]:
        assert stream.block(draw["draw"], draw["attempt"]) == _words(draw["block"])
    # The samplers take one index per call: attempt 0 of draws 0, 1, 2, ... in turn.
    sampled = _stream(path)
    firsts = sorted(
        {
            draw["draw"]: int(draw["uniform53"]) for draw in case["draws"] if draw["attempt"] == 0
        }.items()
    )
    for index, (draw, u53) in enumerate(firsts):
        assert draw == index
        assert rng.uniform53(sampled) == u53
    assert sampled.position == len(firsts)


@pytest.mark.parametrize("case", STREAMS["bounded"], ids=lambda case: f"{case['n']}@{case['draw']}")
def test_bounded_draws(case: Dict[str, Any]) -> None:
    stream = _stream(case["path"])
    for _ in range(case["draw"]):
        rng.uniform53(stream)
    for attempt, word in enumerate(case["words"]):
        assert stream.block(case["draw"], attempt)[0] == int(word, 16)
    assert rng.bounded_uint(stream, int(case["n"])) == int(case["value"])
    assert stream.position == case["draw"] + 1


@pytest.mark.parametrize(
    "case", STREAMS["bernoulli"], ids=lambda case: f"{case['p']}@{case['uniform53']}"
)
def test_bernoulli_is_kernel_0_1_0s_rule(case: Dict[str, Any]) -> None:
    assert rng.bernoulli(_Fixed(int(case["uniform53"])), Decimal(case["p"])) is case["value"]


@pytest.mark.parametrize("case", STREAMS["categorical"], ids=lambda case: case["uniform53"])
def test_categorical_is_kernel_0_1_0s_rule(case: Dict[str, Any]) -> None:
    levels = {name: Decimal(weight) for name, weight in case["levels"].items()}
    assert rng.categorical(_Fixed(int(case["uniform53"])), levels) == case["value"]


@pytest.mark.parametrize("case", STREAMS["permutation"], ids=lambda case: str(case["n"]))
def test_permutation_takes_n_minus_one_indices(case: Dict[str, Any]) -> None:
    stream = _stream(case["path"])
    assert list(rng.permutation(stream, case["n"])) == case["items"]
    assert stream.position == max(case["n"] - 1, 0)


@pytest.mark.parametrize("case", STREAMS["poisson"], ids=lambda case: case["mean"])
def test_poisson_thresholds_and_draws(case: Dict[str, Any]) -> None:
    mean = Decimal(case["mean"])
    thresholds = kernel2.poisson_thresholds(mean)
    assert len(thresholds) == case["tableLength"]
    for index, value in case["thresholds"].items():
        assert thresholds[int(index)] == int(value)
    for draw in case["cases"]:
        u53 = int(draw["uniform53"])
        assert kernel2.poisson_of(u53, mean) == draw["k"]
        assert rng.poisson(_Fixed(u53), mean=mean) == draw["k"]


@pytest.mark.parametrize("case", STREAMS["permute"], ids=lambda case: case["name"])
def test_permute_2_and_its_key(case: Dict[str, Any]) -> None:
    key = layout_key(SEAM_DIGEST, case["keyParts"], counter_based=True)
    assert key.hex() == case["key"]
    domain = int(case["domain"])
    for index, image in zip(case["indexes"], case["images"], strict=True):
        assert permute2(key, domain, int(index)) == int(image)


@pytest.mark.parametrize(
    "case", STREAMS["identifiers"], ids=lambda case: f"{case['path']}:{case['length']}"
)
def test_generated_identifiers(case: Dict[str, Any]) -> None:
    _sim, sim_id, _gen, expose_id, _row, row, column = case["path"]
    common = {"sim_id": sim_id, "expose_id": expose_id, "row_index": row, "column": column}
    value = identifier_value(
        SEAM_DIGEST, **common, length=case["length"], as_uuid=case["asUuid"], counter_based=True
    )
    assert value == case["value"]


@pytest.mark.parametrize("case", STREAMS["seedTreeIdentifiers"], ids=lambda case: str(case["path"]))
def test_subject_and_event_identifiers(case: Dict[str, Any]) -> None:
    assert derive_identifier(_root(), tuple(case["path"]), length=case["length"]) == case["value"]


@pytest.mark.parametrize(
    "case", STREAMS["pluginSeeds"], ids=lambda case: f"{case['simId']}:{case['exposeId']}"
)
def test_a_generator_plugin_s_seed(case: Dict[str, Any]) -> None:
    seed = derive_plugin_seed(_root(), case["simId"], case["exposeId"])
    assert len(seed) == 32 and seed.hex() == case["value"]


def test_a_composed_and_a_decomposed_name_give_one_plugin_seed() -> None:
    composed, decomposed = (case for case in STREAMS["pluginSeeds"] if "caf" in case["exposeId"])
    assert composed["value"] == decomposed["value"]


def test_a_plugin_seed_exists_under_the_counter_based_root_only() -> None:
    legacy = derive_root(bytes.fromhex(STREAMS["seam"]), int(STREAMS["seed"]))
    with pytest.raises(FluxError):
        derive_plugin_seed(legacy, "s", "subscriber")


@pytest.mark.parametrize("case", STREAMS["relationPicks"], ids=lambda case: str(case["path"]))
def test_a_uniform_pick_on_the_first_columns_cell(case: Dict[str, Any]) -> None:
    """The engine's own pick, :meth:`RelationReader._pick`, on the first column's cell."""
    _sim, sim_id, _gen, expose_id, _row, row, column = case["path"]
    parent = ParentView(
        sim_id=sim_id,
        expose_id="parent",
        seam_digest=SEAM_DIGEST,
        rows=int(case["parentRows"]),
        columns=(),
        drivers=(),
    )
    null = case["nullFraction"]
    driver = ForeignKey(
        relationship="pick",
        from_columns=(column,),
        to_columns=(column,),
        pick="uniform",
        parent=parent,
        first_null=None if null is None else Decimal(null),
        counter_based=True,
    )
    reader = RelationReader(_root())
    owner = _Owner(sim_id, expose_id, SEAM_DIGEST)
    assert reader._pick(owner, driver, row) == int(case["pick"])
    # The draw index the pick took: the null draw's, when there is one, comes first.
    stream = CellStreams(_root(), sim_id, expose_id).stream_at(row, column)
    if null is not None:
        rng.bernoulli(stream, Decimal(null))
    assert stream.position == case["pickDraw"]


def test_a_cell_stream_is_the_paths_counter_stream() -> None:
    path = ("sim", "s", "gen", "orders", "row", 7, "customer_id")
    cell = CellStreams(_root(), "s", "orders").stream_at(7, "customer_id")
    whole = _stream(path)
    assert [cell.block(draw) for draw in range(3)] == [whole.block(draw) for draw in range(3)]


def test_the_transform_constants() -> None:
    constants = TRANSFORMS["constants"]
    names = ("ln2_hi", "ln2_lo", "Lg1", "Lg2", "Lg3", "Lg4", "Lg5", "Lg6", "Lg7")
    held = (kernel2._LN2_HI, kernel2._LN2_LO, kernel2._LG1, kernel2._LG2, kernel2._LG3,
            kernel2._LG4, kernel2._LG5, kernel2._LG6, kernel2._LG7)  # fmt: skip
    assert {name: _bits(value) for name, value in zip(names, held, strict=True)} == constants["log"]
    assert _bits(kernel2._SQRT_HALF) == constants["sqrtHalf"]
    tables = {
        "centralNumerator": kernel2._CENTRAL_NUM,
        "centralDenominator": kernel2._CENTRAL_DEN,
        "nearNumerator": kernel2._NEAR_NUM,
        "nearDenominator": kernel2._NEAR_DEN,
        "farNumerator": kernel2._FAR_NUM,
        "farDenominator": kernel2._FAR_DEN,
    }
    for name, values in tables.items():
        assert [_bits(value) for value in values] == constants["as241"][name], name


@pytest.mark.parametrize("case", TRANSFORMS["log"], ids=lambda case: case["x"])
def test_the_logarithm(case: Dict[str, str]) -> None:
    assert _bits(kernel2.log_basic(_from_bits(case["x"]))) == case["ln"]


@pytest.mark.parametrize("case", TRANSFORMS["ppnd16"], ids=lambda case: case["p"])
def test_the_inverse_normal(case: Dict[str, str]) -> None:
    assert _bits(kernel2.ppnd16(_from_bits(case["p"]))) == case["z"]


@pytest.mark.parametrize("case", TRANSFORMS["truncatedNormal"], ids=lambda case: case["name"])
def test_the_truncated_normal(case: Dict[str, Any]) -> None:
    declaration = case["declaration"]
    mean, stddev = Decimal(declaration["mean"]), Decimal(declaration["stddev"])
    lo, hi, scale = Decimal(declaration["lo"]), Decimal(declaration["hi"]), declaration["scale"]
    plan = kernel2.normal_plan(mean, stddev, lo, hi, scale)
    assert plan.mirror is case["mirror"]
    held = {"a": plan.a, "b": plan.b, "cdfA": plan.cdf_a, "sfB": plan.sf_b, "mass": plan.mass,
            "mean": plan.mean, "stddev": plan.stddev}  # fmt: skip
    assert {name: _bits(value) for name, value in held.items()} == case["binary64"]
    assert [plan.lo_units, plan.hi_units] == [int(value) for value in case["units"]]
    for draw in case["cases"]:
        u53, units = int(draw["uniform53"]), int(draw["units"])
        assert kernel2.truncated_normal_units(u53, plan) == units
        drawn = rng.truncated_normal(
            _Fixed(u53), mean=mean, stddev=stddev, lo=lo, hi=hi, scale=scale
        )
        assert drawn == Decimal(units).scaleb(-scale)
        assert drawn.as_tuple().exponent == -scale


@pytest.mark.parametrize("case", TRANSFORMS["exponential"], ids=lambda case: case["rate"])
def test_the_exponential(case: Dict[str, Any]) -> None:
    rate, scale = Decimal(case["rate"]), case["scale"]
    for draw in case["cases"]:
        u53, units = int(draw["uniform53"]), int(draw["units"])
        assert kernel2.exponential_units(u53, rate, scale) == units
        assert rng.exponential(_Fixed(u53), rate=rate, scale=scale) == Decimal(units).scaleb(-scale)


def test_this_process_keeps_subnormals() -> None:
    kernel2.require_subnormals()


def test_a_counter_stream_has_no_word_sequence() -> None:
    stream = _stream(("world", "w", "cohorts"))
    with pytest.raises(TypeError):
        stream.next_raw()
    words: List[int] = []
    with pytest.raises(TypeError):
        words.extend(stream.next_raw_words(2))
