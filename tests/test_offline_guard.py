# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The offline guarantee's checks that need no network namespace (docs/explanation/offline.md).

FLUX needs no internet to produce results, and nothing in it talks to a language model. Four
properties keep that true, and each fails here, by name, when a change breaks it:

1. no module imports a network or model client outside the few places that exist to reach a
   target the person names (the sinks, the OpenLineage emitter);
2. every pytest run CI makes carries the ``OFFLINE`` flags, which block sockets except to this
   machine, so a test or the code it runs cannot reach another host unnoticed;
3. those flags do what they say, in a pytest process of their own;
4. no test opts out of the block, and a future one that must is a reviewed change to this file.

The whole-program check, a real CLI journey in a network namespace with no interface, is the
``Offline journey`` job in .github/workflows/ci.yml.
"""

from __future__ import annotations

import ast
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOTS = (REPO_ROOT / "flux_engine", REPO_ROOT / "packages" / "flux-stats" / "src")

pytestmark = pytest.mark.unit

# ------------------------------------------------------------------ 1. no client of a network

#: A module that opens a connection, or a client of a hosted model or a cloud, by top-level name or
#: dotted prefix. Whatever a person's code does with their own model, nothing in FLUX imports one.
NETWORK_MODULES: FrozenSet[str] = frozenset(
    {
        "socket",
        "socketserver",
        "ssl",
        "http.client",
        "http.server",
        "urllib.request",
        "urllib3",
        "requests",
        "httpx",
        "aiohttp",
        "websocket",
        "websockets",
        "smtplib",
        "ftplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "xmlrpc",
        "grpc",
        "boto3",
        "botocore",
        "fsspec",
        "openai",
        "anthropic",
        "litellm",
        "langchain",
        "langchain_core",
        "llama_index",
        "ollama",
        "cohere",
        "mistralai",
        "transformers",
        "huggingface_hub",
        "google.generativeai",
        "google.genai",
        "vertexai",
    }
)

#: The only places a client of a target the person names is imported, and what each may import.
#: A new entry is a reviewed change: say in the PR what it reaches and when.
ALLOWED: Dict[str, Tuple[str, ...]] = {
    # The OpenLineage client carries a run event to the URL the person's own code gives the emitter.
    "flux_engine/lineage/emitter.py": ("openlineage",),
    # url2pathname turns a file:// URL into a path; no request is made.
    "flux_engine/generators/registry.py": ("urllib.request",),
    # getaddrinfo resolves the Kafka brokers' names, the target's and those the cluster advertises,
    # only when a credential would go over SASL_PLAINTEXT, to check every address is this machine;
    # it opens no connection.
    "flux_engine/sinks/_kafka_broker.py": ("socket",),
}

#: Libraries only the sinks may import: each writes to a target named on the command line.
SINK_ONLY: FrozenSet[str] = frozenset({"obstore", "pyiceberg", "deltalake", "adbc_driver_manager"})

#: Calls whose first argument names a module, so a lazy import is read like a ``import`` line.
IMPORTING_CALLS: FrozenSet[str] = frozenset(
    {"import_module", "__import__", "require", "importorskip"}
)


def _called_module(node: ast.Call) -> str:
    """The module a lazy import call names (``import_module("x")``), or an empty string."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    first = node.args[0] if node.args else None
    if name in IMPORTING_CALLS and isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return ""


def _modules_named(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Call) and _called_module(node):
            yield node.lineno, _called_module(node)


def _matches(module: str, banned: str) -> bool:
    return module == banned or module.startswith(banned + ".")


def _sources() -> List[Path]:
    return sorted(path for root in SOURCE_ROOTS for path in root.rglob("*.py"))


def _is_allowed(relative: str, module: str, banned: str) -> bool:
    if any(_matches(module, ok) for ok in ALLOWED.get(relative, ())):
        return True
    return banned in SINK_ONLY and relative.startswith("flux_engine/sinks/")


def _violations(path: Path) -> List[str]:
    relative = path.relative_to(REPO_ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    return [
        f"{relative}:{line}: {module}"
        for line, module in _modules_named(tree)
        for banned in NETWORK_MODULES | SINK_ONLY
        if _matches(module, banned) and not _is_allowed(relative, module, banned)
    ]


def test_no_module_imports_a_network_or_model_client_outside_the_reviewed_places() -> None:
    sources = _sources()
    found = [violation for path in sources for violation in _violations(path)]
    assert len(sources) > 100, "the scan found no source; its roots moved"
    assert not found, (
        "a network or model client is imported where none is allowed (docs/explanation/offline.md); "
        "if the new import reaches only a target the person names, add it to ALLOWED with the "
        "reason:\n  " + "\n  ".join(found)
    )


def test_every_allowed_import_is_still_there() -> None:
    """A stale entry would let the next import into that file pass unreviewed."""
    for relative, modules in ALLOWED.items():
        path = REPO_ROOT / relative
        assert path.is_file(), f"{relative} is allowed to import {modules} but does not exist"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [module for _, module in _modules_named(tree)]
        for module in modules:
            assert any(
                _matches(name, module) for name in names
            ), f"{relative} no longer imports {module}"


# ------------------------------------------------------------ 2. every CI pytest run is guarded


def _makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def _offline_flags() -> List[str]:
    match = re.search(r"^OFFLINE \?= (.+)$", _makefile(), re.MULTILINE)
    assert match, "the Makefile defines no OFFLINE flags"
    return shlex.split(match.group(1))


#: Runs that are not CI test runs: the benchmark (serial, a developer's) and the one that
#: only measures durations to rewrite tests/fast-durations.json.
UNGUARDED_TARGETS: FrozenSet[str] = frozenset({"bench", "fast-durations"})


def test_the_offline_flags_block_sockets_except_to_this_machine() -> None:
    flags = _offline_flags()
    assert "--disable-socket" in flags
    assert "--allow-unix-socket" in flags
    [hosts] = [flag for flag in flags if flag.startswith("--allow-hosts=")]
    assert set(hosts.partition("=")[2].split(",")) == {"127.0.0.1", "::1", "localhost"}


def test_every_pytest_recipe_in_the_makefile_carries_the_offline_flags() -> None:
    target = ""
    unguarded: List[str] = []
    guarded = 0
    for line in _makefile().splitlines():
        header = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
        if header:
            target = header.group(1)
        if not line.startswith("\t") or "$(BIN)pytest" not in line:
            continue
        if "--collect-only" in line or target in UNGUARDED_TARGETS:
            continue
        if "$(OFFLINE)" in line:
            guarded += 1
        else:
            unguarded.append(f"{target}: {line.strip()}")
    assert guarded >= 10, "found almost no guarded pytest recipe; the Makefile's shape changed"
    assert not unguarded, "a pytest recipe runs without $(OFFLINE):\n  " + "\n  ".join(unguarded)


# ------------------------------------------------------------ 3. the flags do what they say

_PROBE_TESTS = textwrap.dedent("""
    import socket

    import pytest
    from pytest_socket import SocketBlockedError, SocketConnectBlockedError


    def test_an_outside_address_is_blocked():
        with pytest.raises((SocketBlockedError, SocketConnectBlockedError)):
            socket.create_connection(("192.0.2.1", 9), timeout=1)


    def test_loopback_works():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=5)
        peer, _ = server.accept()
        client.sendall(b"x")
        assert peer.recv(1) == b"x"
        for each in (client, peer, server):
            each.close()


    def test_a_unix_socket_pair_works():
        left, right = socket.socketpair()
        left.sendall(b"y")
        assert right.recv(1) == b"y"
        left.close()
        right.close()
    """)


def test_the_flags_block_outside_hosts_and_allow_loopback_and_unix_sockets(tmp_path: Path) -> None:
    (tmp_path / "test_probe.py").write_text(_PROBE_TESTS, encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "--rootdir",
            str(tmp_path),
            *_offline_flags(),
            str(tmp_path / "test_probe.py"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 passed" in done.stdout


# ----------------------------------------------------------------- 4. nobody opts out unreviewed

#: The tests allowed to re-enable sockets, each as a path relative to the repository. Empty: no
#: test needs the network. Adding one is a reviewed claim that the test must reach a real host,
#: and the PR says which and why.
NEEDS_THE_NETWORK: FrozenSet[str] = frozenset()

_OPT_OUT = re.compile(
    r"enable_socket|socket_enabled|force-enable-socket|\.disable_socket|enable_socket\("
)


def test_no_test_re_enables_sockets_unreviewed() -> None:
    this_file = Path(__file__).resolve()
    users: Set[str] = set()
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")) + sorted(
        (REPO_ROOT / "packages" / "flux-stats" / "tests").rglob("*.py")
    ):
        if path.resolve() == this_file:
            continue
        if _OPT_OUT.search(path.read_text(encoding="utf-8")):
            users.add(path.relative_to(REPO_ROOT).as_posix())
    assert users == set(NEEDS_THE_NETWORK), (
        "a test opts out of the offline guard; list it in NEEDS_THE_NETWORK with the reason, or "
        f"remove the opt-out: {sorted(users - NEEDS_THE_NETWORK)}"
    )
