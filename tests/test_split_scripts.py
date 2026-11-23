# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The two scripts behind the sharded CI jobs: the partition check and the durations merge.

``scripts/check_split_partition.py`` is the guard that no shard drops or repeats a test, so it has to
fail on exactly those cases. ``scripts/merge_durations.py`` joins the shards' timing files into the
one file the next run balances by.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


partition = _load("check_split_partition")
merger = _load("merge_durations")

pytestmark = pytest.mark.unit


def _listing(path: Path, nodes: list[str]) -> None:
    path.write_text("\n".join(nodes + ["", f"{len(nodes)} tests collected"]) + "\n", "utf-8")


def _split(scratch: Path, groups: list[list[str]], *, whole: list[str]) -> None:
    _listing(scratch / "all.txt", whole)
    for number, group in enumerate(groups, start=1):
        _listing(scratch / f"g-{len(groups)}-{number}.txt", group)


NODES = ["a.py::t1", "a.py::t2", "b.py::t3", "c.py::t4[x]"]


def test_an_exact_split_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _split(tmp_path, [NODES[:1], NODES[1:]], whole=NODES)
    assert partition.main([str(tmp_path), "all", "g", "2"]) == 0
    assert "partitioned exactly 2 ways: 4 tests" in capsys.readouterr().out


def test_a_test_in_two_groups_fails(tmp_path: Path) -> None:
    _split(tmp_path, [NODES[:2], NODES[1:]], whole=NODES)
    with pytest.raises(AssertionError, match="more than one group.*a.py::t2"):
        partition.main([str(tmp_path), "all", "g", "2"])


def test_a_dropped_test_fails(tmp_path: Path) -> None:
    _split(tmp_path, [NODES[:1], NODES[2:]], whole=NODES)
    with pytest.raises(AssertionError, match="missing.*a.py::t2"):
        partition.main([str(tmp_path), "all", "g", "2"])


def test_an_extra_test_and_an_empty_group_fail(tmp_path: Path) -> None:
    _split(tmp_path, [NODES[:2], NODES[2:] + ["z.py::t9"]], whole=NODES)
    with pytest.raises(AssertionError, match="extra.*z.py::t9"):
        partition.main([str(tmp_path), "all", "g", "2"])
    _split(tmp_path, [NODES, []], whole=NODES)
    with pytest.raises(AssertionError, match="empty group"):
        partition.main([str(tmp_path), "all", "g", "2"])


def test_every_listed_split_is_checked(tmp_path: Path) -> None:
    _split(tmp_path, [NODES[:2], NODES[2:]], whole=NODES)
    _split(tmp_path, [NODES[:1], NODES[1:2], NODES[2:3]], whole=NODES)  # three ways, one test short
    with pytest.raises(AssertionError, match="3-way split: missing"):
        partition.main([str(tmp_path), "all", "g", "2", "3"])


def _durations(path: Path, durations: dict[str, float]) -> Path:
    path.write_text(json.dumps(durations), "utf-8")
    return path


def test_shard_files_merge_sorted(tmp_path: Path) -> None:
    one = _durations(tmp_path / "1.json", {"b.py::t": 2.0, "a.py::t": 1.0})
    two = _durations(tmp_path / "2.json", {"c.py::t": 3})
    out = tmp_path / "out" / "merged.json"
    assert merger.main([str(out), str(one), str(two)]) == 0
    merged = json.loads(out.read_text("utf-8"))
    assert list(merged) == ["a.py::t", "b.py::t", "c.py::t"]
    assert merged["c.py::t"] == 3.0


def test_a_test_timed_by_two_shards_is_refused(tmp_path: Path) -> None:
    one = _durations(tmp_path / "1.json", {"a.py::t": 1.0})
    two = _durations(tmp_path / "2.json", {"a.py::t": 2.0})
    with pytest.raises(ValueError, match="more than one shard"):
        merger.merge([one, two])


@pytest.mark.parametrize("bad", [{}, {"a.py::t": -1}, {"a.py::t": "1"}, {"a.py::t": True}, []])
def test_an_unusable_shard_file_is_refused(tmp_path: Path, bad: object) -> None:
    path = tmp_path / "1.json"
    path.write_text(json.dumps(bad), "utf-8")
    with pytest.raises(ValueError):
        merger.merge([path])
