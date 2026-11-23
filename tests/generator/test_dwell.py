# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The dwell family: histogram and exponential dwells, the open share, the family and its document."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from fractions import Fraction

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernel2 import exponential_units
from flux_engine.core.rng import FLX_5021
from flux_engine.generator.dates import DAY_MS, UNIFORM53, draw_end, duration_table
from flux_engine.generator.dwell import (
    DwellFamily,
    ExponentialDwell,
    HistogramDwell,
    decimal_text,
    draw_dwell,
    draw_family,
)
from flux_engine.seam.dates import Histogram

TOP = UNIFORM53 - 1
SAMPLES = [0, 1, 12345, UNIFORM53 // 7, UNIFORM53 // 3, UNIFORM53 // 2, UNIFORM53 - 12345, TOP]


@pytest.mark.parametrize("closed", [None, "lower", "upper"])
@pytest.mark.parametrize("open_weight", [None, "2"])
@pytest.mark.parametrize("unit", [1, DAY_MS])
def test_histogram_dwell_is_draw_end(closed, open_weight, unit):
    histogram = Histogram(("0", "5", "10", "30"), ("3", "4", "1"), closed, open_weight)
    table = duration_table(histogram)
    for u in SAMPLES:
        assert draw_dwell(HistogramDwell(histogram), u, unit) == draw_end(table, 0, u, unit)


def test_histogram_open_slot_draws_no_end():
    histogram = Histogram(("0", "10"), ("1",), None, "1")
    assert draw_dwell(HistogramDwell(histogram), 0, 1) is not None
    assert draw_dwell(HistogramDwell(histogram), TOP, 1) is None


def _expected(mean: Fraction, op: Fraction, u: int, unit: int):
    cut = math.floor(UNIFORM53 * op)
    if u < cut:
        return None
    v = (u - cut) * UNIFORM53 // (UNIFORM53 - cut)
    rate = float(Fraction(1) / (mean * unit))
    return exponential_units(v, Decimal(rate), 0)


@pytest.mark.parametrize("unit", [1, DAY_MS])
@pytest.mark.parametrize("op", [Fraction(0), Fraction(1, 4), Fraction(9, 10)])
def test_exponential_matches_formula(unit, op):
    mean = Fraction(13, 2)
    dwell = ExponentialDwell(mean, op)
    for u in SAMPLES:
        assert draw_dwell(dwell, u, unit) == _expected(mean, op, u, unit)


def test_exponential_formula_values():
    dwell = ExponentialDwell(Fraction(10))
    assert draw_dwell(dwell, 0, 1) == 0
    rate = 0.1
    for u in (UNIFORM53 // 2, UNIFORM53 // 10 * 9):
        x = -math.log(1.0 - u * 2.0**-53) / rate
        assert abs(draw_dwell(dwell, u, 1) - x) <= 1
    assert draw_dwell(dwell, TOP, 1) > 300


def test_exponential_open_boundary():
    dwell = ExponentialDwell(Fraction(5), Fraction(1, 4))
    cut = UNIFORM53 // 4
    assert draw_dwell(dwell, cut - 1, 1) is None
    assert draw_dwell(dwell, 0, 1) is None
    assert draw_dwell(dwell, cut, 1) == 0
    assert draw_dwell(dwell, TOP, 1) is not None


def test_open_zero_never_no_end():
    dwell = ExponentialDwell(Fraction(5))
    assert all(draw_dwell(dwell, u, 1) is not None for u in SAMPLES)


@pytest.mark.parametrize("unit", [1, DAY_MS])
def test_monotone_in_u(unit):
    dwell = ExponentialDwell(Fraction(20), Fraction(1, 5))
    values = [draw_dwell(dwell, u, unit) for u in sorted(SAMPLES)]
    ends = [v for v in values if v is not None]
    assert ends == sorted(ends)
    assert values == [None] * (len(values) - len(ends)) + ends


def test_rate_is_rounded_once():
    # 1 / (13/10) rounds differently as a float quotient than from the exact rational.
    mean = Fraction(13, 10)
    exact = float(Fraction(1) / mean)
    assert exact != 1.0 / float(mean)
    assert Decimal(exact) == Decimal(exact)
    for u in SAMPLES:
        assert draw_dwell(ExponentialDwell(mean), u, 1) == exponential_units(u, Decimal(exact), 0)


def test_refusals():
    for bad in (Fraction(0), Fraction(-1)):
        with pytest.raises(ValueError):
            ExponentialDwell(bad)
    for bad in (Fraction(-1, 10), Fraction(1), Fraction(2)):
        with pytest.raises(ValueError):
            ExponentialDwell(Fraction(1), bad)
    with pytest.raises(ValueError):
        ExponentialDwell(Fraction(1, 3))
    with pytest.raises(FluxError) as excinfo:
        draw_dwell(ExponentialDwell(Fraction(10**15)), 5, DAY_MS)
    assert excinfo.value.code is FLX_5021
    with pytest.raises(FluxError):
        draw_dwell(ExponentialDwell(Fraction(10**15)), 5, 1)
    with pytest.raises(ValueError):
        DwellFamily("s", (("b", HistogramDwell(_H)), ("a", HistogramDwell(_H))), None)
    with pytest.raises(ValueError):
        DwellFamily(None, (("a", HistogramDwell(_H)),), None)


_H = Histogram(("0", "10"), ("1",))
_E = ExponentialDwell(Fraction(7, 2), Fraction(1, 10))
_FAMILY = DwellFamily("status", (("closed", HistogramDwell(_H)), ("open", _E)), _E)


def test_dwell_for():
    assert _FAMILY.dwell_for("closed") == HistogramDwell(_H)
    assert _FAMILY.dwell_for("open") is _E
    assert _FAMILY.dwell_for("other") is _E
    assert _FAMILY.dwell_for(None) is _E
    bare = DwellFamily("status", _FAMILY.states, None)
    assert bare.dwell_for("closed") == HistogramDwell(_H)
    assert bare.dwell_for("other") is None
    assert bare.dwell_for(None) is None


def test_draw_family():
    bare = DwellFamily("status", _FAMILY.states, None)
    assert draw_family(bare, "other", 5, 1) == (False, None)
    assert draw_family(bare, None, 5, 1) == (False, None)
    assert draw_family(bare, "closed", 5, 1) == (True, draw_dwell(HistogramDwell(_H), 5, 1))
    assert draw_family(_FAMILY, None, UNIFORM53 // 2, DAY_MS) == (
        True,
        draw_dwell(_E, UNIFORM53 // 2, DAY_MS),
    )
    assert draw_family(_FAMILY, "open", 0, 1) == (True, None)


def test_one_dwell_for_every_row():
    family = DwellFamily(None, (), _E)
    assert family.dwell_for("anything") is _E
    assert family.dwell_for(None) is _E


def test_as_document_stable_and_distinct():
    document = _FAMILY.as_document()
    assert list(document) == ["given", "states", "otherwise"]
    assert document["states"][1] == {
        "state": "open",
        "dwell": {"exponential": {"mean": "3.5", "open": "0.1"}},
    }
    assert document["states"][0]["dwell"] == {"histogram": _H.as_member()}
    assert json.dumps(document) == json.dumps(_FAMILY.as_document())
    others = [
        DwellFamily("status", _FAMILY.states, None),
        DwellFamily("kind", _FAMILY.states, _E),
        DwellFamily("status", _FAMILY.states[:1], _E),
        DwellFamily("status", _FAMILY.states, ExponentialDwell(Fraction(7, 2))),
    ]
    texts = {json.dumps(f.as_document(), sort_keys=True) for f in [_FAMILY] + others}
    assert len(texts) == 5
    assert DwellFamily(None, (), None).as_document() == {
        "given": None,
        "states": [],
        "otherwise": None,
    }


def test_decimal_text():
    assert [
        decimal_text(Fraction(v)) for v in ("5", "3/2", "1/8", "0", "1/1000", "100", "2000/8")
    ] == [
        "5",
        "1.5",
        "0.125",
        "0",
        "0.001",
        "100",
        "250",
    ]
    with pytest.raises(ValueError):
        decimal_text(Fraction(1, 3))


# -- a tiny or huge mean is refused with FLX-5021, never an OverflowError ----------------------------


def _first_refused(unit: int, step: int, mean_of) -> int:
    """The first exponent from *step* at which a mean of ``mean_of(k)`` is refused."""
    for k in range(step, 1200):
        try:
            draw_dwell(ExponentialDwell(mean=mean_of(k)), 12345678901, unit)
        except FluxError as error:
            assert error.code == FLX_5021, error
            return k
    raise AssertionError("never refused")


@pytest.mark.parametrize("unit", [1, DAY_MS])
def test_a_mean_too_small_for_a_binary64_rate_is_refused_with_flx_5021(unit):
    # The smallest mean that passes gives a rate of at most 2**900; the first one below is refused.
    first = _first_refused(unit, 0, lambda k: Fraction(1, 10**k))
    below = ExponentialDwell(mean=Fraction(1, 10**first))
    with pytest.raises(FluxError) as caught:
        draw_dwell(below, 5, unit)
    assert caught.value.code == FLX_5021
    passing = ExponentialDwell(mean=Fraction(1, 10 ** (first - 1)))
    assert Fraction(1) / (passing.mean * unit) <= 2**900
    draw_dwell(passing, 5, unit)  # a rate this large draws zero or a few units, and does not raise
    assert Fraction(1) / (below.mean * unit) > 2**900


@pytest.mark.parametrize("unit", [1, DAY_MS])
def test_a_320_zero_mean_is_refused_not_overflowed(unit):
    mean = Fraction("0." + "0" * 320 + "1")
    with pytest.raises(FluxError) as caught:
        draw_dwell(ExponentialDwell(mean=mean), 12345678901, unit)
    assert caught.value.code == FLX_5021


@pytest.mark.parametrize("unit", [1, DAY_MS])
def test_a_huge_mean_is_refused_with_flx_5021(unit):
    first = _first_refused(unit, 0, lambda k: Fraction(10**k))
    huge = ExponentialDwell(mean=Fraction(10**first))
    with pytest.raises(FluxError) as caught:
        draw_dwell(huge, 5, unit)
    assert caught.value.code == FLX_5021
    draw_dwell(ExponentialDwell(mean=Fraction(10 ** (first - 1))), 5, unit)


def test_the_rate_is_checked_once_per_mean_and_unit():
    from flux_engine.generator import dwell as module

    module._rate_of.cache_clear()
    exponential = ExponentialDwell(mean=Fraction(3, 2))
    for u in SAMPLES:
        draw_dwell(exponential, u, DAY_MS)
    info = module._rate_of.cache_info()
    assert info.misses == 1 and info.hits == len(SAMPLES) - 1


# -- the state lookup equals the scan -------------------------------------------------------------


def _scan(family, state, otherwise):
    """The dwell of *state* found by scanning the family's states in order."""
    if state is not None:
        for text, dwell in family.states:
            if text == state:
                return dwell
    return otherwise


def test_dwell_for_equals_the_scan_for_listed_unlisted_and_null_states():
    one = ExponentialDwell(mean=Fraction(2))
    two = ExponentialDwell(mean=Fraction(5), open=Fraction(1, 4))
    other = HistogramDwell(Histogram(("0", "5"), ("1",), None, None))
    states = tuple(sorted((text, one if i % 2 else two) for i, text in enumerate("abcdefghij")))
    for otherwise in (None, other):
        family = DwellFamily("segment", states, otherwise)
        for state in [*"abcdefghij", "zz", "", "A", None]:
            assert family.dwell_for(state) is _scan(family, state, otherwise)
    assert isinstance(DwellFamily("segment", states, None).states, tuple)


def test_a_family_with_many_states_looks_one_up_without_scanning():
    states = tuple((f"s{i:04d}", ExponentialDwell(mean=Fraction(i + 1))) for i in range(1024))
    family = DwellFamily("segment", states, None)
    assert family.dwell_for("s1023") is states[1023][1]
    assert family.dwell_for("s0000") is states[0][1]
    assert family.dwell_for("nope") is None
