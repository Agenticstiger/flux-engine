# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` keeps a contract's date order on a history's snapshot when the release proves none (ADR 0024).

A library's loans have versions, and the table of the current loans orders two of its dates: a loan
that is closed was closed on or after the day it was signed. The closing day is a rule of the
versions (the start of the version a loan is closed in), and the signing day is a plain column of
them. A private release can list the two dates as a pair and publish no order for it (a cell is
suppressed), or publish no pair; the two dates were then drawn apart, and the check broke on a share
of the loans above its band.

Pinned here: with no readable order for the pair the history keeps the signing day with the first
start, so the check holds on every loan, and the register says so; a release that publishes the
order, a signing day that a rule sets, and a check on a column that is no version's start or end keep
the bundle they gave before.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from tests.cli.test_init_dates import HOLDS, _pair
from tests.cli.test_init_dates import _levels as _order

from .test_history_init import _metrics, _ms, _namespace
from .test_init_snapshot_beside_a_count import _library, _published, _rows, _written

pytestmark = pytest.mark.integration

#: The closing day of a loan: the start of a version of a loan that is closed in it.
_DERIVE = """\
      invariants:
        - id: loan_closed_on_start
          derive: closed_on
          expr: CASE WHEN channel = 'web' THEN valid_from END
"""
_CHECK = """\
      invariants:
        - id: loan_closed_after_signed
          check: closed_on IS NULL OR closed_on >= signed_on
          mustBeLessOrEqualTo: 5
          unit: percent
"""
_CLOSED = "        - { name: closed_on, type: DATE, required: false }\n"
_SIGNED = "        - { name: signed_on, type: DATE, required: true }\n"


def _dated(derive: str = _DERIVE) -> str:
    """The library whose loans, and their versions, hold a signing and a closing day."""
    text = _library()
    versions, current = text.split("  - exposeId: loan_current\n")
    versions = versions.replace(
        "    semantics:\n      entities:\n        - { name: loan_version",
        derive + "    semantics:\n      entities:\n        - { name: loan_version",
        1,
    )
    versions = versions.replace(
        "        - { name: signed_on, type: DATE, required: true }\n",
        "        - { name: signed_on, type: DATE, required: true }\n" + _CLOSED,
        1,
    )
    versions = versions.replace(
        "        - { name: signed_on, type: time }\n",
        "        - { name: signed_on, type: time }\n        - { name: closed_on, type: time }\n",
        1,
    )
    current = current.replace(
        "        - { name: channel, type: STRING, required: true }\n",
        "        - { name: channel, type: STRING, required: true }\n" + _SIGNED + _CLOSED,
        1,
    ).replace(
        "    semantics:\n",
        _CHECK + "    semantics:\n",
        1,
    )
    current = current.replace(
        "        - { name: channel, type: categorical }\n",
        "        - { name: channel, type: categorical }\n"
        "        - { name: signed_on, type: time }\n"
        "        - { name: closed_on, type: time }\n",
        1,
    )
    return versions + "  - exposeId: loan_current\n" + current


def _stamps(rows: int) -> Dict[str, Any]:
    low, high = _ms(2025, 1, 1), _ms(2025, 9, 1)
    return {"domain": "timestamp", "metrics": _metrics(low, high, rows)}


def _profile_of(pairs: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The library's profile with the two dates, and *pairs* listed for the current loans."""
    profile = _published(counted=False)
    versions, current = profile["tables"]["loan_version"], profile["tables"]["loan_current"]
    versions["columns"]["closed_on"] = _stamps(1850)
    current["columns"]["signed_on"] = _stamps(1000)
    current["columns"]["closed_on"] = _stamps(1000)
    if pairs is not None:
        current["datePairs"] = pairs
    return profile


def _released(pair: Dict[str, Any]) -> Dict[str, Any]:
    """*pair* disclosed by the rule this profile declares."""
    text = json.dumps(pair).replace('"disclosure": "none"', '"disclosure": "acro-default"')
    found: Dict[str, Any] = json.loads(text)
    return found


#: A pair the release lists and publishes no order for: its cells are suppressed.
NOISY = [_released(_pair("signed_on", "closed_on", _order(after=None, before=None, same=None)))]


def _kept_with_first_start(tmp_path: Path) -> Optional[str]:
    """The column the written history keeps with each entity's first start, if any."""
    column: Optional[str] = _namespace(tmp_path)["history"][0].get("activation")
    return column


def _closed_after_signed(tmp_path: Path) -> List[bool]:
    # loan_id, shelf_id, tier, channel, signed_on, closed_on
    return [row[5] is None or row[5] >= row[4] for row in _rows(tmp_path, "loan_current")]


@pytest.mark.parametrize("pairs", [NOISY, None], ids=["suppressed order", "no pair"])
def test_a_release_with_no_readable_order_keeps_the_signing_day_with_the_first_start(
    tmp_path: Path, pairs: Optional[List[Dict[str, Any]]]
) -> None:
    result = _written(tmp_path, _dated(), _profile_of(pairs))
    assert result.exit_code == 0, result.stderr
    assert _kept_with_first_start(tmp_path) == "signed_on"
    assert any(
        "history activation" in line and "signed_on" in line for line in result.stdout.splitlines()
    )
    held = _closed_after_signed(tmp_path)
    assert held and all(held)


def _digest(folder: Path) -> str:
    """One digest of every file the bundle in *folder* holds, by path."""
    found = hashlib.sha256()
    for path in sorted((folder / "bundle").rglob("*")):
        if path.is_file():
            found.update(path.relative_to(folder).as_posix().encode("utf-8"))
            found.update(path.read_bytes())
    return found.hexdigest()


def _render(
    root: Path,
    name: str,
    contract: str,
    profile: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    collect: bool,
) -> Path:
    """The bundle of *contract* and *profile* in a folder of *root*, with the orders read or left off."""
    from flux_engine.author import bundle

    folder = root / name
    folder.mkdir()
    with monkeypatch.context() as patch:
        if not collect:
            patch.setattr(bundle, "_orders_of_versions", lambda *args, **kwargs: ())
        _written(folder, contract, profile)
    return folder


def _lifecycle_declared(contract: str) -> str:
    return contract.replace(
        "{ name: plan, type: natural, expr: loan_id }",
        "name: plan\n          type: natural\n          expr: loan_id\n"
        "          lifecycle:\n            activation: renewed_on",
        1,
    ).replace(
        "        - { name: signed_on, type: DATE, required: true }\n" + _CLOSED,
        "        - { name: signed_on, type: DATE, required: true }\n"
        + _CLOSED
        + "        - { name: renewed_on, type: DATE, required: true }\n",
        1,
    )


def _case(name: str) -> Any:
    """The contract and the profile of one shape that must keep its bytes."""
    profile = _profile_of(NOISY)
    contract = _dated()
    if name == "published order":
        profile = _profile_of([_released(_pair("signed_on", "closed_on", HOLDS))])
    elif name == "strict check":
        contract = contract.replace("closed_on >= signed_on", "closed_on > signed_on", 1)
    elif name == "reversed direction":
        contract = contract.replace(
            "closed_on IS NULL OR closed_on >= signed_on",
            "signed_on IS NULL OR signed_on >= closed_on",
            1,
        )
    elif name == "derive with when":
        contract = contract.replace(
            "          derive: closed_on\n",
            "          derive: closed_on\n          when: \"channel = 'web'\"\n",
            1,
        )
    elif name == "derive of no validity column":
        contract = _dated(derive=_DERIVE.replace("valid_from", "signed_on"))
    elif name == "contract lifecycle.activation":
        contract = _lifecycle_declared(contract)
    return contract, profile


KEEPS = [
    "published order",
    "strict check",
    "reversed direction",
    "derive with when",
    "derive of no validity column",
    "contract lifecycle.activation",
]


@pytest.mark.parametrize("name", KEEPS)
def test_a_shape_the_order_does_not_read_keeps_the_bundle_it_gave_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    contract, profile = _case(name)
    read = _render(tmp_path, "read", contract, profile, monkeypatch, collect=True)
    off = _render(tmp_path, "off", contract, profile, monkeypatch, collect=False)
    assert _digest(read) == _digest(off)


def test_the_orders_read_change_the_bundle_of_a_release_with_no_readable_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, profile = _dated(), _profile_of(NOISY)
    read = _render(tmp_path, "read", contract, profile, monkeypatch, collect=True)
    off = _render(tmp_path, "off", contract, profile, monkeypatch, collect=False)
    assert _digest(read) != _digest(off)


#: A day the release publishes on the start's day: the pair of the start and the signing day.
_TIE = _released(_pair("valid_from", "signed_on", _order(after=0, before=0, same=1000)))


def test_a_signing_day_published_on_the_starts_day_keeps_the_history_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile_of(NOISY)
    profile["tables"]["loan_version"]["datePairs"] = [_TIE]
    profile["tables"]["loan_version"]["history"].pop("versionLength", None)
    read = _render(tmp_path, "read", _dated(), profile, monkeypatch, collect=True)
    off = _render(tmp_path, "off", _dated(), profile, monkeypatch, collect=False)
    assert _digest(read) == _digest(off)
    assert _namespace(read).get("history", []) == []


def test_a_declared_lifecycle_activation_still_lifts_the_refusal(tmp_path: Path) -> None:
    profile = _profile_of(NOISY)
    profile["tables"]["loan_version"]["datePairs"] = [
        _released(_pair("valid_from", "renewed_on", _order(after=0, before=0, same=1000)))
    ]
    profile["tables"]["loan_version"]["columns"]["renewed_on"] = _stamps(1850)
    profile["tables"]["loan_version"]["history"].pop("versionLength", None)
    _written(tmp_path, _lifecycle_declared(_dated()), profile)
    assert _kept_with_first_start(tmp_path) == "renewed_on"


def _nullable(profile: Dict[str, Any], nulls: int) -> str:
    """The library whose current loans may hold no signing day, and *nulls* of them hold none."""
    metrics = profile["tables"]["loan_current"]["columns"]["signed_on"]["metrics"]
    metrics["nullCount"] = {**metrics["count"], "definition": "nullCount/1", "value": nulls}
    return _dated().replace(_SIGNED, _SIGNED.replace("required: true", "required: false"))


def test_a_signing_day_that_may_be_null_keeps_its_own_draw(tmp_path: Path) -> None:
    profile = _profile_of(NOISY)
    contract = _nullable(profile, 100)
    result = _written(tmp_path, contract, profile)
    assert _kept_with_first_start(tmp_path) is None
    assert any(
        "history activation" in line and "NULL" in line for line in result.stdout.splitlines()
    )
    (line,) = [item for item in result.stdout.splitlines() if "history activation" in item]
    assert not any(character.isdigit() for character in line.split("may be NULL")[-1])
    column = _namespace(tmp_path)["emits"]["loan_current"]["fidelity"]["columns"]["signed_on"]
    assert Fraction(column["nullFraction"]) > 0


def test_a_nullable_signing_day_with_no_nulls_published_is_kept_with_the_first_start(
    tmp_path: Path,
) -> None:
    profile = _profile_of(NOISY)
    contract = _nullable(profile, 0)
    _written(tmp_path, contract, profile)
    assert _kept_with_first_start(tmp_path) == "signed_on"


_RENEWED = "        - { name: renewed_on, type: DATE, required: true }\n"
_SECOND = """\
        - id: z_loan_closed_after_renewed
          check: closed_on IS NULL OR closed_on >= renewed_on
          mustBeLessOrEqualTo: 5
          unit: percent
"""


def test_a_second_check_that_qualifies_is_named_as_left_unkept(tmp_path: Path) -> None:
    contract = (
        _dated()
        .replace(_CLOSED, _CLOSED + _RENEWED)
        .replace(
            "        - { name: closed_on, type: time }\n",
            "        - { name: closed_on, type: time }\n        - { name: renewed_on, type: time }\n",
        )
        .replace(_CHECK, _CHECK + _SECOND, 1)
    )
    profile = _profile_of(NOISY)
    for table in ("loan_version", "loan_current"):
        profile["tables"][table]["columns"]["renewed_on"] = _stamps(1000)
    result = _written(tmp_path, contract, profile)
    assert _kept_with_first_start(tmp_path) == "signed_on"
    lines = [line for line in result.stdout.splitlines() if "history activation" in line]
    assert any("z_loan_closed_after_renewed" in line and "unkept" in line for line in lines)


def test_a_closing_day_that_is_no_version_start_leaves_the_signing_day_alone(
    tmp_path: Path,
) -> None:
    contract = _dated(derive=_DERIVE.replace("valid_from", "signed_on"))
    _written(tmp_path, contract, _profile_of(NOISY))
    assert _kept_with_first_start(tmp_path) is None
