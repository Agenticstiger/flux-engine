# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes lifetimes and dates within them from a profile's date pairs (ADR 0024 section 10).

Pinned here, from small hand-written profiles of format 1-3-0:

* a table's pair whose order holds on at least :data:`ORDER_HOLDS_AT_LEAST` of its rows is a
  lifetime, oriented, with its histogram cut at day 0 as the duration, and its break share as the
  target;
* a relationship's pair is a ``within`` entry, with its histogram as the offset and the summed
  shares as the target, and a parent's date that a child is ordered after is a start-only lifetime;
* a histogram grouped by a column of the table is a dwell family given that column, and a pair with
  no usable duration is a lifetime with its start only;
* every pair that is not used is recorded as unused with its reason;
* a profile with no date members gives the bundle it gave before, and the request asks for the
  pairs only where the contract has them.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
import yaml

from flux_engine.author import bundle as authoring
from flux_engine.author.dates import (
    ORDER_HOLDS_AT_LEAST,
    DatedExpose,
    _window_occurrences,
    declare_dates,
    window_start_ms,
)
from flux_engine.cli._init import run_init
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.dates import read_lifetimes_block, read_within_block
from flux_engine.seam.resolve import FLX_3105
from flux_engine.stages.apply import apply as apply_stage
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan as plan_stage
from flux_engine.stages.verify import verify as verify_stage
from flux_stats.request import MOST_CALENDAR_COLUMNS, MOST_DATE_PAIRS, load_request_text

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _declarations_are_read_before_any_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read what ``init`` declares from these small synthetic profiles, before the trial of the bundle.

    The profiles here are built to show one declaration each, and most do not reproduce their own
    exposure or calendar. The trial that leaves out such an item, and the bundle that results, are
    covered by ``test_init_verifies.py`` and ``test_init_exposure.py``.
    """
    monkeypatch.setattr(authoring, "_refuse_unless_reproduced", lambda *args, **kwargs: None)


REPO_ROOT = Path(__file__).resolve().parents[2]
ORDERS_CONTRACT = REPO_ROOT / "tests" / "fixtures" / "author" / "orders.fluid.yaml"
ORDERS_PROFILE = REPO_ROOT / "tests" / "fixtures" / "author" / "orders.profile.json"
NAMESPACE = "com.agentics.flux/1"
SCHEMA_URL = "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json"
DAY = 86_400_000
START = 1_700_000_000_000
END = START + 365 * DAY

CONTRACT = """\
fluidVersion: 0.7.7
kind: DataProduct
id: shop.silver.dated
name: Dated shop
metadata:
  owner:
    team: shop
exposes:
- exposeId: customer
  kind: table
  contract:
    schema:
    - name: customer_id
      type: STRING
      required: true
    - name: signed_up
      type: TIMESTAMP
      required: true
    - name: closed_at
      type: TIMESTAMP
    - name: renewed_at
      type: TIMESTAMP
  semantics:
    entities:
    - name: customer
      type: primary
      expr: customer_id
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/customer.parquet
- exposeId: invoice
  kind: table
  contract:
    schema:
    - name: invoice_id
      type: STRING
      required: true
    - name: customer_id
      type: STRING
      required: true
    - name: issued_at
      type: TIMESTAMP
      required: true
    - name: paid_at
      type: TIMESTAMP
  semantics:
    entities:
    - name: invoice
      type: primary
      expr: invoice_id
    relationships:
    - name: invoice_to_customer
      to: customer
      fromColumns:
      - customer_id
      toColumns:
      - customer_id
      cardinality: many_to_one
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/invoice.parquet
- exposeId: store
  kind: table
  contract:
    schema:
    - name: store_id
      type: STRING
      required: true
    - name: opened_at
      type: TIMESTAMP
      required: true
  semantics:
    entities:
    - name: store
      type: primary
      expr: store_id
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/store.parquet
"""

#: A contract whose invoices also point at a store: two relationships to dated parents.
TWO_PARENTS = (
    CONTRACT.replace(
        "    - name: paid_at\n      type: TIMESTAMP\n",
        "    - name: paid_at\n      type: TIMESTAMP\n    - name: store_id\n      type: STRING\n"
        "      required: true\n",
    )
    .replace(
        "      cardinality: many_to_one\n  binding:\n    platform: local\n    format: parquet\n"
        "    location:\n      path: ./out/invoice.parquet",
        "      cardinality: many_to_one\n    - name: invoice_to_store\n      to: store\n"
        "      fromColumns:\n      - store_id\n      toColumns:\n      - store_id\n"
        "      cardinality: many_to_one\n  binding:\n    platform: local\n    format: parquet\n"
        "    location:\n      path: ./out/invoice.parquet",
    )
    .replace(
        "    - name: opened_at\n      type: TIMESTAMP\n      required: true\n",
        "    - name: opened_at\n      type: TIMESTAMP\n      required: true\n    - name: refit_at\n      type: TIMESTAMP\n",
    )
)


# -- building a profile ----------------------------------------------------------------------------


def _size(rows: int) -> Dict[str, int]:
    return {"min": rows, "max": rows}


def _levels(after: Optional[int], before: Optional[int], same: Optional[int]) -> Dict[str, Any]:
    counts = [after, before, same]
    total = sum(count for count in counts if count is not None)
    group: Dict[str, Any] = {"group": "", "k": total, "levelCounts": counts, "status": "F"}
    if None in counts:
        group["cellStatus"] = ["A" if count is None else "F" for count in counts]
    return {
        "definition": "levelHistogram/1",
        "levels": ["after", "before", "same"],
        "sampleSize": _size(total),
        "disclosure": "none",
        "groups": [group],
    }


def _histogram(edges: Sequence[int], counts: Sequence[int], **extra: Any) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "bins": [str(edge) for edge in edges],
        "sampleSize": _size(sum(counts)),
        "disclosure": "none",
        "groups": [{"group": "", "k": sum(counts), "binCounts": list(counts), "status": "F"}],
        **extra,
    }


def _pair(
    start: str,
    end: str,
    order: Dict[str, Any],
    edges: Sequence[int] = (0, 1, 10, 30),
    counts: Sequence[int] = (40, 30, 30),
    *,
    extra: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "statistics": [_histogram(edges, counts), *extra],
        "order": order,
    }


def _link_pair(
    parent: str,
    child: str,
    order: Dict[str, Any],
    edges: Sequence[int] = (0, 30, 90),
    counts: Sequence[int] = (60, 40),
) -> Dict[str, Any]:
    return {
        "parent": parent,
        "child": child,
        "statistics": [_histogram(edges, counts)],
        "order": order,
    }


def _stamp(low: int = START, high: int = END, *, maximum_only: bool = False) -> Dict[str, Any]:
    def metric(definition: str, value: int) -> Dict[str, Any]:
        return {
            "definition": definition,
            "status": "F",
            "value": str(value),
            "sampleSize": _size(100),
            "disclosure": "none",
        }

    metrics = {"max": metric("max/1", high)}
    if not maximum_only:
        metrics["min"] = metric("min/1", low)
    return {"domain": "timestamp", "metrics": metrics}


def _profile(
    *,
    customer: Sequence[Dict[str, Any]] = (),
    invoice: Sequence[Dict[str, Any]] = (),
    store: Sequence[Dict[str, Any]] = (),
    links: Sequence[Dict[str, Any]] = (),
    store_links: Sequence[Dict[str, Any]] = (),
    closed_at: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    def table(
        rows: int, columns: Dict[str, Any], pairs: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"rowCount": rows, "columns": columns}
        if pairs:
            entry["datePairs"] = list(pairs)
        return entry

    def relationship(
        child: str, parent: str, column: str, dates: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "parent": {"table": parent, "columns": [column]},
            "child": {"table": child, "columns": [column]},
            "statistics": [_histogram((0, 2, 8), (60, 40))],
        }
        if dates:
            entry["dates"] = list(dates)
        return entry

    document: Dict[str, Any] = {
        "_producer": "https://example.test/producer",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "customer": table(
                100,
                {
                    "customer_id": {"domain": "category"},
                    "signed_up": _stamp(),
                    "closed_at": closed_at or _stamp(),
                    "renewed_at": _stamp(),
                },
                customer,
            ),
            "invoice": table(
                400,
                {
                    "invoice_id": {"domain": "category"},
                    "customer_id": {"domain": "category"},
                    "issued_at": _stamp(),
                    "paid_at": _stamp(),
                    **({"store_id": {"domain": "category"}} if store_links else {}),
                },
                invoice,
            ),
            "store": table(
                10,
                {
                    "store_id": {"domain": "category"},
                    "opened_at": _stamp(),
                    **({"refit_at": _stamp()} if store_links else {}),
                },
                store,
            ),
        },
        "relationships": [relationship("invoice", "customer", "customer_id", links)],
    }
    if store_links:
        document["relationships"].append(relationship("invoice", "store", "store_id", store_links))
    return document


class Written:
    """The result of ``fluxsim init`` on a contract and a profile."""

    def __init__(
        self,
        root: Path,
        contract: str,
        profile: Dict[str, Any],
        window: Optional[Tuple[str, str]] = None,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.directory = root / "bundle"
        contract_path = root / "dated.fluid.yaml"
        contract_path.write_text(contract, encoding="utf-8")
        self.profile_path = root / "dated.profile.json"
        self.profile_path.write_text(json.dumps(profile), encoding="utf-8")
        self.result = run_init(
            str(self.directory),
            contract=str(contract_path),
            profile=str(self.profile_path),
            window=window,
        )
        assert self.result.exit_code == 0, self.result.stderr
        self.simulation = yaml.safe_load(
            (self.directory / "simulation.flux.yml").read_text(encoding="utf-8")
        )

    @property
    def block(self) -> Dict[str, Any]:
        block = self.simulation["extensions"][NAMESPACE]
        assert isinstance(block, dict)
        return block

    @property
    def lifetimes(self) -> Dict[str, Dict[str, Any]]:
        return {entry["emit"]: entry for entry in self.block.get("lifetimes", [])}

    @property
    def within(self) -> List[Dict[str, Any]]:
        return list(self.block.get("within", []))

    def unused(self, pointer_prefix: str) -> List[str]:
        """The reasons of the unused statistics under a pointer."""
        return [
            item["reason"]
            for item in self.block["unused"]
            if item["statistic"]["pointer"].startswith(pointer_prefix)
        ]


def _iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _written(
    tmp_path: Path,
    profile: Dict[str, Any],
    contract: str = CONTRACT,
    window: Optional[Tuple[str, str]] = None,
) -> Written:
    return Written(tmp_path, contract, profile, window)


HOLDS = _levels(after=60, before=0, same=40)


# -- lifetimes -------------------------------------------------------------------------------------


def test_a_pair_that_holds_is_a_lifetime_with_its_histogram_as_the_duration(tmp_path: Path) -> None:
    written = _written(tmp_path, _profile(customer=[_pair("signed_up", "closed_at", HOLDS)]))
    assert written.lifetimes == {
        "customer": {
            "emit": "customer",
            "start": "signed_up",
            "end": "closed_at",
            "duration": {"bins": ["0", "1", "10", "30"], "binWeights": ["4", "3", "3"]},
        }
    }


def test_the_share_that_breaks_the_order_is_the_target_and_the_duration_is_cut_at_day_zero(
    tmp_path: Path,
) -> None:
    order = _levels(after=60, before=10, same=30)  # 10 of 100 rows break the order
    pair = _pair("signed_up", "closed_at", order, edges=(-10, 0, 10, 30), counts=(10, 50, 40))
    written = _written(tmp_path, _profile(customer=[pair]))
    entry = written.lifetimes["customer"]
    assert entry["target"] == "10"
    # the bin [-10, 0) lies wholly below day 0, so it is dropped
    assert entry["duration"] == {"bins": ["0", "10", "30"], "binWeights": ["5", "4"]}


def test_a_bin_that_straddles_day_zero_starts_at_zero_with_its_days_from_zero_up(
    tmp_path: Path,
) -> None:
    order = _levels(after=50, before=10, same=40)
    # [-3, 1): four whole days, one of them (day 0) from zero up, a quarter of 80 rows
    pair = _pair("signed_up", "closed_at", order, edges=(-3, 1, 11), counts=(80, 20))
    entry = _written(tmp_path, _profile(customer=[pair])).lifetimes["customer"]
    assert entry["duration"] == {"bins": ["0", "1", "11"], "binWeights": ["1", "1"]}


def test_a_pair_that_holds_the_other_way_is_oriented_and_its_histogram_negated(
    tmp_path: Path,
) -> None:
    # closed_at is mostly before signed_up: (closed_at, signed_up) holds, and is the lifetime
    order = _levels(after=0, before=95, same=5)
    pair = _pair("signed_up", "closed_at", order, edges=(-30, -10, 0), counts=(20, 80))
    entry = _written(tmp_path, _profile(customer=[pair])).lifetimes["customer"]
    assert (entry["start"], entry["end"]) == ("closed_at", "signed_up")
    # the days from closed_at to signed_up: [0, 10) with 80 and [10, 30) with 20, upper-closed
    assert entry["duration"] == {
        "bins": ["0", "10", "30"],
        "binWeights": ["4", "1"],
        "closed": "upper",
    }
    assert "target" not in entry


def test_a_negated_histogram_that_reaches_below_zero_is_cut_on_whole_days(
    tmp_path: Path,
) -> None:
    order = _levels(after=5, before=90, same=5)
    # days start to end: (-30,-10] etc. negated gives edges reaching to -3 with the upper side
    pair = _pair("signed_up", "closed_at", order, edges=(-30, -10, 3), counts=(80, 20))
    entry = _written(tmp_path, _profile(customer=[pair])).lifetimes["customer"]
    # negated: [-3, 10) with weight 20 and [10, 30) with 80, closed upper; cut to whole days from 0
    assert entry["duration"]["bins"][0] == "0"
    assert "closed" not in entry["duration"]
    assert entry["target"] == "5"


def test_the_pair_with_the_most_qualifying_rows_is_the_lifetime(tmp_path: Path) -> None:
    weaker = _pair("signed_up", "closed_at", _levels(after=90, before=10, same=0))
    stronger = _pair("signed_up", "renewed_at", _levels(after=60, before=0, same=40))
    written = _written(tmp_path, _profile(customer=[weaker, stronger]))
    assert written.lifetimes["customer"]["end"] == "renewed_at"
    reasons = written.unused("/tables/customer/datePairs/0")
    assert reasons and all("holds on more rows" in reason for reason in reasons)


def test_ties_go_to_the_first_pair_in_the_profile(tmp_path: Path) -> None:
    first = _pair("signed_up", "closed_at", HOLDS)
    second = _pair("signed_up", "renewed_at", HOLDS)
    written = _written(tmp_path, _profile(customer=[first, second]))
    assert written.lifetimes["customer"]["end"] == "closed_at"


def test_an_end_whose_maximum_lies_after_the_window_is_kept(tmp_path: Path) -> None:
    # The end publishes only a maximum, which init also gives as its range's minimum, so the
    # durations reach it from the earliest start: a kept end the range leaves no value for is
    # refused by plan (ADR 0024 section 4).
    pair = _pair("signed_up", "closed_at", HOLDS, edges=(0, 400, 800), counts=(50, 50))
    late = _stamp(high=END + 400 * DAY, maximum_only=True)
    # The window spans every published range, the late maximum too, so the run's window is given:
    # a window of the other columns' span leaves the end's maximum after it.
    window = (_iso(START), _iso(END))
    written = _written(tmp_path, _profile(customer=[pair], closed_at=late), window=window)
    assert written.lifetimes["customer"]["endPastWindow"] == "kept"
    inside = _written(tmp_path / "b", _profile(customer=[pair]), window=window)
    assert "endPastWindow" not in inside.lifetimes["customer"]


# -- start-only lifetimes and within ---------------------------------------------------------------


def test_a_parent_date_a_child_is_ordered_after_is_a_start_only_lifetime(tmp_path: Path) -> None:
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    written = _written(tmp_path, _profile(links=[link]))
    assert written.lifetimes == {"customer": {"emit": "customer", "start": "signed_up"}}


def test_a_date_after_its_parents_start_is_a_within_entry_with_its_offset(tmp_path: Path) -> None:
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    written = _written(tmp_path, _profile(links=[link]))
    assert written.within == [
        {
            "emit": "invoice",
            "relationship": "invoice_to_customer",
            "date": "issued_at",
            "offset": {"bins": ["0", "30", "90"], "binWeights": ["3", "2"]},
        }
    ]


def test_the_target_of_a_within_entry_sums_the_share_before_the_start_and_after_the_end(
    tmp_path: Path,
) -> None:
    pair = _pair("signed_up", "closed_at", HOLDS)
    start = _link_pair("signed_up", "issued_at", _levels(after=350, before=10, same=40))
    end = _link_pair("closed_at", "issued_at", _levels(after=20, before=370, same=10))
    written = _written(tmp_path, _profile(customer=[pair], links=[start, end]))
    (entry,) = written.within
    # 10 of 400 before the start (2.5 %), 20 of 400 after the end (5 %)
    assert entry["target"] == "7.5"
    assert written.lifetimes["customer"]["end"] == "closed_at"
    assert written.unused("/relationships/0/dates/1/statistics") == [
        "only the order of this pair is read, for the target of an entry"
    ]


def test_a_start_of_the_childs_own_lifetime_with_an_end_is_a_period(tmp_path: Path) -> None:
    child = _pair("issued_at", "paid_at", HOLDS)
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    written = _written(tmp_path, _profile(invoice=[child], links=[link]))
    (entry,) = written.within
    assert entry["lifetime"] is True and "date" not in entry
    assert written.lifetimes["invoice"]["end"] == "paid_at"


def test_a_start_of_the_childs_own_lifetime_with_no_end_stays_a_date(tmp_path: Path) -> None:
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    written = _written(tmp_path, _profile(links=[link]))
    assert written.within[0]["date"] == "issued_at"


def test_one_offset_per_child_column_keeps_the_relationship_with_more_rows(
    tmp_path: Path,
) -> None:
    customer = _link_pair("signed_up", "paid_at", _levels(after=200, before=0, same=0))
    store = _link_pair("opened_at", "paid_at", _levels(after=300, before=0, same=0))
    written = _written(
        tmp_path, _profile(links=[customer], store_links=[store]), contract=TWO_PARENTS
    )
    (entry,) = written.within
    assert entry["relationship"] == "invoice_to_store"
    reasons = written.unused("/relationships/0/dates/0")
    assert any("a second relationship" in reason for reason in reasons)


# -- what is not used, and why ---------------------------------------------------------------------


def test_a_pair_below_the_bar_is_unused_with_its_share_and_the_bar(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=55, before=45, same=0))
    written = _written(tmp_path, _profile(customer=[pair]))
    assert written.lifetimes == {}
    reasons = written.unused("/tables/customer/datePairs/0")
    assert reasons
    assert all("55%" in reason and "90%" in reason for reason in reasons)


def test_a_suppressed_cell_of_an_order_leaves_the_pair_unused(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=60, before=None, same=40))
    written = _written(tmp_path, _profile(customer=[pair]))
    assert written.lifetimes == {}
    assert all("suppressed" in reason for reason in written.unused("/tables/customer/datePairs/0"))


def test_a_link_pair_whose_parent_column_starts_no_lifetime_is_unused(tmp_path: Path) -> None:
    start = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    other = _link_pair("renewed_at", "paid_at", _levels(after=300, before=0, same=100))
    written = _written(tmp_path, _profile(links=[start, other]))
    reasons = written.unused("/relationships/0/dates/1")
    assert reasons and all("does not start a lifetime" in reason for reason in reasons)


def test_a_relationship_the_contract_does_not_declare_is_unused(tmp_path: Path) -> None:
    profile = _profile(links=[_link_pair("signed_up", "issued_at", HOLDS)])
    profile["relationships"][0]["child"]["columns"] = ["invoice_id"]
    written = _written(tmp_path, profile)
    assert written.within == []
    assert any(
        "declares no to-one relationship" in r for r in written.unused("/relationships/0/dates")
    )


# -- nothing published, nothing written ------------------------------------------------------------


def test_a_profile_with_no_date_member_gives_the_bundle_it_gave_before(tmp_path: Path) -> None:
    profile = _profile()
    written = _written(tmp_path, profile)
    assert "lifetimes" not in written.block and "within" not in written.block
    plain = copy.deepcopy(profile)
    for table in plain["tables"].values():
        table.pop("datePairs", None)
    again = _written(tmp_path / "again", plain)
    for name in ("simulation.flux.yml", "world.flux.yml", "persona.flux.yml"):
        assert (written.directory / name).read_bytes().replace(
            written.profile_path.name.encode(), b"P"
        ) == (again.directory / name).read_bytes().replace(again.profile_path.name.encode(), b"P")


def test_the_bar_is_nine_tenths() -> None:
    assert ORDER_HOLDS_AT_LEAST == Fraction(9, 10)


# -- what is written is read ----------------------------------------------------------------------


def test_the_entries_are_ones_the_reader_accepts(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", HOLDS)
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    block = _written(tmp_path, _profile(customer=[pair], links=[link])).block
    emits = frozenset({"customer", "invoice", "store"})
    code = FLX_3105
    assert read_lifetimes_block(block["lifetimes"], emits=emits, where="x", code=code)
    assert read_within_block(block["within"], emits=emits, where="x", code=code)


def test_a_profile_whose_orders_hold_on_every_row_gives_a_bundle_that_plans(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=60, before=0, same=40))
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    written = _written(tmp_path, _profile(customer=[pair], links=[link]))
    assert written.lifetimes and written.within
    planned = plan_stage(
        str(written.directory),
        state_dir=str(tmp_path / "state"),
        profile=str(written.profile_path),
    )
    assert planned.exit_code == 0, planned.stderr


def test_a_bundle_with_targets_is_written_and_keeps_them(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=60, before=10, same=30))
    link = _link_pair("signed_up", "issued_at", _levels(after=350, before=10, same=40))
    written = _written(tmp_path, _profile(customer=[pair], links=[link]))
    assert written.lifetimes["customer"]["target"] == "10"
    assert written.within[0]["target"] == "2.5"


# -- declare_dates directly ------------------------------------------------------------------------


def test_the_window_decides_endPastWindow_directly() -> None:
    profile = _profile(customer=[_pair("signed_up", "closed_at", HOLDS)])
    from flux_engine.author.dates import DatedExpose
    from flux_engine.seam.contract import LogicalKind

    kinds = {name: LogicalKind.TIMESTAMP for name in ("signed_up", "closed_at", "renewed_at")}
    exposes = {"customer": DatedExpose(kinds=kinds, blocked=frozenset())}
    after = declare_dates(profile, exposes, None, window_end=END + 1, minimum_cell=1)
    before = declare_dates(profile, exposes, None, window_end=END - 1, minimum_cell=1)
    assert "endPastWindow" not in after.lifetimes[0]
    assert before.lifetimes[0]["endPastWindow"] == "kept"


# -- the request -----------------------------------------------------------------------------------


def _request(tmp_path: Path, contract: Path) -> str:
    result = run_init(str(tmp_path / "b"), contract=str(contract))
    assert result.exit_code == 0, result.stderr
    return (tmp_path / "b.profile-request.yml").read_text(encoding="utf-8")


def test_the_request_asks_for_every_pair_of_date_columns(tmp_path: Path) -> None:
    contract = tmp_path / "dated.fluid.yaml"
    contract.write_text(CONTRACT, encoding="utf-8")
    text = _request(tmp_path, contract)
    assert "profile-request/1-2-0/" in text  # which also holds the 1-1-0 asks of the dates
    request = load_request_text(text)
    tables = {table.expose_id: table for table in request.tables}
    assert [(pair.start, pair.end) for pair in tables["customer"].date_pairs] == [
        ("signed_up", "closed_at"),
        ("signed_up", "renewed_at"),
        ("closed_at", "renewed_at"),
    ]
    assert [(pair.start, pair.end) for pair in tables["invoice"].date_pairs] == [
        ("issued_at", "paid_at")
    ]
    assert tables["store"].date_pairs == ()
    (relationship,) = request.relationships
    assert [(link.parent, link.child) for link in relationship.dates] == [
        (parent, child)
        for parent in ("signed_up", "closed_at", "renewed_at")
        for child in ("issued_at", "paid_at")
    ]


DATELESS_CONTRACT = """\
fluidVersion: 0.7.7
kind: DataProduct
id: shop.silver.dateless
name: Dateless shop
metadata:
  owner:
    team: shop
exposes:
- exposeId: store
  kind: table
  contract:
    schema:
    - name: store_id
      type: STRING
      required: true
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/store.parquet
"""


def test_a_contract_with_no_date_column_keeps_its_request_as_format_one_zero_zero(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "dateless.fluid.yaml"
    contract.write_text(DATELESS_CONTRACT, encoding="utf-8")
    text = _request(tmp_path, contract)
    assert "profile-request/1-0-0/" in text
    for key in ("datePairs", "dates", "calendar", "exposure"):
        assert key not in text


def test_a_contract_with_one_date_column_asks_its_calendar(tmp_path: Path) -> None:
    text = _request(tmp_path, ORDERS_CONTRACT)
    assert "profile-request/1-2-0/" in text
    assert "datePairs" not in text
    assert [table.calendar for table in load_request_text(text).tables] == [("created_at",)]


def test_the_request_asks_for_the_calendar_and_the_exposure_of_every_date_column(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "dated.fluid.yaml"
    contract.write_text(CONTRACT, encoding="utf-8")
    request = load_request_text(_request(tmp_path, contract))
    tables = {table.expose_id: table for table in request.tables}
    assert tables["customer"].calendar == ("signed_up", "closed_at", "renewed_at")
    assert tables["invoice"].calendar == ("issued_at", "paid_at")
    assert tables["store"].calendar == ("opened_at",)
    (relationship,) = request.relationships
    # The pairs the request asks the days apart of come first, then each date column alone.
    assert [(item.start, item.end) for item in relationship.exposure] == [
        ("signed_up", "closed_at"),
        ("signed_up", "renewed_at"),
        ("closed_at", "renewed_at"),
        ("signed_up", None),
        ("closed_at", None),
        ("renewed_at", None),
    ]


def test_a_table_with_many_date_columns_asks_at_most_the_calendar_bound(tmp_path: Path) -> None:
    columns = "".join(f"    - name: d{n:04d}\n      type: DATE\n" for n in range(1100))
    contract = tmp_path / "wide.fluid.yaml"
    contract.write_text(
        "fluidVersion: 0.7.7\nkind: DataProduct\nid: wide\nname: Wide\nmetadata:\n  owner:\n"
        "    team: t\nexposes:\n- exposeId: wide\n  kind: table\n  contract:\n    schema:\n"
        + columns
        + "  binding:\n    platform: local\n    format: parquet\n    location:\n"
        "      path: ./out/wide.parquet\n",
        encoding="utf-8",
    )
    calendar = load_request_text(_request(tmp_path, contract)).tables[0].calendar
    assert len(calendar) == MOST_CALENDAR_COLUMNS
    assert calendar[0] == "d0000" and calendar[-1] == "d1023"


def test_a_table_with_many_date_columns_is_asked_at_most_the_bound(tmp_path: Path) -> None:
    columns = "".join(f"    - name: d{n:03d}\n      type: DATE\n" for n in range(50))
    contract = tmp_path / "wide.fluid.yaml"
    contract.write_text(
        "fluidVersion: 0.7.7\nkind: DataProduct\nid: wide\nname: Wide\nmetadata:\n  owner:\n"
        "    team: t\nexposes:\n- exposeId: wide\n  kind: table\n  contract:\n    schema:\n"
        + columns
        + "  binding:\n    platform: local\n    format: parquet\n    location:\n"
        "      path: ./out/wide.parquet\n",
        encoding="utf-8",
    )
    request = load_request_text(_request(tmp_path, contract))
    pairs = request.tables[0].date_pairs
    assert len(pairs) == MOST_DATE_PAIRS  # 50 columns give 1 225 pairs
    assert (pairs[0].start, pairs[0].end) == ("d000", "d001")
    assert (pairs[48].start, pairs[48].end) == ("d000", "d049")
    assert (pairs[49].start, pairs[49].end) == ("d001", "d002")


# -- dwell families and lifetimes that cannot be generated (ADR 0025 section 3) ----------------------

SEGMENT_CONTRACT = CONTRACT.replace(
    "    - name: renewed_at\n      type: TIMESTAMP\n",
    "    - name: renewed_at\n      type: TIMESTAMP\n"
    "    - name: segment\n      type: STRING\n"
    "    - name: tier\n      type: INTEGER\n",
    1,
)


def _grouped(
    by: str,
    groups: Dict[str, Optional[Sequence[int]]],
    edges: Sequence[int] = (0, 5, 20),
    **extra: Any,
) -> Dict[str, Any]:
    """A ``marginalHistogram/1`` grouped by a column: a group of ``None`` is withheld."""
    items: List[Dict[str, Any]] = []
    for key, counts in groups.items():
        if counts is None:
            items.append({"group": key, "status": "S"})
        else:
            items.append({"group": key, "k": sum(counts), "binCounts": list(counts), "status": "F"})
    return {
        "definition": "marginalHistogram/1",
        "groupBy": by,
        "bins": [str(edge) for edge in edges],
        "sampleSize": _size(100),
        "disclosure": "none",
        "groups": items,
        **extra,
    }


def _with_columns(profile: Dict[str, Any]) -> Dict[str, Any]:
    columns = profile["tables"]["customer"]["columns"]
    columns["segment"] = {"domain": "category"}
    columns["tier"] = {"domain": "number"}
    return profile


def _family_pair(
    grouped: Dict[str, Any],
    *,
    ungrouped: bool = True,
    order: Optional[Dict[str, Any]] = None,
    edges: Sequence[int] = (0, 1, 10, 30),
) -> Dict[str, Any]:
    pair = _pair("signed_up", "closed_at", order or HOLDS, edges, extra=[grouped])
    if not ungrouped:
        del pair["statistics"][0]
    return pair


def _customer(tmp_path: Path, pair: Dict[str, Any]) -> Written:
    profile = _with_columns(_profile(customer=[pair]))
    return _written(tmp_path, profile, SEGMENT_CONTRACT)


def test_a_grouped_pair_writes_a_family_with_a_state_per_group(tmp_path: Path) -> None:
    grouped = _grouped("segment", {"basic": (30, 10), "pro": (5, 45)})
    duration = _customer(tmp_path, _family_pair(grouped)).lifetimes["customer"]["duration"]
    assert duration["given"] == "segment"
    assert sorted(duration["states"]) == ["basic", "pro"]
    assert duration["states"]["basic"] == {"bins": ["0", "5", "20"], "binWeights": ["3", "1"]}
    assert duration["states"]["pro"] == {"bins": ["0", "5", "20"], "binWeights": ["1", "9"]}
    assert duration["otherwise"] == {"bins": ["0", "1", "10", "30"], "binWeights": ["4", "3", "3"]}


def test_a_withheld_group_is_left_out_and_recorded(tmp_path: Path) -> None:
    grouped = _grouped("segment", {"basic": (30, 10), "pro": None})
    pair = _family_pair(grouped)
    written = _customer(tmp_path, pair)
    assert list(written.lifetimes["customer"]["duration"]["states"]) == ["basic"]
    kinds = {name: LogicalKind.TIMESTAMP for name in ("signed_up", "closed_at")}
    held = DatedExpose(kinds=kinds, blocked=frozenset(), columns=frozenset({*kinds, "segment"}))
    profile = _with_columns(_profile(customer=[pair]))
    found = declare_dates(profile, {"customer": held}, None, window_end=END, minimum_cell=5)
    pointer = ("tables", "customer", "datePairs", "0", "statistics", "1")
    notes = [note for tokens, note in found.used if tokens == pointer]
    assert len(notes) == 1 and "left out: 'pro' (withheld, status S)" in notes[0]


def test_a_group_with_no_day_from_zero_up_is_left_out(tmp_path: Path) -> None:
    grouped = _grouped("segment", {"basic": (30, 10), "early": (40, 40)}, edges=(-20, -5, 0, 20))
    grouped["groups"][1]["binCounts"] = [40, 40, 0]
    grouped["groups"][1]["k"] = 80
    grouped["groups"][0]["binCounts"] = [0, 0, 40]
    pair = _family_pair(grouped, edges=(0, 1, 10, 30))
    states = _customer(tmp_path, pair).lifetimes["customer"]["duration"]["states"]
    assert list(states) == ["basic"]
    assert states["basic"] == {"bins": ["0", "20"], "binWeights": ["1"]}


def test_a_pair_with_no_usable_ungrouped_histogram_has_no_otherwise(tmp_path: Path) -> None:
    grouped = _grouped("segment", {"basic": (30, 10)})
    duration = _customer(tmp_path, _family_pair(grouped, ungrouped=False)).lifetimes["customer"][
        "duration"
    ]
    assert "otherwise" not in duration
    assert list(duration["states"]) == ["basic"]


def test_number_group_levels_are_written_as_the_generator_reads_them(tmp_path: Path) -> None:
    grouped = _grouped("tier", {"-1.5": (30, 10), "0": (20, 20), "12": (5, 45)})
    duration = _customer(tmp_path, _family_pair(grouped)).lifetimes["customer"]["duration"]
    assert duration["given"] == "tier"
    assert sorted(duration["states"]) == ["-1.5", "0", "12"]


def test_a_group_by_the_start_or_the_end_or_a_foreign_column_is_unused() -> None:
    kinds = {name: LogicalKind.TIMESTAMP for name in ("signed_up", "closed_at")}
    held = DatedExpose(
        kinds=kinds, blocked=frozenset(), columns=frozenset({*kinds, "customer_id", "segment"})
    )
    for by, word in (("signed_up", "start"), ("closed_at", "end"), ("nowhere", "no column")):
        pair = _family_pair(_grouped(by, {"x": (30, 10)}))
        profile = _with_columns(_profile(customer=[pair]))
        found = declare_dates(profile, {"customer": held}, None, window_end=END, minimum_cell=5)
        assert found.lifetimes[0]["duration"] == {
            "bins": ["0", "1", "10", "30"],
            "binWeights": ["4", "3", "3"],
        }
        pointer = ("tables", "customer", "datePairs", "0", "statistics", "1")
        assert [reason for tokens, reason in found.unused if tokens == pointer and word in reason]


def test_a_pair_with_no_usable_duration_writes_a_start_only_lifetime(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=60, before=10, same=30))
    pair["statistics"] = []
    written = _written(tmp_path, _profile(customer=[pair]))
    assert written.lifetimes == {"customer": {"emit": "customer", "start": "signed_up"}}
    reasons = written.unused("/tables/customer/datePairs/0")
    assert reasons and all("no usable duration" in reason for reason in reasons)


def test_a_histogram_with_no_day_from_zero_up_writes_a_start_only_lifetime(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", HOLDS, edges=(-30, -10, 0), counts=(50, 50))
    written = _written(tmp_path, _profile(customer=[pair]))
    assert written.lifetimes == {"customer": {"emit": "customer", "start": "signed_up"}}


def test_the_target_of_an_entry_sums_only_the_parts_that_still_apply(tmp_path: Path) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=60, before=10, same=30))
    pair["statistics"] = []
    end = _link_pair("closed_at", "issued_at", _levels(after=50, before=0, same=350))
    start = _link_pair("signed_up", "issued_at", _levels(after=350, before=10, same=40))
    written = _written(tmp_path, _profile(customer=[pair], links=[end, start]))
    assert written.lifetimes["customer"] == {"emit": "customer", "start": "signed_up"}
    assert written.within[0]["target"] == "2.5"
    planned = plan_stage(
        str(written.directory), state_dir=str(tmp_path / "state"), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr


def test_a_written_family_plans_applies_and_verifies(tmp_path: Path) -> None:
    levels = {
        "definition": "levelHistogram/1",
        "levels": ["basic", "pro"],
        "sampleSize": _size(100),
        "disclosure": "none",
        "groups": [{"group": "", "k": 100, "levelCounts": [50, 50], "status": "F"}],
    }
    grouped = _grouped("segment", {"basic": (30, 10), "pro": (5, 45)})
    pair = _family_pair(grouped, order=_levels(after=60, before=0, same=40))
    profile = _with_columns(_profile(customer=[pair]))
    profile["tables"]["customer"]["columns"]["segment"]["statistics"] = [levels]
    written = _written(tmp_path, profile, SEGMENT_CONTRACT)
    assert "given" in written.lifetimes["customer"]["duration"]
    state = tmp_path / "state"
    planned = plan_stage(
        str(written.directory), state_dir=str(state), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(
        written.directory, state_dir=state, run_id=str(planned.artifacts["run_id"])
    )
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    verified = verify_stage(
        written.directory,
        run_id=layout.run_id,
        state_dir=state,
        allow_declared_baseline_drift=True,
    )
    assert verified.success, verified.stderr
    assert "lifetime" in verified.stdout


def test_a_profile_without_grouped_statistics_gives_the_bytes_it_gave_before(
    tmp_path: Path,
) -> None:
    pair = _pair("signed_up", "closed_at", _levels(after=60, before=10, same=30))
    written = _written(tmp_path, _profile(customer=[pair]))
    assert written.lifetimes["customer"] == {
        "emit": "customer",
        "start": "signed_up",
        "end": "closed_at",
        "duration": {"bins": ["0", "1", "10", "30"], "binWeights": ["4", "3", "3"]},
        "target": "10",
    }


# -- seasonal rates from a published calendar (ADR 0025 sections 2 and 7) --------------------------

LEVELS = {
    "month": tuple(f"{month:02d}" for month in range(1, 13)),
    "dayOfWeek": tuple(str(day) for day in range(1, 8)),
    "hourOfWeek": tuple(f"{hour:03d}" for hour in range(168)),
}

#: Saturday 27 January 2024 to Monday 5 February 2024 at noon: ten days, five of January and five
#: of February. Counted by hand, Monday and Saturday and Sunday occur twice, the other four
#: weekdays once.
SMALL_START = window_start_ms("2024-01-27T00:00:00Z")
SMALL_END = window_start_ms("2024-02-05T12:00:00Z")


def _stat(key: str, counts: Sequence[Optional[int]], **extra: Any) -> Dict[str, Any]:
    """A published calendar histogram of *key*; a ``None`` count is a suppressed cell."""
    published = [0 if count is None else count for count in counts]
    group: Dict[str, Any] = {
        "group": "",
        "k": sum(published),
        "levelCounts": published,
        "status": "F",
    }
    if None in counts:
        group["cellStatus"] = ["A" if count is None else "F" for count in counts]
    return {
        "definition": "levelHistogram/1",
        "levels": list(LEVELS[key]),
        "sampleSize": _size(sum(published)),
        "disclosure": "none",
        "groups": [group],
        **extra,
    }


def _calendar(column: str, **histograms: Dict[str, Any]) -> Dict[str, Any]:
    return {"column": column, **histograms}


def _months(**counts: int) -> List[int]:
    """The 12 month counts: ``m1`` is January; a month not named counts none."""
    return [counts.get(f"m{month}", 0) for month in range(1, 13)]


def _with_calendar(*calendar: Dict[str, Any]) -> Dict[str, Any]:
    profile = _profile(customer=[_pair("signed_up", "closed_at", HOLDS)])
    profile["tables"]["customer"]["calendar"] = list(calendar)
    return profile


def _declared(
    profile: Dict[str, Any],
    kind: LogicalKind = LogicalKind.TIMESTAMP,
    *,
    start: Optional[int] = SMALL_START,
    end: int = SMALL_END,
) -> Any:
    kinds = {name: kind for name in ("signed_up", "closed_at", "renewed_at")}
    exposes = {"customer": DatedExpose(kinds=kinds, blocked=frozenset())}
    return declare_dates(profile, exposes, None, window_start=start, window_end=end, minimum_cell=1)


def _reasons(declared: Any, key: str) -> List[str]:
    return [reason for tokens, reason in declared.unused if tokens[-2:] == ("0", key)]


def test_each_level_is_its_count_over_its_occurrences_in_the_window() -> None:
    # January holds 5 of the window's days and February 5; levels outside it take the weight 1.
    # 10 rows in January is 2 a day and 30 in February is 6 a day: weights 2 and 6, scaled 1/3 and 1.
    month = _stat("month", _months(m1=10, m2=30, m3=7))
    declared = _declared(_with_calendar(_calendar("closed_at", month=month)))
    assert declared.rates == [
        {
            "emit": "customer",
            "column": "closed_at",
            "month": ["0.333333", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1"],
        }
    ]


def test_a_weekday_factor_divides_by_the_days_of_that_weekday_and_scales_to_one() -> None:
    # Monday to Sunday occur 2, 1, 1, 1, 1, 2, 2 times, so the weights are 1, 3, 7, 1, 2, 3, 4.
    week = _stat("dayOfWeek", [2, 3, 7, 1, 2, 6, 8])
    declared = _declared(_with_calendar(_calendar("closed_at", dayOfWeek=week)))
    # Largest weight 7 gives 1; each other is its share of 7 to six significant digits.
    assert declared.rates[0]["dayOfWeek"] == [
        "0.142857",
        "0.428571",
        "1",
        "0.142857",
        "0.285714",
        "0.428571",
        "0.571429",
    ]


def test_a_suppressed_cell_weighs_the_smallest_positive_weight_and_a_level_outside_the_window_one() -> (
    None
):
    # Wednesday is suppressed; Friday counts none and is zero; weights 1, 3, -, 1, 0, 3, 4.
    # The suppressed cell takes the smallest positive weight, 1, so it is not the most frequent level.
    week = _stat("dayOfWeek", [2, 3, None, 1, 0, 6, 8])
    month = _stat("month", _months(m1=10, m2=30, m6=99))
    declared = _declared(_with_calendar(_calendar("closed_at", month=month, dayOfWeek=week)))
    entry = declared.rates[0]
    assert entry["dayOfWeek"] == ["0.25", "0.75", "0.25", "0.25", "0", "0.75", "1"]
    # June is not in the window, whatever it counts.
    assert entry["month"][5] == "1"
    assert "smallest positive weight" in " ".join(
        note for tokens, note in declared.used if tokens[-1] == "dayOfWeek"
    )


def test_a_suppressed_level_outside_the_window_keeps_weight_one() -> None:
    # January 5 days at 10 rows (2 a day), February 5 days at 30 (6 a day); March is suppressed and
    # outside the window, so it weighs 1 whatever the others do.
    month = _stat("month", _months(m1=10, m2=30, m3=None))
    declared = _declared(_with_calendar(_calendar("closed_at", month=month)))
    assert declared.rates[0]["month"][:3] == ["0.333333", "1", "1"]


def test_a_suppressed_level_inside_the_window_is_never_the_largest() -> None:
    month = _stat("month", _months(m1=None, m2=30))
    declared = _declared(_with_calendar(_calendar("closed_at", month=month)))
    # February is 6 a day and the only positive weight, so January takes it: both are 1.
    assert declared.rates[0]["month"][:2] == ["1", "1"]
    month = _stat("month", _months(m1=None, m2=30, m12=0))
    week = _stat("dayOfWeek", [2, 3, None, 1, 6, 6, 8])
    declared = _declared(_with_calendar(_calendar("closed_at", month=month, dayOfWeek=week)))
    weights = [Fraction(text) for text in declared.rates[0]["dayOfWeek"]]
    assert weights[2] == min(w for w in weights if w > 0) and max(weights) == 1 and weights[2] < 1


def test_an_hour_of_the_week_is_counted_in_the_window_the_bundle_writes() -> None:
    hours = _stat("hourOfWeek", [1] * 168)
    declared = _declared(_with_calendar(_calendar("closed_at", hourOfWeek=hours)))
    held = [0] * 168
    first, last = SMALL_START // 3_600_000, SMALL_END // 3_600_000
    for hour in range(first, last + 1):
        moment = datetime.fromtimestamp(hour * 3600, tz=timezone.utc)
        held[moment.weekday() * 24 + moment.hour] += 1
    assert sum(held) == last - first + 1 and 0 not in held
    weights = [Fraction(1, count) for count in held]
    largest = max(weights)
    written = declared.rates[0]["hourOfWeek"]
    assert len(written) == 168
    for text, weight in zip(written, weights, strict=True):
        assert Fraction(text) == pytest.approx(float(weight / largest), rel=1e-5)
    assert "1" in written


def test_a_noisy_histogram_is_unused_with_its_reason() -> None:
    noisy = _stat("month", _months(m1=10, m2=30), noise={"mechanism": "laplace"})
    week = _stat("dayOfWeek", [2, 3, 7, 1, 2, 6, 8])
    declared = _declared(_with_calendar(_calendar("closed_at", month=noisy, dayOfWeek=week)))
    assert "month" not in declared.rates[0] and "dayOfWeek" in declared.rates[0]
    assert "released with noise" in _reasons(declared, "month")[0]


def test_a_histogram_with_no_usable_weight_is_unused_and_an_empty_entry_is_not_written() -> None:
    # January and February are suppressed and no other month is in the window: nothing to weigh.
    outside = _stat("month", [None, None, 40, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    nothing = _stat("dayOfWeek", [0, 0, 0, 0, 0, 0, 0])
    declared = _declared(_with_calendar(_calendar("closed_at", month=outside, dayOfWeek=nothing)))
    assert declared.rates == []
    assert "no level has both a published count" in _reasons(declared, "month")[0]
    assert "counts none" in _reasons(declared, "dayOfWeek")[0]


def test_an_hour_of_the_week_of_a_date_is_unused() -> None:
    hours = _stat("hourOfWeek", [1] * 168)
    month = _stat("month", _months(m1=10, m2=30))
    declared = _declared(
        _with_calendar(_calendar("closed_at", month=month, hourOfWeek=hours)), LogicalKind.DATE
    )
    assert "hourOfWeek" not in declared.rates[0] and "month" in declared.rates[0]
    assert _reasons(declared, "hourOfWeek") == [
        "a DATE has no hour, so an hour of the week shapes nothing of it"
    ]


def test_a_calendar_for_a_column_no_entry_sets_is_unused() -> None:
    # renewed_at is neither a lifetime's end nor a within date, and signed_up only starts the lifetime.
    month = _stat("month", _months(m1=10, m2=30))
    declared = _declared(
        _with_calendar(_calendar("renewed_at", month=month), _calendar("signed_up", month=month))
    )
    assert declared.rates == []
    assert len(declared.unused) >= 2
    reasons = [reason for tokens, reason in declared.unused if "calendar" in tokens]
    assert len(reasons) == 2
    assert all(
        "rates shape only a date a within entry or a duration sets" in why for why in reasons
    )


def test_no_window_start_writes_no_rates() -> None:
    month = _stat("month", _months(m1=10, m2=30))
    declared = _declared(_with_calendar(_calendar("closed_at", month=month)), start=None)
    assert declared.rates == []
    assert "window" in _reasons(declared, "month")[0]


def _invoice_profile(*, rows: int = 400) -> Dict[str, Any]:
    """Invoices dated after their customer's signup, with a calendar for the invoice date.

    The calendar is busy on weekdays and in business hours, a shape the run reaches from offsets
    that span the window; a month shape would be bounded by where each customer's offset lands.
    """
    pair = _pair("signed_up", "closed_at", HOLDS)
    link = _link_pair(
        "signed_up", "issued_at", _levels(after=300, before=0, same=100), (0, 150, 360), (50, 50)
    )
    profile = _profile(customer=[pair], links=[link])
    profile["tables"]["invoice"]["rowCount"] = rows
    month = _stat("month", _months(m3=300, m10=100, m1=40, m7=0))
    week = _stat("dayOfWeek", [10, 10, 10, 10, 10, 1, 1])
    busy = [10 if day < 5 and 9 <= hour < 17 else 1 for day in range(7) for hour in range(24)]
    hours = _stat("hourOfWeek", busy)
    profile["tables"]["invoice"]["calendar"] = [
        _calendar("issued_at", month=month, dayOfWeek=week, hourOfWeek=hours)
    ]
    return profile


def test_a_within_date_with_a_calendar_gets_a_rates_entry(tmp_path: Path) -> None:
    written = _written(tmp_path, _invoice_profile())
    (entry,) = written.block["rates"]
    assert entry["emit"] == "invoice" and entry["column"] == "issued_at"
    assert set(entry) == {"emit", "column", "month", "hourOfWeek"}
    assert [len(entry[key]) for key in ("month", "hourOfWeek")] == [12, 168]
    assert max(Fraction(text) for text in entry["month"]) == 1
    # 14 November 2023 to 13 November 2024: July holds no count, March 31 days, October 31.
    assert entry["month"][6] == "0"
    assert entry["month"][9] == "0.333333"  # (100 / 31) / (300 / 31), six digits
    assert entry["month"][2] == "1"
    read = [entry["month"], entry["hourOfWeek"]]
    assert all(isinstance(weight, str) for factor in read for weight in factor)
    # The hour of the week carries the weekday, so the weekday's own rate is left out.
    assert written.unused("/tables/invoice/calendar/0/dayOfWeek") == [
        "hourOfWeek already carries the weekday"
    ]
    assert not written.unused("/tables/invoice/calendar/0/month")
    assert not written.unused("/tables/invoice/calendar/0/hourOfWeek")


def test_the_written_bundle_with_rates_plans_applies_and_verifies_its_calendar(
    tmp_path: Path,
) -> None:
    # An invoice's time of day is its customer's, since the offset is whole days, so only the
    # weekday shape is one the run can reach.
    profile = _invoice_profile(rows=20_000)
    # A sampled weekday shape lands within a few thousandths of its tolerance at these sizes, and
    # which side it lands on follows the run's seed, which follows the bundle written.
    profile["tables"]["customer"]["rowCount"] = 2_001
    for key in ("month", "hourOfWeek"):
        del profile["tables"]["invoice"]["calendar"][0][key]
    written = _written(tmp_path, profile)
    state = tmp_path / "state"
    planned = plan_stage(
        str(written.directory), state_dir=str(state), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(
        written.directory,
        state_dir=state,
        run_id=str(planned.artifacts["run_id"]),
    )
    assert applied.success, applied.stderr
    verified = verify_stage(
        written.directory,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=state,
        profile=str(written.profile_path),
        allow_declared_baseline_drift=True,
    )
    assert "calendar:issued_at" in verified.stdout
    assert "calendar:issued_at (invoice): passed" in verified.stdout, verified.stdout


def test_a_bundle_with_all_three_calendar_histograms_verifies_its_calendar(
    tmp_path: Path,
) -> None:
    # The weekday is counted once: the hour of the week carries it, so the bundle declares the
    # month and the hour of the week, and the verdict reads all three of the profile's histograms.
    profile = _invoice_profile(rows=20_000)
    profile["tables"]["customer"]["rowCount"] = 2_000
    # An invoice's time of day is its customer's, and its month the offset's, so the histograms
    # published are the ones the run can reach: a weekday shape at every hour, and the months as
    # the run's own days fall.
    week = [3, 3, 3, 3, 3, 2, 2]
    calendar = profile["tables"]["invoice"]["calendar"][0]
    calendar["dayOfWeek"] = _stat("dayOfWeek", week)
    calendar["hourOfWeek"] = _stat("hourOfWeek", [week[day] for day in range(7) for _ in range(24)])
    calendar["month"] = _stat("month", [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])
    written = _written(tmp_path, profile)
    (entry,) = written.block["rates"]
    assert set(entry) == {"emit", "column", "month", "hourOfWeek"}
    state = tmp_path / "state"
    planned = plan_stage(
        str(written.directory), state_dir=str(state), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(
        written.directory, state_dir=state, run_id=str(planned.artifacts["run_id"])
    )
    assert applied.success, applied.stderr
    verified = verify_stage(
        written.directory,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=state,
        profile=str(written.profile_path),
        allow_declared_baseline_drift=True,
    )
    line = next(row for row in verified.stdout.splitlines() if "calendar:issued_at" in row)
    assert "calendar:issued_at (invoice): passed" in line, line


def test_a_profile_without_calendars_gives_the_bytes_it_gave_before(tmp_path: Path) -> None:
    profile = _invoice_profile()
    plain = copy.deepcopy(profile)
    for table in plain["tables"].values():
        table.pop("calendar", None)
    without = _written(tmp_path / "without", plain)
    assert "rates" not in without.block
    again = _written(tmp_path / "again", copy.deepcopy(plain))
    for name in ("simulation.flux.yml", "world.flux.yml", "persona.flux.yml"):
        assert (without.directory / name).read_bytes().replace(
            without.profile_path.name.encode(), b"P"
        ) == (again.directory / name).read_bytes().replace(again.profile_path.name.encode(), b"P")
    with_calendar = _written(tmp_path / "with", profile)
    block = {
        key: value for key, value in with_calendar.block.items() if key not in ("rates", "unused")
    }
    assert block.keys() == {k for k in without.block if k != "unused"}


def _late_child_profile() -> Dict[str, Any]:
    """A clean profile whose invoices are all dated after the day their customers start.

    A customer's lifetime can last no days, so the earliest end of the parent is its earliest start,
    and the invoice date's own range begins a hundred days later.
    """
    link = _link_pair(
        "signed_up", "issued_at", _levels(after=300, before=0, same=100), (0, 150, 360), (50, 50)
    )
    profile = _profile(customer=[_pair("signed_up", "closed_at", HOLDS)], links=[link])
    profile["tables"]["invoice"]["columns"]["issued_at"] = _stamp(START + 100 * DAY, END)
    return profile


def test_a_child_date_whose_range_starts_after_the_parent_does_gets_a_widened_range(
    tmp_path: Path,
) -> None:
    written = _written(tmp_path, _late_child_profile())
    (entry,) = [item for item in written.within if item.get("date") == "issued_at"]
    assert "target" not in entry  # the fact stays enforced
    emits = written.block["emits"]
    column = emits["invoice"]["fidelity"]["columns"]["issued_at"]
    low = datetime.fromtimestamp(START / 1000, timezone.utc)
    assert column["range"]["min"].startswith(low.strftime("%Y-%m-%dT%H:%M:%S"))
    state = tmp_path / "state"
    planned = plan_stage(
        str(written.directory), state_dir=str(state), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(
        written.directory, state_dir=state, run_id=str(planned.artifacts["run_id"])
    )
    assert applied.success, applied.stderr
    verified = verify_stage(
        written.directory,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=state,
        profile=str(written.profile_path),
        allow_declared_baseline_drift=True,
    )
    lines = [row for row in verified.stdout.splitlines() if "within" in row]
    assert lines, verified.stdout
    assert any("400 of 400" in row for row in lines), lines


def _published_histogram_profile(*, other: bool) -> Dict[str, Any]:
    """The late-child profile with a ``marginalHistogram/1`` of the child date it widens.

    With *other*, the invoice's ``paid_at`` publishes a histogram too, so the table keeps a property
    ``verify`` can compare when the widened date loses its own.
    """
    profile = _late_child_profile()
    low = START + 100 * DAY
    edges = [low + step * 65 * DAY for step in range(5)]
    column = _stamp(low, END)
    column["statistics"] = [_histogram(edges, (100, 120, 100, 80))]
    profile["tables"]["invoice"]["columns"]["issued_at"] = column
    if other:
        paid = _stamp()
        paid["statistics"] = [
            _histogram([START + step * 100 * DAY for step in range(4)] + [END], (90, 110, 120, 80))
        ]
        profile["tables"]["invoice"]["columns"]["paid_at"] = paid
    return profile


def _verified(written: Written, tmp_path: Path) -> Any:
    state = tmp_path / "state"
    planned = plan_stage(
        str(written.directory), state_dir=str(state), profile=str(written.profile_path)
    )
    assert planned.exit_code == 0, planned.stderr
    applied = apply_stage(
        written.directory, state_dir=state, run_id=str(planned.artifacts["run_id"])
    )
    assert applied.success, applied.stderr
    return verify_stage(
        written.directory,
        run_id=str(applied.artifacts["run_id"]),
        state_dir=state,
        profile=str(written.profile_path),
        allow_declared_baseline_drift=True,
    )


def _issued_at_refusals(verified: Any) -> List[str]:
    return [
        row
        for row in (verified.stdout + verified.stderr).splitlines()
        if "issued_at" in row and "FLX-5037" in row
    ]


def test_a_widened_child_date_loses_the_histogram_property_it_cannot_compare(
    tmp_path: Path,
) -> None:
    written = _written(tmp_path, _published_histogram_profile(other=True))
    (entry,) = [item for item in written.within if item.get("date") == "issued_at"]
    assert "target" not in entry  # the fact stays enforced
    fidelity = written.block["emits"]["invoice"]["fidelity"]
    assert fidelity["columns"]["issued_at"]["range"]["min"].startswith("2023-11-14")
    assert [
        item["column"] for item in fidelity["properties"] if item["property"] == "marginalHistogram"
    ] == ["paid_at"]
    verified = _verified(written, tmp_path)
    assert not _issued_at_refusals(verified), verified.stdout
    assert "within:invoice_to_customer.issued_at 400 of 400" in verified.stdout
    assert "fidelity invoice: passed" in verified.stdout


def test_a_widened_child_date_the_table_has_no_other_property_for_is_not_dated(
    tmp_path: Path,
) -> None:
    written = _written(tmp_path, _published_histogram_profile(other=False))
    assert not [item for item in written.within if item.get("date") == "issued_at"]
    reasons = written.unused("/relationships/0/dates/0")
    assert reasons and all("leaves the table no property to verify" in item for item in reasons)
    verified = _verified(written, tmp_path)
    assert not _issued_at_refusals(verified), verified.stdout


def test_a_widened_range_is_recorded_in_the_used_note_of_its_statistic(tmp_path: Path) -> None:
    from flux_engine.author.bundle import AuthorInputs, author_bundle

    written = _written(tmp_path, _late_child_profile())
    authored = author_bundle(AuthorInputs(tmp_path / "dated.fluid.yaml", written.profile_path))
    notes = [
        entry.note
        for entry in authored.ledger
        if entry.tokens[:4] == ("tables", "invoice", "columns", "issued_at")
    ]
    assert any("declared range widened to the parent's lifetime" in note for note in notes)


# -- windows at the ends of the calendar (a December 9999 sentinel) --------------------------------

LAST_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z
FIRST_MS = -62_135_596_800_000  # 0001-01-01T00:00:00Z


def test_a_window_ending_in_december_9999_counts_without_a_date_past_9999() -> None:
    held = _window_occurrences(window_start_ms("9999-01-01T00:00:00Z"), LAST_MS)
    assert held["month"] == [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    assert sum(held["dayOfWeek"]) == 365 and sum(held["hourOfWeek"]) == 365 * 24
    # 9999-12-31 is a Friday.
    assert held["dayOfWeek"][4] == 53 and held["dayOfWeek"][3] == 52


def test_a_window_starting_in_year_one_counts_its_first_days() -> None:
    held = _window_occurrences(FIRST_MS, FIRST_MS + 59 * 86_400_000 + 1)
    assert held["month"][:3] == [31, 28, 0 + 1] and sum(held["month"]) == 60
    # 0001-01-01 was a Monday in the proleptic Gregorian calendar.
    assert held["dayOfWeek"][0] == 9 and sum(held["dayOfWeek"]) == 60


def test_a_window_over_whole_years_counts_every_month_each_year() -> None:
    held = _window_occurrences(FIRST_MS, LAST_MS)
    days = (LAST_MS - FIRST_MS) // 86_400_000 + 1
    assert sum(held["month"]) == days == 3_652_059
    # 9999 whole years of 400-year cycles: January holds 31 days in each of 9999 years.
    assert held["month"][0] == 31 * 9999 and held["month"][1] == 28 * 9999 + 2424
    assert sum(held["dayOfWeek"]) == days


def test_the_arithmetic_counts_match_dates_across_leap_rules() -> None:
    from datetime import date

    for year, month in [(1, 2), (1600, 2), (1900, 2), (2000, 2), (2024, 12), (9999, 12), (1970, 1)]:
        start = date(year, month, 1).toordinal() - date(1970, 1, 1).toordinal()
        end = (
            date(year + month // 12, month % 12 + 1, 1).toordinal() - date(1970, 1, 1).toordinal()
            if year < 9999 or month < 12
            else date(9999, 12, 31).toordinal() - date(1970, 1, 1).toordinal() + 1
        )
        held = _window_occurrences(start * 86_400_000, (end - 1) * 86_400_000)
        assert held["month"][month - 1] == end - start
        assert sum(held["month"]) == end - start


def test_init_on_a_profile_whose_max_is_in_december_9999_runs_clean(tmp_path: Path) -> None:
    for name, calendar in (("plain", False), ("calendar", True)):
        profile = _profile(customer=[_pair("signed_up", "closed_at", HOLDS)])
        for table in profile["tables"].values():
            for column in table["columns"].values():
                if "metrics" in column:
                    column["metrics"]["max"]["value"] = str(LAST_MS)
        if calendar:
            profile["tables"]["customer"]["calendar"] = [
                _calendar("closed_at", month=_stat("month", _months(m1=10, m2=30)))
            ]
        written = _written(tmp_path / name, profile)
        assert written.result.exit_code == 0
        assert ("rates" in written.block) == calendar


# -- exposure (ADR 0025 sections 4 and 7) ----------------------------------------------------------


def _count(value: Optional[int]) -> Dict[str, Any]:
    if value is None:
        return {
            "definition": "count/1",
            "status": "S",
            "sampleSize": _size(100),
            "disclosure": "none",
        }
    return {
        "definition": "count/1",
        "status": "F",
        "value": value,
        "sampleSize": _size(100),
        "disclosure": "none",
    }


def _exposure(
    start: str = "signed_up",
    end: Optional[str] = "closed_at",
    *,
    children: Optional[int] = 400,
    days: Optional[int] = 20_000,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "start": start,
        "children": _count(children),
        "activeDays": _count(days),
        "window": {"start": str(START), "end": str(END)},
    }
    if end is not None:
        item["end"] = end
    return item


def _with_exposure(profile: Dict[str, Any], *items: Dict[str, Any]) -> Dict[str, Any]:
    profile["relationships"][0]["exposure"] = list(items)
    return profile


def _exposed_profile(*items: Dict[str, Any], lifetime: bool = True) -> Dict[str, Any]:
    pair = _pair("signed_up", "closed_at", HOLDS)
    link = _link_pair("signed_up", "issued_at", _levels(after=300, before=0, same=100))
    return _with_exposure(_profile(customer=[pair] if lifetime else [], links=[link]), *items)


def _exposure_notes(written: Written) -> List[str]:
    """The distinct reasons the bundle gives for the exposure counts it leaves unused."""
    return sorted(
        {
            item["reason"]
            for item in written.block["unused"]
            if "/exposure/" in item["statistic"]["pointer"]
        }
    )


def test_an_exposure_of_the_lifetime_written_marks_the_within_entry_and_is_used(
    tmp_path: Path,
) -> None:
    written = _written(tmp_path, _exposed_profile(_exposure()))
    (entry,) = written.within
    assert entry["exposure"] is True
    assert not _exposure_notes(written)
    # The exposure is marked used, so the bundle's unused list holds none of it.
    assert not any("/exposure/" in item["statistic"]["pointer"] for item in written.block["unused"])


def test_an_exposure_of_other_lifetime_columns_is_unused_with_its_reason(tmp_path: Path) -> None:
    written = _written(tmp_path, _exposed_profile(_exposure("renewed_at", "closed_at")))
    (entry,) = written.within
    assert "exposure" not in entry
    (note,) = _exposure_notes(written)
    assert "lifetime columns" in note


def test_an_exposure_with_a_withheld_count_is_unused_with_its_reason(tmp_path: Path) -> None:
    for kwargs in ({"children": None}, {"days": None}):
        written = _written(tmp_path / str(sorted(kwargs)), _exposed_profile(_exposure(**kwargs)))
        assert "exposure" not in written.within[0]
        assert any("withheld" in note for note in _exposure_notes(written))


def test_an_exposure_with_no_active_day_is_unused(tmp_path: Path) -> None:
    written = _written(tmp_path, _exposed_profile(_exposure(days=0)))
    assert "exposure" not in written.within[0]
    (note,) = _exposure_notes(written)
    assert "no active day" in note


def test_an_exposure_of_a_relationship_with_no_within_entry_is_unused(tmp_path: Path) -> None:
    profile = _exposed_profile(_exposure())
    del profile["relationships"][0]["dates"]
    written = _written(tmp_path / "none", profile)
    assert written.within == []
    (note,) = _exposure_notes(written)
    assert "no within entry" in note


def test_a_second_exposure_of_one_relationship_is_unused(tmp_path: Path) -> None:
    written = _written(
        tmp_path, _exposed_profile(_exposure(), _exposure("signed_up", None, children=300))
    )
    assert written.within[0]["exposure"] is True
    (note,) = _exposure_notes(written)
    assert "lifetime columns" in note


def test_a_start_only_lifetime_is_matched_by_an_exposure_with_no_end(tmp_path: Path) -> None:
    written = _written(tmp_path, _exposed_profile(_exposure("signed_up", None), lifetime=False))
    assert written.lifetimes == {"customer": {"emit": "customer", "start": "signed_up"}}
    assert written.within[0]["exposure"] is True
    assert not _exposure_notes(written)
    other = _written(tmp_path / "ended", _exposed_profile(_exposure(), lifetime=False))
    assert "exposure" not in other.within[0]


def test_a_written_bundle_with_an_exposure_plans(tmp_path: Path) -> None:
    written = _written(tmp_path, _exposed_profile(_exposure()))
    planned = plan_stage(
        str(written.directory),
        state_dir=str(tmp_path / "state"),
        profile=str(written.profile_path),
    )
    assert planned.exit_code == 0, planned.stderr


def test_a_profile_without_exposure_members_gives_the_bytes_it_gave_before(tmp_path: Path) -> None:
    plain = _exposed_profile()
    plain["relationships"][0].pop("exposure")
    first = _written(tmp_path / "first", copy.deepcopy(plain))
    assert "exposure" not in first.within[0]
    second = _written(tmp_path / "second", copy.deepcopy(plain))
    for name in ("simulation.flux.yml", "world.flux.yml", "persona.flux.yml"):
        assert (first.directory / name).read_bytes().replace(
            first.profile_path.name.encode(), b"P"
        ) == (second.directory / name).read_bytes().replace(second.profile_path.name.encode(), b"P")
