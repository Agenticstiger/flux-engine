# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux_engine.describe.self_describe() -- component identity and capability derivation."""

from __future__ import annotations

import pytest

from flux_engine.describe import self_describe


@pytest.mark.unit
def test_component_name_is_exact() -> None:
    result = self_describe()
    assert result["component"] == "FLUX Simulation Engine"


@pytest.mark.unit
def test_agentic_capability_is_false_without_the_extra() -> None:
    result = self_describe()
    assert result["capabilities"]["agentic"] is False


@pytest.mark.unit
def test_capabilities_are_all_booleans() -> None:
    result = self_describe()
    assert result["capabilities"], "capabilities dict must not be empty"
    assert all(isinstance(value, bool) for value in result["capabilities"].values())


@pytest.mark.unit
def test_stable_top_level_keys() -> None:
    """``commands`` (the full argparse command tree, so a consumer can
    generate a UI dynamically) is a stable top-level key alongside the six
    identity fields.
    """
    result = self_describe()
    assert set(result.keys()) == {
        "component",
        "version",
        "python_version",
        "spec_source",
        "spec_version",
        "capabilities",
        "commands",
    }


@pytest.mark.unit
def test_commands_tree_has_top_level_subcommands() -> None:
    result = self_describe()
    commands = result["commands"]
    assert "options" in commands and "subcommands" in commands
    subcommands = commands["subcommands"]
    for name in ("validate", "plan", "apply", "run", "verify", "describe", "explain"):
        assert name in subcommands


@pytest.mark.unit
def test_detect_capabilities_treats_a_broken_probe_as_absent(monkeypatch) -> None:
    """A find_spec() that raises (a malformed module name, a broken finder)
    must degrade to "capability absent", never propagate and crash describe.
    """
    import flux_engine.describe as describe_module

    def _broken_find_spec(name: str):
        raise ValueError(f"malformed module name: {name!r}")

    monkeypatch.setattr(describe_module.importlib.util, "find_spec", _broken_find_spec)

    result = describe_module._detect_capabilities()

    # Only the optional-backend probes degrade; the engine's own
    # capabilities are not probed at all, so a broken finder cannot make
    # this installation report that it lost them.
    assert all(result[name] is False for name in describe_module._CAPABILITY_MODULES)
    assert all(result[name] is True for name in describe_module._ENGINE_CAPABILITIES)


# --- capabilities are the engine's, not a third party's --------------------


@pytest.mark.unit
def test_the_engines_own_capabilities_are_reported_true() -> None:
    """``describe`` used to report ``capabilities: (none enabled)``.

    Two of the four names were probing for the wrong thing: ``lineage``
    probed for the ``openlineage`` distribution while every run writes
    OpenLineage RunEvents itself, and ``calibrate`` probed for ``pyabc``
    while ``fluxsim calibrate`` is a shipped command. A host application
    consuming this document -- its stated purpose -- would have hidden two
    working features.
    """
    capabilities = self_describe()["capabilities"]
    for name in ("accuracy", "calibrate", "evidence", "expr", "fidelity", "gate", "lineage"):
        assert capabilities[name] is True, name


@pytest.mark.unit
def test_every_subcommand_that_names_a_capability_is_reported_true() -> None:
    described = self_describe()
    capabilities = described["capabilities"]
    subcommands = set(described["commands"]["subcommands"])
    for name in subcommands & set(capabilities):
        assert capabilities[name] is True, name


@pytest.mark.unit
def test_optional_backends_are_still_probed_and_named_for_the_backend() -> None:
    """An absent backend must not read as an absent feature."""
    capabilities = self_describe()["capabilities"]
    for name in ("agentic", "calibrate_abc", "lineage_transport"):
        assert isinstance(capabilities[name], bool)


@pytest.mark.unit
def test_record_level_synthesis_is_not_offered_as_a_capability() -> None:
    """ADR 0003 puts synthesis from real records out of scope, not merely unfinished.

    describe used to probe synthcity for a ``synth`` capability that nothing in
    the engine could use, so an installed synthcity read as a feature.
    """
    assert "synth" not in self_describe()["capabilities"]


# ── the spec version is read, not hardcoded ────────────────────────────


@pytest.mark.unit
def test_spec_version_comes_from_the_installed_distribution_when_it_is_the_source() -> None:
    """A constant in this module reported 0.5.0 whatever was installed.

    ``fluxsim describe --self --json`` is the document a host reads to decide
    what this installation can do, so an environment holding a newer
    ``flux-spec`` than the vendored fallback would have been told the
    vendored number.
    """
    from importlib.metadata import PackageNotFoundError, version

    from flux_engine.describe import VENDORED_SPEC_VERSION, installed_spec_version
    from flux_engine.spec import SOURCE

    reported = installed_spec_version()
    assert self_describe()["spec_version"] == reported
    if SOURCE == "flux_spec":
        try:
            assert reported == version("flux-spec")
        except PackageNotFoundError:  # pragma: no cover - the shim found it, metadata did not
            assert reported == VENDORED_SPEC_VERSION
    else:
        assert reported == VENDORED_SPEC_VERSION


@pytest.mark.unit
def test_the_vendored_fallback_reports_the_vendored_version(
    monkeypatch: "pytest.MonkeyPatch",
) -> None:
    import flux_engine.describe as describe_module

    monkeypatch.setattr(describe_module, "SOURCE", "vendored")
    assert describe_module.installed_spec_version() == describe_module.VENDORED_SPEC_VERSION


@pytest.mark.unit
def test_the_command_tree_is_built_from_a_public_parser_accessor() -> None:
    """The published command tree hung off a leading-underscore import.

    A name with no stability contract is not a source for the document a
    host renders its UI from.
    """
    import flux_engine.cli as cli

    assert "build_parser" in cli.__all__
    assert self_describe()["commands"]["subcommands"], "the tree must not be empty"
    assert "error" not in self_describe()["commands"]
