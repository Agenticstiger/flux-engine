# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The proof-material guard: it finds proof-system backends and only those.

Synthetic trees, built in a temporary directory, say what it does and does not find. One
test runs it over this repository, which has to pass its own guard: ``make proof-guard`` and
the ``Proof material`` job of the ``CI gate`` run it on every pull request, so a backend sent
in from outside is stopped there.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Tuple

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_proof_material.py"
ROOT = SCRIPT.parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_proof_material", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_proof_material"] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


guard = _load()

_PUBLIC_SEAM = {
    "flux_engine/api/fidelity.py": (
        "from typing import Protocol, runtime_checkable\n\n\n"
        "@runtime_checkable\nclass ProofSystem(Protocol):\n    name: str\n\n\n"
        "class FidelityProver(Protocol):\n    def attest(self) -> None: ...\n\n\n"
        "class Extended(ProofSystem, Protocol):\n    version: str\n"
    ),
    "flux_engine/concordance/verifier.py": (
        "import numpy as np\nfrom noise import pnoise1\nfrom dataclasses import dataclass, field\n\n\n"
        "@dataclass(frozen=True)\nclass NoProofSystem:\n"
        '    """The v0.1 proof system: there is none."""\n\n    name: str = \'none\'\n\n\n'
        "class ProofSystemError(Exception):\n    pass\n\n\n"
        "class ClearStatistic:\n"
        "    proof_system: NoProofSystem = NoProofSystem()\n"
        "    other: NoProofSystem = field(default_factory=NoProofSystem)\n"
        "    third: NoProofSystem = field(default=NoProofSystem())\n\n\n"
        "def build(backend, make_proof_system):\n"
        "    a = ClearStatistic(proof_system=NoProofSystem())\n"
        "    b = ClearStatistic(proof_system=backend.proof_system)\n"
        "    c = ClearStatistic(proof_system=make_proof_system())\n"
        "    return a, b, c\n"
    ),
    "pyproject.toml": (
        "[project]\nname = 'x'\ndependencies = ['pydantic>=2', 'numpy', 'noirmalize', 'sp1ne']\n\n"
        "[project.optional-dependencies]\ndev = ['pytest', 'noise-tools']\n\n"
        "[project.entry-points.'flux_engine.plugins']\nhello = 'x:y'\n"
        "[project.entry-points.'markdown.proofreaders']\nspell = 'x:y'\n"
        "[project.entry-points.'flux.approvers']\ncheck = 'x:y'\n"
    ),
    "flux_engine/noise.py": "x = 1\n",
    "flux_engine/mozkvmish/a.py": "x = 1\n",
    "flux_engine/ris_c0.py": "x = 1\n",
    "flux_engine/concordance/__init__.py": (
        "from flux_engine.concordance.verifier import NoProofSystem\n"
        "from flux_engine.api.fidelity import ProofSystem\n"
    ),
    "flux_engine/api/alias_port.py": (
        "from typing import Protocol as P\n\n\nclass ProofSystem(P):\n    name: str\n"
    ),
    "flux_engine/sp100/a.py": "x = 1\n",
    "flux_engine/plonked.py": "import sp1ral\nfrom plonkish import x\n",
    "tests/test_seam.py": (
        "from enum import Enum\n"
        "from unittest.mock import MagicMock\n"
        "from types import SimpleNamespace\n\n\n"
        "class UnsupportedProofSystem(ValueError):\n    pass\n\n\n"
        "class KnownProver(str, Enum):\n    A = 'a'\n\n\n"
        "class TestNoProofSystem:\n    def test_it(self):\n        pass\n\n\n"
        "class Improver:\n    pass\n\n\n"
        "class Approver:\n    pass\n\n\n"
        "def build(Verifier):\n"
        "    a = Verifier(proof_system=MagicMock())\n"
        "    b = Verifier(proof_system=SimpleNamespace(name='none'))\n"
        "    return a, b\n"
    ),
    "docs/adr/0006-something-else.md": "an ordinary decision record\n",
    "README.md": "Zero-knowledge proofs are discussed elsewhere.\n",
}


def _tree(root: Path, files: Dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def _proof(root: Path) -> List[Tuple[str, str]]:
    return [(f.kind, f.path) for f in guard.check_proof_material(root)]


@pytest.mark.unit
def test_the_public_seam_alone_is_not_proof_material(tmp_path: Path) -> None:
    """The ports (Protocols), the one allowed NoProofSystem, wiring that passes a backend
    through, and ordinary imports and dependencies must all be left alone."""
    _tree(tmp_path, _PUBLIC_SEAM)
    assert _proof(tmp_path) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("spikes/r4/notes.md", "proof spike directory"),
        ("tests/Spikes/a.txt", "proof spike directory"),
        ("docs/adr/0007-r4-proof-spike.md", "proof-spike ADR"),
        ("docs/ADR/0007/context.md", "proof-spike ADR"),
        ("x/guest/src/main.rs", "Cargo, Rust, Noir or zkVM source"),
        ("x/MAIN.RS", "Cargo, Rust, Noir or zkVM source"),
        ("x/Cargo.toml", "Cargo, Rust, Noir or zkVM source"),
        ("x/Cargo.lock", "Cargo, Rust, Noir or zkVM source"),
        ("x/rust-toolchain.toml", "Cargo, Rust, Noir or zkVM source"),
        ("x/rust-toolchain", "Cargo, Rust, Noir or zkVM source"),
        ("x/circuit/src/main.nr", "Cargo, Rust, Noir or zkVM source"),
        ("x/Nargo.toml", "Cargo, Rust, Noir or zkVM source"),
        ("x/guest.elf", "Cargo, Rust, Noir or zkVM source"),
        ("x/vk.vkey", "Cargo, Rust, Noir or zkVM source"),
    ],
)
def test_proof_paths_and_toolchain_files_are_found_wherever_they_sit(
    tmp_path: Path, path: str, kind: str
) -> None:
    _tree(tmp_path, {**_PUBLIC_SEAM, path: "x\n"})
    assert [k for k, _ in _proof(tmp_path)] == [kind]


@pytest.mark.unit
def test_the_adr_is_matched_by_number_and_a_neighbour_is_not(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {"docs/adr/0070-other.md": "x\n", "docs/adr/0006-other.md": "x\n", "docs/adr/0007": "x\n"},
    )
    assert _proof(tmp_path) == [("proof-spike ADR", "docs/adr/0007")]


@pytest.mark.unit
def test_a_spikes_directory_is_one_finding_not_one_per_file(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "spikes/a.rs": "",
            "spikes/Cargo.toml": "",
            "spikes/b/c.py": "class XProofSystem:\n    pass\n",
        },
    )
    assert _proof(tmp_path) == [("proof spike directory", "spikes")]


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "class Risc0ProofSystem:\n    name = 'risc0'\n",
        "from x import ProofSystem\n\n\nclass Backend(ProofSystem):\n    name = 'b'\n",
        "import api\n\n\nclass Backend(api.fidelity.ProofSystem):\n    name = 'b'\n",
        "class Prover(FidelityProver):\n    pass\n",
        "class Sub(NoProofSystem):\n    pass\n",
        "stat = Statistic(proof_system=Risc0())\n",
        "proof_system = Risc0()\n",
        "class S:\n    proof_system: ProofSystem = field(default_factory=Risc0)\n",
        "class S:\n    proof_system: ProofSystem = field(default=Risc0())\n",
        "self.proof_system = Elsewhere.Risc0()\n",
    ],
)
def test_a_proofsystem_implementation_is_found_by_its_name_not_its_path(
    tmp_path: Path, source: str
) -> None:
    where = "anywhere/deep/in/the/tree/mod.py"
    _tree(tmp_path, {where: source})
    found = guard.check_proof_material(tmp_path)
    assert [f.kind for f in found] == ["ProofSystem implementation"]
    assert found[0].path == where and found[0].line >= 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "import risc0_zkvm",
        "import sp1_sdk as sdk",
        "import risc0.host",
        "import risc_zero.host",
        "from noir_python.circuits import prove",
        "from halo2 import x",
    ],
)
def test_importing_a_proof_backend_is_found(tmp_path: Path, line: str) -> None:
    _tree(tmp_path, {"a.py": line + "\n"})
    assert _proof(tmp_path) == [("proof backend import", "a.py")]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("toml", "kind", "detail"),
    [
        ("[project]\nname='x'\ndependencies=['risc0-zkvm>=1']\n", "dependency", "risc0-zkvm"),
        ("[project]\nname='x'\ndependencies=['Risc-Zero.sdk>=1']\n", "dependency", "Risc-Zero.sdk"),
        (
            "[project]\nname='x'\n[project.optional-dependencies]\nproofs=['sp1_sdk']\n",
            "dependency",
            "sp1_sdk",
        ),
        (
            "[dependency-groups]\ndev=['pytest', {include-group='x'}, 'noir-lang']\n",
            "dependency",
            "noir-lang",
        ),
        ("[tool.uv]\ndev-dependencies=['halo2-bindings']\n", "dependency", "halo2-bindings"),
        (
            "[project.entry-points.'flux_engine.proof_systems']\nrisc0='x:y'\n",
            "entry point",
            "flux_engine.proof_systems",
        ),
        ("[project.entry-points.prover]\nx='x:y'\n", "entry point", "prover"),
    ],
)
def test_a_proof_backend_declared_in_pyproject_is_found(
    tmp_path: Path, toml: str, kind: str, detail: str
) -> None:
    _tree(tmp_path, {"pkg/pyproject.toml": toml})
    found = guard.check_proof_material(tmp_path)
    assert [(f.kind, f.detail) for f in found] == [(f"proof backend {kind}", detail)]


@pytest.mark.unit
def test_a_file_that_cannot_be_checked_is_a_finding_not_a_pass(tmp_path: Path) -> None:
    """A file the guard could not read or parse is not a file that is clean."""
    _tree(tmp_path, {"bad.py": "def (:\n", "pyproject.toml": "[[[ not toml\n", "b.bin.py": "\0\0"})
    assert sorted(_proof(tmp_path)) == [
        ("could not be checked", "b.bin.py"),
        ("could not be checked", "bad.py"),
        ("could not be checked", "pyproject.toml"),
    ]
    details = [f.detail for f in guard.check_proof_material(tmp_path)]
    assert all("under Python 3." in detail for detail in details), details


@pytest.mark.unit
def test_a_symlink_named_for_proof_material_is_found_and_never_followed(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "spikes").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "dangling.rs").symlink_to(tmp_path / "missing")
    found = _proof(tmp_path)
    assert ("proof spike directory", "spikes") in found
    assert ("Cargo, Rust, Noir or zkVM source", "dangling.rs") in found
    assert len(found) == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "flux_engine/proof/risc0.py",
        "flux_engine/risc0_backend.py",
        "flux_engine/proof/Sp1Host.py",
        "flux_engine/stubs/halo2.pyi",
        "flux_engine/noir/__init__.py",
        "flux_engine/risc0host.py",
        "flux_engine/pyrisc0.py",
        "flux_engine/RiscZero.py",
        "flux_engine/Halo2Lib/__init__.py",
    ],
)
def test_a_module_or_package_named_for_a_backend_is_found(tmp_path: Path, path: str) -> None:
    _tree(tmp_path, {**_PUBLIC_SEAM, path: "x = 1\n"})
    assert [k for k, _ in _proof(tmp_path)] == ["proof backend module"]


@pytest.mark.unit
def test_a_backend_package_is_one_finding_not_one_per_module(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "flux_engine/risc0/host.py": "class XProofSystem:\n    pass\n",
            "flux_engine/risc0/guest/main.rs": "",
        },
    )
    assert _proof(tmp_path) == [("proof backend module", "flux_engine/risc0")]


@pytest.mark.unit
def test_a_stub_file_is_read_like_a_module(tmp_path: Path) -> None:
    _tree(tmp_path, {"flux_engine/a.pyi": "class Backend(ProofSystem):\n    name: str\n"})
    assert _proof(tmp_path) == [("ProofSystem implementation", "flux_engine/a.pyi")]


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "from api import ProofSystem as PS\n\n\nclass Zk(PS):\n    name = 'zk'\n",
        "from api import ProofSystem\n\nPS = ProofSystem\nQ = PS\n\n\nclass Zk(Q):\n    pass\n",
        "from api import NoProofSystem as N\n\nproof_system = N\nclass Zk(proof_system):\n    pass\n",
        "class Zk(ProofSystem):  # a type-only stub\n    name: str\n",
        "class ZkFidelityProver:\n    pass\n",
        "class Risc0Prover:\n    pass\n",
        "class Backend:\n    name = 'x'\n\n    def attest(self, statement):\n        return None\n",
        "class Backend:\n    name = 'x'\n\n    async def prove(self, statement):\n        return None\n",
        "import subprocess\n\n\nclass NoProofSystem:\n    name = 'none'\n\n"
        "    def prove(self, t):\n        return subprocess.run(['cargo', 'run'])\n",
        "class NoProofSystem:\n    name = 'none'\n\n    class Inner:\n        pass\n",
        "from api import NoProofSystem as N\n\nstat = Statistic(proof_system=Elsewhere())\n",
        "class NoProofSystem(ZkBackend):\n    name: str = 'none'\n",
        "import risc_runner\n\n\nclass NoProofSystem:\n    name = 'none'\n    prove = risc_runner.prove\n",
        "from zk import Backend\n\nNoProofSystem = Backend\n",
        "from zk import Backend as NoProofSystem\n",
        "def NoProofSystem():\n    pass\n",
        "@register\nclass NoProofSystem:\n    name = 'none'\n",
        "class Zk(ProofSystem, Exception):\n    pass\n",
        "Q = PS\nfrom x import ProofSystem as PS\n\n\nclass Z(Q):\n    pass\n",
        "X = ProofSystem\nX = FidelityProver\n\n\nclass Z(X):\n    pass\n",
    ],
)
def test_the_ways_around_a_name_check_are_closed(tmp_path: Path, source: str) -> None:
    """Aliases, a name that only looks allowed, a prover with no telltale base, a class that
    keeps the allowed name but does something."""
    _tree(tmp_path, {"flux_engine/zk/impl.py": source})
    kinds = {k for k, _ in _proof(tmp_path)}
    assert kinds == {"ProofSystem implementation"}, source


@pytest.mark.unit
def test_a_python_file_with_a_byte_order_mark_is_read_like_python_reads_it(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"\xef\xbb\xbfclass Zk(ProofSystem):\n    name = 'zk'\n")
    (tmp_path / "pyproject.toml").write_bytes(
        b"\xef\xbb\xbf[project]\nname='x'\ndependencies=['risc0-zkvm']\n"
    )
    assert sorted(_proof(tmp_path)) == [
        ("ProofSystem implementation", "a.py"),
        ("proof backend dependency", "pyproject.toml"),
    ]


@pytest.mark.unit
def test_a_large_python_file_is_still_checked(tmp_path: Path) -> None:
    padding = "# " + "x" * 6_000_000 + "\n"  # past the term scan's 5 MB limit
    (tmp_path / "big.py").write_text(padding + "class Zk(ProofSystem):\n    pass\n")
    assert _proof(tmp_path) == [("ProofSystem implementation", "big.py")]


@pytest.mark.unit
def test_a_pyproject_with_tables_of_the_wrong_type_is_not_a_crash(tmp_path: Path) -> None:
    for number, toml in enumerate(
        [
            "[project]\noptional-dependencies = ['x']\n",
            "tool = 'x'\n",
            "project = 'x'\n",
            "[project]\nentry-points = [1]\ndependencies = 'x'\n",
            "[tool]\nuv = 'x'\n[dependency-groups]\ndev = 'x'\n",
        ]
    ):
        _tree(tmp_path, {f"p{number}/pyproject.toml": toml})
    assert _proof(tmp_path) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "line",
    [
        "from flux_engine.proof import risc0",
        "import flux_engine.proof.risc0",
        "from . import sp1",
        "from flux_engine.proof.noir_backend import x",
    ],
)
def test_importing_a_backend_submodule_is_found_too(tmp_path: Path, line: str) -> None:
    _tree(tmp_path, {"a.py": line + "\n"})
    assert _proof(tmp_path) == [("proof backend import", "a.py")]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("toml", "detail"),
    [
        ("[project]\nname='x'\ndependencies=['py-sp1>=1']\n", "py-sp1"),
        ("[project]\nname='x'\ndependencies=['pyrisc0']\n", "pyrisc0"),
        ("[project]\nname='x'\ndependencies=['Nargo_Runner']\n", "Nargo_Runner"),
    ],
)
def test_a_distribution_name_is_matched_by_its_words(
    tmp_path: Path, toml: str, detail: str
) -> None:
    _tree(tmp_path, {"pyproject.toml": toml})
    assert [(f.kind, f.detail) for f in guard.check_proof_material(tmp_path)] == [
        ("proof backend dependency", detail)
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("toml", "detail"),
    [
        (
            "[project.entry-points.'flux_engine.backends']\nzk = 'flux_engine.risc0:Backend'\n",
            "flux_engine.backends: zk",
        ),
        (
            "[project.entry-points.'flux_engine.backends']\nsp1 = 'a:b'\n",
            "flux_engine.backends: sp1",
        ),
        ("[project.entry-points.'flux.provers']\nx = 'a:b'\n", "flux.provers"),
        ("[project.entry-points.'flux.proofs']\nx = 'a:b'\n", "flux.proofs"),
        ("[project.entry-points.'flux.proofsystems']\nx = 'a:b'\n", "flux.proofsystems"),
        (
            "[project.entry-points.'flux_engine.ProofSystems']\nx = 'a:b'\n",
            "flux_engine.ProofSystems",
        ),
    ],
)
def test_an_entry_point_is_found_by_its_group_its_name_or_its_target(
    tmp_path: Path, toml: str, detail: str
) -> None:
    _tree(tmp_path, {"pyproject.toml": toml})
    assert [(f.kind, f.detail) for f in guard.check_proof_material(tmp_path)] == [
        ("proof backend entry point", detail)
    ]


# ------------------------------------------------- a list of files, and the command
#
# The command looks at the files git knows, not at every file under the directory: a
# working copy has a .venv and build output, and a backend's name in someone's
# site-packages is not this repository's business.


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _tree(root, {**_PUBLIC_SEAM, ".gitignore": ".venv/\n"})
    _git(root, "add", "-A")
    return root


@pytest.mark.unit
def test_a_list_of_files_finds_what_a_walk_finds_in_those_files_and_nothing_else(
    tmp_path: Path,
) -> None:
    files = {
        **_PUBLIC_SEAM,
        "spikes/a/b.txt": "x\n",
        "docs/adr/0007-x.md": "x\n",
        "crates/lib.rs": "fn main() {}\n",
        "risc0/mod.py": "x = 1\n",
    }
    _tree(tmp_path, {**files, ".venv/lib/thing.rs": "not ours\n"})
    listed = {(f.kind, f.path) for f in guard.check_proof_material(tmp_path, sorted(files))}
    assert listed == {
        ("proof spike directory", "spikes"),
        ("proof-spike ADR", "docs/adr/0007-x.md"),
        ("Cargo, Rust, Noir or zkVM source", "crates/lib.rs"),
        ("proof backend module", "risc0"),
    }
    walked = {(f.kind, f.path) for f in guard.check_proof_material(tmp_path)}
    assert walked - listed == {("Cargo, Rust, Noir or zkVM source", ".venv/lib/thing.rs")}


@pytest.mark.unit
def test_the_command_is_quiet_on_the_public_seam(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert guard.main(["--root", str(repo)]) == 0
    assert "no proof material in" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "spikes/r1/notes.md",
        "docs/adr/0007-a-decision.md",
        "guest/src/main.rs",
        "circuits/main.nr",
        "Cargo.toml",
        "flux_engine/risc0_backend.py",
    ],
)
def test_the_command_fails_on_a_new_file_git_has_not_seen_yet(
    repo: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    _tree(repo, {path: "x = 1\n"})  # new and not ignored: `git add -A` would pick it up
    assert guard.main(["--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "proof material found (" in out and "out of scope here: see CONTRIBUTING.md" in out


@pytest.mark.unit
def test_a_class_that_implements_the_port_fails_the_command_at_its_line(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = "from flux_engine.api.fidelity import ProofSystem\n\n\nclass Mine(ProofSystem):\n    name = 'x'\n"
    _tree(repo, {"flux_engine/backends.py": source})
    assert guard.main(["--root", str(repo)]) == 1
    assert (
        "ProofSystem implementation: flux_engine/backends.py:4  class Mine"
        in capsys.readouterr().out
    )


@pytest.mark.unit
def test_ignored_files_and_files_deleted_from_the_working_copy_are_not_looked_at(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(repo, {".venv/lib/site-packages/thing.rs": "x\n", "gone.rs": "x\n"})
    _git(repo, "add", "gone.rs")
    (repo / "gone.rs").unlink()  # still in the index, no longer on disk
    assert guard.main(["--root", str(repo)]) == 0
    capsys.readouterr()


@pytest.mark.unit
def test_a_directory_git_does_not_know_needs_walk_and_a_missing_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # do not look above it
    plain = tmp_path / "plain"
    _tree(plain, {"a/thing.rs": "x\n"})
    assert guard.main(["--root", str(plain)]) == 2
    assert "pass --walk" in capsys.readouterr().err
    assert guard.main(["--root", str(plain), "--walk"]) == 1
    assert "a/thing.rs" in capsys.readouterr().out
    assert guard.main(["--root", str(tmp_path / "missing")]) == 2
    assert "is not a directory" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_this_repository_passes_its_own_guard(capsys: pytest.CaptureFixture[str]) -> None:
    assert guard.main(["--root", str(ROOT)]) == 0, capsys.readouterr().out


@pytest.mark.unit
def test_the_guard_is_a_merge_gate_and_the_local_targets_run_it() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    job = workflow["jobs"]["proof-guard"]
    assert job["name"] == "Proof material" and "proof-guard" in workflow["jobs"]["ci-gate"]["needs"]
    assert "if" not in job and "continue-on-error" not in job, "it must run on every event"
    steps = job["steps"]
    assert [step["run"] for step in steps if "run" in step] == ["make proof-guard"]
    assert not any("continue-on-error" in step or "if" in step for step in steps)

    makefile = (ROOT / "Makefile").read_text("utf-8")
    flat = re.sub(r"\\\n\s*", " ", makefile)
    for target in ("ci-local", "check"):
        rule = re.search(rf"^{target}:([^=\n]*)$", flat, re.MULTILINE)
        assert rule and "proof-guard" in rule.group(1).split(), f"`make {target}` must run it"
    assert re.search(
        r"^proof-guard:\n\t\$\(BIN\)python scripts/check_proof_material\.py$", makefile, re.M
    )


# ------------------------------------------------------------------- quieter routes
#
# The guard is a tripwire, not a lock (see its docstring), but the routes below are ones a
# person takes by accident or habit, so they are closed: another way to bind the port's
# name, a conditional base, a class built by `type`, a backend named in a string or in a
# less common pyproject table, and an allowed class that carries a backend's name.


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "PS: type = ProofSystem\n\n\nclass X(PS):\n    name = 'x'\n",
        "PS, Q = ProofSystem, 1\n\n\nclass X(PS):\n    name = 'x'\n",
        "class X(ProofSystem if True else object):\n    name = 'x'\n",
        "class X(object or ProofSystem):\n    name = 'x'\n",
        "X = type('X', (ProofSystem,), {})\n",
        "import types\n\nX = types.new_class('X', (ProofSystem,))\n",
    ],
)
def test_the_quiet_ways_to_implement_the_port_are_found(tmp_path: Path, source: str) -> None:
    header = "from flux_engine.api.fidelity import ProofSystem\n\n\n"
    _tree(tmp_path, {"flux_engine/y.py": header + source})
    assert [kind for kind, _ in _proof(tmp_path)] == ["ProofSystem implementation"]


@pytest.mark.unit
def test_building_other_classes_with_type_is_not_a_finding(tmp_path: Path) -> None:
    source = "Base = object\nX = type('X', (Base,), {})\nkind = type(X)\n"
    _tree(tmp_path, {**_PUBLIC_SEAM, "flux_engine/y.py": source})
    assert _proof(tmp_path) == []


@pytest.mark.unit
@pytest.mark.parametrize("body", ["name = 'risc0'", "name: str = 'sp1'", "label = 'halo2-demo'"])
def test_the_allowed_class_may_not_carry_a_backends_name(tmp_path: Path, body: str) -> None:
    _tree(tmp_path, {"flux_engine/n.py": f"class NoProofSystem:\n    {body}\n"})
    assert _proof(tmp_path) == [("ProofSystem implementation", "flux_engine/n.py")]


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "import importlib\n\nm = importlib.import_module('risc0_host')\n",
        "m = __import__('sp1_sdk')\n",
        "from importlib import import_module\n\nm = import_module('vendor.halo2.api')\n",
        "import plonky3\n",
    ],
)
def test_a_backend_named_in_an_import_call_or_a_new_stem_is_found(
    tmp_path: Path, source: str
) -> None:
    _tree(tmp_path, {"a.py": source})
    assert _proof(tmp_path) == [("proof backend import", "a.py")]


@pytest.mark.unit
def test_importing_other_things_by_name_is_not_a_finding(tmp_path: Path) -> None:
    source = (
        "import importlib\n\nname = 'yaml'\nm = importlib.import_module(name)\n"
        "n = importlib.import_module('yaml')\nk = __import__('os')\nplonked = 1\n"
    )
    _tree(tmp_path, {"a.py": source})
    assert _proof(tmp_path) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "toml",
    [
        "[build-system]\nrequires = ['setuptools', 'risc0-build']\n",
        "[tool.poetry.dependencies]\npython = '^3.11'\nsp1-sdk = '*'\n",
        "[tool.poetry.group.dev.dependencies]\nhalo2-proofs = '*'\n",
        "[tool.pdm.dev-dependencies]\ntest = ['plonky3-air']\n",
        "[tool.hatch.envs.default]\ndependencies = ['groth16-tools']\n",
        "[tool.uv.sources]\nzkvm-host = { path = '../x' }\n",
    ],
)
def test_a_backend_declared_in_any_pyproject_table_that_lists_dependencies_is_found(
    tmp_path: Path, toml: str
) -> None:
    _tree(tmp_path, {"pyproject.toml": toml})
    assert _proof(tmp_path) == [("proof backend dependency", "pyproject.toml")]


@pytest.mark.unit
def test_the_other_dependency_tables_with_the_wrong_types_are_not_a_crash(tmp_path: Path) -> None:
    toml = (
        "[build-system]\nrequires = 5\n[tool.poetry]\ndependencies = [1, 2]\ngroup = { dev = 3 }\n"
        "[tool.pdm.dev-dependencies]\ntest = 'not a list'\n[tool.hatch.envs]\ndefault = 4\n"
        "[tool.uv.sources]\n"
    )
    _tree(tmp_path, {"pyproject.toml": toml})
    assert _proof(tmp_path) == []


@pytest.mark.unit
def test_a_directory_git_ignores_or_an_empty_one_is_not_a_clean_bill_of_health(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".gitignore").write_text("build/\n")
    _tree(repo, {"build/snap/spikes/n": "x\n", "build/snap/x/a.rs": "x\n"})
    (repo / "empty").mkdir()
    for directory in ("build/snap", "empty"):
        assert guard.main(["--root", str(repo / directory)]) == 2
        assert "git knows no files under" in capsys.readouterr().err
    assert guard.main(["--root", str(repo / "build" / "snap"), "--walk"]) == 1
