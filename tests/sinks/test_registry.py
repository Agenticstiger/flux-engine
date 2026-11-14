# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The sink registry and the string-option factories behind ``--sink``/``--sink-opt``."""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import EntryPoint
from typing import Any, List, Mapping

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks import registry
from flux_engine.sinks._options import bool_option, int_option, require_known
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

from .conftest import pyarrow_or_skip

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Names and specs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("parquet:out/", ("parquet", "out/")),
        ("parquet", ("parquet", "")),
        ("adbc:duckdb", ("adbc", "duckdb")),
        ("parquet:C:\\data\\out", ("parquet", "C:\\data\\out")),
        ("arrow-ipc:/a:b", ("arrow-ipc", "/a:b")),
    ],
)
def test_a_spec_splits_at_its_first_colon(spec: str, expected: Any) -> None:
    assert registry.parse_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", ":out", "_hidden:x", "9lives:x", "my sink:x"])
def test_a_spec_without_a_sink_name_is_refused(spec: str) -> None:
    with pytest.raises(FluxError) as caught:
        registry.parse_spec(spec)
    assert caught.value.code.code == "FLX-3507"
    assert "parquet" in str(caught.value)


def test_an_unknown_sink_lists_the_known_ones() -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target("kafka:topic")
    assert caught.value.code.code == "FLX-3507"
    assert "adbc, arrow-ipc, parquet" in str(caught.value)


def test_the_built_ins_are_listed_with_their_extras() -> None:
    sinks = {sink.name: sink for sink in registry.available()}
    assert sinks["parquet"].extra == "arrow" and sinks["parquet"].source == "built-in"
    assert sinks["arrow-ipc"].extra == "arrow"
    assert sinks["adbc"].extra == "adbc"
    assert sinks["parquet"].as_document()["extra"] == "flux-sim-engine[arrow]"


def test_listing_the_sinks_imports_no_sink_library() -> None:
    probe = (
        "import sys\n"
        "from flux_engine.sinks.registry import available\n"
        "names = [sink.name for sink in available()]\n"
        "print(names, [m for m in ('pyarrow', 'adbc_driver_manager') if m in sys.modules])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "['adbc', 'arrow-ipc', 'parquet'] []"


# ---------------------------------------------------------------------------
# Third-party sinks through the entry-point group
# ---------------------------------------------------------------------------


class _MemorySink:
    """A third-party sink as the registry sees one: EmitSink plus a receipt."""

    def __init__(self, store: List[Any], context: StreamContext) -> None:
        self.store, self.context, self.receipt = store, context, None
        self.rows: List[Any] = []

    def open(self, header: Any) -> None:
        self.header = header

    def write(self, row: Any) -> None:
        self.rows.append(list(row))

    def close(self) -> None:
        if len(self.rows) == self.context.rows:
            self.store.extend(self.rows)
            self.receipt = SinkReceipt(
                sink="memory",
                run_id=self.context.run_id,
                expose_id=self.context.expose_id,
                output_digest=self.context.output_digest.value,
                rows=len(self.rows),
                target="memory",
            )


class MemoryTarget:
    name = "memory"
    store: List[Any] = []

    def __init__(self, target: str, options: Mapping[str, str]) -> None:
        require_known("memory", options, ("label",))
        self.target, self.options = target, dict(options)

    def sink_for(self, context: StreamContext) -> _MemorySink:
        return _MemorySink(self.store, context)


def memory_factory(target: str, options: Mapping[str, str]) -> MemoryTarget:
    return MemoryTarget(target, options)


def not_a_factory(target: str, options: Mapping[str, str]) -> object:
    return object()


def _points(*pairs: Any) -> Any:
    return lambda: tuple(
        EntryPoint(name=name, value=value, group=registry.ENTRY_POINT_GROUP)
        for name, value in pairs
    )


def test_a_third_party_sink_is_found_through_its_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry, "_entry_points", _points(("memory", f"{__name__}:memory_factory"))
    )
    target = registry.open_target("memory:here", {"label": "x"})
    assert isinstance(target, MemoryTarget)
    assert target.target == "here" and target.options == {"label": "x"}
    listed = {sink.name: sink for sink in registry.available()}["memory"]
    assert listed.source == "entry point" and not listed.shadowed
    assert listed.value == f"{__name__}:memory_factory"


def test_a_third_party_sink_cannot_shadow_a_built_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry, "_entry_points", _points(("parquet", f"{__name__}:memory_factory"))
    )
    shadow = [sink for sink in registry.available() if sink.source != "built-in"]
    assert [(sink.name, sink.shadowed) for sink in shadow] == [("parquet", True)]
    pyarrow_or_skip()  # the next line loads the Parquet sink, which imports it
    assert registry.load_factory("parquet").__module__ == "flux_engine.sinks.parquet"


def test_a_factory_that_returns_no_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_entry_points", _points(("broken", f"{__name__}:not_a_factory")))
    with pytest.raises(FluxError) as caught:
        registry.open_target("broken:x")
    assert caught.value.code.code == "FLX-3504"


# ---------------------------------------------------------------------------
# Option parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, value", [("12", 12), ("1_000", 1000), (" 7 ", 7)])
def test_integer_options(raw: str, value: int) -> None:
    assert int_option({"n": raw}, "n", None) == value


@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "ten", "", "²", "1__0", "_1", "١٢"])
def test_bad_integer_options_name_the_option(raw: str) -> None:
    with pytest.raises(FluxError, match="n must be a positive integer"):
        int_option({"n": raw}, "n", None)


@pytest.mark.parametrize(
    "raw, value", [("true", True), ("YES", True), ("on", True), ("0", False), ("no", False)]
)
def test_boolean_options(raw: str, value: bool) -> None:
    assert bool_option({"v": raw}, "v", not value) is value


def test_a_bad_boolean_option_names_the_option() -> None:
    with pytest.raises(FluxError, match="v must be true or false"):
        bool_option({"v": "maybe"}, "v", False)


def test_an_entry_point_named_with_capitals_or_underscores_can_be_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry, "_entry_points", _points(("My_Sink.v2", f"{__name__}:memory_factory"))
    )
    target = registry.open_target("My_Sink.v2:here")
    assert isinstance(target, MemoryTarget) and target.target == "here"
