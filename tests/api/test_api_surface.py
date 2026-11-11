# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The extension contract's own gate: the public names, and four properties.

``flux_engine.api`` is the surface third-party implementations target, so a
name that disappears or changes shape breaks somebody outside this
repository. This module pins:

1. **The exact export list.** A snapshot, not a subset check -- an addition
   fails here too, so the API version and the docs are considered before a
   name ships rather than after.
2. **That every dataclass is frozen** and every plural field is immutable,
   which is what makes a record a record.
3. **That every Protocol is ``runtime_checkable``**, so an implementation
   can be checked at a boundary instead of being trusted.
4. **That ``VerifyVerdict`` cannot be collapsed to one answer.** No
   ``__bool__``, and no accessor that combines reproducibility with
   validity. This is the property the whole readout exists for: the engine
   can answer whether a run reproduces, and it cannot answer whether the
   world behaves as the scenario said. A convenience accessor added later
   fails this test.

The banned-vocabulary check at the end mirrors ``scripts/register_lint.py``
over this package's docstrings and ``docs/reference/python-api.md``, which
that script's docs sweep also covers -- two independent readings of the same
rule.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pkgutil
import re
import textwrap
from pathlib import Path
from typing import Any, Protocol

import pytest
from hypothesis import given
from hypothesis import strategies as st

import flux_engine.api as api
from flux_engine.api import stages as stages_module
from flux_engine.api.stages import EngineError, StageName, StageResult
from flux_engine.api.verdict import Reproducibility, Validity, VerifyVerdict

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The snapshot. Sorted, so a rebase that reorders __all__ is not a failure
# while a rename or a removal is.
EXPECTED_PUBLIC_NAMES = sorted(
    [
        "__api_version__",
        "AccuracyGate",
        "AccuracyOutcome",
        "AccuracyVerdict",
        "ApplyFailed",
        "Calibrator",
        "Claim",
        "ClaimBasis",
        "ClaimType",
        "Credibility",
        "DatasetRef",
        "DIGEST_DOMAINS",
        "DigestRef",
        "EmitHeader",
        "EmitSink",
        "EngineError",
        "ExpressionEvaluator",
        "ExpressionScope",
        "FidelityProver",
        "FidelityStatement",
        "FidelityVerifier",
        "LineageEmitter",
        "LineageEvent",
        "LineageEventType",
        "ModelPort",
        "ObservedReadout",
        "PlanFailed",
        "PredictionRecord",
        "ProofSystem",
        "Provenance",
        "ReplayFailed",
        "Reproducibility",
        "RunFailed",
        "Scorecard",
        "ShipFailed",
        "SkillInvocation",
        "SkillProposal",
        "SkillRunner",
        "StageName",
        "StageResult",
        "Validity",
        "ValidateFailed",
        "VerifyFailed",
        "VerifyVerdict",
    ]
)

# Names that would let a caller write ``if verdict:``-by-another-spelling --
# a single accessor standing in for two disjoint answers.
_COMBINING_NAMES = frozenset(
    {
        "__bool__",
        "ok",
        "passed",
        "success",
        "successful",
        "overall",
        "verdict",
        "is_ok",
        "is_valid",
        "valid",
        "verified",
        "all_good",
        "result",
        "score",
    }
)

_BANNED_WORDS = [
    "concordance",
    "zero-knowledge",
    "tolerance predicate",
    "clear-statistic",
    "practise",
    "mathematically",
    "proves",
    "legally admissible",
    "certified",
    "FLUX Simulator",
]
_BANNED_PATTERN = re.compile("|".join(re.escape(w) for w in _BANNED_WORDS), re.IGNORECASE)


def _api_members() -> dict[str, Any]:
    return {name: getattr(api, name) for name in api.__all__ if name != "__api_version__"}


def _protocols() -> dict[str, type]:
    return {
        name: obj
        for name, obj in _api_members().items()
        if inspect.isclass(obj)
        and issubclass(obj, Protocol)
        and getattr(obj, "_is_protocol", False)
    }


def _dataclasses() -> dict[str, type]:
    return {
        name: obj
        for name, obj in _api_members().items()
        if inspect.isclass(obj) and dataclasses.is_dataclass(obj)
    }


def _annotations_matching(pattern: str) -> list[str]:
    """Every dataclass field whose declared annotation matches *pattern*.

    Reads the raw annotation strings rather than resolved types: with
    ``from __future__ import annotations`` in force, and the digest type
    imported only under ``TYPE_CHECKING``, resolving them would need the
    kernel import this package exists to avoid.
    """
    compiled = re.compile(pattern)
    return [
        f"{name}.{f.name}: {f.type}"
        for name, obj in _dataclasses().items()
        for f in dataclasses.fields(obj)
        if compiled.search(str(f.type))
    ]


# ── 1. the snapshot ────────────────────────────────────────────────────


@pytest.mark.unit
def test_public_names_match_the_snapshot() -> None:
    assert sorted(api.__all__) == EXPECTED_PUBLIC_NAMES


@pytest.mark.unit
def test_every_exported_name_actually_resolves() -> None:
    missing = [name for name in api.__all__ if not hasattr(api, name)]
    assert missing == []


@pytest.mark.unit
def test_api_version_is_declared() -> None:
    assert api.__api_version__ == "0.1"


@pytest.mark.unit
def test_all_is_sorted_within_no_duplicates() -> None:
    assert len(api.__all__) == len(set(api.__all__))


@pytest.mark.unit
def test_every_submodule_export_is_re_exported_by_the_package() -> None:
    """No name is public in a submodule but absent from the package surface.

    A half-exported name is the one that gets imported from its submodule
    path by a consumer and then moved as though it were internal.
    """
    package_names = set(api.__all__)
    for module_info in pkgutil.iter_modules([str(Path(api.__file__).parent)]):
        module = __import__(f"flux_engine.api.{module_info.name}", fromlist=["__all__"])
        for name in getattr(module, "__all__", []):
            assert name in package_names, f"{module_info.name}.{name} is not re-exported"


# ── 2. frozen records ──────────────────────────────────────────────────


@pytest.mark.unit
def test_every_dataclass_is_frozen() -> None:
    not_frozen = [
        name
        for name, obj in _dataclasses().items()
        if not obj.__dataclass_params__.frozen  # type: ignore[attr-defined]
    ]
    assert not_frozen == []


@pytest.mark.unit
def test_no_dataclass_field_is_a_mutable_sequence() -> None:
    """Plural fields are tuples, never lists or sets.

    ``frozen=True`` freezes the binding, not the object bound. A frozen
    record holding a list is one ``.append`` away from not being one.
    """
    offenders = _annotations_matching(r"\b(list|set|List|Set)\[")
    assert offenders == []


@pytest.mark.unit
def test_no_float_appears_anywhere_in_the_surface() -> None:
    """KERNEL-SPEC section 2.1: no float in any hashed value.

    Anything on this surface can reach a digest, so the surface carries
    ``Decimal`` and ``int`` and nothing else numeric.
    """
    offenders = _annotations_matching(r"\bfloat\b")
    assert offenders == []


# ── 3. runtime-checkable Protocols ─────────────────────────────────────


@pytest.mark.unit
def test_the_expected_protocols_are_present() -> None:
    assert sorted(_protocols()) == [
        "AccuracyGate",
        "Calibrator",
        "DigestRef",
        "EmitSink",
        "ExpressionEvaluator",
        "FidelityProver",
        "FidelityVerifier",
        "LineageEmitter",
        "ModelPort",
        "ProofSystem",
        "SkillRunner",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_protocols()))
def test_every_protocol_is_runtime_checkable(name: str) -> None:
    """Every port on this surface can be checked at a boundary.

    All eleven are structural contracts a caller may want to verify before
    handing work to a third-party object, so all eleven are decorated --
    there is no Protocol here for which the check would be meaningless.
    """
    protocol = _protocols()[name]
    assert getattr(protocol, "_is_runtime_protocol", False), f"{name} is not runtime_checkable"
    # And the decoration actually works, rather than merely being present.
    assert isinstance(object(), protocol) is False


@pytest.mark.unit
def test_no_protocol_member_carries_an_implementation() -> None:
    """Protocols and frozen dataclasses only -- no logic on this surface."""
    offenders: list[str] = []
    for name, protocol in _protocols().items():
        for member, value in vars(protocol).items():
            if member.startswith("_") or not inspect.isfunction(value):
                continue
            source = textwrap.dedent(inspect.getsource(value))
            func = ast.parse(source).body[0]
            assert isinstance(func, ast.FunctionDef)
            statements = [
                node
                for node in func.body
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
                and not isinstance(node, ast.Pass)
            ]
            if statements:
                offenders.append(f"{name}.{member}")
    assert offenders == []


# ── 4. the verdict cannot be collapsed ─────────────────────────────────


@pytest.mark.unit
def test_verify_verdict_has_no_dunder_bool() -> None:
    assert "__bool__" not in vars(VerifyVerdict)
    # Not merely absent from this class -- absent from the whole MRO, so a
    # base class cannot supply one later either. ``object`` defines no
    # ``__bool__``; default truthiness is the interpreter's fallback.
    assert not hasattr(VerifyVerdict, "__bool__")
    # ``__len__`` would make ``if verdict:`` meaningful by the same route.
    assert not hasattr(VerifyVerdict, "__len__")


@pytest.mark.unit
def test_verify_verdict_has_no_combining_accessor() -> None:
    surface = set(vars(VerifyVerdict)) | {f.name for f in dataclasses.fields(VerifyVerdict)}
    assert surface & _COMBINING_NAMES == set()


@pytest.mark.unit
def test_verify_verdict_reports_two_disjoint_fields() -> None:
    field_names = [f.name for f in dataclasses.fields(VerifyVerdict)]
    assert "reproducibility" in field_names
    assert "validity" in field_names


@pytest.mark.unit
def test_default_verdict_asserts_nothing_about_the_world() -> None:
    """The v0.1 default: reproducibility unchecked, validity not asserted."""
    verdict = VerifyVerdict()
    assert verdict.reproducibility is Reproducibility.NOT_CHECKED
    assert verdict.validity is Validity.NOT_ASSERTED


@pytest.mark.unit
def test_the_documented_headline_renders_exactly() -> None:
    verdict = VerifyVerdict(
        reproducibility=Reproducibility.REPRODUCIBLE,
        validity=Validity.NOT_ASSERTED,
    )
    assert verdict.headline() == "reproducible: yes | validity: not asserted"


@pytest.mark.property
@given(
    reproducibility=st.sampled_from(list(Reproducibility)),
    validity=st.sampled_from(list(Validity)),
)
def test_the_headline_always_states_both_answers(
    reproducibility: Reproducibility, validity: Validity
) -> None:
    """Property: neither half can drop out, for any combination.

    A headline that omitted the validity half whenever it was
    ``not asserted`` would read, to every reader, as the stronger claim.
    """
    headline = VerifyVerdict(reproducibility=reproducibility, validity=validity).headline()
    left, _, right = headline.partition(" | ")
    assert left.startswith("reproducible: ") and len(left) > len("reproducible: ")
    assert right.startswith("validity: ") and len(right) > len("validity: ")


# ── stage results ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_artifacts_is_a_read_only_view() -> None:
    result = StageResult("plan", 0, {"plan": {"planDigest": "sha256:00"}})
    assert result.artifacts["plan"] == {"planDigest": "sha256:00"}
    with pytest.raises(TypeError):
        result.artifacts["plan"] = {}  # type: ignore[index]


@pytest.mark.unit
def test_artifacts_copy_is_mutable_and_does_not_write_back() -> None:
    result = StageResult("plan", 0, {"plan": 1})
    copy = dict(result.artifacts)
    copy["plan"] = 2
    assert result.artifacts["plan"] == 1


@pytest.mark.unit
def test_success_tracks_the_exit_code() -> None:
    assert StageResult("run", 0).success is True
    assert StageResult("run", 1).success is False


@pytest.mark.unit
def test_raise_for_status_is_silent_on_success() -> None:
    StageResult("ship", 0).raise_for_status()


@pytest.mark.unit
@pytest.mark.parametrize("stage", StageName)
def test_each_stage_raises_its_own_failure_class(stage: str) -> None:
    """One ``<Stage>Failed`` per stage, all catchable as ``EngineError``."""
    expected = getattr(stages_module, f"{''.join(p.title() for p in stage.split('_'))}Failed")
    result = StageResult(stage, 2, stderr="boom")
    with pytest.raises(expected) as caught:
        result.raise_for_status()
    assert isinstance(caught.value, EngineError)
    assert caught.value.stage == stage
    assert caught.value.result is result
    assert "boom" in str(caught.value)


@pytest.mark.unit
def test_an_unknown_stage_falls_through_to_the_base_error() -> None:
    """An unrecognised stage fails loudly, not silently as a success."""
    with pytest.raises(EngineError) as caught:
        StageResult("teleport", 1).raise_for_status()
    assert type(caught.value) is EngineError


@pytest.mark.unit
def test_stage_names_cover_the_six_step_loop_and_replay() -> None:
    assert StageName == ("validate", "plan", "apply", "run", "verify", "ship", "replay")


@pytest.mark.unit
def test_stage_result_repr_does_not_leak_captured_output() -> None:
    """``repr`` names the artifact keys, never the stage's raw output.

    A result repr reaches log aggregators and exception chains; captured
    stderr can carry anything the stage printed.
    """
    rendered = repr(StageResult("apply", 1, {"k": "v"}, stdout="OUT-SECRET", stderr="ERR-SECRET"))
    assert "OUT-SECRET" not in rendered
    assert "ERR-SECRET" not in rendered


# ── register discipline ────────────────────────────────────────────────


@pytest.mark.unit
def test_no_banned_vocabulary_in_the_api_docstrings() -> None:
    hits: list[str] = []
    package_dir = Path(api.__file__).parent
    for path in sorted(package_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = _BANNED_PATTERN.search(line)
            if match:
                hits.append(f"{path.name}:{lineno}: {match.group(0)!r}")
    assert hits == []


@pytest.mark.unit
def test_no_banned_vocabulary_in_the_api_docs() -> None:
    doc = REPO_ROOT / "docs" / "reference" / "python-api.md"
    assert doc.is_file(), "docs/reference/python-api.md is part of the deliverable"
    hits = [
        f"{lineno}: {m.group(0)!r}"
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1)
        if (m := _BANNED_PATTERN.search(line))
    ]
    assert hits == []


@pytest.mark.unit
def test_every_public_name_is_documented_in_the_api_docs() -> None:
    """Documentation is a deliverable: no name ships undocumented."""
    doc = (REPO_ROOT / "docs" / "reference" / "python-api.md").read_text(encoding="utf-8")
    undocumented = [name for name in api.__all__ if name not in doc]
    assert undocumented == []
