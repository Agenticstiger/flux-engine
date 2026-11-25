# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A file sink writes what it wrote before a delivery handed every sink the new context (ADR 0038).

It needs no Kafka library, so it runs with the main suite and not among the tests of
``make kafka-extra``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, List, Mapping

import pytest

from flux_engine.sinks.receipts import StreamContext
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.run import run

from .test_deliver_privacy_grade import PROFILE, _bundle

pytestmark = pytest.mark.integration


class _Stripped:
    """A target that hands its sink the context as it was before the new fields."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name

    def sink_for(self, context: StreamContext) -> Any:
        return self.inner.sink_for(
            dataclasses.replace(
                context,
                privacy_grade=None,
                bundle_directory=None,
                simulation_id=None,
                delivered=(),
            )
        )


def test_a_file_sink_writes_the_same_files_and_receipt_with_or_without_the_new_fields(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pyarrow")
    from flux_engine.sinks.registry import open_target
    from flux_engine.stages.deliver import deliver_run

    bundle, state = _bundle(tmp_path), tmp_path / "state"
    result = run(bundle, state_dir=state, profile=PROFILE)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(state, result.artifacts["plan"]["run_id"])

    def receipts(directory: Path, strip: bool) -> List[Mapping[str, Any]]:
        target: Any = open_target(f"parquet:{directory}")
        if strip:
            target = _Stripped(target)
        delivered = deliver_run(layout, bundle, target=target)
        files = {
            str(path.relative_to(directory)): path.read_bytes()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        }
        documents = [
            {key: value for key, value in item.as_document().items() if key != "target"}
            for item in delivered
        ]
        return [{"receipts": documents, "files": files}]

    with_fields = receipts(tmp_path / "a", strip=False)
    without = receipts(tmp_path / "b", strip=True)
    assert with_fields == without
    assert with_fields[0]["files"], "the sink wrote files"
