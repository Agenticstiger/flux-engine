# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The generator's side: FLUX in a sandbox that holds the contract and the profile only.

Every command here runs under ``env -i`` with a temporary HOME, a two-entry PATH and
no AWS variable, from a virtual environment that has no AWS SDK installed. On macOS
it also runs under ``sandbox-exec`` with a profile that refuses network access and
refuses to read anything under the user's home, the temporary directories or the work
root, which holds every run's directory, except the sandbox itself, its virtual
environment and the Python installation. The source data, the owner's work files,
other runs' files and ``~/.aws`` are therefore unreadable to it, and the run records
the exact commands, environment and profile used.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: The uv that writes the repository's lock format.
UV = ("uvx", "--from", "uv==0.13.8", "uv")
#: Packages that must not be importable on the generator side.
FORBIDDEN_MODULES = ("boto3", "botocore", "awswrangler", "s3fs", "duckdb")

#: The checkout's own packages and where their code sits in it. uv keys a local build by its
#: files' times, so a checkout that leaves ``pyproject.toml`` alone would reuse a wheel built
#: from another commit: these are rebuilt on every run, and the environment is refused unless
#: their code is the checkout's, file for file.
LOCAL_PACKAGES = (
    ("flux-sim-engine", "flux_engine"),
    ("flux-stats", "packages/flux-stats/src/flux_stats"),
)
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_BUNDLE_AUTHOR = Path(__file__).with_name("bundle.py")


@dataclass
class Sandbox:
    """The generator side's directory, interpreter and confinement."""

    root: Path
    venv: Path
    #: More directories the generator may not read, such as the run's own
    #: directory, which holds the source and the owner's files.
    deny: Sequence[Path] = ()
    commands: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def env(self) -> Dict[str, str]:
        return {
            "HOME": str(self.root / "home"),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TMPDIR": str(self.root / "tmp"),
        }

    @property
    def profile_path(self) -> Path:
        return self.root / "sandbox.sb"

    def _python_home(self) -> Path:
        return Path(os.path.realpath(self.venv / "bin" / "python")).parent.parent

    def write_profile(self) -> Optional[str]:
        """Write the sandbox-exec profile; None where there is no sandbox-exec."""
        if sys.platform != "darwin" or not SANDBOX_EXEC.exists():
            return None
        home = os.path.realpath(Path.home())
        denied = [home, "/private/tmp", "/private/var/folders", "/Volumes"]
        denied += [os.path.realpath(path) for path in self.deny]
        for path in [
            *denied,
            str(self._python_home()),
            os.path.realpath(self.venv),
            str(self.root),
        ]:
            if '"' in str(path) or "\\" in str(path):
                raise RuntimeError(f"a sandbox path cannot hold a quote or a backslash: {path}")
        for kept in (str(self._python_home()), os.path.realpath(self.venv)):
            for path in self.deny:
                if Path(os.path.realpath(path)).is_relative_to(kept):
                    # The allow rule for the interpreter would re-open the denied run.
                    raise RuntimeError(f"the generator's interpreter at {kept} contains {path}")
        allowed = [
            str(self._python_home()),
            os.path.realpath(self.venv),
            os.path.realpath(self.root),
        ]
        text = "\n".join(
            [
                "(version 1)",
                "(allow default)",
                "(deny network*)",
                "(deny file-read-data file-write* "
                + " ".join(f'(subpath "{path}")' for path in denied)
                + ")",
                "(allow file-read-data "
                + " ".join(f'(subpath "{path}")' for path in allowed)
                + ")",
                f'(allow file-write* (subpath "{os.path.realpath(self.root)}"))',
                "",
            ]
        )
        self.profile_path.write_text(text, encoding="utf-8")
        return text

    def run(
        self, step: str, argv: Sequence[str], *, timeout: int = 7200
    ) -> subprocess.CompletedProcess:
        command = [str(part) for part in argv]
        if self.profile_path.exists():
            command = [str(SANDBOX_EXEC), "-f", str(self.profile_path), *command]
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        self.commands.append(
            {
                "step": step,
                "argv": command,
                "cwd": str(self.root),
                "env": self.env,
                "exitCode": result.returncode,
                "seconds": round(time.monotonic() - started, 1),
                "stdoutTail": result.stdout[-4000:],
                "stderrTail": result.stderr[-4000:],
            }
        )
        return result


def build_venv(repo: Path, venv: Path) -> Dict[str, Any]:
    """Install the checkout's engine, as locked, with its Parquet sink and nothing else."""
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv.resolve())}
    command = [*UV, "sync", "--frozen", "--no-dev", "--no-editable", "--extra", "arrow"]
    for name, _ in LOCAL_PACKAGES:
        command += ["--reinstall-package", name]
    synced = subprocess.run(
        [*command, "--python", "3.12"], cwd=repo, env=env, capture_output=True, text=True
    )
    if synced.returncode != 0:
        raise RuntimeError(f"uv sync of the generator environment failed: {synced.stderr[-2000:]}")
    python = venv / "bin" / "python"
    probe = (
        "import importlib.util, importlib.metadata, json, sys\n"
        f"forbidden = {list(FORBIDDEN_MODULES)!r}\n"
        "present = [m for m in forbidden if importlib.util.find_spec(m) is not None]\n"
        "dists = sorted(f\"{d.metadata['Name']}=={d.version}\" for d in importlib.metadata.distributions())\n"
        "print(json.dumps({'forbiddenPresent': present, 'distributions': dists}))\n"
    )
    found = json.loads(
        subprocess.run(
            [str(python), "-I", "-c", probe], check=True, capture_output=True, text=True
        ).stdout
    )
    if found["forbiddenPresent"]:
        raise RuntimeError(f"generator environment holds {found['forbiddenPresent']}")
    for name, source in LOCAL_PACKAGES:
        module = Path(source).name
        located = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                f"import os, {module}; print(os.path.dirname({module}.__file__))",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if python_sources(Path(located)) != python_sources(repo / source):
            raise RuntimeError(
                f"the generator environment's {name} is not this checkout's code; rebuild it"
            )
    return found


def python_sources(directory: Path) -> Dict[str, str]:
    """Each ``.py`` file under *directory*, by its relative path, with its sha256."""
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def run_generator(
    root: Path,
    venv: Path,
    contract: Path,
    profile: Path,
    *,
    work_root: Path,
    seed: int,
    jobs: int = 1,
    allow_unconfined: bool = False,
) -> Dict[str, Any]:
    """Author the bundle, run it, verify it against the profile; return what happened.

    *root* is the generator's directory inside the *work_root*, which holds every
    run's directory. Without sandbox-exec the generator would be confined by
    ``env -i`` alone, so the run stops unless *allow_unconfined* says that is
    acceptable.
    """
    if Path(os.path.realpath(work_root)) not in Path(os.path.realpath(root)).parents:
        raise RuntimeError(f"the generator's directory {root} is not inside the work root")
    if not _can_confine() and not allow_unconfined:
        raise RuntimeError(
            "sandbox-exec is not available on this host, so the generator could read the "
            "source; pass --allow-unconfined to run it under env -i alone"
        )
    if root.exists():
        shutil.rmtree(root)
    for name in ("inputs", "home", "tmp"):
        (root / name).mkdir(parents=True)
    inputs = root / "inputs"
    shutil.copy2(contract, inputs / contract.name)
    shutil.copy2(profile, inputs / profile.name)
    shutil.copy2(_BUNDLE_AUTHOR, inputs / _BUNDLE_AUTHOR.name)
    # Every run directory under the work root holds that run's source and the
    # owner's files. The whole work root is denied, wherever it is, and this run's
    # directory too, should a link take it elsewhere; only the generator's own
    # directory, its venv and its Python are allowed back.
    sandbox = Sandbox(root, venv, deny=[work_root, root.parent])
    confinement = sandbox.write_profile()
    fluxsim = str(venv / "bin" / "fluxsim")
    python = str(venv / "bin" / "python")

    def must(step: str, argv: Sequence[str]) -> subprocess.CompletedProcess:
        result = sandbox.run(step, argv)
        if result.returncode != 0:
            raise RuntimeError(
                f"{step} failed ({result.returncode}): {result.stderr[-2000:] or result.stdout[-2000:]}"
            )
        return result

    must("init", [fluxsim, "init", "bundle"])
    must(
        "author",
        [
            python,
            "-I",
            str(inputs / _BUNDLE_AUTHOR.name),
            "--contract",
            str(inputs / contract.name),
            "--profile",
            str(inputs / profile.name),
            "--bundle",
            "bundle",
            "--notes",
            "authoring.json",
            "--seed",
            str(seed),
        ],
    )
    must("validate", [fluxsim, "validate", "bundle"])
    run = must(
        "run",
        [
            fluxsim,
            "run",
            "bundle",
            "--profile",
            str(inputs / profile.name),
            "--state-dir",
            "state",
            "--sink",
            "parquet:parquet",
            "--jobs",
            str(jobs),
        ],
    )
    run_id = _run_id(run.stdout)
    verify = sandbox.run(
        "verify",
        [
            fluxsim,
            "verify",
            "bundle",
            "--run-id",
            run_id,
            "--profile",
            str(inputs / profile.name),
            "--state-dir",
            "state",
            "--json",
        ],
    )
    (root / "verify.json").write_text(verify.stdout, encoding="utf-8")
    try:
        verified = json.loads(verify.stdout)
    except ValueError:
        verified = None
    # verify exits 1 when a fidelity property fails or is refused; that is a result.
    # A refusal of the stage itself (a coded error, and no row check) is not.
    envelope = verified if isinstance(verified, dict) else {}
    artifacts = envelope.get("artifacts") or {}
    refused = (envelope.get("error") or {}).get("code")
    if verify.returncode not in (0, 1) or "rowConformance" not in artifacts or refused:
        raise RuntimeError(
            f"fluxsim verify gave no report ({verify.returncode}, {refused}): "
            f"{verify.stderr[-2000:] or verify.stdout[-2000:]}"
        )
    record = {
        "runId": run_id,
        "sandboxExec": confinement is not None,
        "sandboxProfile": confinement,
        "commands": sandbox.commands,
        "verify": verified,
        "digests": digests(root),
    }
    (root / "commands.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _can_confine() -> bool:
    return sys.platform == "darwin" and SANDBOX_EXEC.exists()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def digests(root: Path) -> Dict[str, Any]:
    """sha256 of the bundle FLUX ran and of every Parquet file it wrote.

    The same scale factor, seed and engine give the same bundle and the same bytes,
    so two runs can be compared by these alone.
    """

    def tree(base: Path, pattern: str) -> Dict[str, str]:
        return {
            str(path.relative_to(base)): _sha256(path)
            for path in sorted(base.rglob(pattern))
            if path.is_file()
        }

    bundle, outputs = tree(root / "bundle", "*"), tree(root / "parquet", "*.parquet")

    def combined(files: Mapping[str, str]) -> str:
        text = "".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items()))
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    return {"bundle": combined(bundle), "outputs": combined(outputs), "outputFiles": outputs}


def _refuse_links(root: Path, paths: Sequence[Path]) -> None:
    """Refuse a link among *paths*, which the generator wrote under *root*.

    The generator writes inside its sandbox, but the harness reads what it wrote
    outside it. A link it left behind, to the source or to the held-out people,
    would have their rows scored as synthetic ones. This follows the ``data``
    filter of Python's tarfile (PEP 706), which refuses a link that leaves the
    destination, but refuses every link: the generator has no reason to write one.
    """
    inside = Path(os.path.realpath(root))
    for path in paths:
        if (
            path.is_symlink()
            or not Path(os.path.realpath(path)).is_relative_to(inside)
            or (path.is_file() and path.stat().st_nlink > 1)
        ):
            raise RuntimeError(f"the generator's output holds a link: {path}")


def output_files(root: Path, table: str) -> List[Path]:
    """A table's Parquet files as the generator wrote them, refusing a link."""
    folder = root / "parquet" / table
    files = sorted(folder.glob("*.parquet"))
    _refuse_links(root, (folder, *files))
    return files


def authoring_notes(root: Path) -> Dict[str, Any]:
    """The bundle author's notes, ``authoring.json``, refusing a link as the output does."""
    path = root / "authoring.json"
    _refuse_links(root, (path,))
    return json.loads(path.read_text(encoding="utf-8"))


def _run_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("run ") and line.rstrip().endswith(" complete"):
            return line.split()[1]
    raise RuntimeError("fluxsim run printed no run id")
