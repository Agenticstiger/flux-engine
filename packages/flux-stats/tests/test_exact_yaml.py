# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A FLUX bundle's YAML is read exactly, and within bounds (the ``yaml`` extra)."""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal

import pytest

pytest.importorskip("yaml")

import yaml  # noqa: E402

from flux_stats import exact_yaml  # noqa: E402
from flux_stats.canonical import render_decimal  # noqa: E402
from flux_stats.errors import FluxError  # noqa: E402
from flux_stats.exact_yaml import (  # noqa: E402
    ExactLoader,
    ReadBudget,
    ReadLimits,
    check_documents,
    load_documents,
)
from flux_stats.reading import digits_written_out  # noqa: E402

pytestmark = pytest.mark.unit

NAME = "s.flux.yml"


def _refused(text: str, budget: ReadBudget | None = None) -> FluxError:
    with pytest.raises(FluxError) as refused:
        load_documents(text, name=NAME, budget=budget)
    return refused.value


def _chain(levels: int) -> str:
    """Mapping a{n} merges a{n-1} twice, so it holds 2^(n+1) pairs."""
    lines = ["a0: &a0 {k0: 1, k1: 2}"]
    lines += [f"a{n}: &a{n} {{<<: [*a{n - 1}, *a{n - 1}]}}" for n in range(1, levels + 1)]
    return "\n".join(lines) + "\n"


def test_a_float_is_the_decimal_its_text_spells_and_a_timestamp_its_text() -> None:
    (document,) = load_documents("a: 0.7\nb: 1.10\nt: 2026-07-01T00:00:00Z\n", name=NAME)
    assert document == {"a": Decimal("0.7"), "b": Decimal("1.10"), "t": "2026-07-01T00:00:00Z"}


def test_every_mapping_document_of_the_text_is_read() -> None:
    assert load_documents("a: 1\n---\n- 2\n---\nb: 3\n", name=NAME) == ({"a": 1}, {"b": 3})


def test_unreadable_yaml_names_the_file() -> None:
    error = _refused("a: [\n")
    assert error.code.code == "FLX-3105"
    assert f"{NAME}: unreadable YAML" in str(error)


def test_an_integer_too_long_to_form_is_refused_by_its_position() -> None:
    error = _refused("rows: " + "9" * 5000 + "\n")
    assert error.code.code == "FLX-1003"
    assert NAME in str(error) and "(line 1, column 7)" in str(error)


def test_aliases_that_write_out_too_many_values_are_refused() -> None:
    lines = ['a: &a ["x", "x", "x", "x", "x", "x", "x", "x", "x", "x"]']
    for name, previous in zip("bcdefg", "abcdef", strict=True):
        lines.append(f"{name}: &{name} [{', '.join([f'*{previous}'] * 10)}]")
    error = _refused("\n".join(lines) + "\n")
    assert error.code.code == "FLX-3105"
    assert (
        "writes out more than 1000000 values once its aliases and merge keys are written out "
        "(line " in str(error)
    )


def test_a_document_that_holds_itself_through_an_alias_is_refused() -> None:
    assert "holds itself" in str(_refused("r: &r [*r]\n"))
    assert "holds itself" in str(_refused("a: &a {<<: *a}\n"))


def test_nesting_is_counted_through_aliases() -> None:
    lines = ["a0: &a0 [x]"] + [f"a{n}: &a{n} [*a{n - 1}]" for n in range(1, 70)]
    assert "nests deeper than 64 levels" in str(_refused("\n".join(lines) + "\n"))


def test_text_nested_too_deeply_to_parse_is_refused() -> None:
    assert "nests too deeply to read" in str(_refused("x: " + "[" * 2000 + "]" * 2000 + "\n"))


def test_a_number_is_read_up_to_the_digits_bound_and_refused_past_it() -> None:
    (document,) = load_documents("x: 1.0e+999\n", name=NAME)
    assert document["x"] == Decimal("1.0e+999")
    assert "more than 1000 digits written out" in str(_refused("x: 1.0e+1000\n"))
    assert "more than 1000 digits written out" in str(_refused("x: 1.0e+300000000\n"))


def test_a_zero_counts_the_digits_its_scale_spells() -> None:
    # The canonical form keeps the scale a number was written with, so 0.e-10000000 is spelled
    # in ten million digits: it needs more than 1000 digits written out, as 1.0e-10000000 does.
    for text in ("0.e-10000000", "-0.e-10000000", "0.0e-300000000", "0." + "0" * 1001, "0.e-1001"):
        error = _refused(f"x: {text}\n")
        assert "a zero whose scale needs more than 1000 digits written out" in str(error), text
        assert error.code.code == "FLX-3105"
    # At the bound it reads, in either spelling, and keeps the scale it was written with.
    for text in ("0.e-1000", "0." + "0" * 1000, "-0.e-1000"):
        (document,) = load_documents(f"x: {text}\n", name=NAME)
        assert document["x"] == 0
        assert document["x"].as_tuple().exponent == -1000
    # A zero with a positive exponent is spelled "0", whatever its exponent.
    (document,) = load_documents("x: 0.e+99999999\n", name=NAME)
    assert render_decimal(document["x"]) == "0"


def test_a_negative_number_at_the_bound_is_counted_for_its_sign_and_point_as_well() -> None:
    # -1.e-1000 is spelled "-0." and a thousand digits: 1,003 characters, not 1,002.
    held = "n: &n -1.e-1000\nl: [*n]\n"
    assert load_documents(held, name=NAME, budget=ReadBudget(ReadLimits(characters=2008)))
    assert "more than 2007 characters" in str(
        _refused(held, ReadBudget(ReadLimits(characters=2007)))
    )


def test_the_digits_a_number_is_counted_for_are_those_its_canonical_spelling_holds() -> None:
    import random

    rng = random.Random(13)
    texts = [
        "0",
        "-0",
        "0.000",
        "0E-5",
        "-0E-1000",
        "0E+7",
        "1.500",
        "0.001",
        "123.45",
        "1E+999",
        "1E-1000",
        "-0.25",
    ]
    for _ in range(500):
        digits = rng.choice(["0", "00", "000", "1", "12", "405", "9990", "123456"])
        point = rng.randint(0, len(digits))
        texts.append(
            rng.choice(["", "-"])
            + digits[:point]
            + "."
            + digits[point:]
            + rng.choice(["", "e+5", "E-3", "e-40", "e+0", "e-0"])
        )
    for text in texts:
        number = Decimal(text)
        spelled = render_decimal(number)
        # The digits it holds, a leading "0." not counted, as for every number the count reads.
        held = sum(character.isdigit() for character in spelled)
        held -= 1 if spelled.lstrip("-").startswith("0.") else 0
        assert digits_written_out(number) == held, text
        assert exact_yaml._spelled_length(number) == len(spelled), text


def test_a_merge_key_chain_that_copies_too_many_pairs_is_refused() -> None:
    error = _refused(_chain(29))
    assert "merge keys (<<) copy more than 100000 pairs" in str(error)


def test_merges_that_each_stay_under_the_bound_are_counted_together() -> None:
    # Each mapping in b merges 2,048 pairs, far under the bound; fifty pass it.
    text = _chain(10) + "b: [" + ", ".join(["{<<: [*a10]}"] * 50) + "]\n"
    assert "merge keys" in str(_refused(text))


def test_one_budget_bounds_the_merges_of_every_file_it_reads() -> None:
    budget = ReadBudget()
    load_documents(_chain(14), name="one.flux.yml", budget=budget)  # 65,532 pairs
    with pytest.raises(FluxError) as refused:
        load_documents(_chain(14), name="two.flux.yml", budget=budget)
    assert "two.flux.yml" in str(refused.value) and "merge keys" in str(refused.value)


def test_a_merge_key_inside_the_bound_reads_as_the_safe_loader_reads_it() -> None:
    text = "base: &b {x: 1, y: 1}\nm: {<<: *b, y: 2}\n"
    (document,) = load_documents(text, name=NAME)
    assert document["m"] == yaml.safe_load(text)["m"] == {"x": 1, "y": 2}


def test_the_bounds_can_be_tightened() -> None:
    budget = ReadBudget(ReadLimits(values=5))
    assert "more than 5 values" in str(_refused("a: [1, 2, 3, 4, 5]\n", budget))


def _empty_merges(hosts: int, parts: int) -> str:
    """*hosts* mappings that each merge one list of *parts* empty mappings.

    They write out about *hosts* plus *parts* values, and building them visits *hosts*
    times *parts* mappings.
    """
    return (
        "e: &e {}\ns: &s ["
        + ", ".join(["*e"] * parts)
        + "]\nm: ["
        + ", ".join(["{<<: *s}"] * hosts)
        + "]\n"
    )


def _long_string(length: int, holders: int) -> str:
    """One string of *length* characters held by *holders* aliases."""
    return f's: &s "{"x" * length}"\nl: [' + ", ".join(["*s"] * holders) + "]\n"


def test_merge_keys_that_name_too_many_empty_mappings_are_refused() -> None:
    # 56 KB of text and 8,007 values would be 16 million visits to build.
    error = _refused(_empty_merges(1001, 1000))
    assert error.code.code == "FLX-3105"
    assert "names more than 1000000 mappings in its merge keys (line 3, column " in str(error)
    with pytest.raises(FluxError) as refused:
        check_documents(_empty_merges(1001, 1000), name=NAME)
    assert "names more than 1000000 mappings in its merge keys" in str(refused.value)


def test_the_mappings_merge_keys_name_are_counted_for_each_mapping_that_holds_one() -> None:
    budget = ReadBudget(ReadLimits(merged=100))
    assert load_documents(_empty_merges(10, 10), name=NAME, budget=ReadBudget(budget.limits))
    refused = _refused(_empty_merges(11, 10), ReadBudget(budget.limits))
    assert "names more than 100 mappings in its merge keys" in str(refused)
    # One mapping written fifty-one times through an alias is one mapping.
    aliased = "e: &e {}\ns: &s [" + ", ".join(["*e"] * 10) + "]\n"
    aliased += "m: [&h {<<: *s}, " + ", ".join(["*h"] * 50) + "]\n"
    assert load_documents(aliased, name=NAME, budget=ReadBudget(budget.limits))


def test_what_a_merge_key_names_is_worked_out_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = exact_yaml._merged_parts

    def counting(value_node: object) -> list[object]:
        calls.append(1)
        return real(value_node)  # type: ignore[no-any-return]

    monkeypatch.setattr(exact_yaml, "_merged_parts", counting)
    _refused(_empty_merges(1200, 1000))
    assert len(calls) <= 2


def test_a_long_string_held_by_many_aliases_is_refused_by_its_characters() -> None:
    # 58 KB of text; the values bound sees about two thousand values.
    error = _refused(_long_string(50_000, 2_000))
    assert error.code.code == "FLX-3105"
    assert (
        "writes out more than 67108864 characters once its aliases and merge keys are written "
        "out (line 2, column "
    ) in str(error)
    with pytest.raises(FluxError):
        check_documents(_long_string(50_000, 2_000), name=NAME)


def test_the_characters_of_keys_values_aliases_and_merge_keys_are_all_counted() -> None:
    # Key s (1), its value (100), key l (1) and three aliases of the value (300).
    held = _long_string(100, 3)
    assert load_documents(held, name=NAME, budget=ReadBudget(ReadLimits(characters=402)))
    refused = _refused(held, ReadBudget(ReadLimits(characters=401)))
    assert "writes out more than 401 characters" in str(refused)
    # Key s, its pair (1 + 50), key m, the pair of s merged twice (2 * 51) and m's own pair (1 + 20).
    merged = 's: &s {k: "' + "y" * 50 + '"}\nm: {<<: [*s, *s], z: "' + "z" * 20 + '"}\n'
    assert load_documents(merged, name=NAME, budget=ReadBudget(ReadLimits(characters=176)))
    assert "more than 175 characters" in str(
        _refused(merged, ReadBudget(ReadLimits(characters=175)))
    )


def _nested_numbers(levels: int) -> str:
    """One seven character number, held by ten aliases, those by ten, and so on *levels* times."""
    lines = ["id: x", "n: &n 1.e+999"]
    previous = "n"
    for level in range(1, levels + 1):
        lines.append(f"l{level}: &l{level} [" + ", ".join([f"*{previous}"] * 10) + "]")
        previous = f"l{level}"
    return "\n".join(lines) + f"\ntop: [*{previous}]\n"


def test_a_number_counts_as_the_characters_its_decimal_is_spelled_in() -> None:
    # 1.e+99 is six characters of text and a hundred written out: key n (1) and its value
    # (100), key l (1) and three aliases of the value (300).
    held = "n: &n 1.e+99\nl: [*n, *n, *n]\n"
    assert load_documents(held, name=NAME, budget=ReadBudget(ReadLimits(characters=402)))
    assert "writes out more than 401 characters" in str(
        _refused(held, ReadBudget(ReadLimits(characters=401)))
    )
    # A zero is spelled at its scale: 0.e-98 is "0." and ninety-eight zeros.
    zero = "n: &n 0.e-98\nl: [*n, *n]\n"
    assert load_documents(zero, name=NAME, budget=ReadBudget(ReadLimits(characters=302)))
    assert "more than 301 characters" in str(_refused(zero, ReadBudget(ReadLimits(characters=301))))
    # A number written plainly is its text, and so is one an exponent does not lengthen.
    plain = "n: &n 1.5\nl: [*n, *n, *n]\nm: [1.5e0, -2.5e-1]\n"
    assert load_documents(plain, name=NAME, budget=ReadBudget(ReadLimits(characters=27)))
    assert "more than 26 characters" in str(_refused(plain, ReadBudget(ReadLimits(characters=26))))


def test_aliased_numbers_that_are_spelled_out_past_the_bound_are_refused() -> None:
    # About 320 bytes that stay inside every count of values and of text, and whose digest is
    # written out at 200 MB, are refused for what the numbers are spelled in.
    text = _nested_numbers(5)
    assert len(text) < 400
    error = _refused(text)
    assert error.code.code == "FLX-3105"
    assert "writes out more than 67108864 characters once its aliases and merge keys" in str(error)
    with pytest.raises(FluxError):
        check_documents(text, name=NAME)
    # Ten thousand of them, which the document writes out twice, are 20 MB, and read.
    assert load_documents(_nested_numbers(4), name=NAME)


def test_what_a_number_is_counted_for_is_worked_out_once_and_a_long_one_not_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spelled: list[int] = []
    real = exact_yaml._spelled_length

    def counting(number: Decimal) -> int:
        spelled.append(1)
        return real(number)

    monkeypatch.setattr(exact_yaml, "_spelled_length", counting)
    # One number held by two thousand aliases is counted once.
    held = "n: &n 1.e+99\nl: [" + ", ".join(["*n"] * 2_000) + "]\n"
    check_documents(held, name=NAME)
    assert len(spelled) == 1
    # A number whose text is as long as it can be counted for is not parsed to find out: a hundred
    # thousand digits held two thousand times costs the length of its text, not 2,000 parses of it.
    spelled.clear()
    long_number = "n: &n " + "1" * 100_000 + ".e+1\nl: [" + ", ".join(["*n"] * 2_000) + "]\n"
    with pytest.raises(FluxError):
        check_documents(long_number, name=NAME)
    assert spelled == []


def test_a_number_that_needs_too_many_digits_is_still_refused_where_it_is_built() -> None:
    # It counts as no more than the digits allow, so the refusal is the older one.
    error = _refused("x: 1.e+5000\n")
    assert "more than 1000 digits" in str(error)
    assert "characters" not in str(error)


def test_the_characters_a_number_is_counted_for_are_those_it_is_spelled_in() -> None:
    import random

    spelled = [
        "1.e+999",
        ".1e-999",
        "-1.5e3",
        "0.e-98",
        "-0.0e-5",
        "0.e+7",
        "123.456e-2",
        "9.0e0",
        "1.E+2",
        "-0.e-3",
    ]
    rng = random.Random(7)
    for _ in range(300):
        digits = "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 10)))
        point = rng.randint(0, len(digits))
        spelled.append(
            rng.choice(["", "-", "+"])
            + digits[:point]
            + "."
            + digits[point:]
            + rng.choice("eE")
            + rng.choice(["+", "-"])
            + str(rng.randint(0, 1100))
        )
    for text in spelled:
        number = Decimal(text)
        assert exact_yaml._spelled_length(number) == len(render_decimal(number)), text


def test_merge_keys_inside_merge_keys_read_as_far_as_pyyaml_reads_them() -> None:
    # A merge key adds no level, so the bound on levels does not limit these, and the
    # measure must not be what does: its walk does not use the interpreter's stack.
    text = "{<<: " * 400 + "{k: 1}" + "}" * 400 + "\n"
    try:
        built = yaml.safe_load(text)
    except RecursionError:  # pragma: no cover - this interpreter cannot build it either
        pytest.skip("PyYAML cannot build it here")
    assert load_documents(text, name=NAME) == (built,)


def test_the_check_refuses_what_the_read_refuses_and_leaves_bad_yaml_to_its_reader() -> None:
    check_documents("a: [\n", name=NAME)
    with pytest.raises(FluxError) as refused:
        check_documents(_chain(29), name=NAME)
    assert "merge keys" in str(refused.value)
    with pytest.raises(FluxError) as refused:
        check_documents("rows: " + "9" * 5000 + "\n", name=NAME)
    assert refused.value.code.code == "FLX-1003"


def test_a_character_yaml_does_not_allow_is_unreadable_yaml_to_the_read_and_the_check() -> None:
    # PyYAML refuses it when a loader is made from the text, before any read.
    error = _refused("a: 1\n---\nb: bell \x07\n")
    assert error.code.code == "FLX-3105"
    assert f"{NAME}: unreadable YAML: unacceptable character #x0007: " in str(error)
    assert "special characters are not allowed (line 3, column 9)" in str(error)
    # The check refuses it too: a reader of the file meets it only after building
    # every document before it, which the check could then not have measured.
    with pytest.raises(FluxError) as refused:
        check_documents(_chain(29) + "---\nb: bell \x07\n", name=NAME)
    assert f"{NAME}: unreadable YAML: unacceptable character #x0007" in str(refused.value)
    assert "special characters are not allowed (line " in str(refused.value)


def test_a_scalar_its_explicit_tag_cannot_build_is_refused_by_its_position() -> None:
    # PyYAML's constructor raises a KeyError with no mark; the check finds the scalar.
    error = _refused("a: !!bool maybe\n")
    assert error.code.code == "FLX-3105"
    assert f"{NAME}: a scalar read as a boolean has no boolean value (line 1, column 4)" in str(
        error
    )


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("a: 1\nb: [1, 2\nc: 3\n", "expected ',' or ']', but got ':' (line 3, column 2)"),
        ("a: 1\nb\nc: 3\n", "could not find expected ':' (line 3, column 1)"),
        ("a: !foo 1\n", "could not determine a constructor for the tag '...' (line 1, column 4)"),
        ("a: [\n", "expected the node content, but found '<stream end>' (line 2, column 1)"),
    ],
)
def test_unreadable_yaml_names_the_problem_and_where_the_parser_found_it(
    text: str, why: str
) -> None:
    # The problem PyYAML names, not its context ("while parsing a flow
    # sequence"), and the line and column of its mark.
    assert f"{NAME}: unreadable YAML: {why}" in str(_refused(text))


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("a: *value-77\n", "value-77"),
        ("a: !value-77 x\n", "value-77"),
        ("a: &value-77 1\nb: &value-77 2\n", "value-77"),
        ('a: "a \\q"\n', "\\q"),
    ],
)
def test_a_refusal_repeats_no_word_of_the_text(text: str, secret: str) -> None:
    # PyYAML quotes the alias, tag, anchor or character it found; a refusal names
    # the file and the place, never the document's own text.
    message = str(_refused(text))
    assert secret not in message
    assert "'...'" in message and "(line " in message


def test_a_duplicate_anchor_says_so() -> None:
    message = str(_refused("a: &x 1\nb: &x 2\n"))
    assert f"{NAME}: unreadable YAML: found duplicate anchor '...' (line 2, column 4)" in message


def test_a_refusal_is_short_whatever_the_text_quotes() -> None:
    message = str(_refused("t: !" + "x" * 100_000 + " y\n"))
    assert len(message) < 400


def test_the_fallback_check_keeps_the_callers_limits() -> None:
    # A KeyError from a constructor is placed by the check, with the caller's own
    # limits, so a bound the caller raised never stands in for the real problem.
    text = "a: " + "[" * 80 + "]" * 80 + "\n---\nb: !!bool maybe\n"
    budget = ReadBudget(ReadLimits(depth=200))
    with pytest.raises(FluxError) as refused:
        load_documents(text, name=NAME, budget=budget)
    assert "a scalar read as a boolean has no boolean value (line 3, column 4)" in str(
        refused.value
    )


def test_a_refusal_never_raises_on_an_error_without_a_place() -> None:
    class Mark:
        line = None
        column = None

    class Odd(Exception):
        problem = "something odd"
        problem_mark = Mark()
        position = -1

    error = exact_yaml._unreadable(Odd(), "a: 1\n", NAME)
    assert error.detail == f"{NAME}: unreadable YAML: something odd"


def test_a_character_yaml_does_not_allow_is_placed_by_line_and_column() -> None:
    # PyYAML gives an offset into the text, not a mark; YAML's line breaks
    # count, a CRLF as one.
    after_crlf = str(_refused("a: 1\r\nb: x\x00\n"))
    assert "special characters are not allowed (line 2, column 5)" in after_crlf
    after_separator = str(_refused("a: 1" + chr(0x2028) + "b: x\x00\n"))
    assert "(line 2, column 5)" in after_separator


@pytest.mark.parametrize(
    ("scalar", "why"),
    [
        ("!!bool maybe", "a scalar read as a boolean has no boolean value"),
        ("!!bool ''", "a scalar read as a boolean has no boolean value"),
        ("!!float ZZZ", "a scalar read as a float has no float value"),
        ("!!float ''", "a scalar read as a float has no float value"),
    ],
)
def test_the_check_refuses_a_scalar_a_safe_loader_cannot_build_by_its_position(
    scalar: str, why: str
) -> None:
    # A plain SafeLoader raises a bare KeyError, ValueError or IndexError for these.
    with pytest.raises(FluxError) as refused:
        check_documents(f"a: 1\nb: {scalar}\n", name=NAME)
    assert refused.value.code.code == "FLX-3105"
    assert f"{NAME}: {why} (line 2, column 4)" in str(refused.value)


def test_the_check_builds_what_a_safe_loader_builds() -> None:
    check_documents(
        "a: .inf\nb: .nan\nc: 1:30.5\nd: yes\ne: !!float 1e5000\nf: !!bool True\n", name=NAME
    )


@pytest.mark.parametrize(
    ("scalar", "why"),
    [
        ("!!float NaN", "an infinite or not-a-number value has no digest"),
        ("!!float Infinity", "an infinite or not-a-number value has no digest"),
        ("!!float ZZZ", "a scalar read as a float is not a decimal number"),
        ("!!float 1:30", "a YAML sexagesimal number has no canonical decimal spelling"),
    ],
)
def test_a_float_no_digest_can_hold_is_refused_by_its_position_never_its_value(
    scalar: str, why: str
) -> None:
    error = _refused(f"a: {scalar}\n")
    assert error.code.code == "FLX-3105"
    assert f"{NAME}: {why}" in str(error) and "(line 1, column 4)" in str(error)
    assert scalar.split()[1] not in str(error)


def test_a_python_without_the_integer_digit_limit_reads_to_cpythons_default() -> None:
    # Python 3.10.0 to 3.10.6 have no sys.int_info.default_max_str_digits.
    script = (
        "import collections, sys\n"
        "sys.int_info = collections.namedtuple('int_info', 'bits_per_digit')(30)\n"
        "import flux_stats.exact_yaml as exact_yaml\n"
        "print(exact_yaml._MOST_INTEGER_DIGITS)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    ).stdout
    assert out.strip() == "4300"


def test_the_loader_holds_a_document_to_the_bounds_whoever_drives_it() -> None:
    with pytest.raises(FluxError):
        list(yaml.load_all("r: &r [*r]\n", Loader=ExactLoader))


def test_without_pyyaml_reading_names_the_extra_that_installs_it() -> None:
    script = (
        "import sys\n"
        "sys.modules['yaml'] = None\n"
        "import flux_stats\n"
        "from flux_stats.exact_yaml import load_documents\n"
        "try:\n"
        "    load_documents('a: 1', name='x')\n"
        "except ImportError as error:\n"
        "    print(error)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120
    ).stdout
    assert "pip install 'flux-stats[yaml]'" in out
