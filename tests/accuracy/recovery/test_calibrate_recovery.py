# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Recovery test for ``calibrate``: it declares and never fits, so recovery is exact.

``calibrate`` reads a Playback's authored ``spec.scorecard`` and reports it. There is nothing
to estimate, so the criteria are equalities, checked against a recomputation that does not
use the engine's code path (``docs/accuracy/DESIGN.md``, "Recovery tests"):

* ``intervalCoverage`` and ``backtestWindow`` come back exactly as authored (for a float,
  ``float(recovered) == authored``, so nothing was rounded);
* the authored ``credibility`` comes back exactly through the seam gate
  (``max_seam_credibility``), and the gate's boundary is exact: a threshold equal to the
  authored value is met, the next representable float above it is not;
* ``grade`` is never invented: the scorecard has none, however a grade was authored, and the
  CLI says "not graded";
* the evidence digest equals an independent recomputation (``hashlib`` and ``json`` over the
  authored block, not the engine's canonicaliser) and moves if and only if the authored
  block moves;
* dials pass through unchanged.

The scorecards are drawn from the FLUX schema's own ``scorecard`` definition
(hypothesis-jsonschema), plus fixed cases for the edges: zero, one, the tiniest and the
largest numbers, each grade, and the ``minCredibility`` gate of the fixture's Simulation.
Through the CLI the fixture ``telco-fidelity-small`` is copied into ``tmp_path`` and only its
Playback is rewritten; the bundle's closed input set is not affected (``calibrate`` does not
read ``flux-manifest.json`` or ``flux.lock``).
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy", "hypothesis_jsonschema")

import contextlib  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import shutil  # noqa: E402
import unicodedata  # noqa: E402
from decimal import Decimal  # noqa: E402
from functools import lru_cache  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple  # noqa: E402

import pytest  # noqa: E402
import yaml  # noqa: E402
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis_jsonschema import from_schema  # noqa: E402
from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

import flux_engine._vendor.flux_spec.validate as vendored_validate  # noqa: E402
from flux_engine.api.calibration import Credibility, Provenance  # noqa: E402
from flux_engine.calibrate.declared import (  # noqa: E402
    DECLARED_METHOD_ID,
    DeclaredCalibrator,
    max_seam_credibility,
    require_min_credibility,
    scorecard_from_playback,
)
from flux_engine.cli import main  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier, current_tier  # noqa: E402

FAMILY = "recovery"
REPO_ROOT = Path(__file__).resolve().parents[3]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
#: The fixture Simulation's ``emits[].minCredibility``: a bundle whose only Playback sits below
#: it does not validate, so a scorecard sent through the CLI must reach it.
MIN_CREDIBILITY = 0.7

TIER = current_tier()


# ---------------------------------------------------------------------------
# The scorecard schema, and an independent recomputation of the evidence digest
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def scorecard_schema() -> Dict[str, Any]:
    """The FLUX schema's ``scorecard`` definition (Playback.spec), with its ``$defs``."""
    document = json.loads(Path(vendored_validate.FLUX_SCHEMA_PATH).read_text(encoding="utf-8"))

    def find(node: Any) -> Optional[Dict[str, Any]]:
        if isinstance(node, dict):
            candidate = node.get("scorecard")
            if isinstance(candidate, dict) and "properties" in candidate:
                return candidate
            for child in node.values():
                found = find(child)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = find(child)
                if found is not None:
                    return found
        return None

    scorecard = find(document)
    assert scorecard is not None, "the FLUX schema no longer defines a Playback scorecard"
    return {"$defs": document["$defs"], **scorecard}


@lru_cache(maxsize=1)
def scorecard_validator() -> Draft202012Validator:
    return Draft202012Validator(scorecard_schema(), format_checker=FormatChecker())


@contextlib.contextmanager
def recorded(
    log: ResultLog, case: str, criterion: str, *, n: Optional[int] = None
) -> Iterator[None]:
    """Log the case as passed or failed, whichever it turns out to be, and re-raise a failure."""
    try:
        yield
    except Exception:
        log.record(family=FAMILY, case=case, criterion=criterion, passed=False, n=n)
        raise
    log.record(family=FAMILY, case=case, criterion=criterion, passed=True, n=n)


def literal(value: Any) -> str:
    """The shortest decimal spelling of an authored number, no exponent: its YAML literal.

    Python's ``repr`` of a float is the shortest string that reads back as that float, so
    the literal denotes exactly the value the author typed. An integer is spelled as one.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return format(Decimal(repr(value)), "f")


def independent_digest(block: Mapping[str, Any], *, spelled: bool = False) -> str:
    """``sha256:`` over the authored scorecard block, without the engine's canonicaliser.

    A numeric field is a decimal string at its authored scale (KERNEL-SPEC section 2.1), other
    fields are strings; keys sorted, no whitespace, UTF-8. Python's ``json`` is not RFC 8785
    in general, but for a mapping of short strings with ASCII keys it produces the same bytes.
    With *spelled* the numeric fields are already literals (the CLI reads them as Decimals).
    """
    document: Dict[str, str] = {}
    for key, value in block.items():
        if key in ("intervalCoverage", "credibility"):
            document[key] = str(value) if spelled else literal(value)
        else:
            document[key] = unicodedata.normalize("NFC", str(value))
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def playback_of(block: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"worldRef": "q3-retention-world"}
    if block is not None:
        spec["scorecard"] = dict(block)
    return {"kind": "Playback", "id": "pb-recovery", "spec": spec}


@st.composite
def scorecards(draw: Any) -> Dict[str, Any]:
    block = draw(from_schema(scorecard_schema()))
    assert not list(scorecard_validator().iter_errors(block))
    return dict(block)


#: Fixed cases: the fixture's own scorecard, the edges of ``unitInterval``, each grade, an
#: integer-typed number, a window in each unit, a number at the float limits.
FIXED_BLOCKS: Tuple[Tuple[str, Dict[str, Any]], ...] = (
    (
        "fixture",
        {"backtestWindow": "P90D", "grade": "B", "intervalCoverage": 0.92, "credibility": 0.78},
    ),
    ("zeros", {"grade": "F", "intervalCoverage": 0.0, "credibility": 0.0}),
    ("ones", {"grade": "A", "intervalCoverage": 1.0, "credibility": 1.0}),
    ("integers", {"grade": "C", "intervalCoverage": 0, "credibility": 1}),
    ("required-only", {"grade": "D", "credibility": 0.5}),
    ("tiny", {"grade": "E", "intervalCoverage": 5e-324, "credibility": 2.2250738585072014e-308}),
    (
        "just-below-one",
        {"grade": "A", "intervalCoverage": 0.9999999999999999, "credibility": 0.9999999999999999},
    ),
    (
        "weeks",
        {"backtestWindow": "P12W", "grade": "B", "intervalCoverage": 0.8, "credibility": 0.1},
    ),
    ("months", {"backtestWindow": "P6M", "grade": "A", "credibility": 0.123456789012345}),
    (
        "years",
        {"backtestWindow": "P1Y", "grade": "C", "intervalCoverage": 0.95, "credibility": 0.3},
    ),
    *(
        (f"grade-{letter}", {"grade": letter, "intervalCoverage": 0.5, "credibility": 0.5})
        for letter in "ABCDEF"
    ),
)


# ---------------------------------------------------------------------------
# The declared calibrator, over the schema's whole domain
# ---------------------------------------------------------------------------


def check_recovered(block: Mapping[str, Any]) -> None:
    """Every criterion for one authored block, through the Python API."""
    assert not list(scorecard_validator().iter_errors(block)), "the case is outside the schema"
    scorecard = scorecard_from_playback(playback_of(block))

    # Never invented: no grade, however a grade was authored; and the report says so.
    assert scorecard.grade is None
    assert scorecard.provenance is Provenance.DECLARED
    assert scorecard.credibility is Credibility.DECLARED
    assert scorecard.method_id == DECLARED_METHOD_ID

    # intervalCoverage and backtestWindow come back exactly.
    if "intervalCoverage" in block:
        authored = block["intervalCoverage"]
        assert scorecard.interval_coverage == Decimal(literal(authored))
        assert float(scorecard.interval_coverage) == authored
    else:
        assert scorecard.interval_coverage is None
    assert scorecard.backtest_window == block.get("backtestWindow")

    # credibility comes back exactly through the seam gate, and the gate is exact at the edge.
    authored_credibility = block["credibility"]
    docs = {"pb": playback_of(block)}
    recovered = max_seam_credibility(docs, world_ref="q3-retention-world")
    assert recovered == Decimal(literal(authored_credibility))
    assert float(recovered) == authored_credibility
    assert (
        require_min_credibility(
            docs, world_ref="q3-retention-world", min_credibility=authored_credibility
        )
        == recovered
    )
    above = math.nextafter(float(authored_credibility), 2.0)
    with pytest.raises(FluxError) as refused:
        require_min_credibility(docs, world_ref="q3-retention-world", min_credibility=above)
    assert refused.value.code.code == "FLX-7010"

    # The evidence digest is the independent recomputation.
    assert scorecard.evidence_digest is not None
    assert scorecard.evidence_digest.value == independent_digest(block)


@pytest.mark.parametrize("name,block", FIXED_BLOCKS, ids=[name for name, _ in FIXED_BLOCKS])
def test_fixed_scorecards_are_recovered_exactly(
    name: str, block: Dict[str, Any], tier: Tier, accuracy_log: ResultLog
) -> None:
    with recorded(
        accuracy_log,
        f"calibrate-fixed-{name}",
        "authored scorecard fields, credibility and digest recovered exactly",
        n=1,
    ):
        check_recovered(block)


def test_any_schema_valid_scorecard_is_recovered_exactly(accuracy_log: ResultLog) -> None:
    @TIER.property_settings(pr=80, nightly=3000, label="calibrate-recovery-api")
    @given(scorecards())
    def prop(block: Dict[str, Any]) -> None:
        check_recovered(block)

    with recorded(
        accuracy_log,
        "calibrate-property-api",
        "every schema-valid scorecard is recovered exactly through the API",
    ):
        prop()


def test_evidence_digest_moves_if_and_only_if_the_authored_block_moves(
    accuracy_log: ResultLog,
) -> None:
    @TIER.property_settings(pr=50, nightly=2000, label="calibrate-digest-moves")
    @given(scorecards(), scorecards())
    def prop(first: Dict[str, Any], second: Dict[str, Any]) -> None:
        def digest(block: Mapping[str, Any]) -> str:
            found = scorecard_from_playback(playback_of(block)).evidence_digest
            assert found is not None
            return found.value

        def identity(block: Mapping[str, Any]) -> Dict[str, str]:
            # What "the same authored block" means: the same fields, a number at its authored
            # scale. 0.5 and 0.50 are the same float and so the same block; 1 and 1.0 are not.
            return {
                key: (literal(value) if key in ("intervalCoverage", "credibility") else str(value))
                + (":int" if isinstance(value, int) and not isinstance(value, bool) else "")
                for key, value in block.items()
            }

        assert (digest(first) == digest(second)) == (identity(first) == identity(second))
        # Field order is not part of the block.
        reordered = dict(reversed(list(first.items())))
        assert digest(reordered) == digest(first)

    with recorded(
        accuracy_log,
        "calibrate-property-digest-moves",
        "the evidence digest moves if and only if the authored block moves",
    ):
        prop()


def test_digest_ignores_everything_outside_the_scorecard_and_moves_with_each_field() -> None:
    base = dict(FIXED_BLOCKS[0][1])
    playback = playback_of(base)
    original = scorecard_from_playback(playback).evidence_digest
    playback["spec"]["calibration"] = {"window": "P7D", "method": "mle"}
    playback["name"] = "renamed"
    assert scorecard_from_playback(playback).evidence_digest == original
    for field, other in (
        ("grade", "C"),
        ("intervalCoverage", 0.91),
        ("credibility", 0.79),
        ("backtestWindow", "P91D"),
    ):
        moved = scorecard_from_playback(playback_of({**base, field: other})).evidence_digest
        assert moved != original, field
    removed = {k: v for k, v in base.items() if k != "backtestWindow"}
    assert scorecard_from_playback(playback_of(removed)).evidence_digest != original
    # A grade is part of the digest even though it is never reported.
    only_grade = scorecard_from_playback(playback_of({**base, "grade": "A"}))
    assert only_grade.grade is None and only_grade.evidence_digest != original


def test_no_authored_scorecard_establishes_nothing() -> None:
    scorecard = scorecard_from_playback(playback_of(None))
    assert scorecard.credibility is Credibility.NOT_ESTABLISHED
    assert scorecard.grade is None
    assert scorecard.interval_coverage is None and scorecard.backtest_window is None
    assert scorecard.evidence_digest is None
    assert max_seam_credibility({"pb": playback_of(None)}, world_ref="q3-retention-world") is None


_DIAL_VALUES = st.decimals(
    min_value=Decimal("-1000000"), max_value=Decimal("1000000"), allow_nan=False, places=9
)


def test_dials_pass_through_unchanged(accuracy_log: ResultLog) -> None:
    @TIER.property_settings(pr=30, nightly=1000, label="calibrate-dials")
    @given(
        st.dictionaries(
            st.text(st.characters(codec="ascii", categories=["L", "N"]), min_size=1), _DIAL_VALUES
        ),
        scorecards(),
    )
    def prop(dials: Dict[str, Decimal], block: Dict[str, Any]) -> None:
        returned, scorecard = DeclaredCalibrator(playback=playback_of(block)).calibrate({}, dials)
        assert dict(returned) == dials
        assert all(
            returned[name] is value or str(returned[name]) == str(value)
            for name, value in dials.items()
        )
        assert scorecard.grade is None

    with recorded(
        accuracy_log, "calibrate-property-dials", "dials pass through the calibrator unchanged"
    ):
        prop()


# ---------------------------------------------------------------------------
# Through the CLI
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A private copy of the small bundle; the tests rewrite only its Playback."""
    target = tmp_path_factory.mktemp("calibrate-recovery") / "bundle"
    shutil.copytree(SMALL_BUNDLE, target)
    return target


def write_playback(bundle: Path, block: Mapping[str, Any]) -> None:
    """Rewrite the bundle's Playback with *block* as its scorecard, numbers as literals."""
    path = bundle / "playback.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # The numbers are written as the literal text of their decimal spelling so that the
    # recovered value is a function of the file, not of this process's float formatting.
    text = yaml.dump(
        {
            **document,
            "spec": {**document["spec"], "scorecard": {k: _Raw(v) for k, v in block.items()}},
        },
        sort_keys=False,
        Dumper=_Dumper,
    )
    path.write_text(text, encoding="utf-8")


class _Raw:
    """A scorecard value to be written to YAML as a number literal (or a plain string)."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _Dumper(yaml.SafeDumper):
    pass


def _represent_raw(dumper: yaml.SafeDumper, raw: _Raw) -> Any:
    value = raw.value
    if isinstance(value, str):
        return dumper.represent_str(value)
    spelling = literal(value)
    tag = "tag:yaml.org,2002:int" if isinstance(value, int) else "tag:yaml.org,2002:float"
    return dumper.represent_scalar(tag, spelling)


_Dumper.add_representer(_Raw, _represent_raw)


def run_cli(*argv: str) -> Tuple[int, Dict[str, Any]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
        code = main([*argv, "--json"])
    return code, json.loads(buffer.getvalue())


def cli_credibility(block: Mapping[str, Any]) -> Dict[str, Any]:
    """*block* with its credibility lifted into the fixture's ``minCredibility`` gate."""
    low = MIN_CREDIBILITY
    lifted = min(1.0, low + float(block["credibility"]) * (1.0 - low))
    return {**block, "credibility": lifted}


def check_cli(bundle: Path, block: Mapping[str, Any]) -> Dict[str, Any]:
    write_playback(bundle, block)
    code, envelope = run_cli("calibrate", str(bundle))
    assert code == 0 and envelope["ok"], envelope["error"]
    scorecard = envelope["artifacts"]["scorecard"]
    assert envelope["artifacts"]["playbackId"] == "pb-billing-history"
    assert scorecard["grade"] is None  # never invented
    assert scorecard["credibility"] == "declared"
    assert scorecard["provenance"] == "declared"
    assert scorecard["methodId"] == DECLARED_METHOD_ID
    if "intervalCoverage" in block:
        recovered = Decimal(scorecard["intervalCoverage"])
        assert recovered == Decimal(literal(block["intervalCoverage"]))
        assert float(recovered) == block["intervalCoverage"]
    else:
        assert scorecard["intervalCoverage"] is None
    assert scorecard["backtestWindow"] == block.get("backtestWindow")
    # The bundle was read as written: the digest is over the literals in the file.
    spelled = {
        k: (literal(v) if k in ("intervalCoverage", "credibility") else v) for k, v in block.items()
    }
    assert scorecard["evidenceDigest"] == independent_digest(spelled, spelled=True)
    return scorecard


@pytest.mark.parametrize("name,block", FIXED_BLOCKS, ids=[name for name, _ in FIXED_BLOCKS])
def test_cli_reports_fixed_scorecards_exactly(
    name: str, block: Dict[str, Any], bundle: Path, accuracy_log: ResultLog
) -> None:
    with recorded(
        accuracy_log,
        f"calibrate-cli-{name}",
        "the CLI reports the authored scorecard exactly and grades nothing",
        n=1,
    ):
        check_cli(bundle, cli_credibility(block))


def test_cli_reports_any_schema_valid_scorecard_exactly(
    bundle: Path, accuracy_log: ResultLog
) -> None:
    @TIER.property_settings(pr=6, nightly=120, label="calibrate-recovery-cli")
    @given(scorecards())
    def prop(block: Dict[str, Any]) -> None:
        check_cli(bundle, cli_credibility(block))

    with recorded(
        accuracy_log,
        "calibrate-property-cli",
        "the CLI reports every schema-valid scorecard exactly",
    ):
        prop()


def test_cli_digest_moves_with_the_authored_block_and_nothing_else(bundle: Path) -> None:
    base = {"backtestWindow": "P90D", "grade": "B", "intervalCoverage": 0.92, "credibility": 0.78}
    digests = {}
    for label, block in (
        ("base", base),
        ("grade", {**base, "grade": "A"}),
        ("coverage", {**base, "intervalCoverage": 0.93}),
        ("credibility", {**base, "credibility": 0.79}),
        ("window", {**base, "backtestWindow": "P91D"}),
        ("again", dict(reversed(list(base.items())))),
    ):
        digests[label] = check_cli(bundle, block)["evidenceDigest"]
    assert digests["again"] == digests["base"]
    moved = {label: value for label, value in digests.items() if label not in ("base", "again")}
    assert all(value != digests["base"] for value in moved.values())
    assert len(set(digests.values())) == 5


@pytest.mark.parametrize(
    "credibility,accepted",
    [
        (MIN_CREDIBILITY, True),
        (math.nextafter(MIN_CREDIBILITY, 2.0), True),
        (math.nextafter(MIN_CREDIBILITY, 0.0), False),
        (0.0, False),
    ],
    ids=["at-the-gate", "just-above", "just-below", "zero"],
)
def test_cli_credibility_is_what_the_seam_gate_sees(
    credibility: float, accepted: bool, bundle: Path
) -> None:
    write_playback(bundle, {"grade": "B", "credibility": credibility})
    code, envelope = run_cli("calibrate", str(bundle))
    if accepted:
        assert code == 0 and envelope["ok"]
    else:
        assert code == 1 and envelope["error"]["code"] == "FLX-3105"
        assert f"credibility {Decimal(literal(credibility))}" in envelope["error"]["message"]


def test_cli_dry_run_says_not_graded(bundle: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_playback(bundle, dict(FIXED_BLOCKS[0][1]))
    assert main(["calibrate", str(bundle), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "not graded" in out and "credibility declared" in out
    assert "grade B" not in out


def test_cli_write_leaves_dials_empty_and_unchanged(bundle: Path, tmp_path: Path) -> None:
    write_playback(bundle, dict(FIXED_BLOCKS[0][1]))
    state = tmp_path / "state"
    run_id = "0123456789abcdef"
    code, envelope = run_cli(
        "calibrate", str(bundle), "--write", "--run-id", run_id, "--state-dir", str(state)
    )
    assert code == 0, envelope["error"]
    written = json.loads(Path(envelope["artifacts"]["path"]).read_text(encoding="utf-8"))
    assert written["kind"] == "CalibrationProvenance"
    assert written["dials"] == {}
    assert written["worldRef"] == "q3-retention-world"
