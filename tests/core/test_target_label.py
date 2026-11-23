# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A sink target is printed and stored only as a label of its non-secret parts."""

from __future__ import annotations

import json
from typing import List, Tuple, Union
from urllib.parse import quote, quote_plus

import pytest

from flux_engine.core.target_label import (
    PLAIN_SINKS,
    WITHHELD,
    replace_target,
    spellings,
    target_label,
)

pytestmark = pytest.mark.unit

SECRET = "Zq7FAKEVAL1xY"


def _leaks(text: str) -> bool:
    """Whether *text* holds any piece of the fake secret, four characters or more."""
    return any(
        SECRET[start:stop] in text
        for start in range(len(SECRET))
        for stop in range(start + 4, len(SECRET) + 1)
    )


@pytest.mark.parametrize(
    "raw, label",
    [
        ("postgresql://u:pw@h:5432/db?sslmode=require", "postgresql://…@h:5432/db?…"),
        ("postgresql://h:5432/db", "postgresql://h:5432/db"),
        ("s3://bkt/pfx/run-a", "s3://bkt/pfx/run-a"),
        ("s3://bkt/pfx/run-a/", "s3://bkt/pfx/run-a/"),
        ("file:x.db?mode=ro", "file:x.db?…"),
        ("file:x.db", "file:x.db"),
        ("file:///abs/x.db", "file:///abs/x.db"),
        ("jdbc:mysql://u:pw@h/db", "jdbc:mysql://…@h/db"),
        ("//u:pw@h/db", "//…@h/db"),
        ("//h/db", "//h/db"),
        ("postgresql://h/db#frag", "postgresql://h/db#…"),
        ("postgresql://h/db?a=b#frag", "postgresql://h/db?…#…"),
        ("sqlite:$SIM_URI", "sqlite:$SIM_URI"),
        ("", ""),
        ("host=127.0.0.1 dbname=x port=5432", "host=127.0.0.1 dbname=x port=5432"),
        ("Server=h;Database=d;Uid=u;Pwd=x", "Server=h;Database=d (2 settings withheld)"),
        ("host=h password=x", "host=h (1 setting withheld)"),
        ("HOST=h SSLMode=require user=u", "HOST=h SSLMode=require (1 setting withheld)"),
        ("host=h dbname='a b'", "host=h (1 setting withheld)"),
        ("Driver={ODBC Driver 18};Server=h", "Server=h (1 setting withheld)"),
        ("user=u password=x", "(2 settings withheld)"),
    ],
)
def test_a_target_is_shown_by_the_label_its_shape_allows(raw: str, label: str) -> None:
    assert target_label("adbc", raw) == label


@pytest.mark.parametrize(
    "raw",
    [
        "out/run",
        "/abs/dir",
        "C:\\data\\out",
        "/tmp/My Drive/out",
        "out dir/run",
        "C:\\My Drive\\out",
    ],
)
def test_a_local_path_is_shown_whole_for_a_sink_that_writes_a_directory(raw: str) -> None:
    for sink in sorted(PLAIN_SINKS):
        assert target_label(sink, raw) == raw
    # No other sink's string is a path: it may be anything.
    assert target_label("adbc", raw).startswith("adbc target sha256:")
    assert target_label("sqlite", raw).startswith("sqlite target sha256:")


@pytest.mark.parametrize(
    "raw",
    [
        f"postgresql://u:pa#{SECRET}@h/db",
        f"postgresql://u:pa?{SECRET}@h/db",
        f"postgresql://u:pa/{SECRET}@h/db",
        f"postgresql://u:pa@{SECRET}@h/db",
        f"jdbc:mysql://u:{SECRET}@h/db",
        f"u:{SECRET}@h:5432/db",
        f"u:{SECRET}@h",
        f"//u:{SECRET}@h:5432/db",
        f"postgresql://h/db?password={SECRET}",
        f"postgresql://h/db?x=1#{SECRET}",
        f"host=h password\n=\n{SECRET}",
        f"host=h password\x0b=\x0c{SECRET}",
        f"host=h password = '{SECRET} x'",
        f"host=h password='a\\'{SECRET}'",
        f"host=h password=a\\ host={SECRET}",
        f"password=a;b{SECRET}",
        f"Password={{a}}}}{SECRET}}}",
        f"Server=h;Pwd={{a;{SECRET}}}",
        f"Server=h;Pwd={SECRET};Database=d",
        f"host=h password=x;dbname={SECRET}",
        f"host=h password='x host={SECRET}'",
        f"host={SECRET}!",
        f"out/{SECRET}@x",
        f"postgresql://u:p w@h/{SECRET}",
        f"Server=h;Pwd='{SECRET}",
        f"Server=h;Pwd={{{SECRET}",
        f"postgresql://h/{SECRET};x=1",
    ],
)
def test_no_label_holds_a_piece_of_a_secret_whatever_the_shape(raw: str) -> None:
    for sink in ("adbc", "parquet"):
        label = target_label(sink, raw)
        assert not _leaks(label), (raw, label)
        # A label is its own label.
        assert target_label(sink, label) == label


def test_a_path_with_white_space_is_shown_but_never_one_that_reads_as_another_shape() -> None:
    assert target_label("x", "host=h dbname=x") == "host=h dbname=x"
    assert target_label("x", "host=h password=x") == "host=h (1 setting withheld)"
    for raw in (f"u:{SECRET} x", f"user:pw {SECRET}/db", f"out dir/{SECRET}@x", f"a b?{SECRET}"):
        label = target_label("x", raw)
        assert not _leaks(label), (raw, label)
        assert label.startswith("x target sha256:")


def test_what_parses_as_neither_is_the_sink_name_and_a_short_digest() -> None:
    label = target_label("adbc", f"password=a;b{SECRET}")
    assert label.startswith("adbc target sha256:") and len(label) == len("adbc target sha256:") + 12
    assert target_label("adbc", f"password=a;b{SECRET}") == label
    assert target_label("parquet", f"password=a;b{SECRET}") != label
    assert target_label("adbc", "password=a;b other") != label


def test_user_information_is_everything_before_the_last_at() -> None:
    assert target_label("x", "postgresql://u:p@ss@h/db") == "postgresql://…@h/db"
    # A path holding an at sign is withheld whole, and nothing of the rest is shown.
    assert target_label("x", "postgresql://h/a@b") == "postgresql://…"


def test_a_shown_value_keeps_to_a_conservative_set() -> None:
    assert target_label("x", "host=[::1] port=5432 dbname=a/b.c_d-e:f") == (
        "host=[::1] port=5432 dbname=a/b.c_d-e:f"
    )
    assert target_label("x", "host=h$x") == "(1 setting withheld)"


def test_a_pair_after_a_withheld_one_with_no_space_is_withheld_with_it() -> None:
    # Read as libpq, the secret's value would run on to the next pair.
    assert target_label("x", "Pwd=a;Database=d") == "(2 settings withheld)"
    assert target_label("x", "Pwd=a; Database=d") == "Database=d (1 setting withheld)"


def test_a_label_does_not_take_quadratic_time() -> None:
    hostile = "a:" * 5000 + "@" * 5000
    assert target_label("x", hostile)
    assert target_label("x", "k=" + "'" * 20000)
    assert target_label("x", "postgresql://" + "u:p@" * 5000 + "h/db").endswith("@h/db")


# -- the message a library writes ------------------------------------------------


def test_a_message_that_echoes_the_target_prints_its_label() -> None:
    raw = f"postgresql://u:{SECRET}@h:5432/db?sslmode=require"
    for echo in (raw, raw.replace("://", ":/")):
        text = replace_target(f"could not connect to {echo}: refused", "adbc", [raw])
        assert text == "could not connect to postgresql://…@h:5432/db?…: refused"


def test_a_message_that_echoes_only_a_withheld_part_prints_the_ellipsis() -> None:
    raw = f"postgresql://u:{SECRET}@h/db?token={SECRET}x"
    text = replace_target(
        f"password {SECRET} rejected for u:{SECRET}; token {SECRET}x", "adbc", [raw]
    )
    assert not _leaks(text)
    assert WITHHELD in text


def test_a_message_that_echoes_a_dsn_value_prints_the_label() -> None:
    raw = f"host=h dbname=x password='{SECRET} z'"
    text = replace_target(f"FATAL: bad {raw} / {SECRET} z", "adbc", [raw])
    assert not _leaks(text)
    assert "host=h dbname=x (1 setting withheld)" in text


def test_a_message_with_nothing_known_is_unchanged() -> None:
    assert replace_target("no such file: x.db", "adbc", []) == "no such file: x.db"
    assert replace_target("no such file: out/run", "parquet", ["out/run", ""]) == (
        "no such file: out/run"
    )


def test_a_short_withheld_part_is_left_in_a_message() -> None:
    assert replace_target("a is ok at h", "adbc", ["postgresql://a:b@h/db?x=1"]) == "a is ok at h"


def test_a_variables_value_is_written_as_the_variables_name() -> None:
    value = f"postgresql://u:{SECRET}@h/db"
    text = replace_target(f"cannot open {value}", "adbc", [(value, "$SIM_URI")])
    assert text == "cannot open $SIM_URI"


def test_a_string_too_short_to_replace_safely_is_left_alone() -> None:
    assert replace_target("the parquet sink needs a directory", "parquet", [" ", "ab"]) == (
        "the parquet sink needs a directory"
    )


# -- round three: the leaks an independent check found -----------------------------


@pytest.mark.parametrize(
    "raw",
    [
        f"Bearer {SECRET}",
        f"Token {SECRET}",
        f"password:{SECRET}",
        f'"password": "{SECRET}"',
        f"sqlite:password:{SECRET}",
        f"{SECRET}",
        f"u:{SECRET}@h:5432/db",
    ],
)
def test_a_string_that_is_no_url_is_never_shown_as_a_path_by_a_sink_that_takes_a_uri(
    raw: str,
) -> None:
    for sink in ("adbc", "sqlite", "duckdb", "postgresql", "option"):
        label = target_label(sink, raw)
        assert not _leaks(label), (sink, raw, label)
        assert label.startswith(f"{sink} target sha256:")
        assert target_label(sink, label) == label


def test_only_a_file_url_is_shown_as_scheme_and_opaque_part() -> None:
    assert target_label("sqlite", "file:x.db") == "file:x.db"
    assert target_label("sqlite", "file:x.db?mode=ro") == "file:x.db?…"
    assert target_label("sqlite", "file:a;b=c") == "file:…"
    assert target_label("sqlite", f"urn:{SECRET}").startswith("sqlite target sha256:")


@pytest.mark.parametrize(
    "raw, label",
    [
        (f"postgresql://h:{SECRET}/db", "postgresql://…/db"),
        (f"postgresql://u:{SECRET}%40h/db", "postgresql://…/db"),
        ("postgresql://h:5432/db", "postgresql://h:5432/db"),
        ("postgresql://[::1]:5432/db", "postgresql://[::1]:5432/db"),
        ("postgresql://h1:5432,h2:5433/db", "postgresql://h1:5432,h2:5433/db"),
        ("postgresql://h%40x:5432/db", "postgresql://…/db"),
    ],
)
def test_a_host_is_shown_only_when_it_is_one_and_a_port_only_when_it_is_digits(
    raw: str, label: str
) -> None:
    assert target_label("adbc", raw) == label
    assert target_label("adbc", label) == label


@pytest.mark.parametrize(
    "path", [f"db%3Fpassword%3D{SECRET}", f"db;{SECRET}", f"db={SECRET}", f"{SECRET}%40x"]
)
def test_a_path_holding_a_delimiter_or_an_escape_is_withheld(path: str) -> None:
    label = target_label("adbc", f"postgresql://h/{path}")
    assert label == "postgresql://h/…"
    assert target_label("adbc", label) == label


def test_a_slash_before_the_last_at_sign_leaves_only_the_scheme() -> None:
    assert target_label("parquet", f"s3://bkt/pfx@{SECRET}/run") == "s3://…"
    assert target_label("adbc", f"jdbc:mysql://a/b@{SECRET}") == "jdbc:mysql://…"
    # The text after the sign is withheld from a message too.
    text = replace_target(f"no such key {SECRET}", "parquet", [f"s3://bkt/pfx@{SECRET}/run"])
    assert not _leaks(text)


def test_a_message_holding_the_encoded_or_quoted_form_of_a_known_string_is_written_as_its_label() -> (
    None
):
    raw = f"postgresql://u:{SECRET} x@h/db"
    for echo in (quote(raw, safe=""), quote_plus(raw), repr(raw)[1:-1], json.dumps(raw)[1:-1]):
        text = replace_target(f"driver said {echo} twice", "adbc", [raw])
        assert not _leaks(text), (echo, text)
    # A part the label withholds, in an encoded spelling.
    part = f"{SECRET}/ab"
    text = replace_target(f"bad {quote(part, safe='')}", "adbc", [f"postgresql://h/db?k={part}"])
    assert not _leaks(text)


def test_replacing_a_large_string_is_linear_and_uses_no_pattern() -> None:
    import time

    big = " " * 100_000 + SECRET
    text = f"error: {big} end"
    raws: List[Union[str, Tuple[str, str]]] = [big, (big[::-1], "$VAR")]
    raws += [f"{SECRET}{n}" for n in range(200)]
    start = time.perf_counter()
    result = replace_target(text, "adbc", raws)
    elapsed = time.perf_counter() - start
    assert not _leaks(result)
    # Work done, not a clock: ten seconds is two orders above what this takes.
    assert elapsed < 10.0


def test_a_replacement_is_never_searched_again() -> None:
    raw = "postgresql://u:abcdef@host/db"
    # The label holds ``host``, a part nobody withheld; "abcdef" is replaced once.
    assert replace_target(f"x {raw} y abcdef", "adbc", [raw]) == "x postgresql://…@host/db y …"


def test_the_spellings_of_a_string_are_its_encoded_and_quoted_forms() -> None:
    forms = spellings("a b'c")
    assert forms[0] == "a b'c" and "a%20b%27c" in forms and "a+b%27c" in forms
    assert len(set(forms)) == len(forms)


def test_a_value_that_ends_in_a_line_break_is_replaced_where_a_library_echoes_it_whole() -> None:
    for ending in ("\n", " ", "\r\n", "\x85", " \t"):
        raw = f"file:x.db?mode=ro&cache={SECRET}{ending}"
        text = replace_target(f"failed to open '{raw.strip()}' now", "adbc", [raw])
        assert not _leaks(text), (ending, text)
    multi = f"first {SECRET}\nsecond {SECRET}"
    assert not _leaks(replace_target(f"saw first {SECRET} here", "adbc", [multi]))


def test_a_digest_labelled_string_still_has_its_secret_positions_replaced_in_a_message() -> None:
    raw = f"md:sim?motherduck_token={SECRET}"
    assert target_label("adbc", raw).startswith("adbc target sha256:")
    for echo in (f"token {SECRET} rejected", f"motherduck_token%3D{SECRET}", f"x=1&k={SECRET}"):
        text = replace_target(echo, "adbc", [raw])
        assert not _leaks(text), (echo, text)
    # Keys, schemes and ordinary words stay readable.
    text = replace_target("md:sim failed: motherduck_token unknown", "adbc", [raw])
    assert "motherduck_token unknown" in text
    userinfo = f"user:{SECRET}@account"
    assert not _leaks(replace_target(f"login {SECRET}", "adbc", [userinfo]))


def test_an_iceberg_word_is_a_label_for_the_iceberg_sink_only() -> None:
    assert target_label("iceberg", "iceberg:cat/ns") == "iceberg:cat/ns"
    for sink in ("adbc", "sqlite", "parquet", "option"):
        label = target_label(sink, f"iceberg:{SECRET}")
        assert label.startswith(f"{sink} target sha256:")
