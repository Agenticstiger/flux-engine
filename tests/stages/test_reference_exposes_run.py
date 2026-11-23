# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A whole run whose catalogue is the rows its profile publishes (ADR 0023 section 2).

``tests/fixtures/related-star`` is copied and its contract and emits are replaced by a ``product``
catalogue (``reference: true``) and a ``line`` table that points at it. The profile is written
beside the bundle and pinned by its digest. The tests run the pipeline every command uses, and then
``plan``, ``apply``, ``verify`` and ``ship``:

* the catalogue's output is its published rows, on every seed, and the lines pick only those rows;
* ``apply --run-id`` and ``verify`` need no profile: the plan holds the rows it was made from;
* every declaration that cannot hold is refused before a row exists, with the code the ADR names;
* ``EVIDENCE.md`` lists the catalogue as released whole, and a run with none is unchanged.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify as run_verify
from flux_stats.profile import SCHEMA_URL_1_3_0
from tests.bundles import repin_contract

from .conftest import REPO_ROOT

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "related-star"
SPEC = REPO_ROOT / "docs" / "spec"
_CONTRACT = "store-star.fluid.yml"
_SIMULATION = "simulation.flux.yml"
LINES = 400

_BINDING = (
    "    binding:\n      platform: aws\n      format: glue_table\n"
    "      location: {{ database: retail, table: {name}, region: eu-west-1 }}\n"
)


def _contract_text(*, sensitivity: str = "") -> str:
    return f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: retail.store_star
name: Catalogue
metadata:
  owner:
    team: growth-lab
exposes:
  - exposeId: product
    kind: table
{_BINDING.format(name="product")}    contract:
      schema:
        - {{ name: sku, type: STRING, required: true }}
        - {{ name: pname, type: STRING, required: true{sensitivity} }}
        - {{ name: price, type: "DECIMAL(10,2)", required: true }}
        - {{ name: kind, type: STRING, required: false }}
    semantics:
      entities:
        - {{ name: product, type: primary, expr: sku }}
  - exposeId: line
    kind: table
{_BINDING.format(name="line")}    contract:
      schema:
        - {{ name: l_id, type: STRING, required: true }}
        - {{ name: l_sku, type: STRING, required: false }}
        - {{ name: l_qty, type: INT, required: false }}
    semantics:
      entities:
        - {{ name: line, type: primary, expr: l_id }}
      relationships:
        - name: product
          to: product
          fromColumns: [l_sku]
          toColumns: [sku]
          cardinality: many_to_one
"""


PRODUCTS: Dict[str, Any] = {
    "disclosure": "none",
    "columns": ["sku", "pname", "price", "kind"],
    "rows": [
        ["A-1", "Anvil", "12.5", "tool"],
        ["B-2", "Bolt", "0.25", None],
        ["C-3", "Crate", "30", "bulk"],
        ["D-4", "Dowel", "1.1", "tool"],
        ["E-5", "Eyelet", "0.05", None],
    ],
}


def _profile(references: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    document = json.loads((SPEC / "profile-1-2-example.json").read_text("utf-8"))
    document["_schemaURL"] = SCHEMA_URL_1_3_0
    document["disclosureRules"]["none"] = {}
    document["tables"]["line"] = {
        "rowCount": LINES,
        "columns": {
            "l_qty": {
                "domain": "number",
                "statistics": [
                    {
                        "definition": "mean/1",
                        "sampleSize": {"min": 1000, "max": 9999},
                        "disclosure": "acro-default",
                        "groups": [
                            {
                                "group": "",
                                "k": LINES,
                                "Sx": str(5 * (1 << 16) * LINES),
                                "status": "F",
                            }
                        ],
                    }
                ],
            }
        },
    }
    document["references"] = (
        {"product": copy.deepcopy(PRODUCTS)} if references is None else references
    )
    return document


def _emits(*, product: str, line_levels: str, lines: int) -> str:
    return (
        f"      product:\n{product}"
        "      line:\n"
        f"        rows: {lines}\n"
        "        fidelity:\n"
        "          encoding: { fractionalBits: 16, rounding: floor }\n"
        "          records: { min: 1, max: 1000000 }\n"
        "          columns:\n"
        "            l_qty: { range: { min: '1', max: '9' } }\n"
        f"{line_levels}"
        "          properties:\n"
        "            - property: mean\n"
        "              column: l_qty\n"
        "              tolerance: { type: absolute, value: '100' }\n"
        "          budget: { maxChecksPerState: 20, maxStateAge: P30D }\n"
        "          failureAction: withholdBundle\n"
    )


def _bundle(
    tmp_path: Path,
    *,
    name: str = "bundle",
    profile: Optional[Dict[str, Any]] = None,
    pin: bool = True,
    product: str = "        reference: true\n",
    line_levels: str = "",
    seed: int = 42,
    sensitivity: str = "",
    lines: int = LINES,
) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    (target / _CONTRACT).write_text(_contract_text(sensitivity=sensitivity), "utf-8")
    document = _profile() if profile is None else profile
    _profile_path(target).write_text(json.dumps(document), "utf-8")
    digest = digest_of_document(document, subject="profile").value
    text = (target / _SIMULATION).read_text("utf-8")
    head, _, _ = text.partition("    - productRef:")
    head = head.replace("seed: 42", f"seed: {seed}")
    emits = "".join(
        f"    - productRef: retail.store_star\n      exposeId: {expose}\n"
        '      fluidVersion: "0.7.7"\n'
        '      provenance: { contractDigest: "sha256:' + "0" * 64 + '" }\n'
        for expose in ("product", "line")
    )
    pinned = f"    profile:\n      digest: {digest}\n" if pin else ""
    extension = (
        "extensions:\n  com.agentics.flux/1:\n"
        + pinned
        + "    emits:\n"
        + _emits(product=product, line_levels=line_levels, lines=lines)
    )
    (target / _SIMULATION).write_text(head + emits + extension, "utf-8")
    repin_contract(target / _CONTRACT, target / _SIMULATION, "retail.store_star")
    return target


def _profile_path(bundle: Path) -> Path:
    """The profile sits beside the bundle: a bundle directory holds no file but its documents."""
    return bundle.parent / f"{bundle.name}.profile.json"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _bundle(tmp_path_factory.mktemp("reference-run"))


def _tables(outcome: pipeline.PipelineResult) -> Dict[str, List[Dict[str, Any]]]:
    return {
        e.resolved.expose_id: [
            dict(zip(list(e.expose.column_names), row, strict=True)) for row in e.rows
        ]
        for e in outcome.emits
    }


def _execute(bundle: Path, **kwargs: Any) -> pipeline.PipelineResult:
    return pipeline.execute(bundle, kernel=KERNEL_V2, profile=_profile_path(bundle), **kwargs)


@pytest.mark.integration
def test_the_catalogue_is_its_published_rows_on_every_seed_and_the_lines_pick_only_those(
    bundle: Path, tmp_path: Path
) -> None:
    first = _execute(bundle)
    tables = _tables(first)
    assert [row["sku"] for row in tables["product"]] == ["A-1", "B-2", "C-3", "D-4", "E-5"]
    assert [str(row["price"]) for row in tables["product"]] == [
        "12.50",
        "0.25",
        "30.00",
        "1.10",
        "0.05",
    ]
    assert [row["kind"] for row in tables["product"]] == ["tool", None, "bulk", "tool", None]
    assert {row["l_sku"] for row in tables["line"]} == {"A-1", "B-2", "C-3", "D-4", "E-5"}
    other = _execute(_bundle(tmp_path, seed=7, name="other"))
    assert first.emit("product").result.output_digest == other.emit("product").result.output_digest
    assert first.emit("line").result.output_digest != other.emit("line").result.output_digest
    assert first.references.keys() == {"product"}


@pytest.mark.integration
def test_a_declared_pick_over_the_catalogue_takes_the_row_of_the_drawn_level(
    tmp_path: Path,
) -> None:
    levels = "            l_sku: { levels: { 'A-1': '0.7', 'C-3': '0.3' } }\n"
    declared = _bundle(tmp_path, line_levels=levels)
    lines = _tables(_execute(declared))["line"]
    assert {row["l_sku"] for row in lines} == {"A-1", "C-3"}
    unknown = _bundle(
        tmp_path,
        name="unknown",
        line_levels="            l_sku: { levels: { 'A-1': '0.5', 'Z-9': '0.5' } }\n",
    )
    with pytest.raises(FluxError) as caught:
        _execute(unknown)
    assert caught.value.code.code == "FLX-3403"


@pytest.mark.integration
def test_plan_apply_and_verify_run_the_catalogue_and_check_it_equals_its_published_rows(
    bundle: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    profile = _profile_path(bundle)
    planned = plan(bundle, state_dir=state, profile=profile)
    assert planned.success, planned.stderr
    document = planned.artifacts["plan_document"]
    assert document["references"]["product"]["rows"][0] == ["A-1", "Anvil", "12.5", "tool"]
    run_id = str(planned.artifacts["run_id"])
    # No profile is handed to apply --run-id: the plan holds the rows it was made from.
    applied = apply(bundle, state_dir=state, run_id=run_id)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, run_id)
    verified = run_verify(
        bundle, run_id=layout.run_id, state_dir=state, allow_declared_baseline_drift=True
    )
    assert verified.success, verified.stderr
    (entry,) = verified.artifacts["rules"]["emits"]["product"]
    assert entry["rule"] == "published-rows" and entry["enforced"] is True
    assert (entry["satisfied"], entry["total"], entry["met"]) == (5, 5, True)
    assert "rules product: published-rows 5 of 5" in verified.stdout


@pytest.mark.integration
def test_an_edited_catalogue_stream_fails_verify(bundle: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, profile=_profile_path(bundle))
    run_id = str(planned.artifacts["run_id"])
    assert apply(bundle, state_dir=state, run_id=run_id).success
    layout = RunLayout.for_state_dir(state, run_id)
    emit = layout.emit_path("product")
    lines = emit.read_text("utf-8").split("\n")
    lines[1] = lines[1].replace("Bolt", "Bolts")
    emit.write_text("\n".join(lines), "utf-8")
    verified = run_verify(
        bundle, run_id=run_id, state_dir=state, allow_declared_baseline_drift=True
    )
    # The edited stream no longer reproduces the run digest, so verify says so first.
    assert not verified.success


@pytest.mark.integration
def test_evidence_lists_the_catalogue_as_released_whole_and_a_run_with_none_is_unchanged(
    bundle: Path, tmp_path: Path
) -> None:
    outcome = run(
        bundle,
        state_dir=tmp_path / "state",
        profile=_profile_path(bundle),
        verify=True,
        ship=True,
        out_dir=tmp_path / "out",
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    (evidence,) = (tmp_path / "state").glob("runs/*/EVIDENCE.md")
    text = evidence.read_text("utf-8")
    assert "Reference exposes, released whole and outside the guarantee above: `product`" in text
    from flux_engine.evidence.report import render_reference_block

    assert render_reference_block(()) == ""


@pytest.mark.parametrize(
    ("label", "mutate", "code", "needle"),
    [
        ("no-pin", {"pin": False}, "FLX-3105", "pins no profile"),
        (
            "not-published",
            {"profile": _profile({})},
            "FLX-3105",
            "publishes no rows",
        ),
        (
            "other-key",
            {"product": "        reference: true\n        rows: 5\n"},
            "FLX-3105",
            "no other key",
        ),
        ("not-true", {"product": "        reference: false\n"}, "FLX-3105", "reference is true"),
        (
            "labelled",
            {"sensitivity": ", sensitivity: internal"},
            "FLX-4005",
            "released whole",
        ),
    ],
)
@pytest.mark.integration
def test_every_refusal_is_the_one_the_adr_names_before_a_row_exists(
    tmp_path: Path, label: str, mutate: Dict[str, Any], code: str, needle: str
) -> None:
    refused = _bundle(tmp_path, name=label, **mutate)
    state = tmp_path / "state"
    result = plan(refused, state_dir=state, profile=_profile_path(refused))
    assert result.exit_code != 0
    assert result.error_code == code, result.stderr
    assert needle in result.stderr
    assert not list(state.glob("runs/*/plan.json"))


@pytest.mark.parametrize("cell", ["1" + "0" * 30, "1E+1000000"])
@pytest.mark.integration
def test_a_published_decimal_out_of_range_is_refused_with_its_code(
    tmp_path: Path, cell: str
) -> None:
    products = copy.deepcopy(PRODUCTS)
    products["rows"][0][2] = cell
    refused = _bundle(tmp_path, profile=_profile({"product": products}))
    result = plan(refused, state_dir=tmp_path / "state", profile=_profile_path(refused))
    assert result.exit_code != 0
    assert result.error_code == "FLX-3403", result.stderr
    assert "'price'" in result.stderr


@pytest.mark.integration
def test_a_pinned_profile_the_command_is_not_handed_is_refused(
    bundle: Path, tmp_path: Path
) -> None:
    result = plan(bundle, state_dir=tmp_path / "state")
    assert result.error_code == "FLX-3109", result.stderr


@pytest.mark.integration
def test_kernel_0_1_0_refuses_the_declaration_and_names_the_kernel(bundle: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(bundle, kernel=KERNEL_V1, profile=_profile_path(bundle))
    assert caught.value.code.code == "FLX-3105"
    assert "kernel 0.1.0" in str(caught.value) and "kernel 0.2.0" in str(caught.value)


@pytest.mark.integration
def test_validate_takes_the_bundle_and_refuses_a_reference_with_no_pin(tmp_path: Path) -> None:
    assert validate(_bundle(tmp_path)).exit_code == 0
    unpinned = validate(_bundle(tmp_path, name="unpinned", pin=False))
    assert unpinned.exit_code != 0 and "FLX-3105" in unpinned.stderr


@pytest.mark.slow
@pytest.mark.integration
def test_jobs_do_not_change_a_byte_of_the_lines_or_the_catalogue(tmp_path: Path) -> None:
    big = _bundle(tmp_path, lines=2 * parallel.MIN_ROWS_PER_JOB + 301)
    digests = []
    for jobs in (1, 4):
        outcome = _execute(big, jobs=jobs)
        digests.append({e.resolved.expose_id: e.result.output_digest.value for e in outcome.emits})
    assert digests[0] == digests[1]
