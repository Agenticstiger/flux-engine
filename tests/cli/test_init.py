# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init``: the first command a newcomer types.

What it promises: it writes the starter project and nothing else, it never
overwrites a file, it leaves nothing half-written behind, and it ends by
printing a next command that works from where the person actually is.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from importlib import metadata
from pathlib import Path
from typing import IO, Any, List, Optional, Tuple

import pytest

import flux_engine.starter as starter_module
from flux_engine.cli import _init as init_module
from flux_engine.cli import main
from flux_engine.starter import PURPOSES, starter_files

pytestmark = pytest.mark.unit

_HEADLINE = "created {name} with a small FLUX bundle and its FLUID contract (10 files):"


def _run(argv: List[str], capsys: pytest.CaptureFixture[str]) -> Tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture(autouse=True)
def _plain_command_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests of the output do not depend on how this test process was started."""
    monkeypatch.setattr(init_module, "command_prefix", lambda: "fluxsim")


# --- what it writes ---------------------------------------------------------


def test_init_writes_every_starter_file_byte_for_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    code, _out, err = _run(["init", str(target)], capsys)
    assert code == 0
    assert err == ""
    assert {p.name: p.read_bytes() for p in target.iterdir()} == {
        f.name: f.data for f in starter_files()
    }


def test_init_lists_each_file_with_its_purpose_and_prints_the_next_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run(["init", "demo"], capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == _HEADLINE.format(name="demo")
    for name, purpose in PURPOSES.items():
        assert any(line.strip().startswith(name) and purpose in line for line in lines), name
    assert out.rstrip().endswith("next:\n  cd demo\n  fluxsim run . --verify --ship -o out")


def test_init_creates_missing_parent_directories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "work" / "sims" / "demo"
    code, _out, _err = _run(["init", str(target)], capsys)
    assert code == 0
    assert (target / "simulation.flux.yml").is_file()


def test_init_accepts_a_directory_that_exists_and_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    target.mkdir()
    code, _out, _err = _run(["init", str(target)], capsys)
    assert code == 0
    assert len(list(target.iterdir())) == len(PURPOSES)


def test_init_dot_writes_here_and_prints_no_cd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run(["init", "."], capsys)
    assert code == 0
    assert (tmp_path / "world.flux.yml").is_file()
    assert out.rstrip().endswith("next:\n  fluxsim run . --verify --ship -o out")


def test_init_quotes_a_directory_name_the_shell_would_split(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run(["init", "my demo"], capsys)
    assert code == 0
    cd_line = [line.strip() for line in out.splitlines() if line.strip().startswith("cd ")][0]
    assert shlex.split(cd_line) == ["cd", "my demo"]


def test_init_separates_a_directory_name_that_starts_with_a_dash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cd -x`` is an option and ``cd -`` goes back to the last directory."""
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run(["init", "--", "-dash"], capsys)
    assert code == 0
    assert (tmp_path / "-dash" / "world.flux.yml").is_file()
    assert "\n  cd -- -dash\n" in out


# --- what it refuses --------------------------------------------------------


def test_init_refuses_a_directory_that_holds_anything_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    target.mkdir()
    keep = target / "keep.txt"
    keep.write_text("mine")
    code, out, err = _run(["init", str(target)], capsys)
    assert code == 1
    assert out == ""
    assert "is not empty" in err and "never overwrites" in err
    assert [p.name for p in target.iterdir()] == ["keep.txt"]
    assert keep.read_text() == "mine"


def test_init_says_what_it_found_because_a_dotfile_is_invisible_to_ls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    target.mkdir()
    for name in (".DS_Store", "b", "a", "c", "d"):
        (target / name).write_text("")
    code, _out, err = _run(["init", str(target)], capsys)
    assert code == 1
    assert "it holds .DS_Store, a, b, ..." in err


def test_init_refuses_a_path_that_is_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "demo"
    target.write_text("not a directory")
    code, _out, err = _run(["init", str(target)], capsys)
    assert code == 1
    assert "is not a directory" in err
    assert target.read_text() == "not a directory"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_init_refuses_a_link_to_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "demo"
    try:
        target.symlink_to(tmp_path / "missing")
    except OSError:  # pragma: no cover - a platform that forbids symlinks
        pytest.skip("cannot create a symbolic link here")
    code, _out, err = _run(["init", str(target)], capsys)
    assert code == 1
    assert "symbolic link to nothing" in err


def test_init_with_an_empty_name_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(["init", ""], capsys)
    assert code == 2
    assert out == ""
    assert "fluxsim init demo" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter a directory with no permissions")
def test_init_reports_a_directory_it_cannot_read_as_its_own_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "locked"
    target.mkdir()
    target.chmod(0)
    try:
        code, _out, err = _run(["init", str(target)], capsys)
    finally:
        target.chmod(0o700)
    assert code == 1
    assert err.startswith("init: cannot use ")
    assert "Permission denied" in err


# --- it leaves nothing half-written ------------------------------------------


class _FailsAfterFiveBytes:
    """A file handle that writes a little and then runs out of disk."""

    def __init__(self, real: IO[bytes]) -> None:
        self._real = real

    def __enter__(self) -> "_FailsAfterFiveBytes":
        return self

    def __exit__(self, *exc: object) -> None:
        self._real.close()

    def write(self, data: bytes) -> int:
        self._real.write(data[:5])
        self._real.flush()
        raise OSError(28, "No space left on device")


def _fail_the_fourth_file_halfway(monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = Path.open
    created = {"count": 0}

    def flaky(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, mode, *args, **kwargs)
        if "x" in mode:
            created["count"] += 1
            if created["count"] == 4:
                return _FailsAfterFiveBytes(handle)
        return handle

    monkeypatch.setattr(Path, "open", flaky)


def test_a_failed_write_removes_every_file_including_the_partial_one_and_the_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "demo"
    _fail_the_fourth_file_halfway(monkeypatch)
    code, _out, err = _run(["init", str(target)], capsys)
    assert code == 1
    assert "No space left on device" in err and "was removed" in err
    assert not target.exists()


def test_a_failed_write_removes_the_parent_directories_it_made(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a" / "b" / "demo"
    _fail_the_fourth_file_halfway(monkeypatch)
    code, _out, _err = _run(["init", str(target)], capsys)
    assert code == 1
    assert not (tmp_path / "a").exists()


def test_a_failed_write_keeps_a_directory_that_was_already_there_and_leaves_it_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "demo"
    target.mkdir()
    _fail_the_fourth_file_halfway(monkeypatch)
    code, _out, _err = _run(["init", str(target)], capsys)
    assert code == 1
    assert target.is_dir()
    assert list(target.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")
def test_a_link_that_appears_after_the_check_is_never_written_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory was empty when it was checked. Someone then planted a link."""
    target = tmp_path / "demo"
    target.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    real_files = starter_module.starter_files

    def plant_then_list() -> Any:
        try:
            (target / "world.flux.yml").symlink_to(victim)
        except OSError:  # pragma: no cover - a platform that forbids symlinks
            pytest.skip("cannot create a symbolic link here")
        return real_files()

    monkeypatch.setattr(starter_module, "starter_files", plant_then_list)
    code, _out, err = _run(["init", str(target)], capsys)
    assert code == 1
    assert "could not write into" in err
    assert victim.read_text() == "precious"
    assert (target / "world.flux.yml").is_symlink()
    assert [p.name for p in target.iterdir()] == ["world.flux.yml"]


# --- --json ------------------------------------------------------------------


def test_init_json_envelope_names_the_files_and_the_next_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _err = _run(["init", "demo", "--json"], capsys)
    envelope = json.loads(out)
    assert code == 0
    assert envelope["stage"] == "init"
    assert envelope["ok"] is True
    assert envelope["error"] is None
    assert envelope["artifacts"]["directory"] == "demo"
    assert envelope["artifacts"]["files"] == [f.name for f in starter_files()]
    assert envelope["artifacts"]["next"] == ["cd demo", "fluxsim run . --verify --ship -o out"]


def test_init_json_refusal_is_an_envelope_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "x").write_text("x")
    code, out, _err = _run(["--json", "init", str(tmp_path / "taken")], capsys)
    envelope = json.loads(out)
    assert code == 1
    assert envelope["stage"] == "init"
    assert envelope["ok"] is False
    assert envelope["error"]["code"] is None
    assert "is not empty" in envelope["error"]["message"]


# --- the project it writes actually runs -------------------------------------


@pytest.mark.integration
def test_the_scaffolded_project_runs_verifies_and_ships(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The five-minute path, in process: init, then ``run --verify --ship``."""
    demo = tmp_path / "demo"
    assert main(["init", str(demo)]) == 0
    capsys.readouterr()
    out_dir = tmp_path / "out"
    code = main(
        ["run", str(demo), "--verify", "--ship", "-o", str(out_dir)]
        + ["--state-dir", str(tmp_path / "state"), "--json"]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert code == 0, envelope
    assert envelope["ok"] is True
    assert len(list(out_dir.glob("*.flux.tgz"))) == 1


# --- the next command is spelled so it works from here -----------------------


class _FakeDistribution:
    """Stands in for the installed distribution's metadata."""

    def __init__(self, direct_url: Optional[str]) -> None:
        self._direct_url = direct_url

    def read_text(self, name: str) -> Optional[str]:
        assert name == "direct_url.json"
        return self._direct_url


def _installed_as(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make the package look as if it were installed from a distribution called *name*."""
    monkeypatch.setattr(metadata, "packages_distributions", lambda: {"flux_engine": [name]})


def _under_uvx(monkeypatch: pytest.MonkeyPatch, direct_url: Optional[object]) -> str:
    monkeypatch.undo()
    monkeypatch.setattr(sys, "prefix", "/home/me/.cache/uv/archive-v0/AbC123xyz")
    _installed_as(monkeypatch, "flux-sim-engine")
    text = None if direct_url is None else json.dumps(direct_url)
    monkeypatch.setattr(metadata, "distribution", lambda name: _FakeDistribution(text))
    return init_module.command_prefix()


def test_under_uvx_from_a_local_wheel_the_hint_repeats_that_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _under_uvx(
        monkeypatch,
        {"url": "file:///tmp/my%20builds/flux_sim_engine-0.1.0-py3-none-any.whl"},
    )
    assert shlex.split(prefix) == [
        "uvx",
        "--from",
        "/tmp/my builds/flux_sim_engine-0.1.0-py3-none-any.whl",
        "fluxsim",
    ]


def test_under_uvx_from_git_the_hint_repeats_the_repository_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _under_uvx(
        monkeypatch,
        {
            "url": "https://example.org/team/engine.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc123"},
        },
    )
    assert shlex.split(prefix) == [
        "uvx",
        "--from",
        "git+https://example.org/team/engine.git@abc123",
        "fluxsim",
    ]


def test_under_uvx_from_a_git_subdirectory_the_hint_keeps_the_subdirectory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = _under_uvx(
        monkeypatch,
        {
            "url": "https://example.org/team/mono.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc123"},
            "subdirectory": "packages/engine",
        },
    )
    assert shlex.split(prefix)[2] == (
        "git+https://example.org/team/mono.git@abc123#subdirectory=packages/engine"
    )


def test_under_uvx_from_an_index_the_hint_names_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``direct_url.json`` means it really did come from an index."""
    assert _under_uvx(monkeypatch, None) == "uvx --from flux-sim-engine fluxsim"


def test_under_uvx_with_unreadable_metadata_the_hint_names_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(sys, "prefix", "/home/me/.cache/uv/archive-v0/AbC123xyz")
    _installed_as(monkeypatch, "flux-sim-engine")

    def missing(name: str) -> Any:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", missing)
    assert init_module.command_prefix() == "uvx --from flux-sim-engine fluxsim"


def test_the_hint_names_whatever_the_distribution_is_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name is asked of the installed metadata, so a rename needs no code change here."""
    monkeypatch.undo()
    monkeypatch.setattr(sys, "prefix", "/home/me/.cache/uv/archive-v0/AbC123xyz")
    _installed_as(monkeypatch, "some-other-name")
    monkeypatch.setattr(metadata, "distribution", lambda name: _FakeDistribution(None))
    assert init_module.command_prefix() == "uvx --from some-other-name fluxsim"


def test_the_hint_falls_back_to_the_published_name_when_nothing_ships_the_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(sys, "prefix", "/home/me/.cache/uv/archive-v0/AbC123xyz")
    monkeypatch.setattr(metadata, "packages_distributions", lambda: {})
    monkeypatch.setattr(metadata, "distribution", lambda name: _FakeDistribution(None))
    assert init_module.command_prefix() == "uvx --from flux-sim-engine fluxsim"


def test_command_prefix_is_plain_when_the_running_script_is_the_one_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    script = tmp_path / "fluxsim"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(script), "init", "demo"])
    monkeypatch.setattr(shutil, "which", lambda name: str(script))
    assert init_module.command_prefix() == "fluxsim"


def test_command_prefix_is_the_absolute_path_when_the_environment_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    venv_bin = tmp_path / "my venv" / "bin"
    venv_bin.mkdir(parents=True)
    script = venv_bin / "fluxsim"
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(script), "init", "demo"])
    monkeypatch.setattr(shutil, "which", lambda name: None)
    prefix = init_module.command_prefix()
    assert shlex.split(prefix) == [str(script.resolve())]


def test_command_prefix_is_the_absolute_path_when_another_fluxsim_wins_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    mine = tmp_path / "a" / "fluxsim"
    other = tmp_path / "b" / "fluxsim"
    for script in (mine, other):
        script.parent.mkdir()
        script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "argv", [str(mine), "init", "demo"])
    monkeypatch.setattr(shutil, "which", lambda name: str(other))
    assert shlex.split(init_module.command_prefix()) == [str(mine.resolve())]


def test_command_prefix_repeats_python_dash_m_when_nothing_named_fluxsim_is_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "__main__.py"), "init", "demo"])
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert shlex.split(init_module.command_prefix()) == [sys.executable, "-m", "flux_engine.cli"]


def test_command_prefix_is_plain_for_python_dash_m_when_fluxsim_is_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "__main__.py"), "init", "demo"])
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/fluxsim")
    assert init_module.command_prefix() == "fluxsim"
