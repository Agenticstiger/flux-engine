# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A secret in the text a plugin supplies is in no refusal: not whole, and not in a piece.

A plugin's message is free text. ``FLX-3124`` quotes the first line of what its import or factory
raised, ``FLX-3120`` quotes what it stated, and a refusal names a module it lacks; each is scrubbed
whole before a line is picked or a length cut, so a secret that straddles the cut leaves no four
characters of itself behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from flux_engine.cli import main
from flux_engine.generators import registry

from .conftest import GENERATING_PLUGIN, make_distribution, plugin_bundle

pytestmark = pytest.mark.integration

#: A fake secret with no four-character piece that ordinary output holds.
_SECRET = "Zq7FAKEVAL1xY"
_VARIABLE = "FLUX_TEST_PLUGIN_API_TOKEN"
#: Where the first line of a plugin's failure is cut.
_CUT = registry._FIRST_LINE


def _leaked(blob: str, secret: str = _SECRET) -> List[str]:
    """Every piece of *secret*, four characters or more, that *blob* holds."""
    return [
        secret[start:stop]
        for start in range(len(secret))
        for stop in range(start + 4, len(secret) + 1)
        if secret[start:stop] in blob
    ]


def _plan(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source: str,
    **bundle: object,
) -> str:
    make_distribution(site, plugin_source=source)
    code = main(
        [
            "plan",
            str(plugin_bundle(tmp_path, **bundle)),  # type: ignore[arg-type]
            "--state-dir",
            str(tmp_path / "state"),
            "--allow-generator",
            "learned",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1, captured.err
    return captured.out + captured.err


def _raising(message: str, *, where: str = "import") -> str:
    if where == "import":
        return f"raise RuntimeError({message!r})\ndef factory(options):\n    return options\n"
    return f"def factory(options):\n    raise RuntimeError({message!r})\n"


_MESSAGES = {
    "an environment credential": f"service refused the key {_SECRET}",
    "an environment credential across the cut": "x" * (_CUT - 5) + _SECRET,
    "a url": f"cannot reach postgresql://svc:{_SECRET}@db.internal/sim",
    "a url across the cut": "x" * (_CUT - 20) + f" postgresql://svc:{_SECRET}@db/sim",
    "a connection string pair": f"bad dsn host=db password={_SECRET} dbname=sim",
    "a pair across the cut": "x" * (_CUT - 12) + f" token={_SECRET} dbname=sim",
}


@pytest.mark.parametrize("where", ["import", "factory"])
@pytest.mark.parametrize("message", list(_MESSAGES.values()), ids=list(_MESSAGES))
def test_a_secret_in_what_a_plugin_raised_is_in_no_refusal(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    where: str,
) -> None:
    monkeypatch.setenv(_VARIABLE, _SECRET)
    output = _plan(site, tmp_path, capsys, _raising(message, where=where))
    assert "FLX-3124" in output and "RuntimeError" in output, output
    assert _leaked(output) == [], output


def test_a_secret_in_the_name_of_a_missing_module_is_in_no_refusal(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_VARIABLE, _SECRET)
    source = f"import {_SECRET}\ndef factory(options):\n    return options\n"
    output = _plan(site, tmp_path, capsys, source)
    assert "FLX-3124" in output and "a module it imports" in output, output
    assert _leaked(output) == [], output


def test_a_secret_in_what_a_plugin_stated_is_in_no_refusal(
    site: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_VARIABLE, _SECRET)
    stated = "x" * 50 + _SECRET
    output = _plan(site, tmp_path, capsys, GENERATING_PLUGIN, options={"stated": stated})
    assert "FLX-3120" in output and "stated" in output, output
    assert _leaked(output) == [], output


def test_the_failure_text_is_scrubbed_before_its_first_line_is_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_VARIABLE, _SECRET)
    text = registry.failure_text(RuntimeError("x" * (_CUT - 5) + _SECRET + "\nsecond line"))
    assert text.startswith("RuntimeError: xxxx") and "second line" not in text
    assert _leaked(text) == [], text


def test_a_plugins_text_without_a_secret_is_left_as_it_is() -> None:
    assert registry.failure_text(ValueError("bad factory")) == "ValueError: bad factory"
    assert registry.failure_text(KeyError()) == "KeyError"
