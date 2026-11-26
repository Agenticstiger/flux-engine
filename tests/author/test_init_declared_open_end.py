# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes the open end and the chain a contract declares into a table's history.

FLUID 0.7.7 declares a versioned table's conventions on its validity window's end dimension:
``validityParams.openEnd``, the value an open version's end holds, and ``chain``, how a closed
version's end meets the next start. ``init`` copies ``openEnd`` into the history entry, so an end
column the contract requires no longer keeps the history out, and the open versions end on
exactly that value. The value never comes from the profile, and it gives the run's window
nothing. A declared chain sets the entry's chain; one the engine does not generate leaves the
history out with the reason.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run

from .test_history_date_pairs import TIERS, _block, _init, _profile
from .test_history_init import _metrics, _ms

pytestmark = pytest.mark.integration

#: The last day, as a profile publishes it in milliseconds.
LAST_DAY_MS = "253402214400000"


def _declaring(
    open_end: Optional[str] = "9999-12-31",
    *,
    chain: Optional[str] = None,
    required: bool = True,
    null: bool = False,
) -> str:
    """``TIERS`` whose validity end declares *open_end* (``null`` with *null*) and *chain*."""
    params = ["isEnd: true"]
    if null:
        params.append("openEnd: null")
    elif open_end is not None:
        params.append(f'openEnd: "{open_end}"')
    if chain is not None:
        params.append(f"chain: {chain}")
    contract = TIERS.replace(
        "validityParams: { isEnd: true }", "validityParams: { " + ", ".join(params) + " }"
    )
    if required:
        contract = contract.replace(
            "{ name: valid_to, type: DATE, required: false }",
            "{ name: valid_to, type: DATE, required: true }",
        )
    assert contract != TIERS
    return contract


def _ending_on(profile: Dict[str, Any], low: str, high: str) -> Dict[str, Any]:
    """*profile* whose versions' ends run from *low* to *high*, both milliseconds."""
    profile["tables"]["tier_version"]["columns"]["valid_to"] = {
        "domain": "timestamp",
        "metrics": _metrics(low, high, 1850),
    }
    return profile


def _history(tmp_path: Path) -> Optional[Dict[str, Any]]:
    entries = _block(tmp_path).get("history")
    return dict(entries[0]) if entries else None


def _history_reasons(result: Any) -> List[str]:
    return [line for line in result.stdout.splitlines() if "/tables/tier_version/history:" in line]


def _rows(tmp_path: Path) -> List[List[Any]]:
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "tiers.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    outcome = run(
        tmp_path / "bundle", state_dir=tmp_path / "run", profile=tmp_path / "tiers.profile.json"
    )
    assert outcome.success, outcome.stderr
    (stream,) = (tmp_path / "run").glob("runs/*/emits/tier_version.ndjson")
    return [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines() if line]


# A row of tier_version: version_id, pass_id, member_id, valid_from, valid_to, tier, ...
PASS, START, END = 1, 3, 4


def _open_versions(rows: List[List[Any]]) -> Counter:
    """How many of each entity's last versions end on each value."""
    last: Dict[Any, List[Any]] = defaultdict(list)
    for row in rows:
        if not last[row[PASS]] or row[START] > last[row[PASS]][START]:
            last[row[PASS]] = row
    return Counter(row[END] for row in last.values())


def test_a_declared_open_end_is_written_and_open_versions_end_on_it(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(), _declaring())
    assert result.exit_code == 0, result.stderr
    history = _history(tmp_path)
    assert history is not None and history["openEnd"] == "9999-12-31"
    rows = _rows(tmp_path)
    ends = _open_versions(rows)
    # The history closes 15 % of the entities' last versions; every other entity is open.
    assert ends["9999-12-31"] == 850
    assert all(row[END] == "9999-12-31" or row[END] < "9999" for row in rows)
    # Only a last version is ever open.
    assert sum(1 for row in rows if row[END] == "9999-12-31") == 850


def test_a_published_open_end_gives_the_window_nothing(tmp_path: Path) -> None:
    # The open versions' 9999-12-31 is the column's published maximum: the window and the history
    # still end where the data does.
    profile = _ending_on(_profile(), _ms(2025, 2, 1), LAST_DAY_MS)
    result = _init(tmp_path, profile, _declaring())
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    window = yaml.safe_load(text)["spec"]["timeBounds"]
    assert window["end"] < "9999"
    history = _history(tmp_path)
    assert history is not None and history["until"] < "9999"
    assert _open_versions(_rows(tmp_path))["9999-12-31"] == 850


#: The last instant of the year ladder a profile bins a date's quantiles on.
LAST_INSTANT_MS = "253402300799999"


def _quartiles_past_the_data(
    profile: Dict[str, Any],
    closed: int,
    ladder: Optional[List[str]] = None,
    minimum: Optional[str] = None,
    maximum: Optional[str] = None,
) -> Dict[str, Any]:
    """*profile* whose ends publish every quartile in the bin of the open end, and no *minimum*.

    The quartiles are binned on a ladder of years whose last bin runs from 2100 to the last
    instant, unless *ladder* gives other edges, so the profile says only that *closed* versions
    end somewhere below that bin. With *maximum*, the last bin and the published maximum end
    there, before the open end.
    """
    if ladder is None:
        ladder = [_ms(year, 1, 1) for year in range(2020, 2101)]
    ladder = ladder + [LAST_INSTANT_MS if maximum is None else maximum]
    high = LAST_DAY_MS if maximum is None else maximum
    metrics = _metrics("0" if minimum is None else minimum, high, 1850)
    if minimum is None:
        del metrics["min"]["value"]
        metrics["min"]["status"] = "A"
    group = {
        "group": "",
        "k": 1850,
        "quantileBin": len(ladder) - 2,
        "binBelow": closed,
        "binThrough": 1850,
        "status": "F",
    }
    profile["tables"]["tier_version"]["columns"]["valid_to"] = {
        "domain": "timestamp",
        "metrics": metrics,
        "statistics": [
            {
                "definition": "quantile/2",
                "level": level,
                "bins": ladder,
                "sampleSize": metrics["count"]["sampleSize"],
                "disclosure": "acro-default",
                "groups": [dict(group)],
            }
            for level in ("0.25", "0.5", "0.75")
        ],
    }
    return profile


def test_closed_ends_below_every_published_quartile_lie_where_the_versions_start(
    tmp_path: Path,
) -> None:
    # A closed version ends the day before the next one starts, so the ends the profile places
    # only below 2100 are drawn over the starts' span: the bin next to the open end would
    # stretch the run's window to 2100.
    profile = _quartiles_past_the_data(_profile(), closed=300)
    result = _init(tmp_path, profile, _declaring(chain="dayBefore"))
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["spec"]["timeBounds"]["end"] < "2026"
    column = _block(tmp_path)["emits"]["tier_version"]["fidelity"]["columns"]["valid_to"]
    assert column["bins"] == ["2025-01-01", "2025-09-01", "9999-12-31"]
    # The register says where the moved bin's rows now lie.
    reasons = [
        str(item.get("reason"))
        for item in _block(tmp_path)["parameters"]
        if item["pointer"].endswith("/columns/valid_to/binWeights/0")
    ]
    assert reasons and all("over the span the window's start column" in text for text in reasons)
    rows = _rows(tmp_path)
    assert all(row[END] == "9999-12-31" or row[END] < "2026" for row in rows)


def test_closed_ends_of_starts_within_one_day_hold_that_day(tmp_path: Path) -> None:
    # The versions start within one morning, and the end column holds whole days: the closed ends
    # lie on that day.
    contract = _declaring(chain="dayBefore").replace(
        "{ name: valid_from, type: DATE, required: true }",
        "{ name: valid_from, type: TIMESTAMP, required: true }",
    )
    assert "valid_from, type: TIMESTAMP" in contract
    morning = _ms(2025, 3, 1)
    profile = _quartiles_past_the_data(_profile(), closed=300)
    profile["tables"]["tier_version"]["columns"]["valid_from"] = {
        "domain": "timestamp",
        "metrics": _metrics(morning, str(int(morning) + 5 * 3_600_000), 1850),
    }
    result = _init(tmp_path, profile, contract)
    assert result.exit_code == 0, result.stderr
    column = _block(tmp_path)["emits"]["tier_version"]["fidelity"]["columns"]["valid_to"]
    assert column["bins"] == ["2025-03-01", "2025-03-02", "9999-12-31"]


def test_closed_ends_stay_below_the_bin_of_the_open_end_that_starts_inside_the_data(
    tmp_path: Path,
) -> None:
    # The bin of the open end starts in June, before the last start: the profile counts the
    # closed ends below it, so they are drawn from the first start to June.
    ladder = [_ms(year, 1, 1) for year in range(2020, 2025)] + [_ms(2025, 6, 1)]
    profile = _quartiles_past_the_data(_profile(), closed=300, ladder=ladder)
    result = _init(tmp_path, profile, _declaring(chain="dayBefore"))
    assert result.exit_code == 0, result.stderr
    column = _block(tmp_path)["emits"]["tier_version"]["fidelity"]["columns"]["valid_to"]
    assert column["bins"] == ["2025-01-01", "2025-06-01", "9999-12-31"]


def test_a_published_minimum_keeps_the_closed_ends_where_the_profile_puts_them(
    tmp_path: Path,
) -> None:
    profile = _quartiles_past_the_data(_profile(), closed=300, minimum=_ms(2025, 3, 1))
    result = _init(tmp_path, profile, _declaring(chain="dayBefore"))
    assert result.exit_code == 0, result.stderr
    column = _block(tmp_path)["emits"]["tier_version"]["fidelity"]["columns"]["valid_to"]
    assert column["bins"][0] == "2025-03-01"


def test_a_last_bin_that_ends_before_the_open_end_keeps_the_bin_below_it(tmp_path: Path) -> None:
    # No end the profile publishes is the open end: the last bin is data, and so is the one below.
    ladder = [_ms(year, 1, 1) for year in range(2020, 2025)] + [_ms(2025, 6, 1)]
    ladder.remove(_ms(2024, 1, 1))
    profile = _quartiles_past_the_data(_profile(), 300, ladder=ladder, maximum=_ms(2025, 10, 1))
    result = _init(tmp_path, profile, _declaring(chain="dayBefore"))
    assert result.exit_code == 0, result.stderr
    column = _block(tmp_path)["emits"]["tier_version"]["fidelity"]["columns"]["valid_to"]
    assert column["bins"] == ["2023-01-01", "2025-06-01", "2025-10-01"]


def test_closed_ends_keep_to_the_range_the_contract_declares_for_the_starts(
    tmp_path: Path,
) -> None:
    contract = _declaring(chain="dayBefore").replace(
        "{ name: valid_from, type: DATE, required: true }",
        "{ name: valid_from, type: DATE, required: true, validationRules: "
        "[{ type: range, constraint: \"between '2025-02-01' and '2025-08-01'\" }] }",
    )
    profile = _quartiles_past_the_data(_profile(), closed=300)
    result = _init(tmp_path, profile, contract)
    assert result.exit_code == 0, result.stderr
    column = _block(tmp_path)["emits"]["tier_version"]["fidelity"]["columns"]["valid_to"]
    assert column["bins"] == ["2025-02-01", "2025-08-01", "9999-12-31"]


def test_an_end_column_holding_only_the_open_end_is_written_by_the_history(
    tmp_path: Path,
) -> None:
    # Every published end is 9999-12-31, a value no draw can write on every row: the declared
    # open end lets the history write it.
    profile = _ending_on(_profile(), LAST_DAY_MS, LAST_DAY_MS)
    result = _init(tmp_path, profile, _declaring())
    assert result.exit_code == 0, result.stderr
    history = _history(tmp_path)
    assert history is not None and history["openEnd"] == "9999-12-31"
    assert _open_versions(_rows(tmp_path))["9999-12-31"] == 850


def test_without_a_declared_open_end_the_one_value_names_the_declaration(tmp_path: Path) -> None:
    profile = _ending_on(_profile(), LAST_DAY_MS, LAST_DAY_MS)
    result = _init(tmp_path, profile, _declaring(None))
    assert result.exit_code == 0, result.stderr
    assert _history(tmp_path) is None
    (reason,) = _history_reasons(result)
    declare = reason.index("declare 9999-12-31 as the window's openEnd")
    assert declare < reason.index("write its open end as NULL")


@pytest.mark.parametrize(
    ("contract", "said"),
    [
        (_declaring(None), "the contract declares no open end"),
        (_declaring(null=True), "the contract declares a NULL open end"),
    ],
    ids=["no open end", "a null open end"],
)
def test_a_required_end_with_no_open_end_names_what_to_declare(
    tmp_path: Path, contract: str, said: str
) -> None:
    result = _init(tmp_path, _profile(), contract)
    assert result.exit_code == 0, result.stderr
    assert _history(tmp_path) is None
    (reason,) = _history_reasons(result)
    assert said in reason and "declare the value an open version's end holds as openEnd" in reason


def test_a_null_open_end_on_an_end_that_may_be_null_writes_nulls(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(), _declaring(null=True, required=False))
    assert result.exit_code == 0, result.stderr
    history = _history(tmp_path)
    assert history is not None and "openEnd" not in history
    assert _open_versions(_rows(tmp_path))[None] == 850


def test_a_declared_chain_sets_the_entrys_chain(tmp_path: Path) -> None:
    # The profile's versions meet by nextStart; the contract says each ends the day before.
    result = _init(tmp_path, _profile(), _declaring(chain="dayBefore"))
    assert result.exit_code == 0, result.stderr
    history = _history(tmp_path)
    assert history is not None and history["chain"] == "dayBefore"
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    chain = states["/tables/tier_version/history/chain"]
    assert chain["state"] == "used"
    assert "the contract declares the chain dayBefore" in chain["note"]
    assert "the versions meet by nextStart" in chain["note"]
    rows = _rows(tmp_path)
    assert rows


def test_a_declared_chain_the_engine_cannot_generate_leaves_the_history_out(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile(), _declaring(chain="none"))
    assert result.exit_code == 0, result.stderr
    assert _history(tmp_path) is None
    (reason,) = _history_reasons(result)
    assert "the contract declares chain none" in reason


def test_a_contract_range_that_ends_at_the_open_end_is_not_a_window(tmp_path: Path) -> None:
    # A range up to the declared open end is the open end's, not a span of dates: no refusal.
    contract = _declaring("2999-12-31").replace(
        "        - { name: valid_to, type: DATE, required: true }\n",
        "        - name: valid_to\n          type: DATE\n          required: true\n"
        "          validationRules:\n            - type: range\n"
        "              constraint: \"between '2024-01-01' and '2999-12-31'\"\n",
    )
    result = _init(tmp_path, _profile(), contract)
    assert result.exit_code == 0, result.stderr
    history = _history(tmp_path)
    assert history is not None and history["openEnd"] == "2999-12-31"
