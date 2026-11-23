# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fake installed distributions for the generator registry tests, built in ``tmp_path``."""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence

import pytest
import yaml

from flux_engine.generators import registry

PACKAGE = "fxgenpkg"
GROUP = registry.ENTRY_POINT_GROUP

#: A plugin that generates the columns of ``tests/fixtures/telco-fidelity-small`` (a timestamp
#: inside its range, a decimal inside its range, a string out of its levels). Each value is a
#: function of the seed, the column and the row alone, so any split of a range gives the same
#: values (reproducibility ``bytes``). ``options["misbehave"]`` makes it return one bad value, at
#: the row ``options["at"]``: a value of the wrong ``type``, out of ``range``, outside the
#: ``levels``, a string too ``long``, a ``null`` in a required column, a list too ``short`` or a
#: list too ``many``. ``options["stated"]`` sets the reproducibility it states. The module keeps
#: every ``(start, stop)`` it is called with in ``CALLS``.
GENERATING_PLUGIN = """
import hashlib
from datetime import datetime, timezone
from decimal import Decimal


CALLS = []


def _word(seed, name, row):
    material = seed + name.encode("utf-8") + row.to_bytes(8, "big")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def _instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Generator:
    def __init__(self, options):
        self.options = dict(options)

    def statement(self, request):
        return {"grade": "P5", "reproducibility": self.options.get("stated", "bytes")}

    def _value(self, column, seed, row):
        word = _word(seed, column.name, row)
        declared = column.constraints.get("range") or {}
        low = declared.get("min")
        high = declared.get("max")
        if column.type == "TIMESTAMP":
            low, high = low or "2026-01-01T00:00:00Z", high or "2026-12-31T00:00:00Z"
            first, last = _instant(low), _instant(high)
            return (first + (last - first) * (word % 10000) / 10000).replace(microsecond=0)
        low, high = low or "0", high or "100"
        if column.type.startswith("DECIMAL"):
            return (Decimal(low) + (Decimal(high) - Decimal(low)) * (word % 10000) / 10000).quantize(
                Decimal("0.0001")
            )
        if column.type == "INT":
            return int(low) + word % (int(high) - int(low) + 1)
        levels = column.constraints.get("levels")
        return levels[word % len(levels)] if levels else "v%d" % (word % 100)

    def generate(self, request, start, stop):
        CALLS.append((start, stop))
        out = []
        mode = self.options.get("misbehave")
        at = int(self.options.get("at", "3"))
        for column in request.columns:
            values = [self._value(column, request.seed, row) for row in range(start, stop)]
            if mode and start <= at < stop:
                index = at - start
                if mode == "type" and column.type == "STRING":
                    values[index] = 7
                elif mode == "range" and column.type.startswith("DECIMAL"):
                    values[index] = Decimal(column.constraints["range"]["max"]) + 1
                elif mode == "levels" and column.type == "STRING":
                    values[index] = "ZZ"
                elif mode == "long" and column.type == "STRING":
                    values[index] = "x" * 50
                elif mode == "null" and not column.nullable:
                    values[index] = None
                elif mode == "short" and column.type == "STRING":
                    values.pop()
                elif mode == "many" and column.type == "STRING":
                    values.append("IE")
            out.append(values)
        return out


def factory(options):
    return Generator(options)
"""


def make_distribution(
    site: Path,
    *,
    dist: str = "fxgen-plugin",
    version: str = "1.0",
    entry: str = "learned",
    value: str = f"{PACKAGE}.plugin:factory",
    package_dir: Optional[Path] = None,
    record: bool = True,
    record_skips: Sequence[str] = (),
    editable: bool = False,
    metadata_dir: Optional[Path] = None,
    source_url: Optional[str] = None,
    plugin_source: Optional[str] = None,
) -> Path:
    """Write ``fxgenpkg`` and its ``.dist-info`` under *site*; return the package directory.

    *package_dir* puts the package elsewhere (an editable install's source
    tree), and *metadata_dir* puts the ``.dist-info`` directory elsewhere. An editable install
    records the directory above the package as its source, in ``direct_url.json``, unless
    *source_url* says another.
    *plugin_source* replaces the module the entry point names, for a test that runs a
    plugin (:data:`GENERATING_PLUGIN`).
    """
    package = package_dir if package_dir is not None else site / PACKAGE
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "plugin.py").write_text(
        (
            plugin_source
            if plugin_source is not None
            else "def factory(options):\n    return ('plugin', dict(options))\n"
        ),
        encoding="utf-8",
    )
    stem = dist.replace("-", "_")  # a real .dist-info folder escapes hyphens in the name
    info = (metadata_dir if metadata_dir is not None else site) / f"{stem}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(f"[{GROUP}]\n{entry} = {value}\n", encoding="utf-8")
    if editable:
        (info / "direct_url.json").write_text(
            json.dumps(
                {
                    "url": source_url if source_url is not None else package.parent.as_uri(),
                    "dir_info": {"editable": True},
                }
            ),
            encoding="utf-8",
        )
    if record:
        listed = [
            f"{PACKAGE}/__init__.py",
            f"{PACKAGE}/plugin.py",
            f"{info.name}/METADATA",
            f"{info.name}/entry_points.txt",
        ]
        lines = [f"{path},," for path in listed if path not in record_skips]
        (info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return package


def _forget_package() -> None:
    for name in [m for m in sys.modules if m == PACKAGE or m.startswith(f"{PACKAGE}.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


@pytest.fixture(autouse=True)
def _clean_modules() -> Iterator[None]:
    _forget_package()
    yield
    _forget_package()


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake site directory on ``sys.path`` that counts as the installation directory."""
    directory = tmp_path / "site"
    directory.mkdir()
    monkeypatch.syspath_prepend(str(directory))
    monkeypatch.setattr(registry, "installation_directories", lambda: (directory.resolve(),))
    return directory


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Another directory on ``sys.path`` that is not an installation directory."""
    directory = tmp_path / "elsewhere"
    directory.mkdir()
    monkeypatch.syspath_prepend(str(directory))
    return directory


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"
EXPOSE = "payment_recovery_moment"
NAMESPACE = "com.agentics.flux/1"


def plugin_bundle(
    tmp_path: Path,
    *,
    name: str = "bundle",
    options: Optional[Dict[str, Any]] = None,
    **declared: Any,
) -> Path:
    """A copy of the small fixture bundle whose one expose is declared under ``generators``.

    The declaration is the one :func:`make_distribution`'s defaults satisfy: entry point
    ``learned``, distribution ``fxgen-plugin`` at ``1.0``, grade ``P5``, reproducibility
    ``bytes``. *declared* replaces any part of it.
    """
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    entry: Dict[str, Any] = {
        "plugin": "learned",
        "distribution": "fxgen-plugin",
        "version": "1.0",
        "grade": "P5",
        "reproducibility": "bytes",
    }
    entry.update(declared)
    if options is not None:
        entry["options"] = options
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["extensions"][NAMESPACE]["generators"] = {EXPOSE: entry}
    # A plugin's values are not drawn to the fixture's declared mean, so the property that
    # checks it is left out; the support property, which the levels keep, stays.
    fidelity = document["extensions"][NAMESPACE]["emits"][EXPOSE]["fidelity"]
    fidelity["properties"] = [item for item in fidelity["properties"] if item["property"] != "mean"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return target


@pytest.fixture
def installed(site: Path) -> Path:
    """The generating test plugin, installed under the fake installation directory."""
    make_distribution(site, plugin_source=GENERATING_PLUGIN)
    return site


CONTRACT = "payment-recovery.fluid.yml"
PRODUCT = "telco.gold.payment_recovery_moment"
_LAST_COLUMN = "        - name: market\n          type: STRING\n"


def add_invariants(bundle: Path, invariants: str) -> None:
    """Add ``invariants`` (YAML at the contract block's indent) to the fixture's contract and re-pin it.

    Invariants are FLUID 0.7.7's, so the contract moves to that version.
    """
    from flux_engine.core.digest import digest_of_document
    from flux_engine.seam.resolve import load_documents_exact

    contract = bundle / CONTRACT
    text = contract.read_text(encoding="utf-8")
    assert text.count(_LAST_COLUMN) == 1
    assert 'fluidVersion: "0.7.5"' in text
    contract.write_text(
        text.replace('fluidVersion: "0.7.5"', 'fluidVersion: "0.7.7"').replace(
            _LAST_COLUMN, _LAST_COLUMN + "      invariants:\n" + invariants, 1
        ),
        encoding="utf-8",
    )
    (document,) = [doc for doc in load_documents_exact(contract) if doc.get("id") == PRODUCT]
    path = bundle / "simulation.flux.yml"
    simulation = yaml.safe_load(path.read_text(encoding="utf-8"))
    (emit,) = simulation["spec"]["emits"]
    emit["fluidVersion"] = "0.7.7"  # a preview version is opted into by pinning it exactly
    emit["provenance"]["contractDigest"] = digest_of_document(
        document, subject=f"contractDigest:{PRODUCT}"
    ).value
    path.write_text(yaml.safe_dump(simulation, sort_keys=False), encoding="utf-8")


def edit_emit(bundle: Path, change: Any) -> None:
    """Change the fixture's emit block (``extensions[namespace].emits[expose]``) with *change*."""
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document["extensions"][NAMESPACE]["emits"][EXPOSE])
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
