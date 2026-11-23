# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A declared generator plugin loads only when the person allows it, and states what it declares.

ADR 0032 section 3, steps 3 to 7: ``--allow-generator NAME`` on every command that would import a
plugin, the resolution with the bundle, state and current directories barred, the load, and the
statement that must equal the declaration. Every refusal comes before the plugin's code runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern
from flux_engine.generators import loading, registry
from flux_engine.generators.errors import FLX_3119
from flux_engine.generators.request import ColumnRequest, Request
from flux_engine.seam.generators import GeneratorDeclaration, PrivacyAccount
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from flux_engine.stages.run import run

from .conftest import EXPOSE, GENERATING_PLUGIN, PACKAGE, make_distribution, plugin_bundle

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "ab" * 32


def _nothing_written(tmp_path: Path, state: str = "state") -> bool:
    return not list((tmp_path / state).glob("runs/*"))


# ---------------------------------------------------------------------------
# Step 3: the person's word
# ---------------------------------------------------------------------------


def test_plan_without_the_flag_is_refused_before_any_import(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    outcome = plan(bundle, state_dir=tmp_path / "state")
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3119")
    assert "--allow-generator learned" in outcome.stderr
    assert PACKAGE not in sys.modules and f"{PACKAGE}.plugin" not in sys.modules
    assert _nothing_written(tmp_path)


def test_the_refusal_says_only_the_person_decides_and_names_plain_plugins(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("other", "third"))
    assert outcome.error_code == "FLX-3119"
    assert "declares the generator plugin(s) learned" in outcome.stderr
    assert "allows other, third" in outcome.stderr
    assert "[" not in outcome.stderr.splitlines()[0] and "'" not in outcome.stderr.splitlines()[0]
    assert "only the person running fluxsim decides" in outcome.stderr
    assert "stops and asks that person, naming the plugin" in outcome.stderr
    assert "an agent never adds it on its own judgement" in outcome.stderr
    assert "to their own command" in outcome.stderr and "(--allow-generator" not in outcome.stderr
    assert "have reviewed" not in outcome.stderr
    entry = FLX_3119.explanation
    assert "only the person running fluxsim decides" in entry
    assert "never adds --allow-generator on its own judgement" in entry
    assert "Add the flag" not in entry


def test_a_flag_that_names_another_plugin_does_not_allow_this_one(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("other",))
    assert outcome.error_code == "FLX-3119"
    assert "allows other;" in outcome.stderr and "--allow-generator learned" in outcome.stderr
    assert PACKAGE not in sys.modules


def test_run_and_apply_without_the_flag_are_refused_before_any_import(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    ran = run(bundle, state_dir=tmp_path / "state")
    assert (ran.exit_code, ran.error_code) == (1, "FLX-3119")
    applied = apply(bundle, state_dir=tmp_path / "state2")
    assert (applied.exit_code, applied.error_code) == (1, "FLX-3119")
    assert PACKAGE not in sys.modules
    assert _nothing_written(tmp_path) and _nothing_written(tmp_path, "state2")


def test_replay_without_the_flag_is_refused_before_any_import(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path)
    planned = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert planned.exit_code == 0, planned.stderr
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]
    replayed = replay(
        bundle,
        run_id=planned.artifacts["run_id"],
        assert_digest="sha256:" + "0" * 64,
        state_dir=tmp_path / "state",
    )
    assert (replayed.exit_code, replayed.error_code) == (1, "FLX-3119")
    assert PACKAGE not in sys.modules


def test_apply_commits_a_stage_without_the_flag_and_imports_nothing(
    installed: Path, tmp_path: Path
) -> None:
    """The flag is for a command that generates rows: committing a staged plan generates none."""
    bundle = plugin_bundle(tmp_path)
    planned = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert planned.exit_code == 0, planned.stderr
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]
    applied = apply(bundle, run_id=planned.artifacts["run_id"], state_dir=tmp_path / "state")
    assert applied.exit_code == 0, applied.stderr
    assert PACKAGE not in sys.modules


def test_the_cli_takes_the_flag_on_plan_run_apply_and_replay(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = str(plugin_bundle(tmp_path))
    state = ["--state-dir", str(tmp_path / "state")]
    assert main(["plan", bundle, *state]) == 1
    assert "FLX-3119" in capsys.readouterr().err
    assert main(["plan", bundle, *state, "--allow-generator", "learned"]) == 0
    capsys.readouterr()
    assert main(["run", bundle, "--state-dir", str(tmp_path / "s2")]) == 1
    assert "FLX-3119" in capsys.readouterr().err
    assert (
        main(["run", bundle, "--state-dir", str(tmp_path / "s2"), "--allow-generator", "learned"])
        == 0
    )
    capsys.readouterr()
    assert main(["apply", bundle, "--state-dir", str(tmp_path / "s3")]) == 1
    assert "FLX-3119" in capsys.readouterr().err
    applied = ["--json", "apply", bundle, "--state-dir", str(tmp_path / "s3")]
    assert main([*applied, "--allow-generator", "learned"]) == 0
    artifacts = json.loads(capsys.readouterr().out)["artifacts"]
    run_id, run_digest = artifacts["run_id"], artifacts["run_digest"]
    replay_args = ["replay", bundle, "--run-id", run_id, "--assert-digest", run_digest]
    assert main([*replay_args, "--state-dir", str(tmp_path / "s3")]) == 1
    assert "FLX-3119" in capsys.readouterr().err
    assert (
        main([*replay_args, "--state-dir", str(tmp_path / "s3"), "--allow-generator", "learned"])
        == 0
    )


def test_the_flag_takes_a_name_and_never_a_path(
    installed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = str(plugin_bundle(tmp_path))
    with pytest.raises(SystemExit) as refused:
        main(["plan", bundle, "--allow-generator", "../learned"])
    assert refused.value.code == 2
    assert "generator entry point" in capsys.readouterr().err
    assert PACKAGE not in sys.modules


def test_a_bundle_that_declares_no_plugin_needs_no_flag(tmp_path: Path) -> None:
    import shutil

    from .conftest import FIXTURE

    bundle = tmp_path / "plain"
    shutil.copytree(FIXTURE, bundle)
    assert plan(bundle, state_dir=tmp_path / "state").exit_code == 0
    assert plan(bundle, state_dir=tmp_path / "s2", allow_generators=("unused",)).exit_code == 0


# ---------------------------------------------------------------------------
# Steps 4 to 6: only a declared entry point, and its code where it is installed
# ---------------------------------------------------------------------------


def test_a_plugin_that_is_not_installed_is_refused_naming_what_is(tmp_path: Path) -> None:
    bundle = plugin_bundle(tmp_path)
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3114")
    assert PACKAGE not in sys.modules


def test_the_resolution_bars_the_bundle_the_state_directory_and_the_current_directory(
    installed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: Dict[str, Any] = {}
    real = registry.resolve

    def resolve(*args: Any, **kwargs: Any) -> Any:
        seen["roots"] = list(kwargs["forbidden_roots"])
        return real(*args, **kwargs)

    monkeypatch.setattr(registry, "resolve", resolve)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    bundle = plugin_bundle(tmp_path)
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert outcome.exit_code == 0, outcome.stderr
    assert seen["roots"] == [
        bundle.resolve(),
        (tmp_path / "state").resolve(),
        elsewhere.resolve(),
    ]


def test_a_plugin_under_the_bundle_directory_is_refused_unimported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module beside the bundle that ``python -m`` would find first is not an installed plugin."""
    bundle = plugin_bundle(tmp_path)
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(registry, "installation_directories", lambda: (site.resolve(),))
    make_distribution(site, plugin_source=GENERATING_PLUGIN, package_dir=bundle / PACKAGE)
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3117")
    assert PACKAGE not in sys.modules


def test_an_allowed_plugin_loads_and_the_loaded_plugin_is_the_factory_s(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path, options={"temperature": "1"})
    result = pipeline.execute(bundle, allow_generators=["learned"], state_dir=tmp_path / "state")
    loaded = result.generators[EXPOSE]
    assert type(loaded.plugin).__name__ == "Generator"
    assert loaded.plugin.options == {"temperature": "1"}
    assert loaded.editable is False
    assert (loaded.resolved.distribution, loaded.resolved.version) == ("fxgen-plugin", "1.0")
    assert f"{PACKAGE}.plugin" in sys.modules


def test_a_bundle_that_declares_no_plugin_loads_none(tmp_path: Path) -> None:
    from .conftest import FIXTURE

    assert pipeline.execute(FIXTURE).generators == {}


def test_an_editable_plugin_loads_under_the_flag_and_keeps_its_editable_flag(
    site: Path, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    make_distribution(
        site, plugin_source=GENERATING_PLUGIN, package_dir=source / PACKAGE, editable=True
    )
    import importlib

    sys.path.insert(0, str(source))
    try:
        importlib.invalidate_caches()
        bundle = plugin_bundle(tmp_path)
        result = pipeline.execute(
            bundle, allow_generators=["learned"], state_dir=tmp_path / "state"
        )
    finally:
        sys.path.remove(str(source))
    assert result.generators[EXPOSE].editable is True


# ---------------------------------------------------------------------------
# Step 7: the statement must equal the declaration
# ---------------------------------------------------------------------------


def test_a_plugin_that_states_another_reproducibility_is_refused_before_a_row(
    installed: Path, tmp_path: Path
) -> None:
    bundle = plugin_bundle(tmp_path, options={"stated": "environment"})
    outcome = plan(bundle, state_dir=tmp_path / "state", allow_generators=("learned",))
    assert (outcome.exit_code, outcome.error_code) == (1, "FLX-3120")
    assert "reproducibility (declared 'bytes', stated 'environment')" in outcome.stderr
    assert _nothing_written(tmp_path)


class _Stub:
    """A plugin object that states what it is given, so no module is imported."""

    def __init__(self, stated: Any) -> None:
        self.stated = stated

    def statement(self, request: Request) -> Any:
        return self.stated


def _declaration(grade: Pattern = Pattern.P4, **changes: Any) -> GeneratorDeclaration:
    fields: Dict[str, Any] = {
        "expose_id": EXPOSE,
        "plugin": "learned",
        "distribution": "fxgen-plugin",
        "version": "1.0",
        "grade": grade,
        "reproducibility": "bytes",
    }
    if grade is Pattern.P4:
        fields["privacy"] = PrivacyAccount(
            unit="one data subject with all of its records",
            accounting="a Renyi accountant",
            epsilon="3",
            delta="0.00001",
        )
        fields["model_digest"] = DIGEST
    fields.update(changes)
    return GeneratorDeclaration(**fields)


def _loaded(declaration: GeneratorDeclaration, stated: Any) -> loading.LoadedGenerator:
    resolved = registry.ResolvedGenerator(
        name="learned",
        value=f"{PACKAGE}.plugin:factory",
        distribution="fxgen-plugin",
        version="1.0",
        editable=False,
        install_root=Path("/"),
        package_root=None,
    )
    return loading.LoadedGenerator(declaration, resolved, _Stub(stated))


def _request() -> Request:
    return Request(
        expose_id=EXPOSE,
        columns=(ColumnRequest("amount", "DECIMAL(18,4)", True),),
        n=4,
        start=0,
        stop=4,
        seed=b"\x00" * 32,
    )


def _p4_statement() -> Dict[str, Any]:
    return loading.declared_statement(_declaration())


def test_a_statement_that_repeats_the_declaration_is_accepted() -> None:
    loading.check_statement(_loaded(_declaration(), _p4_statement()), _request())
    loading.check_statement(
        _loaded(_declaration(Pattern.P5), {"grade": "P5", "reproducibility": "bytes"}), _request()
    )


def _changed(path: str, value: Any) -> Dict[str, Any]:
    statement = _p4_statement()
    target: Any = statement
    *parents, last = path.split(".")
    for part in parents:
        target = target[part]
    target[last] = value
    return statement


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("grade", "P5"),
        ("privacy.unit", "one record"),
        ("privacy.accounting", "an unrelated accountant"),
        ("privacy.epsilon", "4"),
        ("privacy.delta", "0.0001"),
        ("model.digest", "sha256:" + "cd" * 32),
        ("reproducibility", "none"),
    ],
)
def test_a_statement_that_differs_in_one_field_is_refused_naming_it(path: str, value: Any) -> None:
    with pytest.raises(FluxError) as refused:
        loading.check_statement(_loaded(_declaration(), _changed(path, value)), _request())
    assert refused.value.code.code == "FLX-3120"
    assert f"{path} (declared" in str(refused.value)


def test_a_statement_in_which_the_loss_is_stated_as_rho_instead_is_refused() -> None:
    stated = _p4_statement()
    del stated["privacy"]["epsilon"], stated["privacy"]["delta"]
    stated["privacy"]["rho"] = "1"
    with pytest.raises(FluxError) as refused:
        loading.check_statement(_loaded(_declaration(), stated), _request())
    message = str(refused.value)
    assert "privacy.epsilon (declared '3', not stated)" in message
    assert "privacy.rho (stated '1', not declared)" in message


@pytest.mark.parametrize("part", ["privacy", "model", "reproducibility", "grade"])
def test_a_statement_that_leaves_out_a_declared_part_is_refused(part: str) -> None:
    stated = _p4_statement()
    del stated[part]
    with pytest.raises(FluxError) as refused:
        loading.check_statement(_loaded(_declaration(), stated), _request())
    assert refused.value.code.code == "FLX-3120" and "not stated" in str(refused.value)


def test_a_p5_plugin_may_state_no_account() -> None:
    stated = {"grade": "P5", "reproducibility": "bytes", "privacy": {"unit": "a record"}}
    with pytest.raises(FluxError) as refused:
        loading.check_statement(_loaded(_declaration(Pattern.P5), stated), _request())
    assert "privacy.unit (stated 'a record', not declared)" in str(refused.value)


@pytest.mark.parametrize("stated", [None, "P4", ["P4"], 4])
def test_a_statement_that_is_not_a_mapping_is_refused(stated: Any) -> None:
    with pytest.raises(FluxError) as refused:
        loading.check_statement(_loaded(_declaration(), stated), _request())
    assert refused.value.code.code == "FLX-3120" and "not a mapping" in str(refused.value)


def test_a_statement_that_raises_is_refused_by_name_and_never_quotes_the_error() -> None:
    class Raises:
        def statement(self, request: Request) -> Any:
            raise RuntimeError("secret detail")

    loaded = _loaded(_declaration(), {})
    loaded = loading.LoadedGenerator(loaded.declaration, loaded.resolved, Raises())
    with pytest.raises(FluxError) as refused:
        loading.check_statement(loaded, _request())
    assert refused.value.code.code == "FLX-3120"
    assert "RuntimeError" in str(refused.value) and "secret detail" not in str(refused.value)


def test_a_stated_loss_of_another_class_is_a_difference() -> None:
    from decimal import Decimal

    with pytest.raises(FluxError):
        loading.check_statement(_loaded(_declaration(), _changed("privacy.epsilon", 3)), _request())
    with pytest.raises(FluxError):
        loading.check_statement(
            _loaded(_declaration(), _changed("privacy.epsilon", Decimal(3))), _request()
        )


def test_the_statement_is_given_the_request_for_the_whole_expose(
    installed: Path, tmp_path: Path
) -> None:
    seen: List[Request] = []
    real = loading.check_statement

    def spy(loaded: loading.LoadedGenerator, request: Request) -> None:
        seen.append(request)
        real(loaded, request)

    import flux_engine.stages.pipeline as pipeline_module

    original = pipeline_module.check_statement
    pipeline_module.check_statement = spy  # type: ignore[assignment]
    try:
        bundle = plugin_bundle(tmp_path, options={"k": "v"})
        result = pipeline.execute(bundle, allow_generators=["learned"])
    finally:
        pipeline_module.check_statement = original  # type: ignore[assignment]
    (request,) = seen
    assert result.generators and request.expose_id == EXPOSE
    assert (request.n, request.start, request.stop) == (64, 0, 64)
    assert [column.name for column in request.columns] == ["aborted_at", "amount", "market"]
    assert request.columns[0] == ColumnRequest(
        "aborted_at",
        "TIMESTAMP",
        False,
        {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"}},
    )
    assert request.columns[1].nullable is True
    assert request.columns[2].constraints == {"levels": ["IE", "GB", "FR", "DE"]}
    assert len(request.seed) == 32 and request.options == {"k": "v"}


def test_the_options_a_plugin_is_given_are_a_copy(installed: Path, tmp_path: Path) -> None:
    bundle = plugin_bundle(tmp_path, options={"nested": {"a": ["x"]}})
    result = pipeline.execute(bundle, allow_generators=["learned"])
    loaded = result.generators[EXPOSE]
    loaded.plugin.options["nested"]["a"].append("changed")
    assert loaded.declaration.options == {"nested": {"a": ["x"]}}


def test_forbidden_roots_default_to_the_dot_flux_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    roots: Optional[List[Path]] = loading.forbidden_roots(tmp_path / "bundle", None)
    assert roots == [
        (tmp_path / "bundle").resolve(),
        (tmp_path / ".flux").resolve(),
        tmp_path.resolve(),
    ]


def test_a_plugin_label_quotes_a_distribution_name_with_control_characters() -> None:
    resolved = registry.ResolvedGenerator(
        name="learned",
        value=f"{PACKAGE}.plugin:factory",
        distribution="fxgen\x1b[2J",
        version="1.0\nFLX-0000: forged‮",
        editable=False,
        install_root=Path("/"),
        package_root=None,
    )
    loaded = loading.LoadedGenerator(_declaration(), resolved, _Stub({"grade": "P4"}))
    for text in (loaded.name, _refusal_of(loaded)):
        assert "\x1b" not in text and "‮" not in text and "\n" not in text
    assert "forged" in loaded.name


def _refusal_of(loaded: loading.LoadedGenerator) -> str:
    with pytest.raises(FluxError) as caught:
        loading.check_statement(loaded, _request())
    return str(caught.value)
