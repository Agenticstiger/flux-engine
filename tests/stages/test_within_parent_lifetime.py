# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A ``within`` entry that keeps a history's start inside its parent's life is ``parent.lifetime: true``.

ADR 0017, amendment of 2026-11-25. The ``history-fleet`` fixture gets a ``lifetimes`` entry on its
depot (``opened_on`` to ``closed_on``) and a ``within`` entry on the history's emit for the
relationship to the depot and the history's validity start. What is pinned:

* the spelling plans, and the run is the run the same bundle makes with ``parent.lifetime: true``:
  every version starts inside its depot's lifetime and a closed depot's entities end by its close;
* a ``within`` entry that is not that spelling keeps ``FLX-6203``: another relationship, a path of
  several hops, the validity end, the activation column, a history with no parent (whose message
  names ``parent.lifetime``), an entry that carries a draw of its own;
* a target with no ``lifetimes`` entry is ``FLX-3403``, as for ``parent.lifetime``;
* a within entry on a date the history does not set, and a bundle that does not use the spelling,
  are read as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify
from tests.bundles import edit_flux

from ._fleet import DEPOT, plan_refused, run_bundle
from ._fleet import HISTORY as EMIT
from ._fleet import rows as read_rows
from .test_history_parent_run import _bundle, _check_bound

pytestmark = pytest.mark.integration

SPELLING: Dict[str, Any] = {"emit": EMIT, "relationship": DEPOT, "date": "valid_from"}


def _spelled(tmp_path: Path, name: str = "bundle", **within: Any) -> Path:
    """The parent-lifetime bundle, with ``parent.lifetime`` written as a ``within`` entry instead."""
    bundle = _bundle(tmp_path / name)

    def edit(flux: Dict[str, Any]) -> None:
        flux["history"][0]["parent"].pop("lifetime")
        flux["within"] = [{**SPELLING, **within}]

    return edit_flux(bundle, edit)


def _edit(bundle: Path, edit: Any) -> Path:
    return edit_flux(bundle, edit)


def _refused_6203(bundle: Path, *fragments: str) -> None:
    result = validate(bundle)
    assert result.exit_code == 1 and result.error_code == "FLX-6203"
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code == "FLX-6203"
    for text in (result.stderr, str(caught.value)):
        for fragment in fragments:
            assert fragment in text


def test_the_spelling_plans_and_the_run_keeps_every_entity_to_its_depots_life(
    tmp_path: Path,
) -> None:
    bundle = _spelled(tmp_path)
    assert validate(bundle).exit_code == 0
    layout = run_bundle(bundle, tmp_path / "state")
    assert _check_bound(layout, None) > 0
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    assert "history:within_parent 60 of 60" in (verified.stdout + verified.stderr).replace(
        "\n", " "
    )


@pytest.mark.parametrize("end", [None, {"mode": "closeLast", "share": "0.1"}])
def test_the_spelling_draws_the_rows_parent_lifetime_draws(
    tmp_path: Path, end: Optional[Dict[str, Any]]
) -> None:
    direct = _bundle(tmp_path / "direct", end=end)
    spelled = edit_flux(
        _bundle(tmp_path / "spelled", end=end),
        lambda flux: (
            flux["history"][0]["parent"].pop("lifetime"),
            flux.update(within=[SPELLING]),
        ),
    )
    first = run_bundle(direct, tmp_path / "state-direct")
    second = run_bundle(spelled, tmp_path / "state-spelled")
    for expose in (EMIT, DEPOT):
        assert read_rows(first, expose) == read_rows(second, expose)
    assert first.emit_path(EMIT).read_bytes() == second.emit_path(EMIT).read_bytes()


def test_the_spelling_alongside_parent_lifetime_is_the_declaration_once(tmp_path: Path) -> None:
    direct = _bundle(tmp_path / "direct")
    both = edit_flux(_bundle(tmp_path / "both"), lambda flux: flux.update(within=[SPELLING]))
    first = run_bundle(direct, tmp_path / "state-direct")
    second = run_bundle(both, tmp_path / "state-both")
    assert first.emit_path(EMIT).read_bytes() == second.emit_path(EMIT).read_bytes()


def test_a_within_on_another_relationship_is_refused(tmp_path: Path) -> None:
    bundle = _spelled(tmp_path, relationship="region")
    _refused_6203(bundle, "column 'valid_from' has more than one source", "within entry")


def test_a_within_through_several_hops_is_refused(tmp_path: Path) -> None:
    bundle = _spelled(tmp_path, relationship="depot.region")
    _refused_6203(bundle, "column 'valid_from' has more than one source", "within entry")


def test_a_within_on_the_validity_end_is_refused(tmp_path: Path) -> None:
    bundle = _spelled(tmp_path, date="valid_to")
    _refused_6203(bundle, "column 'valid_to' has more than one source", "validity end")


def test_a_within_on_the_activation_column_is_refused(tmp_path: Path) -> None:
    bundle = _spelled(tmp_path, date="commissioned_on")
    edit_flux(bundle, lambda flux: flux["history"][0].update(activation="commissioned_on"))
    _refused_6203(bundle, "column 'commissioned_on' has more than one source", "activation")


@pytest.mark.parametrize("carried", [{"target": "0.5"}, {"exposure": True}])
def test_a_within_that_carries_a_draw_of_its_own_is_refused(
    tmp_path: Path, carried: Dict[str, Any]
) -> None:
    bundle = _spelled(tmp_path, **carried)
    result = validate(bundle)
    assert result.exit_code == 1
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V2)
    assert caught.value.code.code in {"FLX-6203", "FLX-3105", "FLX-3403"}


def test_a_history_with_no_parent_is_refused_naming_parent_lifetime(tmp_path: Path) -> None:
    bundle = _spelled(tmp_path)
    edit_flux(bundle, lambda flux: flux["history"][0].pop("parent"))
    _refused_6203(bundle, "column 'valid_from' has more than one source", "parent.lifetime")


def test_a_target_with_no_lifetimes_entry_is_refused_as_parent_lifetime_is(tmp_path: Path) -> None:
    bundle = _spelled(tmp_path)
    edit_flux(bundle, lambda flux: flux.pop("lifetimes"))
    planned = plan_refused(bundle, tmp_path / "state")
    assert "FLX-3403" in planned.stderr and "parent.lifetime" in planned.stderr


def test_a_within_on_a_date_the_history_does_not_set_still_plans(tmp_path: Path) -> None:
    bundle = _spelled(tmp_path, date="commissioned_on")
    assert validate(bundle).exit_code == 0
    pipeline.execute(bundle, kernel=KERNEL_V2)


def test_a_bundle_without_the_spelling_replays_its_digest(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    first = run_bundle(bundle, tmp_path / "one")
    second = run_bundle(bundle, tmp_path / "two")
    names: List[str] = [EMIT, DEPOT]
    for expose in names:
        assert first.emit_path(expose).read_bytes() == second.emit_path(expose).read_bytes()
