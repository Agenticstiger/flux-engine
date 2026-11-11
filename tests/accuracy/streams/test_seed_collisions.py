# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 5: sibling seed-tree paths never collide.

Pass criterion, per shape of sibling paths (``2**16`` paths in the pull-request tier,
``2**22`` nightly):

* every 256-bit ``SeedSequence`` entropy is distinct, and
* every first 64-bit raw word is distinct,

with zero tolerance: one collision is a failure. The references, recorded with each
result, are NumPy's own bound for a 128-bit pool, ``n**2 * 2**-128``, and the birthday
expectation for 64-bit first words, ``n**2 / 2**65``. At ``n = 2**22`` they are about
``2**-84`` and ``2**-21``, so a collision is a defect, not bad luck.

Also pinned, by design of KERNEL-SPEC section 3: composed and decomposed spellings of a
name give the same label and entropy (NFC), and a part holding U+001F or U+001E is
refused with FLX-2202 rather than escaped.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

from typing import Callable, Iterator, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from numpy.random import PCG64, SeedSequence  # noqa: E402

from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.core.seedtree import (  # noqa: E402
    SeedPath,
    path_label,
    signal_path,
    stream_entropy,
    subject_trait_path,
)
from flux_engine.generator.paths import generator_column_path  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.streams.practrand import make_root  # noqa: E402

FAMILY = "streams"
CRITERION = "no collision among sibling 256-bit entropies or among first 64-bit words"

#: Unified CJK ideographs, stable under NFC: a pool of single characters that differ.
_CJK_FIRST = 0x4E00
_CJK_COUNT = 0x9FFF - 0x4E00 + 1


def _generator_rows(n: int) -> Iterator[SeedPath]:
    for row in range(n):
        yield generator_column_path("accuracy", "collisions", row, "amount")


def _subject_traits(n: int) -> Iterator[SeedPath]:
    traits = ("age", "income", "tenure", "region")
    for index in range(n):
        yield subject_trait_path("w-collisions", index // len(traits), traits[index % len(traits)])


def _signal_occurrences(n: int) -> Iterator[SeedPath]:
    for index in range(n):
        yield signal_path("s-collisions", "arrival", index % 256, index // 256)


def _one_character_names(n: int) -> Iterator[SeedPath]:
    """Trait names that each differ from ``"a" * P`` in exactly one character."""
    positions = -(-n // _CJK_COUNT)
    base = "a" * positions
    produced = 0
    for position in range(positions):
        for offset in range(_CJK_COUNT):
            if produced == n:
                return
            name = base[:position] + chr(_CJK_FIRST + offset) + base[position + 1 :]
            yield subject_trait_path("w-collisions", 0, name)
            produced += 1


SHAPES: dict[str, Callable[[int], Iterator[SeedPath]]] = {
    "generator-row": _generator_rows,
    "subject-trait": _subject_traits,
    "signal-occurrence": _signal_occurrences,
    "one-character-names": _one_character_names,
}


def _derive(paths: Iterator[SeedPath], seed: int, n: int) -> Tuple[np.ndarray, np.ndarray]:
    """The ``(n, 4)`` big-endian entropy words and the ``(n,)`` first raw words of *paths*."""
    root = make_root(seed)
    entropy = bytearray(32 * n)
    words = np.empty(n, dtype=np.uint64)
    count = 0
    for path in paths:
        value = stream_entropy(root, path)
        entropy[32 * count : 32 * count + 32] = value.to_bytes(32, "big")
        words[count] = PCG64(SeedSequence(value)).random_raw()
        count += 1
    assert count == n
    return np.frombuffer(bytes(entropy), dtype=">u8").reshape(n, 4), words


def _duplicate_rows(matrix: np.ndarray) -> int:
    order = np.lexsort(matrix.T[::-1])
    ordered = matrix[order]
    return int(np.count_nonzero((ordered[1:] == ordered[:-1]).all(axis=1)))


@pytest.mark.parametrize("shape", list(SHAPES))
def test_sibling_paths_have_distinct_entropies_and_first_words(
    shape: str, tier: Tier, accuracy_log: ResultLog
) -> None:
    n = tier.pick(pr=1 << 16, nightly=1 << 22)
    seed = tier.seed("streams", "collisions", shape)
    entropy, words = _derive(SHAPES[shape](n), seed, n)
    entropy_collisions = _duplicate_rows(entropy)
    word_collisions = int(n - np.unique(words).size)
    accuracy_log.record(
        family=FAMILY,
        case=f"collisions/{shape}",
        criterion=CRITERION,
        passed=entropy_collisions == 0 and word_collisions == 0,
        seed=seed,
        n=n,
        metrics={
            "entropyCollisions": entropy_collisions,
            "firstWordCollisions": word_collisions,
            "boundEntropy128BitPool": n * n * 2.0**-128,
            "expectedFirstWordCollisions": n * n / 2.0**65,
        },
    )
    assert entropy_collisions == 0, f"{shape}: {entropy_collisions} entropy collisions in {n}"
    assert word_collisions == 0, f"{shape}: {word_collisions} first-word collisions in {n}"


def test_the_collision_check_sees_a_duplicate(tier: Tier) -> None:
    """Power check: the duplicate counters are not vacuous."""
    n = 1 << 10
    entropy, words = _derive(_generator_rows(n), tier.seed("streams", "collisions", "power"), n)
    entropy = entropy.copy()
    entropy[7] = entropy[3]
    words[7] = words[3]
    assert _duplicate_rows(entropy) == 1
    assert n - np.unique(words).size == 1


NAMES = ["é", "café", "Ångström", "naïve-trait", "한글", "ṩ", "xéyèz"]


@pytest.mark.parametrize("composed", NAMES)
def test_nfc_equivalent_spellings_share_label_and_entropy(
    composed: str, tier: Tier, accuracy_log: ResultLog
) -> None:
    import unicodedata

    decomposed = unicodedata.normalize("NFD", composed)
    assert decomposed != composed, "the test needs a name whose NFD spelling differs"
    root = make_root(tier.seed("streams", "collisions", "nfc"))
    a = subject_trait_path("w", 7, composed)
    b = subject_trait_path("w", 7, decomposed)
    same = path_label(a) == path_label(b) and stream_entropy(root, a) == stream_entropy(root, b)
    accuracy_log.record(
        family=FAMILY,
        case=f"collisions/nfc/{composed!r}",
        criterion="NFC-equivalent spellings give the same label and entropy",
        passed=same,
        metrics={"differsInCodePoints": decomposed != composed},
    )
    assert same


@pytest.mark.parametrize("separator", ["\x1f", "\x1e"])
@pytest.mark.parametrize("place", ["first", "middle", "last"])
def test_a_part_with_a_separator_is_refused(
    separator: str, place: str, tier: Tier, accuracy_log: ResultLog
) -> None:
    text = {"first": f"{separator}ab", "middle": f"a{separator}b", "last": f"ab{separator}"}[place]
    path = ("world", text, "cohorts")
    root = make_root(tier.seed("streams", "collisions", "separator"))
    codes = []
    for refuse in (lambda: path_label(path), lambda: stream_entropy(root, path)):
        try:
            refuse()
            codes.append("accepted")
        except FluxError as error:
            codes.append(error.code.code)
    accuracy_log.record(
        family=FAMILY,
        case=f"collisions/separator/U+{ord(separator):04X}/{place}",
        criterion="a part holding U+001F or U+001E is refused with FLX-2202",
        passed=codes == ["FLX-2202", "FLX-2202"],
        metrics={"codes": codes},
    )
    assert codes == ["FLX-2202", "FLX-2202"]


def test_a_split_path_does_not_share_a_label_with_a_joined_one() -> None:
    """The collision the refusal exists to prevent: ('a<US>b',) versus ('a', 'b')."""
    assert path_label(("world", "a", "b", "cohorts")) != path_label(("world", "ab", "cohorts"))
    with pytest.raises(FluxError):
        path_label(("world", "a\x1fb", "cohorts"))
