# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy of the ``outcomes`` fixture with one declaration changed, and the runs the tests make."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml

from flux_engine.stages import pipeline

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "outcomes"
BUNDLE = FIXTURE / "bundle"
PROFILE = FIXTURE / "subscriptions.profile.json"
NAMESPACE = "com.agentics.flux/1"
SIMULATION = "sim-telco.silver.subscriptions"
JOURNEY = "jr-renewal"

Edit = Callable[[Dict[str, Any]], None]


def namespace(document: Dict[str, Any]) -> Dict[str, Any]:
    found: Dict[str, Any] = document["extensions"][NAMESPACE]
    return found


def without_responses(document: Dict[str, Any]) -> None:
    """Drop both keys, which leaves a Simulation that declares no family and no forecast."""
    declared = namespace(document)
    declared.pop("responses", None)
    declared.pop("forecasts", None)


def make_bundle(
    tmp_path: Path,
    name: str,
    *,
    simulation: Optional[Edit] = None,
    documents: Optional[Dict[str, Edit]] = None,
    add: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Path:
    """A copy of the fixture bundle with *simulation* and *documents* edited and *add* written."""
    bundle = tmp_path / name
    shutil.copytree(BUNDLE, bundle)
    for filename, edit in {"simulation.flux.yml": simulation, **(documents or {})}.items():
        if edit is None:
            continue
        path = bundle / filename
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        edit(document)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    for filename, document in (add or {}).items():
        (bundle / filename).write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def execute(bundle: Path = BUNDLE, **options: Any) -> pipeline.PipelineResult:
    """One execution of *bundle* against the fixture's profile, unless *profile* is given."""
    options.setdefault("profile", PROFILE)
    return pipeline.execute(bundle, **options)


def profile_document() -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads(PROFILE.read_text(encoding="utf-8"))
    return document


def next_state(
    tmp_path: Path, name: str = "next-state", *, cells: Optional[Dict[Any, Any]] = None
) -> Path:
    """A later release of the fixture's profile: ``cells`` maps ``(query, group)`` to new counts.

    A value of ``None`` withholds the cell (status ``S``). Any other value is the ``[false, true]``
    level counts, and ``k`` becomes their sum. With no *cells* one cell no forecast reads is moved
    a little, so the state is another release (another digest) that leaves every observation the
    forecasts read as the fixture's; ``cells={}`` is the fixture's own document.
    """
    document = profile_document()
    if cells is None:
        cells = {("renewed-not-offered", "standard"): (31, 59)}
    for (query, group), counts in cells.items():
        entry = next(item for item in document["queries"] if item["name"] == query)
        cell = next(g for g in entry["statistics"][0]["groups"] if g["group"] == group)
        if counts is None:
            cell.clear()
            cell.update({"group": group, "status": "S"})
        else:
            cell["levelCounts"] = list(counts)
            cell["k"] = sum(counts)
    path = tmp_path / f"{name}.profile.json"
    path.write_text(json.dumps(document, indent=1, sort_keys=True), encoding="utf-8")
    return path


def edited_profile(
    edit: Optional[Callable[[Dict[str, Any]], None]] = None,
    *,
    cells: Optional[Dict[Any, Any]] = None,
) -> Any:
    """The fixture's profile, read after *edit* and the query *cells* of :func:`next_state`.

    Returns the :class:`flux_stats.profile.Profile` (no digest, so nothing pins it).
    """
    from flux_stats.profile import load_profile_text

    document = profile_document()
    for (query, group), counts in (cells or {}).items():
        entry = next(item for item in document["queries"] if item["name"] == query)
        cell = next(g for g in entry["statistics"][0]["groups"] if g["group"] == group)
        if counts is None:
            cell.clear()
            cell.update({"group": group, "status": "S"})
        else:
            cell["levelCounts"] = list(counts)
            cell["k"] = sum(counts)
    if edit is not None:
        edit(document)
    return load_profile_text(json.dumps(document))[0]
