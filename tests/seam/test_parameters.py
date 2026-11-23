# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The parameter register's seam: the block, the numbers it counts, and its citations.

docs/spec/PARAMETER-REGISTER-SPEC.md. Pure functions over documents, so each
case builds or loads the documents it needs. The register through a real run is
in ``tests/stages/test_parameter_register.py``.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest

from flux_engine.concordance import predicate
from flux_engine.core.errors import FluxError
from flux_engine.runtime import resolve_cast
from flux_engine.seam import parameters as module
from flux_engine.seam.parameters import (
    ENGINE_BLOCK_NUMBERS,
    NOT_PARAMETERS,
    Source,
    build_parameter_register,
    check_profile_citations,
    declared_numbers,
    parse_pointer,
    read_parameters_block,
    render_pointer,
)
from flux_engine.seam.resolve import (
    EXTENSION_NAMESPACE,
    FLX_3105,
    flux_schema_validator,
    load_bundle,
    parameter_entries,
)
from flux_engine.stages.pipeline import parameter_register_of

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SMALL = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
PROFILE_EXAMPLE = json.loads(
    (REPO_ROOT / "docs" / "spec" / "profile-example.json").read_text(encoding="utf-8")
)
WHERE = "sim-x: extensions['com.agentics.flux/1'].parameters"
BLOCK = f"/extensions/{EXTENSION_NAMESPACE.replace('/', '~1')}"


def _read(block: Any, *, pinned: bool = False) -> Tuple[Any, ...]:
    return read_parameters_block(block, pinned=pinned, where=WHERE, code=FLX_3105)


def _refused(block: Any, *, pinned: bool = False) -> str:
    with pytest.raises(FluxError) as raised:
        _read(block, pinned=pinned)
    assert raised.value.code.code == "FLX-3105"
    return raised.value.detail


def _documents(bundle: Path) -> Tuple[Any, Dict[str, Mapping[str, Any]]]:
    loaded = load_bundle(bundle)
    cast = resolve_cast(loaded.flux_documents)
    return cast, dict(cast.documents)


def _register(documents: Mapping[str, Any], entries: Any, simulation_id: str) -> Any:
    return build_parameter_register(
        simulation_id=simulation_id,
        documents=documents,
        namespace=EXTENSION_NAMESPACE,
        entries=entries,
        profile_digest=None,
        code=FLX_3105,
    )


# ---------------------------------------------------------------------------
# RFC 6901
# ---------------------------------------------------------------------------

#: RFC 6901 section 5: the document and every pointer it gives, with its value.
RFC_DOCUMENT: Dict[str, Any] = {
    "foo": ["bar", "baz"],
    "": 0,
    "a/b": 1,
    "c%d": 2,
    "e^f": 3,
    "g|h": 4,
    "i\\j": 5,
    'k"l': 6,
    " ": 7,
    "m~n": 8,
}
RFC_POINTERS: List[Tuple[str, Any]] = [
    ("", RFC_DOCUMENT),
    ("/foo", ["bar", "baz"]),
    ("/foo/0", "bar"),
    ("/", 0),
    ("/a~1b", 1),
    ("/c%d", 2),
    ("/e^f", 3),
    ("/g|h", 4),
    ("/i\\j", 5),
    ('/k"l', 6),
    ("/ ", 7),
    ("/m~0n", 8),
]


@pytest.mark.parametrize(("pointer", "value"), RFC_POINTERS)
def test_every_pointer_of_rfc_6901_section_5_reaches_its_value(pointer: str, value: Any) -> None:
    tokens = parse_pointer(pointer)
    assert module._resolve(RFC_DOCUMENT, tokens) == value
    assert render_pointer(tokens) == pointer


def test_tilde_one_is_decoded_before_tilde_zero() -> None:
    # Section 4: "~01" is "~1" once decoded, never "/".
    assert parse_pointer("/~01") == ("~1",)
    assert render_pointer(("~1",)) == "/~01"


@pytest.mark.parametrize("text", ["foo", "/~2", "/a~", "/~x/b"])
def test_a_text_that_is_not_a_pointer_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_pointer(text)


@pytest.mark.parametrize("pointer", ["/foo/01", "/foo/-", "/foo/2", "/missing", "/foo/0/x"])
def test_a_pointer_that_reaches_nothing_is_a_lookup_error(pointer: str) -> None:
    with pytest.raises(LookupError):
        module._resolve(RFC_DOCUMENT, parse_pointer(pointer))


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------

ENTRY: Dict[str, Any] = {
    "document": "q3-retention-world",
    "pointer": "/spec/population/size",
    "source": "assumption",
    "reason": "Sized for a quick look.",
}
CITED: Dict[str, Any] = {
    "document": "q3-retention-world",
    "pointer": "/spec/population/size",
    "source": "profile",
    "statistic": {"pointer": "/tables/subscriber/rowCount"},
}


def _with(base: Dict[str, Any], **changes: Any) -> Dict[str, Any]:
    entry = dict(base)
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


def test_an_entry_of_each_source_is_read() -> None:
    entries = _read(
        [
            ENTRY,
            _with(CITED, pointer="/spec/temperature"),
            _with(
                CITED,
                pointer="/spec/population/lifecycleMix",
                statistic={
                    "pointer": "/tables/subscriber/columns/market/statistics/0",
                    "definition": "levelHistogram/1",
                },
            ),
            {"document": "q3-retention-world", "pointer": "/spec/seed", "source": "default"},
        ],
        pinned=True,
    )
    assert [entry.source for entry in entries] == [
        Source.ASSUMPTION,
        Source.PROFILE,
        Source.PROFILE,
        Source.DEFAULT,
    ]
    assert entries[0].reason == "Sized for a quick look."
    assert entries[0].tokens == ("spec", "population", "size")
    assert entries[1].citation is not None and entries[1].citation.definition is None
    assert entries[2].citation is not None
    assert entries[2].citation.definition == "levelHistogram/1"
    assert entries[3].reason is None
    assert [entry.label for entry in entries] == [f"{WHERE}[{n}]" for n in range(4)]


def test_an_empty_list_declares_a_register_with_no_entry() -> None:
    assert _read([]) == ()


def test_a_reason_is_kept_without_its_surrounding_space() -> None:
    (entry,) = _read([_with(ENTRY, reason="  A reason.\n")])
    assert entry.reason == "A reason."


@pytest.mark.parametrize(
    ("block", "pinned", "detail"),
    [
        ({"document": "x"}, False, f"{WHERE} is a list of entries"),
        ("entries", False, f"{WHERE} is a list of entries"),
        (["entry"], False, f"{WHERE}[0] is a mapping"),
        ([_with(ENTRY, note="x")], False, "declares ['note']; an entry carries"),
        ([_with(ENTRY, document=None)], False, "names its 'document'"),
        ([_with(ENTRY, pointer=None)], False, "names its 'pointer'"),
        ([_with(ENTRY, source=None)], False, "names its 'source'"),
        ([_with(ENTRY, document="")], False, ".document is the id of a document"),
        ([_with(ENTRY, pointer="spec/population")], False, ".pointer is not a JSON Pointer"),
        ([_with(ENTRY, pointer="/a~2")], False, "'~' is followed by '0' or '1'"),
        ([_with(ENTRY, pointer=3)], False, ".pointer is a JSON Pointer (RFC 6901), a string"),
        ([_with(ENTRY, source="measured")], False, ".source is one of"),
        ([_with(CITED, statistic=None)], True, "a number from the profile names its 'statistic'"),
        ([_with(CITED, reason="x")], True, "a number from the profile carries no 'reason'"),
        ([CITED], False, "cites the profile, and the Simulation pins none"),
        ([_with(CITED, statistic="rowCount")], True, ".statistic is a mapping holding 'pointer'"),
        ([_with(CITED, statistic={"pointer": "/t", "id": 1})], True, "declares ['id']"),
        ([_with(CITED, statistic={"definition": "mean/1"})], True, "names the statistic with"),
        ([_with(CITED, statistic={"pointer": ""})], True, "names a part of the profile"),
        (
            [_with(CITED, statistic={"pointer": "/t", "definition": "median/1"})],
            True,
            ".definition is a registered statistic or metric",
        ),
        ([_with(ENTRY, reason=None)], False, "an assumption gives its 'reason'"),
        ([_with(ENTRY, statistic={"pointer": "/t"})], False, "only a number from the profile"),
        ([_with(ENTRY, reason="   ")], False, ".reason is a non-empty string"),
        ([_with(ENTRY, reason="x" * 1001)], False, "has at most 1000 characters"),
        ([_with(ENTRY, reason="one\ntwo")], False, "is one line of text"),
        ([_with(ENTRY, reason=7)], False, ".reason is a non-empty string"),
        ([ENTRY, _with(ENTRY, source="default")], False, f"names the same numbers as {WHERE}[0]"),
    ],
)
def test_a_malformed_block_is_refused(block: Any, pinned: bool, detail: str) -> None:
    assert detail in _refused(block, pinned=pinned)


def test_two_spellings_of_one_pointer_name_the_same_numbers() -> None:
    # A key is compared in NFC, so the pointer's decomposed spelling is the same pointer.
    first = _with(ENTRY, pointer="/spec/café")
    second = _with(ENTRY, pointer="/spec/café", source="default")
    assert "names the same numbers" in _refused([first, second])


def test_the_resolver_reads_the_block_off_the_simulation() -> None:
    simulation = {
        "extensions": {
            EXTENSION_NAMESPACE: {"parameters": [ENTRY], "profile": None},
        }
    }
    (entry,) = parameter_entries(simulation, where="sim-x") or ()
    assert entry.document == "q3-retention-world"
    assert parameter_entries({"extensions": {EXTENSION_NAMESPACE: {"parameters": None}}}) is None
    assert parameter_entries({}) is None
    with pytest.raises(FluxError) as raised:
        parameter_entries(
            {"extensions": {EXTENSION_NAMESPACE: {"parameters": [CITED]}}}, where="sim-x"
        )
    assert "cites the profile, and the Simulation pins none" in raised.value.detail


# ---------------------------------------------------------------------------
# The numbers a run declares
# ---------------------------------------------------------------------------


def test_the_starter_declares_twenty_numbers_and_its_seeds_are_not_among_them() -> None:
    cast, documents = _documents(STARTER)
    numbers = declared_numbers(
        documents, simulation_id=cast.simulation_id, namespace=EXTENSION_NAMESPACE
    )
    found = {(number.document, number.pointer): number.value for number in numbers}
    assert len(numbers) == 20
    assert found[("q3-retention-world", "/spec/population/size")] == "500"
    # The number as written: the trailing zero of 42.0 is part of what was declared.
    assert found[("persona-pragmatic-family", "/spec/traits/monthlySpend/mean")] == "42.0"
    assert found[("q3-retention-world", "/spec/population/lifecycleMix/new")] == "0.15"
    assert ("q3-retention-world", "/spec/seed") not in found
    assert ("sim-payment-recovery", "/spec/seed") not in found
    # Documents in NFC order of their ids, each in key order, token by token.
    order = [(number.document, number.tokens) for number in numbers]
    assert order == sorted(order)


def test_the_engine_block_counts_its_named_positions_and_nothing_else() -> None:
    loaded = load_bundle(SMALL)
    cast = resolve_cast(loaded.flux_documents)
    register = parameter_register_of(loaded, cast)
    assert register is None  # the fixture declares none
    documents = dict(cast.documents)
    documents["pb-billing-history"] = loaded.flux_documents["pb-billing-history"]
    found = {
        (number.document, number.pointer): number.value
        for number in declared_numbers(
            documents, simulation_id=cast.simulation_id, namespace=EXTENSION_NAMESPACE
        )
    }
    fidelity = f"{BLOCK}/emits/payment_recovery_moment/fidelity"
    simulation = "sim-payment-recovery"
    assert found[(simulation, f"{BLOCK}/emits/payment_recovery_moment/rows")] == "64"
    assert found[(simulation, f"{fidelity}/records/min")] == "4"
    assert found[(simulation, f"{fidelity}/columns/amount/range/max")] == "10000"
    assert found[(simulation, f"{fidelity}/columns/market/maxCardinality")] == "8"
    assert found[(simulation, f"{fidelity}/properties/0/tolerance/value")] == "50"
    assert found[(simulation, f"{fidelity}/properties/1/tolerance/max")] == "0"
    assert found[(simulation, "/spec/emits/0/minCredibility")] == "0.7"
    assert found[("pb-billing-history", "/spec/scorecard/credibility")] == "0.78"
    assert found[("pb-billing-history", "/spec/drift/threshold")] == "0.2"
    # A timestamp bound is text, the grid and the budget are not parameters, and
    # nothing outside `spec` and this namespace counts.
    assert (simulation, f"{fidelity}/columns/aborted_at/range/min") not in found
    assert (simulation, f"{fidelity}/encoding/fractionalBits") not in found
    assert (simulation, f"{fidelity}/budget/maxChecksPerState") not in found
    assert not [key for key in found if "com.example.flux" in key[1]]
    assert not [key for key in found if key[1].startswith(("/agentPolicy", "/skills"))]


def test_a_playback_bound_to_the_run_s_world_is_read_and_another_is_not() -> None:
    loaded = load_bundle(SMALL)
    cast = resolve_cast(loaded.flux_documents)
    simulation = copy.deepcopy(dict(cast.simulation))
    simulation["extensions"][EXTENSION_NAMESPACE]["parameters"] = [
        {
            "document": "pb-billing-history",
            "pointer": "/spec/scorecard",
            "source": "assumption",
            "reason": "A backtest.",
        }
    ]
    documents = dict(loaded.flux_documents)
    documents["sim-payment-recovery"] = simulation
    other = copy.deepcopy(dict(loaded.flux_documents["pb-billing-history"]))
    other["id"] = "pb-elsewhere"
    other["spec"]["worldRef"] = "another-world"
    documents["pb-elsewhere"] = other

    class _Loaded:
        flux_documents = documents

    cast = resolve_cast({k: v for k, v in documents.items() if k != "pb-elsewhere"})
    register = parameter_register_of(_Loaded(), cast)  # type: ignore[arg-type]
    assert register is not None
    read = {registered.number.document for registered in register.numbers}
    assert "pb-billing-history" in read and "pb-elsewhere" not in read
    assert register.counts()["assumption"] == 2


def _placed(*prefix: str) -> set:
    """The keys :data:`ENGINE_BLOCK_NUMBERS` names right after *prefix*."""
    return {
        pattern[len(prefix)]
        for pattern in ENGINE_BLOCK_NUMBERS
        if pattern[: len(prefix)] == prefix and len(pattern) > len(prefix)
    }


@pytest.mark.parametrize(
    ("keys", "prefix", "not_numbers"),
    [
        (
            predicate.COLUMN_KEYS,
            ("emits", "*", "fidelity", "columns", "*"),
            {"support", "null", "family", "given", "unique", "allocation"},
        ),
        (
            predicate._PROPERTY_KEYS,
            ("emits", "*", "fidelity", "properties", "#"),
            {"property", "column", "columns", "groupBy", "measure", "groupByBins"},
        ),
        (
            predicate._TOLERANCE_KEYS,
            ("emits", "*", "fidelity", "properties", "#", "tolerance"),
            {"type", "measure", "sampling"},
        ),
        (predicate._RECORDS_KEYS, ("emits", "*", "fidelity", "records"), set()),
        (predicate._RANGE_KEYS, ("emits", "*", "fidelity", "columns", "*", "range"), set()),
        (
            predicate._BLOCK_KEYS,
            ("emits", "*", "fidelity"),
            {"encoding", "budget", "failureAction"},
        ),
    ],
)
def test_every_key_of_the_fidelity_block_is_placed(
    keys: Tuple[str, ...], prefix: Tuple[str, ...], not_numbers: set
) -> None:
    """A key added to one of the block's closed sets has to be counted or left out on purpose."""
    placed = _placed(*prefix)
    assert set(keys) == placed | not_numbers
    assert not placed & not_numbers


def test_every_namespace_key_is_counted_or_named_as_not_counted_in_the_spec() -> None:
    """The spec says which keys of the namespace hold counted numbers, and the code agrees."""
    from flux_engine.seam.resolve import _NAMESPACE_KEYS

    counted = {pattern[0] for pattern in ENGINE_BLOCK_NUMBERS}
    assert counted == {"emits", "scorecard"}
    spec = (REPO_ROOT / "docs" / "spec" / "PARAMETER-REGISTER-SPEC.md").read_text("utf-8")
    start = spec.index("Also not counted")
    named = set(re.findall(r"`([A-Za-z]+)`", spec[start : spec.index("A number there", start)]))
    # ``profile``, ``strictCoverage`` and ``parameters`` are named earlier; ``unused`` holds citations.
    left_out = _NAMESPACE_KEYS - counted - {"profile", "strictCoverage", "parameters", "unused"}
    assert left_out == named


def _numeric_positions(schema: Mapping[str, Any]) -> List[Tuple[str, Tuple[str, ...]]]:
    """Every (kind, path) the FLUX schema types as a number, ``*`` for a free key."""
    defs = schema.get("$defs", {})
    found: List[Tuple[str, Tuple[str, ...]]] = []

    def walk(node: Any, kind: str, path: Tuple[str, ...], seen: Tuple[str, ...]) -> None:
        if not isinstance(node, Mapping) or len(path) > 12:
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                walk(defs.get(name, {}), kind, path, seen + (name,))
            return
        types = node.get("type")
        if types in ("number", "integer") or (
            isinstance(types, list) and {"number", "integer"} & set(types)
        ):
            found.append((kind, path))
        for key, value in (node.get("properties") or {}).items():
            walk(value, kind, path + (key,), seen)
        extra = node.get("additionalProperties")
        if isinstance(extra, Mapping):
            walk(extra, kind, path + ("*",), seen)
        if isinstance(node.get("items"), Mapping):
            walk(node["items"], kind, path + ("#",), seen)
        for combinator in ("oneOf", "anyOf", "allOf"):
            for branch in node.get(combinator) or ():
                walk(branch, kind, path, seen)

    for clause in schema.get("allOf", ()):
        kind = clause.get("if", {}).get("properties", {}).get("kind", {}).get("const")
        if kind:
            walk(clause.get("then", {}), kind, (), ())
    return found


def test_every_position_left_out_is_a_number_the_flux_schema_declares() -> None:
    """A renamed seed would otherwise be counted silently: each exclusion must still exist."""
    positions = _numeric_positions(flux_schema_validator().schema)
    for kind, prefix in NOT_PARAMETERS:
        assert any(
            found_kind == kind and path[: len(prefix)] == prefix for found_kind, path in positions
        ), (kind, prefix)


# ---------------------------------------------------------------------------
# The register
# ---------------------------------------------------------------------------


def test_the_most_specific_entry_decides_and_an_unnamed_number_is_a_default() -> None:
    cast, documents = _documents(STARTER)
    entries = _read(
        [
            {
                "document": "persona-pragmatic-family",
                "pointer": "/spec/traits",
                "source": "assumption",
                "reason": "A workshop.",
            },
            {
                "document": "persona-pragmatic-family",
                "pointer": "/spec/traits/monthlySpend/min",
                "source": "default",
            },
        ]
    )
    register = _register(documents, entries, cast.simulation_id)
    by_pointer = {
        (registered.number.document, registered.number.pointer): registered
        for registered in register.numbers
    }
    trait = by_pointer[("persona-pragmatic-family", "/spec/traits/digitalFluency")]
    floor = by_pointer[("persona-pragmatic-family", "/spec/traits/monthlySpend/min")]
    size = by_pointer[("q3-retention-world", "/spec/population/size")]
    assert (trait.source, trait.entry.index if trait.entry else None) == (Source.ASSUMPTION, 0)
    assert (floor.source, floor.entry.index if floor.entry else None) == (Source.DEFAULT, 1)
    assert (size.source, size.entry) == (Source.DEFAULT, None)
    assert register.counts() == {"profile": 0, "assumption": 7, "default": 13}
    assert sum(register.counts().values()) == len(register.numbers) == 20


@pytest.mark.parametrize(
    ("entry", "detail"),
    [
        (
            {"document": "nobody", "pointer": "", "source": "default"},
            "names document 'nobody', which this run does not read",
        ),
        (
            {"document": "q3-retention-world", "pointer": "/spec/nowhere", "source": "default"},
            "parameters[0].pointer reaches nothing in 'q3-retention-world': /spec has no key "
            "'nowhere' (it has population, seed, sovereignty, temperature, vertical)",
        ),
        (
            {
                "document": "q3-retention-world",
                "pointer": "/spec/population/markets/7",
                "source": "default",
            },
            "/spec/population/markets has no item '7' (it has 3, from 0)",
        ),
        (
            {"document": "q3-retention-world", "pointer": "/spec/seed/x", "source": "default"},
            "/spec/seed is a value, not a mapping or a list",
        ),
        (
            {"document": "q3-retention-world", "pointer": "/spec/seed", "source": "default"},
            "reaches no number the register counts in 'q3-retention-world'",
        ),
        (
            {"document": "q3-retention-world", "pointer": "/spec/vertical", "source": "default"},
            "reaches no number the register counts",
        ),
    ],
)
def test_an_entry_that_names_no_number_of_the_run_is_refused(
    entry: Dict[str, Any], detail: str
) -> None:
    cast, documents = _documents(STARTER)
    with pytest.raises(FluxError) as raised:
        _register(documents, _read([entry]), cast.simulation_id)
    assert raised.value.code.code == "FLX-3105"
    assert raised.value.detail.startswith(f"{WHERE}[0]")
    assert detail in raised.value.detail


def test_a_pointer_to_a_whole_document_names_every_number_in_it() -> None:
    cast, documents = _documents(STARTER)
    entries = _read([{"document": "jr-payment-recovery", "pointer": "", "source": "default"}])
    register = _register(documents, entries, cast.simulation_id)
    decided = [r for r in register.numbers if r.entry is not None]
    assert [r.number.pointer for r in decided] == [
        "/spec/personaReactions/persona-pragmatic-family/successProbability"
    ]


# ---------------------------------------------------------------------------
# Citations against the pinned profile
# ---------------------------------------------------------------------------


def _cite(pointer: str, definition: Any = None) -> Any:
    statistic: Dict[str, Any] = {"pointer": pointer}
    if definition is not None:
        statistic["definition"] = definition
    entries = _read(
        [
            {
                "document": "q3-retention-world",
                "pointer": "/spec/population/size",
                "source": "profile",
                "statistic": statistic,
            }
        ],
        pinned=True,
    )
    cast, documents = _documents(STARTER)
    return _register(documents, entries, cast.simulation_id)


@pytest.mark.parametrize(
    ("pointer", "definition"),
    [
        ("/tables/payment/rowCount", None),
        ("/tables/payment/columns/amount/metrics/sum", "sum/1"),
        ("/tables/payment/columns/amount/statistics/0", "mean/1"),
        ("/tables/subscriber/columns/tenure_months/statistics/3/groups/0", "mean/1"),
        (
            "/tables/subscriber/columns/market/statistics/0/groups/0/levelCounts/0",
            "levelHistogram/1",
        ),
        ("/tables/subscriber/columns/tenure_months/metrics/quantiles", "quantiles/1"),
        ("/tables/payment/columnPairs/0/statistics/0", "correlation/1"),
        ("/relationships/0/statistics/1", "mean/1"),
        ("/queries/0/statistics/0", "levelHistogram/1"),
    ],
)
def test_a_citation_of_a_published_statistic_holds(pointer: str, definition: Any) -> None:
    check_profile_citations(_cite(pointer, definition), PROFILE_EXAMPLE, where="sim-x")


@pytest.mark.parametrize(
    ("pointer", "definition", "detail"),
    [
        (
            "/tables/nowhere/rowCount",
            None,
            ".pointer reaches nothing in the pinned profile past /tables",
        ),
        ("/tables/payment/rowCount", "count/1", "is a table's rowCount, which has no definition"),
        ("/tables/payment/columns", None, ".pointer reaches no statistic"),
        (
            "/tables/payment/columns/amount/statistics/0",
            None,
            "lands in a mean/1 statistic; name it with 'definition: mean/1'",
        ),
        (
            "/tables/payment/columns/amount/statistics/0",
            "quantile/1",
            "names quantile/1, and the pinned profile holds mean/1 there",
        ),
        (
            "/tables/subscriber/columns/tenure_months/metrics/min",
            "min/1",
            "reaches a value the pinned profile withheld",
        ),
        (
            "/tables/subscriber/columns/tenure_months/statistics/3/groups/1",
            "mean/1",
            "reaches a value the pinned profile withheld",
        ),
        (
            "/tables/subscriber/columns/market/statistics/0/groups/0/levelCounts/1",
            "levelHistogram/1",
            "reaches a value the pinned profile withheld",
        ),
    ],
)
def test_a_citation_the_profile_does_not_publish_is_refused(
    pointer: str, definition: Any, detail: str
) -> None:
    with pytest.raises(FluxError) as raised:
        check_profile_citations(_cite(pointer, definition), PROFILE_EXAMPLE, where="sim-x")
    assert raised.value.code.code == "FLX-3113"
    assert raised.value.detail.startswith(f"{WHERE}[0].statistic")
    assert detail in raised.value.detail


def test_a_statistic_whose_every_group_is_withheld_is_not_published() -> None:
    profile = copy.deepcopy(PROFILE_EXAMPLE)
    statistic = profile["tables"]["payment"]["columns"]["amount"]["statistics"][0]
    statistic["groups"] = [{"group": "", "status": "A"}]
    with pytest.raises(FluxError) as raised:
        check_profile_citations(
            _cite("/tables/payment/columns/amount/statistics/0", "mean/1"), profile, where="sim-x"
        )
    assert "withheld" in raised.value.detail


def test_a_citation_message_quotes_no_value_of_the_profile() -> None:
    with pytest.raises(FluxError) as raised:
        check_profile_citations(
            _cite("/tables/subscriber/columns/tenure_months/metrics/min", "min/1"),
            PROFILE_EXAMPLE,
            where="sim-x",
        )
    withheld = PROFILE_EXAMPLE["tables"]["payment"]["columns"]["amount"]["metrics"]["sum"]["value"]
    assert str(withheld) not in raised.value.detail
    assert str(PROFILE_EXAMPLE["tables"]["payment"]["rowCount"]) not in raised.value.detail


# ---------------------------------------------------------------------------
# More edge cases
# ---------------------------------------------------------------------------


def test_a_list_index_too_long_to_be_one_reaches_nothing() -> None:
    """int() refuses a string of 4301 digits; the pointer is refused before it gets there."""
    cast, documents = _documents(STARTER)
    entry = {
        "document": "q3-retention-world",
        "pointer": "/spec/population/markets/" + "9" * 5000,
        "source": "default",
    }
    with pytest.raises(FluxError) as raised:
        _register(documents, _read([entry]), cast.simulation_id)
    assert raised.value.code.code == "FLX-3105"
    assert "/spec/population/markets has no item '" + "9" * 40 + "...' (it has 3, from 0)" in (
        raised.value.detail
    )
    with pytest.raises(FluxError) as cited:
        check_profile_citations(
            _cite("/tables/payment/columns/amount/statistics/" + "1" * 5000, "mean/1"),
            PROFILE_EXAMPLE,
            where="sim-x",
        )
    assert cited.value.code.code == "FLX-3113"


def _simulation_with(columns: Dict[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {
        "sim": {
            "kind": "Simulation",
            "spec": {},
            "extensions": {
                EXTENSION_NAMESPACE: {"emits": {"e": {"fidelity": {"columns": columns}}}}
            },
        }
    }


def _engine_numbers(columns: Dict[str, Any]) -> Dict[str, str]:
    numbers = declared_numbers(
        _simulation_with(columns), simulation_id="sim", namespace=EXTENSION_NAMESPACE
    )
    prefix = f"{BLOCK}/emits/e/fidelity/columns/"
    return {number.pointer[len(prefix) :]: number.value for number in numbers}


def test_a_level_named_like_a_list_step_is_counted() -> None:
    found = _engine_numbers({"c": {"levels": {"#": "0.5", "a": "0.5"}}})
    assert found == {"c/levels/#": "0.5", "c/levels/a": "0.5"}
    given = _engine_numbers({"c": {"binWeightsGiven": {"#": ["1", "2"]}}})
    assert sorted(given) == ["c/binWeightsGiven/#/0", "c/binWeightsGiven/#/1"]


def test_every_spelling_the_block_s_readers_read_is_counted_in_its_canonical_form() -> None:
    found = _engine_numbers(
        {
            "a": {"range": {"min": ".5", "max": "1e4"}, "mean": "+5", "stddev": " 5. "},
            "t": {"range": {"min": "2026-07-01T00:00:00Z", "max": "1_0"}},
        }
    )
    assert found == {
        "a/mean": "5",
        "a/range/max": "10000",
        "a/range/min": "0.5",
        "a/stddev": "5",
        "t/range/max": "10",
    }


def test_a_number_too_long_to_write_out_is_refused_by_the_block_s_own_rule() -> None:
    # A zero keeps its scale in the canonical form, so "0e-10000000" is spelled in ten million digits.
    for text in ("1E+99999999", "0e-10000000", " 0.e-1001 "):
        with pytest.raises(FluxError) as raised:
            _engine_numbers({"a": {"mean": text}})
        assert raised.value.code.code == "FLX-5030", text
    assert _engine_numbers({"a": {"mean": "0e-1000"}}) == {"a/mean": "0." + "0" * 1000}


def test_a_list_of_groups_or_cells_named_whole_needs_one_published_member() -> None:
    profile = copy.deepcopy(PROFILE_EXAMPLE)
    groups = profile["tables"]["payment"]["columns"]["amount"]["statistics"][0]["groups"]
    for group in groups:
        group.clear()
        group.update({"group": "", "status": "A"})
    with pytest.raises(FluxError) as raised:
        check_profile_citations(
            _cite("/tables/payment/columns/amount/statistics/0/groups", "mean/1"),
            profile,
            where="sim-x",
        )
    assert "withheld" in raised.value.detail
    cells = "/tables/subscriber/columns/market/statistics/0/groups/0/levelCounts"
    # One cell of four is withheld: the table is published in part, as a statistic
    # with one published group is.
    check_profile_citations(_cite(cells, "levelHistogram/1"), PROFILE_EXAMPLE, where="sim-x")
    every_cell_withheld = copy.deepcopy(PROFILE_EXAMPLE)
    group = every_cell_withheld["tables"]["subscriber"]["columns"]["market"]["statistics"][0][
        "groups"
    ][0]
    group["cellStatus"] = ["A"] * len(group["cellStatus"])
    group["levelCounts"] = [None] * len(group["levelCounts"])
    with pytest.raises(FluxError):
        check_profile_citations(
            _cite(cells, "levelHistogram/1"), every_cell_withheld, where="sim-x"
        )


def test_a_message_prints_no_control_character_of_the_document_it_names() -> None:
    documents = {
        "sim": {"kind": "Simulation", "spec": {"a\x1b[2Jb": {"n": 1}}},
    }
    entries = _read([{"document": "sim", "pointer": "/spec/a\x1b[2Jb/zz", "source": "default"}])
    with pytest.raises(FluxError) as raised:
        _register(documents, entries, "sim")
    assert "\x1b" not in raised.value.detail
    assert "has no key 'zz' (it has n)" in raised.value.detail


def test_the_band_of_a_flux_side_rule_is_counted_and_a_register_entry_can_name_it() -> None:
    rules = [
        {"id": "a", "mustBeLessOrEqualTo": 5, "unit": "rows"},
        {"id": "b", "mustBeBetween": [1, "2.5"], "unit": "percent"},
        {"id": "c", "derive": "x", "expr": "EXTRACT(DAY FROM d)"},
    ]
    documents = {
        "sim": {
            "kind": "Simulation",
            "spec": {},
            "extensions": {EXTENSION_NAMESPACE: {"emits": {"e": {"rules": rules}}}},
        }
    }
    numbers = declared_numbers(documents, simulation_id="sim", namespace=EXTENSION_NAMESPACE)
    found = {number.pointer[len(f"{BLOCK}/emits/e/rules/") :]: number.value for number in numbers}
    assert found == {
        "0/mustBeLessOrEqualTo": "5",
        "1/mustBeBetween/0": "1",
        "1/mustBeBetween/1": "2.5",
    }
    entry = {
        "document": "sim",
        "pointer": f"{BLOCK}/emits/e/rules/0/mustBeLessOrEqualTo",
        "source": "assumption",
        "reason": "The most rows the owner accepts.",
    }
    register = _register(documents, _read([entry]), "sim")
    assert register.counts()["assumption"] == 1


def _given_document(depth: int, key: str = "levelWeightsGiven") -> Dict[str, Any]:
    """A Simulation whose one column is drawn given *depth* columns, its table nested as many deep."""
    table: Any = {"a": "1", "b": "3"} if key == "levelWeightsGiven" else ["1", "3"]
    for _ in range(depth):
        table = {"x": table, "y": copy.deepcopy(table)}
    return {
        "kind": "Simulation",
        "spec": {},
        "extensions": {
            EXTENSION_NAMESPACE: {
                "emits": {"e": {"fidelity": {"columns": {"c": {key: table}}}}},
            }
        },
    }


@pytest.mark.parametrize("depth", [1, 2, 3, module.MOST_GIVEN])
@pytest.mark.parametrize("key", ["levelWeightsGiven", "binWeightsGiven"])
def test_every_weight_of_a_table_nested_under_several_given_columns_is_a_counted_number(
    depth: int, key: str
) -> None:
    numbers = declared_numbers(
        {"sim": _given_document(depth, key)}, simulation_id="sim", namespace=EXTENSION_NAMESPACE
    )
    assert len(numbers) == 2 * 2**depth


def test_a_given_may_list_at_most_the_columns_the_register_counts_weights_for() -> None:
    from flux_engine.generator.statistics import statistics_for
    from tests.generator.conftest import expose_from_schema, fidelity

    column = expose_from_schema([{"name": "c", "type": "STRING"}]).columns[0]
    names = [f"p{i}" for i in range(module.MOST_GIVEN + 1)]
    with pytest.raises(FluxError, match="at most 8 columns"):
        statistics_for(column, fidelity({"c": {"given": names, "levels": {"a": "1"}}}))
