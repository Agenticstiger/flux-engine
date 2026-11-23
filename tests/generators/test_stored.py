# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A plugin expose's bytes are stored, not generated again (ADR 0032 section 6).

A bundle that declares ``generators`` is planned with a stage always, applied from that stage only,
and replayed only where generating again is expected to give the stored bytes: ``bytes``, or
``environment`` in the environment that ran it. The engine measures the environment, records it in
``plan.json`` and reports what it stored in ``EVIDENCE.md``.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from flux_engine.api.verdict import Reproducibility
from flux_engine.cli import main
from flux_engine.core.digest import build_plan_digest, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.evidence.report import StoredGenerator, render_reproducibility_section
from flux_engine.generators import environment
from flux_engine.stages.apply import plan_kernel
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.plan import plan
from flux_engine.stages.staging import has_stage, stage_root
from flux_engine.stages.stored import declared_for_run, stored_generators
from flux_engine.starter import scaffold

from .conftest import (
    EXPOSE,
    FIXTURE,
    GENERATING_PLUGIN,
    PACKAGE,
    edit_emit,
    make_distribution,
    plugin_bundle,
)

pytestmark = pytest.mark.integration

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _calls() -> List[Any]:
    return list(sys.modules[f"{PACKAGE}.plugin"].CALLS)


def _planned(bundle: Path, tmp_path: Path, **options: Any) -> str:
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",), **options)
    assert outcome.exit_code == 0, outcome.stderr
    return str(outcome.artifacts["run_id"])


def _bundle(tmp_path: Path, reproducibility: str = "bytes") -> Path:
    """The fixture bundle declaring the test plugin with *reproducibility*, which it states too."""
    return plugin_bundle(
        tmp_path, reproducibility=reproducibility, options={"stated": reproducibility}
    )


def _state(tmp_path: Path) -> List[str]:
    return ["--state-dir", str(tmp_path / "state")]


def _install_editable(site: Path, tmp_path: Path) -> None:
    import importlib

    source = tmp_path / "source"
    make_distribution(
        site, plugin_source=GENERATING_PLUGIN, package_dir=source / PACKAGE, editable=True
    )
    sys.path.insert(0, str(source))
    importlib.invalidate_caches()


@pytest.fixture
def editable_installed(site: Path, tmp_path: Path) -> Any:
    _install_editable(site, tmp_path)
    yield site
    sys.path.remove(str(tmp_path / "source"))


# ---------------------------------------------------------------------------
# plan --no-stage is refused
# ---------------------------------------------------------------------------


def test_plan_no_stage_is_refused_before_any_plugin_code_runs(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    outcome = plan(bundle, state_dir=tmp_path / "state", stage=False, allow_generators=("learned",))
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3122")
    assert "plan without --no-stage" in outcome.stderr and EXPOSE in outcome.stderr
    assert f"{PACKAGE}.plugin" not in sys.modules
    assert not list((tmp_path / "state").glob("runs/*"))
    # The command line says the same, with or without the permission to load the plugin.
    for extra in ([], ["--allow-generator", "learned"]):
        assert main(["plan", str(bundle), *_state(tmp_path), "--no-stage", *extra]) == 1
        assert "FLX-3122" in capsys.readouterr().err
    assert not list((tmp_path / "state").glob("runs/*"))


def test_plan_no_stage_still_works_for_a_bundle_without_generators(tmp_path: Path) -> None:
    outcome = plan(FIXTURE, state_dir=tmp_path / "state", stage=False)
    assert outcome.exit_code == 0 and outcome.artifacts["staged"] is False


# ---------------------------------------------------------------------------
# apply --run-id commits only from the stage
# ---------------------------------------------------------------------------


def test_apply_commits_the_stage_and_asks_the_plugin_for_nothing_more(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    run_id = _planned(bundle, tmp_path)
    assert _calls() == [(0, 64)]
    assert main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]) == 0
    assert _calls() == [(0, 64)]
    layout = RunLayout.for_state_dir(tmp_path / "state", run_id)
    assert layout.receipt_path.exists() and not has_stage(layout)


def test_apply_of_a_committed_run_keeps_it_and_never_asks_the_plugin_again(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    run_id = _planned(bundle, tmp_path)
    layout = RunLayout.for_state_dir(tmp_path / "state", run_id)
    assert main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]) == 0
    capsys.readouterr()
    before = _snapshot(tmp_path)
    assert main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]) == 0
    out = capsys.readouterr().out
    assert f"run {run_id} keeps its stored plugin output" in out and "another seed" in out
    assert _calls() == [(0, 64)]
    assert _snapshot(tmp_path) == before
    assert layout.receipt_path.exists()


def test_apply_refuses_a_stage_that_is_not_this_plan_s(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    run_id = _planned(bundle, tmp_path)
    layout = RunLayout.for_state_dir(tmp_path / "state", run_id)
    manifest = stage_root(layout) / "stage.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["planDigest"] = "sha256:" + "0" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")
    assert main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]) == 1
    assert "FLX-3123" in capsys.readouterr().err
    assert _calls() == [(0, 64)] and not layout.receipt_path.exists()


@pytest.mark.parametrize(
    ("extra", "code"),
    [(["--simulation-id", "other"], "FLX-2301"), (["--zone", "eu-west"], "FLX-3123")],
    ids=["simulation", "zone"],
)
def test_apply_refuses_another_simulation_id_or_zone_than_the_stage_was_made_with(
    installed: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: List[str],
    code: str,
) -> None:
    # A Simulation the bundle does not have is refused as the cast that does not resolve, as it is
    # for a bundle with no plugin; another zone is a stage that was not made for it.
    bundle = plugin_bundle(tmp_path)
    run_id = _planned(bundle, tmp_path)
    outcome = main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id, *extra])
    assert outcome == 1
    assert code in capsys.readouterr().err
    assert _calls() == [(0, 64)]
    layout = RunLayout.for_state_dir(tmp_path / "state", run_id)
    assert not layout.receipt_path.exists() and has_stage(layout)


def test_apply_refuses_strict_coverage_instead_of_generating_again(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    run_id = _planned(bundle, tmp_path)
    args = ["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]
    assert main([*args, "--strict-coverage"]) == 1
    err = capsys.readouterr().err
    assert "FLX-3123" in err and "--strict-coverage" in err
    assert _calls() == [(0, 64)]
    # The stage is still there for an apply that asks for nothing more.
    assert main(args) == 0 and _calls() == [(0, 64)]


def test_apply_of_a_bundle_without_generators_still_generates_what_the_stage_does_not_hold(
    tmp_path: Path,
) -> None:
    outcome = plan(FIXTURE, state_dir=tmp_path / "state", stage=False)
    run_id = outcome.artifacts["run_id"]
    assert main(["apply", str(FIXTURE), *_state(tmp_path), "--run-id", run_id]) == 0
    assert main(["apply", str(FIXTURE), *_state(tmp_path), "--run-id", run_id]) == 0


def test_fluxsim_run_holds_its_execution_in_memory_and_writes_it_once(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    code = main(["run", str(bundle), *_state(tmp_path), "--allow-generator", "learned"])
    assert code == 0
    assert _calls() == [(0, 64)]
    (run_dir,) = (tmp_path / "state" / "runs").iterdir()
    assert (run_dir / "receipt.json").exists() and not (run_dir / ".stage").exists()


# ---------------------------------------------------------------------------
# A committed plugin run is kept, never generated again
# ---------------------------------------------------------------------------

#: A plugin stated ``none`` whose values depend on how many times it was called in the process.
DRIFTING_PLUGIN = GENERATING_PLUGIN.replace(
    "self._value(column, request.seed, row)",
    "self._value(column, request.seed + str(len(CALLS)).encode(), row)",
)


def _snapshot(tmp_path: Path) -> Dict[str, bytes]:
    """Every file under the state directory, by relative path, with its bytes."""
    root = tmp_path / "state"
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.fixture
def drifting(site: Path) -> Path:
    assert DRIFTING_PLUGIN != GENERATING_PLUGIN
    make_distribution(site, plugin_source=DRIFTING_PLUGIN)
    return site


def _applied_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> "tuple[Path, str, RunLayout]":
    bundle = _bundle(tmp_path, "none")
    run_id = _planned(bundle, tmp_path)
    assert main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]) == 0
    capsys.readouterr()
    return bundle, run_id, RunLayout.for_state_dir(tmp_path / "state", run_id)


def test_run_and_apply_again_keep_a_none_plugin_run_and_never_ask_it_again(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    assert _calls() == [(0, 64)]
    before = _snapshot(tmp_path)
    receipt = layout.receipt_path.read_bytes()

    assert main(["run", str(bundle), *_state(tmp_path), "--allow-generator", "learned"]) == 0
    out = capsys.readouterr().out
    assert f"run {run_id} keeps its stored plugin output" in out and "another seed" in out
    assert f"run {run_id} complete" in out
    assert main(["apply", str(bundle), *_state(tmp_path), "--allow-generator", "learned"]) == 0
    assert f"run {run_id} keeps its stored plugin output" in capsys.readouterr().out

    assert _calls() == [(0, 64)]
    assert _snapshot(tmp_path) == before
    assert layout.receipt_path.read_bytes() == receipt


def test_run_again_still_verifies_and_delivers_the_kept_run(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    before = _snapshot(tmp_path)
    target = tmp_path / "parquet"
    args = ["run", str(bundle), *_state(tmp_path), "--allow-generator", "learned", "--verify"]
    assert main([*args, "--sink", f"parquet:{target}"]) == 0
    out = capsys.readouterr().out
    assert "rows: ok" in out and "delivered" in out
    assert _calls() == [(0, 64)]
    assert any(target.rglob("*.parquet"))
    changed = {name for name, data in _snapshot(tmp_path).items() if before.get(name) != data}
    assert changed == {f"runs/{run_id}/sink-receipts.json"}


def test_apply_run_id_with_a_sink_retries_a_delivery_from_the_committed_run(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    stored = (layout.run_dir / "run-digest.json").read_bytes()
    target = tmp_path / "parquet"
    code = main(
        ["apply", str(bundle), *_state(tmp_path), "--run-id", run_id, "--sink", f"parquet:{target}"]
    )
    out = capsys.readouterr().out
    assert code == 0 and "delivered payment_recovery_moment: 64 rows" in out
    assert _calls() == [(0, 64)]
    assert any(target.rglob("*.parquet"))
    assert (layout.run_dir / "run-digest.json").read_bytes() == stored


def test_a_committed_plugin_run_whose_files_moved_is_refused_and_never_generated_again(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    stream = layout.emit_path(EXPOSE)
    stream.write_bytes(stream.read_bytes().replace(b"IE", b"GB", 1) + b"")
    tampered = stream.read_bytes()
    for args in (
        ["apply", str(bundle), *_state(tmp_path), "--run-id", run_id],
        ["apply", str(bundle), *_state(tmp_path), "--allow-generator", "learned"],
        ["run", str(bundle), *_state(tmp_path), "--allow-generator", "learned"],
    ):
        assert main(args) == 1
        err = capsys.readouterr().err
        assert "FLX-2110" in err and "never generated again" in err
    assert _calls() == [(0, 64)]
    assert stream.read_bytes() == tampered


def test_a_committed_plugin_run_without_its_run_digest_is_refused_like_verify_and_ship_do(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    layout.run_digest_path.unlink()
    before = _snapshot(tmp_path)
    for args in (
        ["apply", str(bundle), *_state(tmp_path), "--run-id", run_id],
        ["apply", str(bundle), *_state(tmp_path), "--allow-generator", "learned"],
        ["run", str(bundle), *_state(tmp_path), "--allow-generator", "learned"],
    ):
        assert main(args) == 1
        err = capsys.readouterr().err
        assert "FLX-2109" in err and "run-digest.json" in err and "never generated again" in err
        assert "FileNotFoundError" not in err and "report" not in err
    assert _calls() == [(0, 64)]
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("command", ["run", "apply"])
def test_strict_coverage_on_a_committed_plugin_run_is_refused_the_same_on_run_and_apply(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    before = _snapshot(tmp_path)
    args = [command, str(bundle), *_state(tmp_path), "--allow-generator", "learned"]
    assert main([*args, "--strict-coverage"]) == 1
    err = capsys.readouterr().err
    assert "FLX-3123" in err and "--strict-coverage" in err
    assert (
        main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id, "--strict-coverage"])
        == 1
    )
    assert "FLX-3123" in capsys.readouterr().err
    assert _calls() == [(0, 64)]
    assert _snapshot(tmp_path) == before


def test_the_facade_does_not_generate_a_stored_plugin_run_again(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.stages.apply import apply as apply_stage

    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    before = _snapshot(tmp_path)
    outcome = apply_stage(
        bundle,
        run_id=run_id,
        state_dir=tmp_path / "state",
        use_stage=False,
        allow_generators=("learned",),
    )
    assert outcome.exit_code == 0 and "keeps its stored plugin output" in outcome.stdout
    assert _calls() == [(0, 64)]
    assert _snapshot(tmp_path) == before


def test_apply_run_id_shard_of_a_committed_plugin_run_is_still_refused(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    run_id = _planned(bundle, tmp_path)
    assert main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]) == 0
    capsys.readouterr()
    assert (
        main(["apply", str(bundle), *_state(tmp_path), "--run-id", run_id, "--shard", "1/2"]) == 1
    )
    assert "FLX-3123" in capsys.readouterr().err
    assert _calls() == [(0, 64)]


def test_a_bundle_without_plugins_still_applies_again_and_delivers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcome = plan(FIXTURE, state_dir=tmp_path / "state")
    run_id = outcome.artifacts["run_id"]
    target = tmp_path / "parquet"
    assert main(["apply", str(FIXTURE), *_state(tmp_path), "--run-id", run_id]) == 0
    first = (tmp_path / "state" / "runs" / run_id / "run-digest.json").read_bytes()
    code = main(
        [
            "apply",
            str(FIXTURE),
            *_state(tmp_path),
            "--run-id",
            run_id,
            "--sink",
            f"parquet:{target}",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0 and "keeps its stored" not in out and "delivered" in out
    assert (tmp_path / "state" / "runs" / run_id / "run-digest.json").read_bytes() == first


# ---------------------------------------------------------------------------
# The environment digest
# ---------------------------------------------------------------------------


def test_the_environment_document_names_the_interpreter_the_platform_and_every_distribution() -> (
    None
):
    import platform
    from importlib import metadata

    document = environment.environment_document()
    assert sorted(document) == ["distributions", "interpreter", "platform"]
    assert document["interpreter"] == {
        "implementation": sys.implementation.name,
        "version": platform.python_version(),
    }
    assert document["platform"] == {"system": platform.system(), "machine": platform.machine()}
    listed = document["distributions"]
    assert listed == sorted(listed) and all(len(pair) == 2 for pair in listed)
    assert all(pair[0] == pair[0].lower() and "_" not in pair[0] for pair in listed)
    names = {pair[0] for pair in listed}
    assert "pytest" in names
    assert ["pytest", metadata.version("pytest")] in listed


def test_the_environment_digest_is_the_jcs_document_digest_and_stable_in_a_process() -> None:
    first = environment.environment_digest()
    assert DIGEST.fullmatch(first)
    assert first == environment.environment_digest()
    assert first == digest_of_document(environment.environment_document(), subject="x").value


def test_the_environment_digest_moves_with_an_installed_distribution(site: Path) -> None:
    before = environment.environment_digest()
    make_distribution(site, dist="Fx_Extra.Dist", version="3.1")
    document = environment.environment_document()
    assert ["fx-extra-dist", "3.1"] in document["distributions"]
    assert environment.environment_digest() != before


# ---------------------------------------------------------------------------
# plan.json's generators member
# ---------------------------------------------------------------------------


def _recorded(tmp_path: Path, run_id: str) -> Dict[str, Any]:
    layout = RunLayout.for_state_dir(tmp_path / "state", run_id)
    return json.loads(layout.plan_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_plan_records_each_plugin_and_the_environment_it_ran_in(
    installed: Path, tmp_path: Path
) -> None:
    bundle = _bundle(tmp_path, "environment")
    run_id = _planned(bundle, tmp_path)
    document = _recorded(tmp_path, run_id)
    assert document["generators"] == {
        EXPOSE: {
            "plugin": "learned",
            "distribution": "fxgen-plugin",
            "version": "1.0",
            "editable": False,
            "grade": "P5",
            "reproducibility": "environment",
            "environmentDigest": environment.environment_digest(),
        }
    }


def test_the_plan_records_a_p4_account_and_the_model_digest(
    installed: Path, tmp_path: Path
) -> None:
    model = "sha256:" + "ab" * 32
    account = {
        "unit": "one data subject with all of its records",
        "epsilon": "3",
        "delta": "0.00001",
        "accounting": "a Renyi accountant over the training steps",
    }
    # The test plugin states P5, so P4 is declared on the plan's record through a plugin that says it.
    source = GENERATING_PLUGIN.replace(
        'return {"grade": "P5", "reproducibility": self.options.get("stated", "bytes")}',
        'return {"grade": "P4", "privacy": %r, "model": {"digest": %r},'
        ' "reproducibility": "bytes"}' % (account, model),
    )
    assert source != GENERATING_PLUGIN
    site = installed
    make_distribution(site, plugin_source=source)
    bundle = plugin_bundle(tmp_path, grade="P4", privacy=account, model={"digest": model})
    document = _recorded(tmp_path, _planned(bundle, tmp_path))
    entry = document["generators"][EXPOSE]
    assert entry["grade"] == "P4" and entry["privacy"] == account
    assert entry["modelDigest"] == model and entry["reproducibility"] == "bytes"


def test_the_plan_digest_moves_with_the_environment_only_for_a_bundle_that_declares_generators(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = plugin_bundle(tmp_path)
    first = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    monkeypatch.setattr(environment, "environment_digest", lambda: "sha256:" + "1" * 64)
    second = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert first.artifacts["plan_digest"] != second.artifacts["plan_digest"]
    # A bundle that declares none records no member and is not moved by the environment.
    plain = plan(FIXTURE, state_dir=tmp_path / "plain", stage=False)
    monkeypatch.setattr(environment, "environment_digest", lambda: "sha256:" + "2" * 64)
    again = plan(FIXTURE, state_dir=tmp_path / "plain", stage=False)
    assert "generators" not in plain.artifacts["plan_document"]
    assert plain.artifacts["plan_digest"] == again.artifacts["plan_digest"]


def test_an_editable_plugin_is_recorded_as_none_whatever_the_bundle_declares(
    editable_installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path, reproducibility="bytes")
    document = _recorded(tmp_path, _planned(bundle, tmp_path))
    entry = document["generators"][EXPOSE]
    assert entry["editable"] is True and entry["reproducibility"] == "none"


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _applied(bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Dict[str, str]:
    run_id = _planned(bundle, tmp_path)
    assert main(["--json", "apply", str(bundle), *_state(tmp_path), "--run-id", run_id]) == 0
    digest = json.loads(capsys.readouterr().out)["artifacts"]["run_digest"]
    return {"run_id": run_id, "digest": digest}


def _replay(
    bundle: Path, tmp_path: Path, applied: Dict[str, str], *extra: str, digest: str = ""
) -> int:
    return main(
        [
            "replay",
            str(bundle),
            *_state(tmp_path),
            "--run-id",
            applied["run_id"],
            "--assert-digest",
            digest or applied["digest"],
            *extra,
        ]
    )


def test_replay_of_a_bytes_plugin_generates_again_and_compares(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    applied = _applied(bundle, tmp_path, capsys)
    assert _replay(bundle, tmp_path, applied, "--allow-generator", "learned") == 0
    assert "matches asserted digest" in capsys.readouterr().out
    assert _calls() == [(0, 64), (0, 64)]
    wrong = "sha256:" + "0" * 64
    assert _replay(bundle, tmp_path, applied, "--allow-generator", "learned", digest=wrong) == 1
    assert "FLX-2106" in capsys.readouterr().err


def test_replay_of_a_bytes_plugin_never_writes_into_the_stored_run(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    applied = _applied(bundle, tmp_path, capsys)
    before = _snapshot(tmp_path)
    assert any(name.endswith("plan.json") for name in before)
    assert any(name.endswith("receipt.json") for name in before)
    assert any("emits" in name for name in before)
    assert _replay(bundle, tmp_path, applied, "--allow-generator", "learned") == 0
    assert _calls() == [(0, 64), (0, 64)]
    # The lineage file carries a wall-clock time a rewrite would have moved: nothing was rewritten.
    assert _snapshot(tmp_path) == before
    assert [path.name for path in (tmp_path / "state").iterdir()] == ["runs"]


def test_replay_of_a_plugin_that_gives_other_rows_reports_the_mismatch_and_keeps_the_stored_run(
    site: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    drifting = GENERATING_PLUGIN.replace(
        "self._value(column, request.seed, row)",
        'self._value(column, request.seed + (b"drift" if len(CALLS) > 1 else b""), row)',
    )
    assert drifting != GENERATING_PLUGIN
    make_distribution(site, plugin_source=drifting)
    bundle = plugin_bundle(tmp_path)
    applied = _applied(bundle, tmp_path, capsys)
    before = _snapshot(tmp_path)
    assert _replay(bundle, tmp_path, applied, "--allow-generator", "learned") == 1
    assert _calls() == [(0, 64), (0, 64)]
    err = capsys.readouterr().err
    assert "FLX-2106" in err and applied["digest"] in err
    # Every stored file (stream, receipt, plan, run digest, stage) is byte for byte as it was, and
    # the scratch directory is gone.
    assert _snapshot(tmp_path) == before
    assert [path.name for path in (tmp_path / "state").iterdir()] == ["runs"]


def test_replay_of_an_environment_plugin_compares_where_the_environment_is_the_recorded_one(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "environment")
    applied = _applied(bundle, tmp_path, capsys)
    assert _replay(bundle, tmp_path, applied, "--allow-generator", "learned") == 0
    assert "matches asserted digest" in capsys.readouterr().out
    assert _calls() == [(0, 64), (0, 64)]


def _refused_replay(capsys: pytest.CaptureFixture[str], code: int, *needles: str) -> str:
    """A replay that could not generate again exits non-zero, and says that it verified nothing."""
    captured = capsys.readouterr()
    assert code == 1 and captured.out == ""
    assert "FLX-3123" in captured.err and "verified nothing" in captured.err
    assert "run of record" in captured.err
    for needle in needles:
        assert needle in captured.err
    return captured.err


def test_replay_of_an_environment_plugin_in_another_environment_is_refused(
    installed: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path, "environment")
    applied = _applied(bundle, tmp_path, capsys)
    monkeypatch.setattr(environment, "environment_digest", lambda: "sha256:" + "9" * 64)
    # Nothing is imported for the refusal, but the plugin is allowed: replay would generate it.
    code = _replay(bundle, tmp_path, applied, "--allow-generator", "learned")
    _refused_replay(capsys, code, EXPOSE, "environment")
    # The asserted digest is not compared with the stored run's: the right one is refused as well.
    assert _replay(bundle, tmp_path, applied, "--allow-generator", "learned") == 1
    assert _calls() == [(0, 64)]


def test_replay_of_a_none_plugin_is_refused_and_never_reports_the_stored_run(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "none")
    applied = _applied(bundle, tmp_path, capsys)
    # The right digest and the wrong one are refused alike: nothing is generated or compared.
    for digest in ("", "sha256:" + "0" * 64):
        code = _replay(bundle, tmp_path, applied, digest=digest)
        _refused_replay(capsys, code, "declared none")
    assert _calls() == [(0, 64)]


def test_replay_of_an_editable_plugin_is_refused(
    editable_installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = plugin_bundle(tmp_path, reproducibility="bytes")
    applied = _applied(bundle, tmp_path, capsys)
    code = _replay(bundle, tmp_path, applied, "--allow-generator", "learned")
    _refused_replay(capsys, code, "an editable install")
    assert _calls() == [(0, 64)]


def test_replay_of_a_plugin_run_that_was_never_applied_has_no_stored_run_to_report(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "none")
    run_id = _planned(bundle, tmp_path)
    code = _replay(bundle, tmp_path, {"run_id": run_id, "digest": "sha256:" + "0" * 64})
    assert code == 1
    assert "FLX-3123" in capsys.readouterr().err
    assert _calls() == [(0, 64)]


def test_replay_of_a_bundle_without_generators_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "run", str(FIXTURE), *_state(tmp_path)]) == 0
    artifacts = json.loads(capsys.readouterr().out)["artifacts"]
    digest = artifacts["apply"]["run_digest"]
    run_id = artifacts["apply"]["run_id"]
    applied = {"run_id": run_id, "digest": digest}
    assert _replay(FIXTURE, tmp_path, applied) == 0
    assert "matches asserted digest" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# EVIDENCE.md
# ---------------------------------------------------------------------------


def _evidence(bundle: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> str:
    edit_emit(bundle, lambda emit: emit.pop("fidelity"))  # ship needs a slice for a fidelity block
    code = main(
        ["run", str(bundle), *_state(tmp_path), "--allow-generator", "learned"]
        + ["--ship", "-o", str(tmp_path / "out")]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err + captured.out
    (report,) = (tmp_path / "state").glob("runs/*/EVIDENCE.md")
    text = report.read_text(encoding="utf-8")
    section = text.split("## Reproducibility", 1)[1].split("## Fidelity", 1)[0]
    return section


def test_the_report_says_which_exposes_were_stored_and_not_regenerated(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "environment")
    section = _evidence(bundle, tmp_path, capsys)
    assert "Stored, not regenerated" in section
    assert (
        f"- `{EXPOSE}`: plugin `learned` from `fxgen-plugin` `1.0`, recorded `environment`"
        in section
    )
    assert "editable" not in section
    assert DIGEST.search(section)


def test_the_report_says_why_an_editable_install_is_recorded_none(
    editable_installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path)
    section = _evidence(bundle, tmp_path, capsys)
    assert f"- `{EXPOSE}`: plugin `learned` from `fxgen-plugin` `1.0`, recorded `none`" in section
    assert "editable install" in section and "source tree" in section
    assert "whatever the bundle declares" in section


def test_the_report_of_a_bundle_without_generators_keeps_its_reproducibility_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    demo = tmp_path / "demo"
    scaffold(demo)
    code = main(["run", str(demo), *_state(tmp_path), "--ship", "-o", str(tmp_path / "out")])
    assert code == 0, capsys.readouterr().err
    (report,) = (tmp_path / "state").glob("runs/*/EVIDENCE.md")
    text = report.read_text(encoding="utf-8")
    assert "## Reproducibility\n\nreproducibility: not-checked\n\n## Fidelity" in text


# ---------------------------------------------------------------------------
# A plan.json that was changed is not trusted: the bundle's declaration decides
# ---------------------------------------------------------------------------


def _forged(tmp_path: Path, run_id: str, change: Callable[[Dict[str, Any]], None]) -> str:
    """Rewrite the plan of *run_id*, digest it again and rename the run: what a writer of the state does.

    The run directory is copied whole under the new name, so the copy holds the old receipt and
    stage, as a person who can write the directory could leave them.
    """
    state = tmp_path / "state"
    old = RunLayout.for_state_dir(state, run_id)
    document = json.loads(old.plan_path.read_text(encoding="utf-8"))
    change(document)
    document["planDigest"] = build_plan_digest(document).ref.value
    new = RunLayout.for_state_dir(state, run_id_for(document["planDigest"]))
    shutil.copytree(old.run_dir, new.run_dir)
    new.plan_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return new.run_id


def _without_generators(document: Dict[str, Any]) -> None:
    del document["generators"]


def _forget_plugin() -> None:
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]


@pytest.mark.parametrize(
    "extra",
    [[], ["--shard", "1/2"], ["--strict-coverage"]],
    ids=["whole", "shard", "strict-coverage"],
)
def test_apply_of_a_plan_whose_generators_member_was_deleted_is_refused_before_a_plugin_loads(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: List[str]
) -> None:
    bundle = plugin_bundle(tmp_path)
    forged = _forged(tmp_path, _planned(bundle, tmp_path), _without_generators)
    _forget_plugin()
    capsys.readouterr()
    args = ["apply", str(bundle), *_state(tmp_path), "--run-id", forged]
    assert main([*args, "--allow-generator", "learned", *extra]) == 1
    err = capsys.readouterr().err
    assert "FLX-3123" in err and "not the ones its bundle declares" in err
    assert f"{PACKAGE}.plugin" not in sys.modules
    layout = RunLayout.for_state_dir(tmp_path / "state", forged)
    assert not layout.receipt_path.exists() and not list(layout.run_dir.glob("*shard*"))


@pytest.mark.parametrize(
    "change",
    [
        lambda document: document["generators"][EXPOSE].update(plugin="other"),
        lambda document: document["generators"][EXPOSE].update(version="9.9"),
        lambda document: document["generators"][EXPOSE].update(distribution="another-plugin"),
        lambda document: document["generators"][EXPOSE].update(grade="P4"),
        lambda document: document["generators"][EXPOSE].update(modelDigest="sha256:" + "a" * 64),
        lambda document: document["generators"][EXPOSE].update(privacy={"unit": "x"}),
        lambda document: document["generators"].update(extra=document["generators"][EXPOSE]),
        lambda document: document.update(generators=[]),
    ],
    ids=["plugin", "version", "distribution", "grade", "model", "account", "exposes", "shape"],
)
def test_apply_of_a_plan_whose_record_differs_from_the_declaration_is_refused(
    installed: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Dict[str, Any]], None],
) -> None:
    bundle = plugin_bundle(tmp_path)
    forged = _forged(tmp_path, _planned(bundle, tmp_path), change)
    _forget_plugin()
    capsys.readouterr()
    args = ["apply", str(bundle), *_state(tmp_path), "--run-id", forged, "--allow-generator"]
    assert main([*args, "learned"]) == 1
    err = capsys.readouterr().err
    assert "FLX-3123" in err and "other" not in err.replace("another", "")
    assert f"{PACKAGE}.plugin" not in sys.modules


def test_replay_of_a_plan_whose_reproducibility_was_set_to_none_generates_again(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bundle says ``bytes``, and a forged ``none`` cannot make the stored receipt the answer."""
    bundle = plugin_bundle(tmp_path)
    applied = _applied(bundle, tmp_path, capsys)

    def to_none(document: Dict[str, Any]) -> None:
        document["generators"][EXPOSE]["reproducibility"] = "none"

    forged = {"run_id": _forged(tmp_path, applied["run_id"], to_none), "digest": applied["digest"]}
    code = _replay(bundle, tmp_path, forged, "--allow-generator", "learned")
    captured = capsys.readouterr()
    assert "did not generate the rows again" not in captured.out + captured.err
    assert _calls() == [(0, 64), (0, 64)]  # it generated again, and compared that
    assert code == 1 and "FLX-2106" in captured.err  # the run digest is the forged plan's own


def test_replay_of_a_plan_whose_environment_digest_was_forged_is_refused(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, "environment")
    applied = _applied(bundle, tmp_path, capsys)
    for forged_digest in ("sha256:" + "9" * 64, "sha256:short", 7):

        def forge(document: Dict[str, Any], value: Any = forged_digest) -> None:
            document["generators"][EXPOSE]["environmentDigest"] = value

        run_id = _forged(tmp_path, applied["run_id"], forge)
        code = _replay(
            bundle, tmp_path, {**applied, "run_id": run_id}, "--allow-generator", "learned"
        )
        captured = capsys.readouterr()
        assert code == 1 and captured.out == "" and "FLX-3123" in captured.err
    assert _calls() == [(0, 64)]


def test_replay_of_a_bundle_that_declares_no_plugin_refuses_a_plan_that_records_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "run", str(FIXTURE), *_state(tmp_path)]) == 0
    artifacts = json.loads(capsys.readouterr().out)["artifacts"]["apply"]

    def record_one(document: Dict[str, Any]) -> None:
        document["generators"] = {
            EXPOSE: {
                "plugin": "learned",
                "distribution": "fxgen-plugin",
                "version": "1.0",
                "editable": False,
                "grade": "P5",
                "reproducibility": "none",
                "environmentDigest": environment.environment_digest(),
            }
        }

    forged = _forged(tmp_path, artifacts["run_id"], record_one)
    code = _replay(FIXTURE, tmp_path, {"run_id": forged, "digest": artifacts["run_digest"]})
    captured = capsys.readouterr()
    assert code == 1 and captured.out == "" and "FLX-3123" in captured.err


def test_the_expose_ids_a_refused_replay_names_are_escaped() -> None:
    error = import_module("flux_engine.stages.replay")._cannot_generate(
        "0123456789abcdef", [("x\x1b[2Jy", "declared none"), ("a\nb", "an editable install")]
    )
    text = str(error)
    assert "\x1b" not in text and "\n" not in text
    assert "'x\\x1b[2Jy'" in text and "'a\\nb'" in text


def _declared(bundle: Path) -> Any:
    from flux_engine.stages.apply import load_recorded_plan

    (layout,) = [
        RunLayout.for_state_dir(bundle.parent / "state", p.name)
        for p in (bundle.parent / "state" / "runs").iterdir()
    ]
    document = load_recorded_plan(layout)
    return document, declared_for_run(bundle, document, plan_kernel(document), None)


@pytest.mark.parametrize(
    "change",
    [
        lambda entry: entry.update(reproducibility="bytes\n## forged"),
        lambda entry: entry.update(reproducibility="sometimes"),
        lambda entry: entry.update(reproducibility=None),
        lambda entry: entry.update(reproducibility="none"),  # the bundle declares bytes
        lambda entry: entry.update(environmentDigest="sha256:" + "A" * 64),
        lambda entry: entry.update(environmentDigest="sha256:abc"),
        lambda entry: entry.update(environmentDigest="`x`\n## forged"),
        lambda entry: entry.pop("environmentDigest"),
        lambda entry: entry.update(editable="yes"),
        lambda entry: entry.update(editable=True),  # an editable install is recorded none
    ],
    ids=[
        "newline",
        "unknown",
        "null",
        "none-for-bytes",
        "uppercase-digest",
        "short-digest",
        "markup-digest",
        "no-digest",
        "editable-not-bool",
        "editable-not-none",
    ],
)
def test_ship_refuses_a_recorded_reproducibility_or_digest_that_is_malformed_or_not_the_declared_one(
    installed: Path, tmp_path: Path, change: Callable[[Dict[str, Any]], None]
) -> None:
    bundle = plugin_bundle(tmp_path)
    _planned(bundle, tmp_path)
    document, declared = _declared(bundle)
    assert [item.expose_id for item in stored_generators(declared, document)] == [EXPOSE]
    change(document["generators"][EXPOSE])
    with pytest.raises(FluxError) as caught:
        stored_generators(declared, document)
    assert caught.value.code.code == "FLX-3123"
    assert "forged" not in str(caught.value) and "sometimes" not in str(caught.value)


def test_the_report_writes_every_value_of_a_stored_expose_escaped() -> None:
    hostile = StoredGenerator(
        expose_id="a|b",
        plugin="x`y",
        distribution="d\n## Forged",
        version="1\x1b[2J",
        editable=False,
        reproducibility="bytes\n## Forged",
        environment_digest="sha256:\n## Forged",
    )
    section = render_reproducibility_section(Reproducibility.NOT_CHECKED, [hostile])
    assert "## Forged" not in section and "\x1b" not in section
    assert "a\\|b" in section
    assert "an unknown reproducibility" in section and "an unreadable digest" in section


# ---------------------------------------------------------------------------
# `plan` and `apply --run-id` of a committed run, and of a bundle edited after it
# ---------------------------------------------------------------------------


def test_plan_of_a_committed_plugin_run_asks_the_plugin_for_nothing_and_writes_nothing(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    before = _snapshot(tmp_path)
    plan_bytes = layout.plan_path.read_bytes()
    assert _calls() == [(0, 64)]

    code = main(["plan", str(bundle), *_state(tmp_path), "--allow-generator", "learned"])
    out = capsys.readouterr().out
    assert code == 0
    assert f"run {run_id} keeps its stored plugin output" in out and "another seed" in out
    assert _calls() == [(0, 64)]
    assert layout.plan_path.read_bytes() == plan_bytes
    assert _snapshot(tmp_path) == before


def test_apply_run_id_of_a_bundle_edited_after_its_run_is_refused_not_kept(
    drifting: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, run_id, layout = _applied_none(tmp_path, capsys)
    args = ["apply", str(bundle), *_state(tmp_path), "--run-id", run_id]
    before = _snapshot(tmp_path)
    assert main(args) == 0  # the unedited control keeps the run
    assert "keeps its stored plugin output" in capsys.readouterr().out

    path = bundle / "simulation.flux.yml"
    path.write_text(path.read_text(encoding="utf-8") + "\n# a note added after the run\n")
    assert main(args) == 1
    err = capsys.readouterr().err
    assert "FLX-2101" in err and run_id in err and "changed after the run was made" in err
    assert _calls() == [(0, 64)]
    assert _snapshot(tmp_path) == before
