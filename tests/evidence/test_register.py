# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``parameter-register.json`` and what the reports say of it.

Pure functions over a register built from the starter's own documents. The
record through a real run is in ``tests/stages/test_parameter_register.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from jsonschema import Draft202012Validator

from flux_engine.api.verdict import Reproducibility
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.evidence.register import (
    REGISTER_DIGEST_KEY,
    REGISTER_FILE,
    REGISTER_SCHEMA_URL,
    Assumption,
    RegisterSummary,
    build_register_document,
    parse_register,
    register_digest,
    render_register,
    render_register_line,
    render_register_note,
)
from flux_engine.evidence.report import render_evidence_report
from flux_engine.runtime import resolve_cast
from flux_engine.seam.parameters import build_parameter_register, read_parameters_block
from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105, load_bundle
from flux_engine.stages.layout import RunLayout

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
STARTER = REPO_ROOT / "flux_engine" / "starter" / "bundle"
SPEC = REPO_ROOT / "docs" / "spec"
SCHEMA = json.loads((SPEC / "parameter-register-1.schema.json").read_text(encoding="utf-8"))
ENTRIES: List[Dict[str, Any]] = [
    {
        "document": "q3-retention-world",
        "pointer": "/spec/population/size",
        "source": "assumption",
        "reason": "Sized for a quick look.",
    },
    {
        "document": "persona-pragmatic-family",
        "pointer": "/spec/traits",
        "source": "assumption",
        "reason": "The growth team's persona workshop.",
    },
    {
        "document": "persona-pragmatic-family",
        "pointer": "/spec/traits/monthlySpend/min",
        "source": "default",
    },
]
DIGEST = "sha256:" + "a" * 64


def _register(entries: List[Dict[str, Any]] = ENTRIES, profile: Any = None) -> Any:
    loaded = load_bundle(STARTER)
    cast = resolve_cast(loaded.flux_documents)
    return build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=dict(cast.documents),
        namespace=EXTENSION_NAMESPACE,
        entries=read_parameters_block(
            entries, pinned=profile is not None, where="sim", code=FLX_3105
        ),
        profile_digest=profile,
        code=FLX_3105,
    )


def _summary(**changes: Any) -> RegisterSummary:
    base: Dict[str, Any] = {
        "simulation_id": "sim",
        "profile_digest": None,
        "declared": 20,
        "counts": {"profile": 0, "assumption": 8, "default": 12},
        "assumptions": (),
        "stated_defaults": 0,
    }
    base.update(changes)
    return RegisterSummary(**base)


def test_the_record_lists_every_number_with_its_source_and_entry() -> None:
    document = build_register_document(_register())
    assert document["_schemaURL"] == REGISTER_SCHEMA_URL
    assert document["declared"] == len(document["parameters"]) == 20
    assert document["counts"] == {"profile": 0, "assumption": 8, "default": 12}
    assert document["profile"] is None
    assert [entry["index"] for entry in document["entries"]] == [0, 1, 2]
    rows = {(row["document"], row["pointer"]): row for row in document["parameters"]}
    assert rows[("q3-retention-world", "/spec/population/size")] == {
        "document": "q3-retention-world",
        "kind": "World",
        "pointer": "/spec/population/size",
        "value": "500",
        "source": "assumption",
        "entry": 0,
    }
    assert rows[("persona-pragmatic-family", "/spec/traits/monthlySpend/min")]["entry"] == 2
    assert (
        rows[("jr-payment-recovery", next(p for d, p in rows if d == "jr-payment-recovery"))][
            "entry"
        ]
        is None
    )
    assert not list(Draft202012Validator(SCHEMA).iter_errors(document))


def test_the_record_names_the_pinned_profile_and_each_citation() -> None:
    entries = [
        {
            "document": "q3-retention-world",
            "pointer": "/spec/population/size",
            "source": "profile",
            "statistic": {"pointer": "/tables/subscriber/rowCount"},
        }
    ]
    document = build_register_document(_register(entries, profile=DIGEST))
    assert document["profile"] == {"digest": DIGEST}
    assert document["entries"][0]["statistic"] == {"pointer": "/tables/subscriber/rowCount"}
    assert document["counts"]["profile"] == 1
    assert not list(Draft202012Validator(SCHEMA).iter_errors(document))


def test_the_text_is_canonical_and_its_digest_is_of_the_exact_bytes() -> None:
    text = render_register(_register())
    assert text.endswith("\n") and json.loads(text) == build_register_document(_register())
    assert text == render_register(_register())  # a function of the bundle alone
    data = text.encode("utf-8")
    digest = register_digest(data)
    assert digest.value == "sha256:" + hashlib.sha256(data).hexdigest()
    assert register_digest(data + b" ").value != digest.value
    assert REGISTER_DIGEST_KEY == "parameterRegisterDigest"
    assert (
        RunLayout.for_state_dir(Path("s"), "0" * 16).parameter_register_path.name == REGISTER_FILE
    )


def test_the_record_is_read_back_as_its_shares_and_assumptions() -> None:
    summary = parse_register(render_register(_register()))
    assert (summary.declared, dict(summary.counts)) == (
        20,
        {"profile": 0, "assumption": 8, "default": 12},
    )
    assert summary.assumptions == (
        Assumption("q3-retention-world", "/spec/population/size", "Sized for a quick look.", 1),
        Assumption(
            "persona-pragmatic-family", "/spec/traits", "The growth team's persona workshop.", 7
        ),
    )
    assert summary.stated_defaults == 1
    assert summary.as_document() == {
        "declared": 20,
        "counts": {"profile": 0, "assumption": 8, "default": 12},
    }


def _broken(change: Any) -> str:
    document = build_register_document(_register())
    change(document)
    return json.dumps(document)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update({"_schemaURL": "https://example.com/other"}),
        lambda d: d["counts"].pop("default"),
        lambda d: d["counts"].update({"default": 11}),
        lambda d: d.update({"declared": 19}),
        lambda d: d["parameters"].pop(),
        lambda d: d["parameters"][0].update({"source": "assumption"}),
        lambda d: d["parameters"][0].update({"entry": 9}),
        lambda d: d["entries"][1].update({"index": 7}),
        lambda d: d["entries"][1].update({"source": "fitted"}),
        lambda d: d["counts"].update({"profile": True}),
        lambda d: d["parameters"][0].update({"entry": [1]}),
        lambda d: d["parameters"][0].update({"entry": {"index": 1}}),
        lambda d: d["parameters"][0].update({"entry": True}),
        lambda d: d["entries"][1].update({"index": True}),
        lambda d: d.update({"profile": {"digest": "sha256:`x`"}}),
        lambda d: d.update({"profile": {}}),
    ],
)
def test_a_record_this_build_did_not_write_is_refused(change: Any) -> None:
    with pytest.raises(ValueError):
        parse_register(_broken(change))


def test_the_line_states_the_shares() -> None:
    assert (
        render_register_line(_summary())
        == "parameters: 20 declared -- 0 from a profile, 8 assumed, 12 default"
    )
    assert render_register_line(_summary(profile_digest=DIGEST)).startswith(
        "parameters: 20 declared -- 0 from the pinned profile,"
    )
    assert (
        render_register_line(
            _summary(declared=0, counts={"profile": 0, "assumption": 0, "default": 0})
        )
        == "parameters: the run reads no number the register counts"
    )


def test_the_note_states_the_shares_and_lists_each_assumption() -> None:
    note = render_register_note(parse_register(render_register(_register())))
    assert note.startswith(
        "Parameter register: the register counts 20 numbers of this run: the numbers under "
        "`spec` in the documents the run reads, and the numbers the Simulation's "
        "`com.agentics.flux/1` namespace holds at the positions the specification names (the "
        "fidelity block, the rows and the rules). It does not count seeds, a sweep, the "
        "fidelity block's `encoding` and `budget`, or the namespace's own declarations of how "
        "rows relate and fall in time (such as `links`, `history` and `within`), so the run "
        "reads numbers this report does not show. 0 (0 %) come from a profile "
        "(the Simulation pins none), 8 (40 %) are assumptions and 12 (60 %) are defaults, which "
        "no measurement or assumption supports (1 stated as a default by an entry, 11 named "
        "by no entry). The register (`run/parameter-register.json`) records what each number "
        "rests on. It does not show that any number is right, and `ship` compared no number "
        "with the statistic it cites.\n\nThe assumptions:\n\n"
    )
    assert (
        "The assumptions:\n\n"
        "- `q3-retention-world` `/spec/population/size` (1 number): `Sized for a quick look.`\n"
        "- `persona-pragmatic-family` `/spec/traits` (7 numbers): "
        "`The growth team's persona workshop.`"
    ) in note
    assert note.count("\n\n") == 2  # one paragraph, then the list of assumptions


def test_without_assumptions_the_note_is_one_paragraph() -> None:
    note = render_register_note(
        _summary(counts={"profile": 0, "assumption": 0, "default": 20}, stated_defaults=0)
    )
    assert "\n" not in note
    assert "are defaults, which no measurement or assumption supports (no entry names them)." in (
        note
    )
    one = render_register_note(
        _summary(declared=1, counts={"profile": 0, "assumption": 0, "default": 1})
    )
    assert "1 (100 %) are defaults, which no measurement or assumption supports (no entry " in one
    assert "names it)." in one


@pytest.mark.parametrize(
    ("counts", "shown"),
    [
        ({"profile": 1, "assumption": 1, "default": 1}, "1 (33.3 %)"),
        ({"profile": 1, "assumption": 0, "default": 15}, "1 (6.2 %)"),  # 6.25 to even
        ({"profile": 3, "assumption": 0, "default": 13}, "3 (18.8 %)"),  # 18.75 to even
        ({"profile": 1, "assumption": 1, "default": 2}, "1 (25 %)"),
        ({"profile": 1, "assumption": 0, "default": 4999}, "1 (below 0.1 %)"),
        ({"profile": 4999, "assumption": 0, "default": 1}, "4999 (above 99.9 %)"),
        ({"profile": 1, "assumption": 0, "default": 1999}, "1 (below 0.1 %)"),  # 0.05 to even
        ({"profile": 3, "assumption": 0, "default": 1997}, "3 (0.2 %)"),  # 0.15 to even
    ],
)
def test_a_share_is_exact_when_whole_and_else_rounded_half_to_even(
    counts: Dict[str, int], shown: str
) -> None:
    note = render_register_note(_summary(declared=sum(counts.values()), counts=counts))
    assert f"this report does not show. {shown} come from" in note


def test_the_note_names_the_pinned_profile() -> None:
    note = render_register_note(_summary(profile_digest=DIGEST))
    assert f"come from statistics of the pinned profile `{DIGEST}`" in note


def test_a_reason_is_a_code_span_or_withheld() -> None:
    def note_for(reason: str) -> str:
        assumption = Assumption("q3-retention-world", "/spec/population/size", reason, 1)
        return render_register_note(_summary(assumptions=(assumption,)))

    withheld = "a reason this report does not print (it is in the register file)"
    assert withheld in note_for("It proves nothing")  # a reserved word
    assert withheld in note_for("a \u202e reversed")  # not plain text
    assert withheld in note_for("ends a `code` span")  # a backtick
    # In a code span nothing is markup, and no renderer makes a link of a URL.
    linked = note_for("See [this](https://example.com), www.example.com and a@example.com")
    assert "(1 number): `See [this](https://example.com), www.example.com and a@example.com`" in (
        linked
    )
    # A name the report cannot print is withheld too, never quoted raw.
    assumption = Assumption("doc`x", "/spec/a\nb", "fine", 2)
    note = render_register_note(_summary(assumptions=(assumption,)))
    assert "- a document at a pointer this report does not print (2 numbers)" in note


def test_a_register_with_no_number_states_no_share() -> None:
    note = render_register_note(
        _summary(declared=0, counts={"profile": 0, "assumption": 0, "default": 0})
    )
    assert note == (
        "Parameter register: this run reads no number the register counts, so it states no "
        "share."
    )


def _report(**kwargs: Any) -> str:
    return render_evidence_report(
        bundle_name="demo",
        kernel_version="0.1.0",
        run_digest=DigestRef(value=DIGEST, domain=DigestDomain.JCS_DOCUMENT, subject="runDigest"),
        generated_at="2026-11-17T00:00:00+00:00",
        conformance_summary="Conforms.",
        reproducibility=Reproducibility.NOT_CHECKED,
        **kwargs,
    )


def test_the_report_adds_the_note_under_what_was_not_proven_and_nothing_without_one() -> None:
    plain = _report()
    assert "Parameter register" not in plain
    noted = _report(register_note="Parameter register: a note.", scorecard_note="Scores.")
    tail = noted.split("## What Was Not Proven\n\n", 1)[1]
    assert tail.endswith("Parameter register: a note.\n\nScores.\n")
    assert noted.replace("\n\nParameter register: a note.", "") == _report(scorecard_note="Scores.")


def test_the_example_record_beside_the_spec_validates_and_reads_back() -> None:
    text = (SPEC / "parameter-register-example.json").read_text(encoding="utf-8")
    document = json.loads(text)
    assert not list(Draft202012Validator(SCHEMA).iter_errors(document))
    Draft202012Validator.check_schema(SCHEMA)
    summary = parse_register(text)
    assert summary.declared == 35
    assert dict(summary.counts) == {"profile": 3, "assumption": 2, "default": 30}
