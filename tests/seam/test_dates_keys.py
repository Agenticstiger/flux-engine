# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The namespace keys ``lifetimes``, ``within`` and ``rates`` (ADR 0024 sections 4 and 5, ADR 0025 section 2): read, checked, bound.

The bundle is ``tests/fixtures/temporal`` (customer, account, invoice, payment). A key is refused
with ``FLX-3105`` when it is malformed or when the contract cannot take it, and by kernel 0.1.0. A
declared key moves ``H`` of the emit that declares it and nothing else, ``G`` not at all. Nothing
generates from the keys yet, so a bundle that declares them runs under kernel 0.2.0 to the rows of
the bundle without them.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1
from flux_engine.seam.dates import (
    Histogram,
    LifetimeEntry,
    RatesEntry,
    WithinEntry,
    lifetime_bound_into_h,
    lifetime_entries_of,
    rates_bound_into_h,
    rates_entries_of,
    read_lifetimes_block,
    read_rates_block,
    read_within_block,
    within_bound_into_h,
    within_entries_of,
)
from flux_engine.seam.emit import seam_digests
from flux_engine.seam.resolve import (
    FLX_3105,
    _relationship_targets,
    load_bundle,
    resolve_all_emits,
)
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from tests.fixtures.temporal.bundle import CHAIN, SIMULATION_ID, copy_bundle, copy_uncut

pytestmark = pytest.mark.unit

EMITS = frozenset({"customer", "account", "invoice", "payment"})
EXPOSES = ("customer", "account", "invoice", "payment")
HISTOGRAM = '{ bins: ["0", "30", "90"], binWeights: ["3", "1"] }'


def _read_lifetimes(block: Any) -> Tuple[LifetimeEntry, ...]:
    return read_lifetimes_block(block, emits=EMITS, where="block", code=FLX_3105)


def _read_within(block: Any) -> Tuple[WithinEntry, ...]:
    return read_within_block(block, emits=EMITS, where="block", code=FLX_3105)


def _resolve(root: Path, keys: str, name: str = "bundle") -> Dict[str, Any]:
    bundle = copy_bundle(root, name, keys)
    return {
        resolved.expose_id: resolved
        for resolved in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION_ID)
    }


def _digests(root: Path, keys: str, name: str) -> Dict[str, Tuple[str, str]]:
    found = {}
    for expose_id, resolved in _resolve(root, keys, name).items():
        both = seam_digests(
            resolved.contract_document,
            resolved.emit,
            resolved.compile().column_names,
            resolved.extension,
        )
        found[expose_id] = (both.h.value, both.g.value)
    return found


# -- the readers -------------------------------------------------------------------------------


def test_a_lifetime_is_read_as_written() -> None:
    (entry,) = _read_lifetimes(
        [
            {
                "emit": "customer",
                "start": "cu_start",
                "end": "cu_end",
                "duration": {"bins": ["0", "30"], "binWeights": ["1"], "open": "2"},
                "endPastWindow": "kept",
                "target": "2.5",
            }
        ]
    )
    assert entry == LifetimeEntry(
        "customer",
        "cu_start",
        "cu_end",
        Histogram(("0", "30"), ("1",), None, "2"),
        "kept",
        "2.5",
    )
    assert entry.as_member() == {
        "start": "cu_start",
        "end": "cu_end",
        "duration": {"bins": ["0", "30"], "binWeights": ["1"], "open": "2"},
        "endPastWindow": "kept",
        "target": "2.5",
    }


def test_within_entries_are_read_in_the_order_declared_with_a_chain_split_on_its_dots() -> None:
    first, second = _read_within(
        [
            {"emit": "invoice", "relationship": "account.customer", "date": "i_issued"},
            {"emit": "invoice", "relationship": "account", "date": "i_issued"},
        ]
    )
    assert first.relationship == ("account", "customer") and second.relationship == ("account",)
    assert first.as_member() == {"relationship": "account.customer", "date": "i_issued"}


def test_a_period_is_a_within_entry_with_no_date() -> None:
    (entry,) = _read_within([{"emit": "account", "relationship": "customer", "lifetime": True}])
    assert entry.lifetime and entry.date is None and entry.column == "lifetime"
    assert entry.as_member() == {"relationship": "customer", "lifetime": True}


def test_the_members_read_back_from_a_bound_extension() -> None:
    lifetimes = _read_lifetimes(
        [{"emit": "customer", "start": "cu_start", "end": "cu_end", "duration": _HIST}]
    )
    within = _read_within(
        [{"emit": "account", "relationship": "customer", "date": "a_open", "offset": _HIST}]
    )
    extension = {
        "lifetimes": lifetime_bound_into_h(lifetimes, "customer"),
        "within": within_bound_into_h(within, "account"),
    }
    assert lifetime_entries_of(extension, "customer") == lifetimes
    assert within_entries_of(extension, "account") == within
    assert lifetime_bound_into_h(lifetimes, "account") is None
    assert within_bound_into_h(within, "customer") is None
    assert lifetime_entries_of(None, "customer") == () == within_entries_of({}, "customer")


_HIST = {"bins": ["0", "30", "90"], "binWeights": ["3", "1"]}
_L = {"emit": "customer", "start": "cu_start", "end": "cu_end"}
_W = {"emit": "account", "relationship": "customer", "date": "a_open"}


def _with(base: Dict[str, Any], **changes: Any) -> List[Dict[str, Any]]:
    return [{**base, **changes}]


@pytest.mark.parametrize(
    "block",
    [
        {"emit": "customer", "start": "cu_start"},  # not a list
        "customer",
        ["customer"],  # an entry that is not a mapping
        [{"emit": "customer"}],  # no start
        [{"emit": "customer", "start": ""}],
        [{"emit": "customer", "start": 3}],
        _with(_L, unknown="x"),  # an unknown key
        _with(_L, emit="orders"),  # an emit the Simulation does not emit
        _with(_L, emit=None),
        _with(_L, end="cu_start"),  # the end is the start
        [_L, _L],  # a second lifetime for one emit
        [{"emit": "invoice", "start": "i_issued", "duration": _HIST}],  # no end
        [{"emit": "invoice", "start": "i_issued", "endPastWindow": "open"}],
        [{"emit": "invoice", "start": "i_issued", "target": "1"}],
        _with(_L, endPastWindow="never"),
        _with(_L, endPastWindow=True),
        _with(_L, target=2),  # not a string
        _with(_L, target="x"),
        _with(_L, target="-1"),
        _with(_L, target="100"),
        _with(_L, target="1e2"),
        _with(_L, duration="30"),  # not a mapping
        _with(_L, duration={**_HIST, "of": "x"}),  # an unknown key
        _with(_L, duration={"binWeights": ["1"]}),  # no bins
        _with(_L, duration={"bins": ["0"], "binWeights": []}),  # one edge
        _with(_L, duration={"bins": [0, 30], "binWeights": ["1"]}),  # not strings
        _with(_L, duration={"bins": ["0", "1.5"], "binWeights": ["1"]}),  # not whole days
        _with(_L, duration={"bins": ["30", "0"], "binWeights": ["1"]}),  # not ascending
        _with(_L, duration={"bins": ["-3", "0", "2"], "binWeights": ["1", "1"]}),  # negative
        _with(_L, duration={"bins": ["0", "0"], "binWeights": ["1"]}),
        _with(_L, duration={"bins": ["0", "30"], "binWeights": ["1", "1"]}),  # not one per bin
        _with(_L, duration={"bins": ["0", "30"], "binWeights": []}),
        _with(_L, duration={"bins": ["0", "30"], "binWeights": ["-1"]}),
        _with(_L, duration={"bins": ["0", "30"], "binWeights": [1]}),
        _with(_L, duration={"bins": ["0", "30"], "binWeights": ["0"]}),  # all zero
        _with(_L, duration={"bins": ["0", "30"], "binWeights": ["1"], "closed": "both"}),
        _with(_L, duration={"bins": ["0", "30"], "binWeights": ["1"], "open": "-1"}),
        _with(_L, duration={"bins": ["0", "30"], "binWeights": ["0"], "open": "0"}),
        _with(_L, duration={"bins": [str(n) for n in range(1026)], "binWeights": ["1"] * 1025}),
        [dict(_L, emit="customer")] * 4097,  # more than 4 096 entries
    ],
)
def test_a_malformed_lifetimes_block_is_refused(block: Any) -> None:
    with pytest.raises(FluxError) as raised:
        _read_lifetimes(block)
    assert raised.value.code is FLX_3105
    assert "block" in str(raised.value)


def test_a_lifetime_that_holds_only_an_open_weight_is_read() -> None:
    entry = _read_lifetimes(
        _with(_L, duration={"bins": ["0", "30"], "binWeights": ["0"], "open": "1"})
    )[0]
    assert entry.duration is not None and entry.duration.open == "1"


@pytest.mark.parametrize(
    "block",
    [
        {"emit": "account"},  # not a list
        ["account"],
        [{"emit": "account", "relationship": "customer"}],  # neither date nor lifetime
        [{"emit": "account", "date": "a_open"}],  # no relationship
        _with(_W, lifetime=True),  # both
        _with(_W, lifetime=False),  # a flag that is not true
        _with(_W, date=None, lifetime=1),
        _with(_W, unknown="x"),
        _with(_W, emit="orders"),
        _with(_W, relationship=""),
        _with(_W, relationship="a..b"),
        _with(_W, relationship=".customer"),
        _with(_W, date=""),
        _with(_W, target="100"),
        _with(_W, target=5),
        _with(_W, offset={"bins": ["0", "30"], "binWeights": ["1"], "open": "1"}),  # no open
        _with(_W, offset={"bins": ["0", "30"], "binWeights": ["1", "2"]}),
        _with(_W, offset={"bins": ["5", "3"], "binWeights": ["1"]}),
        [_W, _W],  # one relationship twice for a column
        # entries of one column that are not on one chain
        [_W, {**_W, "relationship": "other"}],
        [
            {**_W, "relationship": "account.customer"},
            {**_W, "relationship": "customer.account"},
        ],
        # two offsets for one column
        [
            {**_W, "offset": _HIST},
            {**_W, "relationship": "customer.region", "offset": _HIST},
        ],
        [{**_W, "relationship": "a"}] * 4097,
    ],
)
def test_a_malformed_within_block_is_refused(block: Any) -> None:
    with pytest.raises(FluxError) as raised:
        _read_within(block)
    assert raised.value.code is FLX_3105
    assert "block" in str(raised.value)


def test_entries_of_one_column_on_one_chain_are_read_and_the_message_names_the_index() -> None:
    entries = _read_within(
        [
            {**_W, "relationship": "customer.region", "offset": _HIST},
            {**_W, "relationship": "customer"},
            {**_W, "relationship": "customer.region.zone"},
        ]
    )
    assert len(entries) == 3
    with pytest.raises(FluxError, match=r"block\[1\]"):
        _read_within([_W, {**_W, "relationship": "other"}])


# -- the contract's side -----------------------------------------------------------------------


def test_the_chain_resolves_and_binds_only_the_emits_own_entries(tmp_path: Path) -> None:
    resolved = _resolve(tmp_path, CHAIN)
    assert resolved["customer"].extension["lifetimes"] == {"start": "cu_start", "end": "cu_end"}
    assert "within" not in resolved["customer"].extension
    assert resolved["account"].extension["lifetimes"] == {"start": "a_open", "end": "a_close"}
    assert resolved["account"].extension["within"] == [
        {"relationship": "customer", "date": "a_open"}
    ]
    assert resolved["invoice"].extension["lifetimes"] == {"start": "i_issued"}
    assert "lifetimes" not in resolved["payment"].extension
    assert resolved["payment"].extension["within"] == [
        {"relationship": "invoice", "date": "p_paid"}
    ]


def test_the_defaults_are_not_written_into_the_bound_member(tmp_path: Path) -> None:
    """``endPastWindow`` defaults where the entry is used; the member keeps the declaration."""
    resolved = _resolve(tmp_path, CHAIN)
    assert "endPastWindow" not in resolved["customer"].extension["lifetimes"]


def test_a_period_and_a_chain_name_and_an_offset_are_accepted(tmp_path: Path) -> None:
    keys = f"""\
lifetimes:
  - {{ emit: customer, start: cu_start, end: cu_end }}
  - {{ emit: account, start: a_open, end: a_close, duration: {HISTOGRAM}, endPastWindow: open }}
  - {{ emit: invoice, start: i_issued }}
within:
  - {{ emit: account, relationship: customer, lifetime: true, target: '1.5' }}
  - {{ emit: invoice, relationship: account, date: i_issued, offset: {HISTOGRAM} }}
  - {{ emit: invoice, relationship: account.customer, date: i_issued }}
"""
    resolved = _resolve(tmp_path, keys)
    assert [entry["relationship"] for entry in resolved["invoice"].extension["within"]] == [
        "account",
        "account.customer",
    ]
    assert resolved["account"].extension["within"][0] == {
        "relationship": "customer",
        "lifetime": True,
        "target": "1.5",
    }


_CONTRACT_REFUSALS = [
    # the columns exist
    ("lifetimes:\n  - { emit: customer, start: nope }", "nope"),
    ("lifetimes:\n  - { emit: customer, start: cu_start, end: nope }", "nope"),
    # the columns are DATE or TIMESTAMP
    ("lifetimes:\n  - { emit: customer, start: cu_score }", "INT"),
    ("lifetimes:\n  - { emit: customer, start: cu_start, end: cu_score }", "INT"),
    # the end is the start's kind
    ("lifetimes:\n  - { emit: account, start: a_open, end: a_limit }", "INT"),
    # open on a column the contract requires, and an open weight on a kept end
    (
        "lifetimes:\n  - { emit: customer, start: cu_end, end: cu_start, endPastWindow: open }",
        "requires",
    ),
    (
        "lifetimes:\n  - { emit: customer, start: cu_end, end: cu_start,"
        ' duration: { bins: ["0", "9"], binWeights: ["1"], open: "1" } }',
        "open weight",
    ),
    (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, endPastWindow: kept,"
        ' duration: { bins: ["0", "9"], binWeights: ["1"], open: "1" } }',
        "open weight",
    ),
    # a within date column
    (CHAIN + "  - { emit: invoice, relationship: account, date: nope }", "nope"),
    (CHAIN + "  - { emit: invoice, relationship: account, date: i_amount }", "INT"),
    # the relationship chain is the contract's, and to-one
    (
        "lifetimes:\n  - { emit: customer, start: cu_start }\nwithin:\n  - { emit: account, relationship: nope, date: a_open }",
        "nope",
    ),
    (
        "lifetimes:\n  - { emit: account, start: a_open }\nwithin:\n  - { emit: invoice, relationship: account.nope, date: i_issued }",
        "nope",
    ),
    # the last target declares a lifetime
    ("within:\n  - { emit: account, relationship: customer, date: a_open }", "no lifetime"),
    (
        "lifetimes:\n  - { emit: account, start: a_open }\nwithin:\n"
        "  - { emit: invoice, relationship: account.customer, date: i_issued }",
        "customer",
    ),
    # lifetime: true needs the emit's own lifetime
    (
        "lifetimes:\n  - { emit: customer, start: cu_start }\nwithin:\n"
        "  - { emit: account, relationship: customer, lifetime: true }",
        "lifetime: true",
    ),
]


@pytest.mark.parametrize(("keys", "named"), _CONTRACT_REFUSALS)
def test_what_the_contract_cannot_take_is_refused(keys: str, named: str, tmp_path: Path) -> None:
    with pytest.raises(FluxError) as raised:
        _resolve(tmp_path, keys)
    assert raised.value.code is FLX_3105
    text = str(raised.value)
    assert SIMULATION_ID in text and named in text


def test_a_relationship_that_is_not_to_one_is_refused(tmp_path: Path) -> None:
    """The contract schema allows only to-one cardinalities, so the check is called on an expose."""
    expose = {
        "exposeId": "account",
        "semantics": {
            "relationships": [{"name": "invoices", "to": "invoice", "cardinality": "one_to_many"}]
        },
    }
    contract = _resolve(tmp_path, "")["account"].contract_document
    with pytest.raises(FluxError, match="not to-one") as raised:
        _relationship_targets(contract, expose, ("invoices",), "at")
    assert raised.value.code is FLX_3105


def test_a_hidden_column_is_a_column_a_lifetime_may_name(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "hidden")
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    assert "      customer:\n        rows: 60\n" in text
    text = text.replace(
        "      customer:\n        rows: 60\n",
        "      customer:\n        rows: 60\n"
        "        hiddenColumns:\n          - { name: churned, type: DATE }\n",
        1,
    ).replace(
        "  com.agentics.flux/1:\n",
        "  com.agentics.flux/1:\n    lifetimes:\n"
        "      - { emit: customer, start: cu_start, end: churned }\n",
        1,
    )
    simulation.write_text(text, "utf-8")
    resolved = {
        entry.expose_id: entry
        for entry in resolve_all_emits(load_bundle(bundle), simulation_id=SIMULATION_ID)
    }
    assert resolved["customer"].extension["lifetimes"] == {"start": "cu_start", "end": "churned"}


# -- kernel 0.1.0 ------------------------------------------------------------------------------


def test_kernel_0_1_0_refuses_the_keys_naming_the_kernel(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "chain", CHAIN)
    planned = plan(bundle, state_dir=tmp_path / ".flux", kernel="0.1.0")
    assert not planned.success and planned.error_code == "FLX-3105"
    assert "kernel 0.1.0" in planned.stderr and "lifetimes" in planned.stderr
    with pytest.raises(FluxError) as raised:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert raised.value.code is FLX_3105
    assert "kernel 0.1.0" in str(raised.value) and "lifetimes" in str(raised.value)


def test_kernel_0_1_0_refuses_a_within_entry_by_itself(tmp_path: Path) -> None:
    """The emit that declares only ``within`` (the lifetime it reads is another emit's) is refused too."""
    emits = _resolve(tmp_path, CHAIN)
    only_within = [
        replace(
            entry,
            extension={k: v for k, v in entry.extension.items() if k != "lifetimes"},
        )
        for entry in emits.values()
        if entry.expose_id == "payment"
    ]
    with pytest.raises(FluxError) as raised:
        pipeline.check_hidden_declared(only_within, where=SIMULATION_ID, counter_based=False)
    assert raised.value.code is FLX_3105
    assert "kernel 0.1.0" in str(raised.value) and "within" in str(raised.value)
    pipeline.check_hidden_declared(only_within, where=SIMULATION_ID, counter_based=True)


def test_kernel_0_1_0_still_runs_a_bundle_without_the_keys(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "plain")
    planned = plan(bundle, state_dir=tmp_path / ".flux", kernel="0.1.0")
    assert planned.success, planned.stderr


def test_validate_refuses_a_malformed_key(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "bad", "lifetimes:\n  - { emit: customer, start: nope }")
    result = validate(bundle)
    assert result.exit_code != 0 and "FLX-3105" in (result.stderr + result.stdout)


# -- H and G -----------------------------------------------------------------------------------


def test_a_declared_key_moves_h_of_its_emit_only_and_g_not_at_all(tmp_path: Path) -> None:
    plain = _digests(tmp_path, "", "plain")
    declared = _digests(tmp_path, CHAIN, "declared")
    for expose_id in ("customer", "account", "invoice", "payment"):
        assert declared[expose_id][1] == plain[expose_id][1], f"G of {expose_id} moved"
        assert declared[expose_id][0] != plain[expose_id][0], f"H of {expose_id} did not move"
    within_only = _digests(
        tmp_path,
        "lifetimes:\n  - { emit: invoice, start: i_issued }\n"
        "within:\n  - { emit: payment, relationship: invoice, date: p_paid }",
        "within-only",
    )
    assert within_only["customer"] == plain["customer"]
    assert within_only["account"] == plain["account"]
    assert within_only["invoice"][0] != plain["invoice"][0]
    assert within_only["payment"][0] != plain["payment"][0]


def test_adding_or_changing_an_entry_moves_h_of_the_declaring_emit(tmp_path: Path) -> None:
    base = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "  - { emit: account, start: a_open }\n"
        "  - { emit: invoice, start: i_issued }\n"
        "within:\n  - { emit: payment, relationship: invoice, date: p_paid }\n"
    )
    first = _digests(tmp_path, base, "first")
    more = _digests(
        tmp_path,
        base + "  - { emit: payment, relationship: invoice.account, date: p_paid }\n",
        "more",
    )
    assert more["payment"][0] != first["payment"][0]
    assert more["customer"][0] == first["customer"][0] and more["invoice"][0] == first["invoice"][0]
    changed = _digests(tmp_path, base.replace("p_paid }", "p_paid, target: '2' }"), "changed")
    assert changed["payment"][0] != first["payment"][0]
    assert changed["customer"][0] == first["customer"][0]
    lifetime = _digests(
        tmp_path, base.replace("end: cu_end }", "end: cu_end, endPastWindow: kept }"), "lifetime"
    )
    assert lifetime["customer"][0] != first["customer"][0]
    assert lifetime["payment"][0] == first["payment"][0]
    default = _digests(
        tmp_path, base.replace("end: cu_end }", "end: cu_end, endPastWindow: open }"), "d"
    )
    assert default["customer"][0] != first["customer"][0]


# -- a run -------------------------------------------------------------------------------------


def _run(bundle: Path, state: Path) -> Tuple[Dict[str, Any], Dict[str, str]]:
    planned = plan(bundle, state_dir=state, kernel="0.2.0")
    assert planned.success, planned.stderr
    applied = apply(
        bundle, run_id=str(planned.artifacts["run_id"]), state_dir=state, auto_plan=False
    )
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    digests = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))["components"]["emits"]
    rows = {
        expose_id: layout.emit_path(expose_id).read_text(encoding="utf-8") for expose_id in EXPOSES
    }
    return {"emits": digests}, rows


def test_a_bundle_that_declares_the_keys_plans_and_applies_under_kernel_0_2_0(
    tmp_path: Path,
) -> None:
    """Nothing reads the keys yet: the date columns are drawn as the contract's columns are.

    ``H`` seeds the run (KERNEL-SPEC section 3), so the declared bundle's rows are another draw of
    the same statistics, and every column holds what its statistics allow.
    """
    plain_digests, plain_rows = _run(copy_bundle(tmp_path, "plain"), tmp_path / "plain-state")
    declared_digests, declared_rows = _run(
        copy_uncut(tmp_path, "declared", CHAIN), tmp_path / "declared-state"
    )
    again_digests, again_rows = _run(copy_uncut(tmp_path, "again", CHAIN), tmp_path / "again-state")
    assert again_rows == declared_rows and again_digests == declared_digests
    for expose_id in EXPOSES:
        assert declared_rows[expose_id].count("\n") == plain_rows[expose_id].count("\n") > 2
    for line in declared_rows["customer"].splitlines()[1:]:
        start = json.loads(line)[1]
        assert "2023-01-01" <= start <= "2025-06-30"


_BIG = "9" * 5000


@pytest.mark.parametrize(
    ("block", "named"),
    [
        (_with(_L, duration={"bins": ["0", _BIG], "binWeights": ["1"]}), "bins"),
        (_with(_L, duration={"bins": ["0", "10000000"], "binWeights": ["1"]}), "bins"),
        (_with(_L, duration={"bins": ["0", "30"], "binWeights": ["1" * 5000]}), "binWeights"),
        (
            _with(_L, duration={"bins": ["0", "30"], "binWeights": ["1"], "open": "1" * 5000}),
            "open",
        ),
        (_with(_L, target="1" * 5000), "target"),
        (_with(_L, target="0." + "1" * 5000), "target"),
    ],
)
def test_a_digit_string_past_its_bound_is_refused_naming_its_key(block: Any, named: str) -> None:
    with pytest.raises(FluxError) as raised:
        _read_lifetimes(block)
    assert raised.value.code is FLX_3105
    assert named in str(raised.value)


def test_an_offset_and_a_within_target_bound_their_digits_too() -> None:
    for entry, named in (
        (_with(_W, offset={"bins": ["-" + _BIG, "0"], "binWeights": ["1"]}), "bins"),
        (_with(_W, offset={"bins": ["0", "30"], "binWeights": [_BIG]}), "binWeights"),
        (_with(_W, target=_BIG), "target"),
    ):
        with pytest.raises(FluxError) as raised:
            _read_within(entry)
        assert raised.value.code is FLX_3105 and named in str(raised.value)


def test_the_widest_bin_edge_and_a_weight_of_most_digits_are_read() -> None:
    from flux_stats.spelling import MOST_DIGITS

    entry = _read_lifetimes(
        _with(_L, duration={"bins": ["0", "9999999"], "binWeights": ["9" * MOST_DIGITS]})
    )[0]
    assert entry.duration is not None
    assert _read_within(_with(_W, offset={"bins": ["-9999999", "0"], "binWeights": ["1"]}))


def test_a_relationship_of_thirty_two_hops_is_read_and_thirty_three_is_refused() -> None:
    assert len(_read_within(_with(_W, relationship=".".join(["r"] * 32)))[0].relationship) == 32
    with pytest.raises(FluxError) as raised:
        _read_within(_with(_W, relationship=".".join(["r"] * 33)))
    assert raised.value.code is FLX_3105
    assert "block[0].relationship" in str(raised.value)


# ---------------------------------------------------------------------------
# A duration that is an exponential or a dwell family (ADR 0025 section 3)
# ---------------------------------------------------------------------------

_EXP = {"mean": "45", "open": "0.25"}


def _family(**changes: Any) -> Dict[str, Any]:
    return {
        "given": "cu_score",
        "states": {"10": _HIST, "20": {"mean": "5"}},
        "otherwise": _EXP,
        **changes,
    }


def test_an_exponential_duration_is_read() -> None:
    from flux_engine.seam.dates import ExponentialDeclaration

    (entry,) = _read_lifetimes(_with(_L, duration={"mean": "30.5"}))
    assert entry.duration == ExponentialDeclaration(mean="30.5")
    (entry,) = _read_lifetimes(_with(_L, duration=_EXP))
    assert entry.duration == ExponentialDeclaration(mean="45", open="0.25")
    assert entry.as_member()["duration"] == _EXP
    assert _read_lifetimes(_with(_L, duration={"mean": "1", "open": "0"}))


def test_a_dwell_family_duration_is_read_and_bound_as_written() -> None:
    from flux_engine.seam.dates import DwellFamilyDeclaration, ExponentialDeclaration

    (entry,) = _read_lifetimes(_with(_L, duration=_family()))
    duration = entry.duration
    assert isinstance(duration, DwellFamilyDeclaration)
    assert duration.given == "cu_score"
    assert [text for text, _ in duration.states] == ["10", "20"]
    assert duration.states[0][1] == Histogram(("0", "30", "90"), ("3", "1"))
    assert duration.states[1][1] == ExponentialDeclaration("5")
    assert duration.otherwise == ExponentialDeclaration("45", "0.25")
    member = entry.as_member()
    assert member["duration"] == _family()
    # What H holds reads back as the entry (the bound member is what the generator reads).
    bound = {"lifetimes": member}
    assert lifetime_entries_of(bound, "customer")[0].duration == duration
    only_otherwise = _read_lifetimes(_with(_L, duration={"given": "cu_score", "otherwise": _HIST}))
    assert only_otherwise[0].duration is not None
    only_states = _read_lifetimes(_with(_L, duration={"given": "cu_score", "states": {"x": _EXP}}))
    assert only_states[0].duration is not None


def test_the_forms_of_a_duration_are_told_apart_by_their_keys() -> None:
    plain = _read_lifetimes(_with(_L, duration=_HIST))[0]
    assert plain.duration == Histogram(("0", "30", "90"), ("3", "1"))
    assert plain.as_member()["duration"] == _HIST


@pytest.mark.parametrize(
    "duration",
    [
        {"mean": "30", **_HIST},  # an exponential and a histogram
        {"mean": "30", "given": "cu_score", "otherwise": _HIST},  # an exponential and a family
        {**_HIST, "given": "cu_score", "states": {}},  # a histogram and a family
        {"open": "0.5"},  # no form
        {},
        {"mean": "30", "unknown": "x"},
        {"mean": 30},  # not a string
        {"mean": "0"},  # not above 0
        {"mean": "0.0"},
        {"mean": "-1"},
        {"mean": "1e3"},
        {"mean": "x"},
        {"mean": "9" * 1001},  # past the digits of a weight
        {"mean": "30", "open": "1"},  # an open share outside [0, 1)
        {"mean": "30", "open": "1.5"},
        {"mean": "30", "open": "-0.1"},
        {"mean": "30", "open": 0.5},
        {"mean": "30", "open": "0." + "9" * 1001},
        _family(states={}, otherwise=None),  # an empty family
        {"given": "cu_score"},  # no states, no otherwise
        _family(states={}) | {"otherwise": None},
        _family(states=["a"]),  # not a mapping
        _family(states={"": _HIST}),  # an empty state
        _family(states={"a": "x"}),  # a state whose dwell is not a mapping
        _family(states={"a": _family()}),  # a family inside a family
        _family(states={"a": {"mean": "3", **_HIST}}),  # a mixed member
        _family(states={"a": {"bins": ["-3", "30"], "binWeights": ["1"]}}),  # negative bins
        _family(otherwise={"bins": ["-3", "30"], "binWeights": ["1"]}),
        _family(otherwise={"mean": "0"}),
        _family(otherwise="x"),
        _family(given=None),
        _family(given=""),
        _family(given="cu_start"),  # the start
        _family(given="cu_end"),  # the end
        _family(extra="x"),  # an unknown key
        {k: v for k, v in _family().items() if k != "given"},
        _family(states={str(n): _EXP for n in range(1025)}),  # more than 1 024 states
    ],
)
def test_a_malformed_exponential_or_family_is_refused(duration: Any) -> None:
    with pytest.raises(FluxError) as raised:
        _read_lifetimes(_with(_L, duration=duration))
    assert raised.value.code is FLX_3105
    assert "block[0].duration" in str(raised.value)


def test_a_family_of_1024_states_is_read() -> None:
    (entry,) = _read_lifetimes(
        _with(_L, duration=_family(states={str(n): _EXP for n in range(1024)}))
    )
    assert entry.duration is not None


def test_a_family_whose_given_the_emit_does_not_hold_is_refused_where_the_contract_is_known(
    tmp_path: Path,
) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, duration: "
        "{ given: nowhere, otherwise: { mean: '30' } } }\n"
    )
    with pytest.raises(FluxError) as raised:
        _resolve(tmp_path, keys)
    assert raised.value.code is FLX_3105 and "nowhere" in str(raised.value)


def test_a_dwell_with_open_rows_on_a_kept_end_is_refused(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: account, start: a_open, end: a_close, endPastWindow: kept, "
        "duration: { mean: '30', open: '0.2' } }\n"
    )
    with pytest.raises(FluxError) as raised:
        _resolve(tmp_path, keys)
    assert raised.value.code is FLX_3105 and "open" in str(raised.value)
    kept = keys.replace(", open: '0.2'", "")
    assert _resolve(tmp_path, kept, "kept")


def test_kernel_0_1_0_refuses_an_exponential_duration(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, duration: "
        "{ mean: '30' } }\n"
    )
    bundle = copy_bundle(tmp_path, "kernel1", keys)
    with pytest.raises(FluxError) as raised:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert raised.value.code is FLX_3105 and "kernel 0.1.0" in str(raised.value)


# -- rates (ADR 0025 section 2) ----------------------------------------------------------------

WEEK = ["1", "1", "1", "1", "1", "0.5", "0.5"]
MONTHS = [str(n) for n in range(1, 13)]
HOURS = ["1"] * 168
RATE = {"emit": "invoice", "column": "i_issued", "dayOfWeek": WEEK}
#: The chain, the invoice's issue date as a within entry of its account, and its rates.
RATES_KEYS = (
    "lifetimes:\n  - { emit: account, start: a_open, end: a_close, duration: "
    "{ bins: ['0', '300', '900'], binWeights: ['2', '1'] } }\n  - { emit: invoice, start: i_issued }\n"
    "within:\n  - { emit: invoice, relationship: account, date: i_issued }\n"
    "rates:\n  - { emit: invoice, column: i_issued, dayOfWeek: ['1', '1', '1', '1', '1', '0.5', '0.5'] }\n"
)


def _read_rates(block: Any) -> Tuple[RatesEntry, ...]:
    return read_rates_block(block, emits=EMITS, where="block", code=FLX_3105)


def test_a_rates_entry_is_read_as_written() -> None:
    (entry,) = _read_rates(
        [{"emit": "invoice", "column": "i_issued", "month": MONTHS, "hourOfWeek": HOURS}]
    )
    assert entry == RatesEntry(
        "invoice", "i_issued", month=tuple(MONTHS), day_of_week=None, hour_of_week=tuple(HOURS)
    )
    assert entry.as_member() == {"column": "i_issued", "month": MONTHS, "hourOfWeek": HOURS}
    (only,) = _read_rates([RATE])
    assert only.as_member() == {"column": "i_issued", "dayOfWeek": WEEK}


def test_the_rates_member_reads_back_from_a_bound_extension() -> None:
    entries = _read_rates([RATE, {"emit": "account", "column": "a_open", "month": MONTHS}])
    extension = {"rates": rates_bound_into_h(entries, "invoice")}
    assert rates_entries_of(extension, "invoice") == (entries[0],)
    assert rates_bound_into_h(entries, "customer") is None
    assert rates_entries_of(None, "invoice") == () == rates_entries_of({}, "invoice")


@pytest.mark.parametrize(
    "block",
    [
        {"emit": "invoice", "column": "i_issued", "dayOfWeek": WEEK},  # not a list
        "invoice",
        ["invoice"],  # an entry that is not a mapping
        _with(RATE, unknown="x"),  # an unknown key
        _with(RATE, emit="orders"),  # an emit the Simulation does not emit
        _with(RATE, emit=None),
        [{"emit": "invoice", "dayOfWeek": WEEK}],  # no column
        _with(RATE, column=""),
        _with(RATE, column=3),
        [{"emit": "invoice", "column": "i_issued"}],  # no factor
        _with(RATE, dayOfWeek=WEEK[:6]),  # the wrong size
        _with(RATE, dayOfWeek=WEEK + ["1"]),
        _with(RATE, dayOfWeek="1111111"),  # not a list
        _with(RATE, dayOfWeek=[1] * 7),  # not strings
        _with(RATE, dayOfWeek=["-1"] + ["1"] * 6),  # below zero
        _with(RATE, dayOfWeek=["1e2"] + ["1"] * 6),  # not a decimal
        _with(RATE, dayOfWeek=["x"] + ["1"] * 6),
        _with(RATE, dayOfWeek=["0"] * 7),  # all zero
        _with(RATE, dayOfWeek=["1" * 1001] + ["1"] * 6),  # more digits than a weight has
        _with(RATE, month=MONTHS[:11]),
        _with(RATE, month=["0"] * 12),
        _with(RATE, hourOfWeek=HOURS[:167]),
        _with(RATE, hourOfWeek=["0"] * 168),
        [RATE, RATE],  # one entry per column of an emit
        [RATE] * 4097,  # more than 4 096 entries
    ],
)
def test_a_malformed_rates_block_is_refused(block: Any) -> None:
    with pytest.raises(FluxError) as raised:
        _read_rates(block)
    assert raised.value.code is FLX_3105
    assert "block" in str(raised.value)


def test_a_rates_weight_of_thirty_digits_is_read_and_thirty_one_is_refused() -> None:
    thirty = "1" * 30
    assert len(thirty) == 30
    (entry,) = _read_rates([_with(RATE, dayOfWeek=[thirty] + ["1"] * 6)[0]])
    assert entry.day_of_week is not None and entry.day_of_week[0] == thirty
    with_point = "0." + "1" * 29  # 30 digits and a point
    _read_rates([_with(RATE, month=[with_point] + ["1"] * 11)[0]])
    for factor, size in (("dayOfWeek", 7), ("month", 12), ("hourOfWeek", 168)):
        too_long = "1" * 31
        with pytest.raises(FluxError) as raised:
            _read_rates([_with(RATE, **{factor: ["1"] * (size - 1) + [too_long]})[0]])
        assert raised.value.code is FLX_3105
        assert f"block[0].{factor}[{size - 1}]" in str(raised.value) and "30" in str(raised.value)


def test_two_columns_of_an_emit_and_one_column_of_two_emits_are_read() -> None:
    assert len(_read_rates([RATE, _with(RATE, column="i_other")[0]])) == 2
    assert len(_read_rates([RATE, _with(RATE, emit="payment")[0]])) == 2


def test_the_bound_extension_of_a_rates_entry_reaches_the_emit(tmp_path: Path) -> None:
    resolved = _resolve(tmp_path, RATES_KEYS)
    assert resolved["invoice"].extension["rates"] == [{"column": "i_issued", "dayOfWeek": WEEK}]
    assert "rates" not in (resolved["account"].extension or {})
    assert "rates" not in (resolved["payment"].extension or {})


@pytest.mark.parametrize(
    "rates, fragment",
    [
        ("{ emit: invoice, column: nope, month: MONTHS }", "does not declare"),
        ("{ emit: invoice, column: i_amount, month: MONTHS }", "INT"),  # not a date
        ("{ emit: invoice, column: i_id, month: MONTHS }", "STRING"),
        # a date no dates step of the emit sets
        ("{ emit: payment, column: p_paid, month: MONTHS }", "no dates step"),
        (
            "{ emit: account, column: a_close, month: MONTHS }",
            "no dates step",
        ),  # no duration draws it
    ],
)
def test_a_rates_entry_the_contract_and_the_steps_cannot_take_is_refused(
    tmp_path: Path, rates: str, fragment: str
) -> None:
    if "a_close" in rates:
        # an end with no duration is not drawn: it is the cell's own value
        keys = (
            "lifetimes:\n  - { emit: account, start: a_open, end: a_close }\n"
            "  - { emit: invoice, start: i_issued }\n"
            "within:\n  - { emit: invoice, relationship: account, date: i_issued }\n"
        )
    else:
        keys = RATES_KEYS.split("rates:")[0]
    keys += f"rates:\n  - {rates.replace('MONTHS', str(MONTHS))}\n"
    with pytest.raises(FluxError) as raised:
        _resolve(tmp_path, keys)
    assert raised.value.code is FLX_3105 and fragment in str(raised.value)


def test_an_hour_of_week_on_a_date_column_is_refused(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end }\n"
        "within:\n  - { emit: account, relationship: customer, date: a_open }\n"
        f"rates:\n  - {{ emit: account, column: a_open, hourOfWeek: {HOURS} }}\n"
    )
    with pytest.raises(FluxError) as raised:
        _resolve(tmp_path, keys)
    assert raised.value.code is FLX_3105
    assert "hourOfWeek" in str(raised.value) and "DATE" in str(raised.value)
    assert _resolve(tmp_path, keys.replace(f"hourOfWeek: {HOURS}", f"month: {MONTHS}"), "ok")


def test_the_columns_a_rates_entry_may_name_are_what_the_dates_steps_set(tmp_path: Path) -> None:
    keys = (
        "lifetimes:\n  - { emit: customer, start: cu_start, end: cu_end, duration: "
        "{ bins: ['0', '100'], binWeights: ['1'] } }\n"
        "  - { emit: account, start: a_open, end: a_close, endPastWindow: kept }\n"
        "within:\n  - { emit: account, relationship: customer, lifetime: true }\n"
        f"rates:\n  - {{ emit: customer, column: cu_end, month: {MONTHS} }}\n"
        f"  - {{ emit: account, column: a_open, month: {MONTHS} }}\n"
    )
    # a duration's end and a period's start
    assert _resolve(tmp_path, keys, "both")
    # a period's end with no duration is its cell's own value
    bad = keys + f"  - {{ emit: account, column: a_close, month: {MONTHS} }}\n"
    with pytest.raises(FluxError) as raised:
        _resolve(tmp_path, bad, "bad")
    assert raised.value.code is FLX_3105 and "a_close" in str(raised.value)


def test_a_rates_key_is_part_of_the_namespace(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "keyed", RATES_KEYS)
    assert validate(bundle).exit_code == 0
    broken = copy_bundle(tmp_path, "broken", RATES_KEYS.replace("'0.5', '0.5'", "'0.5'"))
    result = validate(broken)
    assert result.exit_code != 0 and "FLX-3105" in (result.stderr + result.stdout)


def test_kernel_0_1_0_refuses_rates_naming_the_kernel(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path, "kernel1-rates", RATES_KEYS)
    planned = plan(bundle, state_dir=tmp_path / ".flux", kernel="0.1.0")
    assert not planned.success and planned.error_code == "FLX-3105"
    assert "kernel 0.1.0" in planned.stderr and "rates" in planned.stderr
    with pytest.raises(FluxError) as raised:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert raised.value.code is FLX_3105
    assert "kernel 0.1.0" in str(raised.value) and "rates" in str(raised.value)
    emits = _resolve(tmp_path, RATES_KEYS, "plan-kernel1")
    with pytest.raises(FluxError) as planning:
        pipeline.plan_emit_rules(
            emits["invoice"],
            strict=False,
            where=SIMULATION_ID,
            counter_based=False,
            emits=tuple(emits.values()),
        )
    assert "kernel 0.1.0" in str(planning.value) and "rates" in str(planning.value)


def test_rates_move_h_of_the_declaring_emit_only_and_g_not_at_all(tmp_path: Path) -> None:
    without = RATES_KEYS.split("rates:")[0]
    plain = _digests(tmp_path, without, "no-rates")
    declared = _digests(tmp_path, RATES_KEYS, "rates")
    assert declared["invoice"][0] != plain["invoice"][0]
    for expose_id in EXPOSES:
        assert declared[expose_id][1] == plain[expose_id][1], f"G of {expose_id} moved"
        if expose_id != "invoice":
            assert declared[expose_id][0] == plain[expose_id][0], f"H of {expose_id} moved"
    changed = _digests(tmp_path, RATES_KEYS.replace("'0.5', '0.5'", "'0.25', '0.5'"), "changed")
    assert changed["invoice"][0] != declared["invoice"][0]
    assert changed["invoice"][1] == declared["invoice"][1]
