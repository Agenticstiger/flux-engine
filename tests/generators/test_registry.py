# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The generator registry: listing without importing, resolving one entry point, loading it."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generators import registry

from .conftest import PACKAGE, make_distribution

pytestmark = pytest.mark.unit

DIST = "fxgen-plugin"


def code_of(caught: pytest.ExceptionInfo[FluxError]) -> str:
    return caught.value.code.code


# ---------------------------------------------------------------------------
# available(): everything is listed, nothing is imported
# ---------------------------------------------------------------------------


def test_available_lists_an_entry_point_without_importing_it(site: Path) -> None:
    make_distribution(site)
    found = [info for info in registry.available() if info.name == "learned"]
    assert len(found) == 1
    info = found[0]
    assert info.value == f"{PACKAGE}.plugin:factory"
    assert (info.distribution, info.version) == (DIST, "1.0")
    assert (info.editable, info.outside) == (False, False)
    assert PACKAGE not in sys.modules
    assert f"{PACKAGE}.plugin" not in sys.modules


def test_available_is_sorted_by_name(site: Path) -> None:
    make_distribution(site, entry="zeta")
    make_distribution(site, dist="fxgen-other", entry="alpha")
    names = [info.name for info in registry.available()]
    assert names == sorted(names)
    assert {"alpha", "zeta"} <= set(names)


def test_available_marks_an_editable_distribution(site: Path) -> None:
    make_distribution(site, editable=True)
    (info,) = [info for info in registry.available() if info.name == "learned"]
    assert info.editable is True
    assert info.as_document()["editable"] is True


def test_available_marks_metadata_outside_every_installation_directory(
    site: Path, elsewhere: Path
) -> None:
    make_distribution(elsewhere, entry="stray")
    (info,) = [info for info in registry.available() if info.name == "stray"]
    assert info.outside is True
    assert info.editable is False
    assert PACKAGE not in sys.modules


def test_the_installation_directories_are_the_interpreters_own() -> None:
    import sysconfig

    directories = registry.installation_directories()
    assert Path(sysconfig.get_paths()["purelib"]).resolve() in directories
    assert Path(sysconfig.get_paths()["platlib"]).resolve() in directories


# ---------------------------------------------------------------------------
# resolve(): exactly one entry point, the declared distribution, installed where its code is
# ---------------------------------------------------------------------------


def test_resolve_returns_the_one_installed_entry_point(site: Path) -> None:
    make_distribution(site)
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert resolved.name == "learned"
    assert resolved.value == f"{PACKAGE}.plugin:factory"
    assert (resolved.distribution, resolved.version) == (DIST, "1.0")
    assert resolved.editable is False
    assert resolved.install_root == site.resolve()
    assert resolved.package_root == (site / PACKAGE).resolve()
    assert (site / PACKAGE / "plugin.py").resolve() in resolved.record
    assert PACKAGE not in sys.modules


def test_resolve_normalizes_the_declared_distribution_name(site: Path) -> None:
    make_distribution(site, dist="Fxgen_Plugin")
    resolved = registry.resolve("learned", "fxgen.plugin", "1.0", forbidden_roots=[])
    assert resolved.distribution == "Fxgen_Plugin"


def test_a_name_that_is_not_an_entry_point_name_is_refused(site: Path) -> None:
    make_distribution(site)
    for name in ("", "9lives", "pkg.plugin:factory", "a b", "_x", "../x"):
        with pytest.raises(FluxError) as caught:
            registry.resolve(name, DIST, "1.0", forbidden_roots=[])
        assert code_of(caught) == "FLX-3114"
    assert PACKAGE not in sys.modules


def test_an_unknown_name_is_refused_and_lists_what_is_installed(site: Path) -> None:
    make_distribution(site)
    with pytest.raises(FluxError) as caught:
        registry.resolve("missing", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3114"
    assert "learned" in str(caught.value)


def test_a_different_version_is_refused_naming_both(site: Path) -> None:
    make_distribution(site)
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "2.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3116"
    assert f"{DIST} 1.0" in str(caught.value)
    assert f"{DIST} 2.0" in str(caught.value)
    assert PACKAGE not in sys.modules


def test_a_different_distribution_is_refused(site: Path) -> None:
    make_distribution(site)
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", "another-plugin", "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3116"
    assert "another-plugin" in str(caught.value)


def test_two_distributions_with_one_name_are_refused_naming_each(site: Path) -> None:
    make_distribution(site)
    make_distribution(site, dist="fxgen-rival", version="3.1")
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3115"
    message = str(caught.value)
    assert f"{DIST} 1.0" in message
    assert "fxgen-rival 3.1" in message
    assert PACKAGE not in sys.modules


def test_metadata_outside_every_installation_directory_is_refused(
    site: Path, elsewhere: Path
) -> None:
    make_distribution(elsewhere)
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3117"
    assert str(elsewhere.resolve()) in str(caught.value)
    assert PACKAGE not in sys.modules


def test_an_origin_missing_from_record_is_refused(site: Path) -> None:
    make_distribution(site, record_skips=[f"{PACKAGE}/__init__.py"])
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3117"
    assert "RECORD" in str(caught.value)


def test_a_distribution_with_no_record_is_refused(site: Path) -> None:
    make_distribution(site, record=False)
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3117"


def test_a_shadowing_package_first_on_sys_path_is_refused(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_distribution(site)
    cwd = tmp_path / "cwd"
    (cwd / PACKAGE).mkdir(parents=True)
    (cwd / PACKAGE / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(cwd))
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[cwd])
    assert code_of(caught) == "FLX-3117"
    assert str(cwd.resolve()) in str(caught.value)
    assert PACKAGE not in sys.modules


def test_an_origin_under_a_forbidden_root_inside_the_installation_directory_is_refused(
    site: Path,
) -> None:
    make_distribution(site)
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[site / PACKAGE])
    assert code_of(caught) == "FLX-3117"
    assert "a directory of this run" in str(caught.value)


def test_a_forbidden_root_that_holds_the_installation_directory_does_not_refuse(
    site: Path, tmp_path: Path
) -> None:
    """A virtual environment inside the project is the usual layout, and must work."""
    make_distribution(site)
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[tmp_path, site])
    assert resolved.editable is False


def test_an_editable_distribution_is_accepted_and_marked(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    make_distribution(site, editable=True, record=False, package_dir=source / PACKAGE)
    monkeypatch.syspath_prepend(str(source))
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[tmp_path])
    assert resolved.editable is True
    assert resolved.record == frozenset()
    assert PACKAGE not in sys.modules


def test_an_editable_distribution_still_needs_metadata_in_an_installation_directory(
    site: Path, elsewhere: Path
) -> None:
    make_distribution(elsewhere, editable=True)
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3117"


def test_a_package_in_the_current_directory_cannot_shadow_an_editable_one(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python -m`` puts the current directory first; the source tree must still be the origin."""
    source = tmp_path / "src"
    make_distribution(site, editable=True, record=False, package_dir=source / PACKAGE)
    monkeypatch.syspath_prepend(str(source))
    cwd = tmp_path / "cwd"
    (cwd / PACKAGE).mkdir(parents=True)
    (cwd / PACKAGE / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(cwd))
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[cwd])
    assert code_of(caught) == "FLX-3117"
    message = str(caught.value)
    assert str(cwd.resolve()) in message and str(source.resolve()) in message
    assert PACKAGE not in sys.modules


def test_a_bundle_directory_inside_the_source_tree_cannot_shadow_an_editable_plugin(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shadow lies under the recorded source directory, so only its place under a root of the run says."""
    source = tmp_path / "src"
    make_distribution(site, editable=True, record=False, package_dir=source / PACKAGE)
    monkeypatch.syspath_prepend(str(source))
    bundle = source / "bundle"
    (bundle / PACKAGE).mkdir(parents=True)
    (bundle / PACKAGE / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(bundle))
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[bundle, tmp_path / "state"])
    assert code_of(caught) == "FLX-3117"
    assert str(bundle.resolve()) in str(caught.value) and "found there first" in str(caught.value)
    assert PACKAGE not in sys.modules
    # Running from the source root does not lift it: that root holds the source directory, and the
    # bundle directory under it still does not.
    with pytest.raises(FluxError) as again:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[source, bundle])
    assert code_of(again) == "FLX-3117"


def test_a_command_run_from_the_source_root_of_an_editable_plugin_still_resolves_it(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The current directory is the plugin's own source root: it holds the whole source directory."""
    source = tmp_path / "src"
    make_distribution(site, editable=True, record=False, package_dir=source / PACKAGE)
    monkeypatch.syspath_prepend(str(source))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    resolved = registry.resolve(
        "learned", DIST, "1.0", forbidden_roots=[bundle, source / ".flux", source]
    )
    assert resolved.editable is True
    assert PACKAGE not in sys.modules


def test_an_editable_source_directory_with_a_space_is_read_from_its_file_url(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "my project"
    make_distribution(site, editable=True, record=False, package_dir=source / PACKAGE)
    assert "%20" in (site / "fxgen_plugin-1.0.dist-info" / "direct_url.json").read_text()
    monkeypatch.syspath_prepend(str(source))
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert resolved.editable is True
    assert resolved.package_root == (source / PACKAGE).resolve()


@pytest.mark.parametrize(
    "url",
    ["https://example.com/src", "file://elsewhere.example/src", "src", ""],
    ids=["https", "foreign-host", "relative", "empty"],
)
def test_an_editable_distribution_must_record_a_file_url_on_this_host(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    source = tmp_path / "src"
    make_distribution(
        site, editable=True, record=False, package_dir=source / PACKAGE, source_url=url
    )
    monkeypatch.syspath_prepend(str(source))
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3117"
    assert "direct_url.json" in str(caught.value)


def test_an_editable_distribution_with_no_direct_url_url_is_refused(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    make_distribution(site, editable=True, record=False, package_dir=source / PACKAGE)
    (site / "fxgen_plugin-1.0.dist-info" / "direct_url.json").write_text(
        json.dumps({"dir_info": {"editable": True}}), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(source))
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3117"


# ---------------------------------------------------------------------------
# What a refusal quotes is text from a bundle or an environment: it never prints raw
# ---------------------------------------------------------------------------

_HOSTILE = "x\x1b[2J\nFLX-0000: forged\u202e"


def _assert_inert(message: str) -> None:
    assert "\x1b" not in message
    assert "\u202e" not in message
    assert "\n" not in message


def test_a_pin_with_control_characters_is_quoted_not_printed(site: Path) -> None:
    make_distribution(site)
    for distribution, version in ((_HOSTILE, "1.0"), (DIST, _HOSTILE), (_HOSTILE, _HOSTILE)):
        with pytest.raises(FluxError) as caught:
            registry.resolve("learned", distribution, version, forbidden_roots=[])
        assert code_of(caught) == "FLX-3116"
        _assert_inert(str(caught.value))
        assert "forged" in str(caught.value)


def test_an_installed_name_and_version_with_control_characters_are_quoted(site: Path) -> None:
    make_distribution(site, dist="fxgen-plugin\x1b[2J", version="1.0")
    make_distribution(site, dist="fxgen-rival\u202e", version="3.1")
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3115"
    _assert_inert(str(caught.value))


def test_the_known_names_of_the_environment_are_quoted(site: Path) -> None:
    make_distribution(site, entry="odd\x1b[2J")
    with pytest.raises(FluxError) as caught:
        registry.resolve("missing", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3114"
    _assert_inert(str(caught.value))


def test_a_metadata_directory_with_control_characters_is_quoted(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = tmp_path / "e\x1b[2Jlsewhere\u202e"
    hostile.mkdir()
    monkeypatch.syspath_prepend(str(hostile))
    make_distribution(hostile)
    with pytest.raises(FluxError) as caught:
        registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert code_of(caught) == "FLX-3117"
    _assert_inert(str(caught.value))


# ---------------------------------------------------------------------------
# load(): what loads is what was checked
# ---------------------------------------------------------------------------


def test_load_returns_the_entry_point_object(site: Path) -> None:
    make_distribution(site)
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    factory = registry.load(resolved)
    assert factory({"a": "1"}) == ("plugin", {"a": "1"})


def test_load_of_an_editable_distribution_returns_the_object(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src"
    make_distribution(site, editable=True, record=False, package_dir=source / PACKAGE)
    monkeypatch.syspath_prepend(str(source))
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    assert registry.load(resolved)({}) == ("plugin", {})


def test_a_module_swapped_on_sys_path_after_resolve_is_refused(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_distribution(site)
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    swapped = tmp_path / "swapped"
    make_distribution(swapped, dist="fxgen-swap")
    monkeypatch.syspath_prepend(str(swapped))
    with pytest.raises(FluxError) as caught:
        registry.load(resolved)
    assert code_of(caught) == "FLX-3118"
    assert str(swapped.resolve()) in str(caught.value)


def test_a_module_swapped_in_sys_modules_after_resolve_is_refused(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_distribution(site)
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    fake_file = tmp_path / "fake" / "plugin.py"
    fake_file.parent.mkdir()
    fake_file.write_text("", encoding="utf-8")
    fake = types.ModuleType(f"{PACKAGE}.plugin")
    fake.__file__ = str(fake_file)
    fake.factory = lambda options: ("fake", options)  # type: ignore[attr-defined]
    fake.factory.__module__ = f"{PACKAGE}.plugin"
    monkeypatch.setitem(sys.modules, f"{PACKAGE}.plugin", fake)
    with pytest.raises(FluxError) as caught:
        registry.load(resolved)
    assert code_of(caught) == "FLX-3118"


def test_a_file_dropped_from_record_after_resolve_is_refused(site: Path) -> None:
    make_distribution(site)
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    plugin = (site / PACKAGE / "plugin.py").resolve()
    trimmed = registry.ResolvedGenerator(
        name=resolved.name,
        value=resolved.value,
        distribution=resolved.distribution,
        version=resolved.version,
        editable=False,
        install_root=resolved.install_root,
        package_root=resolved.package_root,
        record=resolved.record - {plugin},
        entry_point=resolved.entry_point,
    )
    with pytest.raises(FluxError) as caught:
        registry.load(trimmed)
    assert code_of(caught) == "FLX-3118"
    assert "RECORD" in str(caught.value)


def test_an_entry_point_that_cannot_be_imported_is_refused(site: Path) -> None:
    make_distribution(site, value=f"{PACKAGE}.plugin:missing")
    resolved = registry.resolve("learned", DIST, "1.0", forbidden_roots=[])
    with pytest.raises(FluxError) as caught:
        registry.load(resolved)
    assert code_of(caught) == "FLX-3124"
    assert "AttributeError" in str(caught.value)
