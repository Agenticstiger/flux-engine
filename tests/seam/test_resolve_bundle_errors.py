# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a bundle that cannot be loaded says to the person who typed the path.

Two failures, both of which used to be reported as ``FLX-3105`` ("bundle
does not validate, so its seam cannot be resolved"), whose catalogue entry
is about reference resolution and id uniqueness:

* a mistyped path, which never reached the validator at all -- and which
  was printed twice, because the joined validator errors were already
  path-prefixed;
* one unreadable or schema-invalid document, whose failure was first in a
  semicolon-joined run-on line and was followed by a *derived* error for
  every reference into it, each asserting that a document sitting on disk
  is "no such document in bundle".
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam import resolve as resolve_module
from flux_engine.seam.resolve import SeamBundle, flux_schema_validator, load_bundle
from flux_engine.spec.validate import Bundle
from flux_stats.exact_yaml import FLX_3105
from tests.bundles import by_file

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.unit


@pytest.fixture()
def bundle_copy(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE_BUNDLE, target)
    return target


def test_a_path_that_does_not_exist_is_its_own_code(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as raised:
        load_bundle(tmp_path / "nope")
    error = raised.value
    assert error.code.code == "FLX-3107"
    assert "is not a directory" in error.detail
    # Named once, not twice: the old message prefixed the path and then
    # printed a validator error that already began with the same path.
    assert error.detail.count(str(tmp_path / "nope")) == 1


def test_a_file_inside_the_bundle_is_not_the_bundle(bundle_copy: Path) -> None:
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy / "simulation.flux.yml")
    assert raised.value.code.code == "FLX-3107"


def test_the_new_code_explains_itself() -> None:
    from flux_engine.core.errors import explain, is_registered

    assert is_registered("FLX-3107")
    text = explain("FLX-3107")
    assert "directory" in text


def test_the_closed_set_code_explains_itself() -> None:
    """`fluxsim explain FLX-3108` has to be real, not a KeyError."""
    from flux_engine.core.errors import explain, is_registered

    assert is_registered("FLX-3108")
    text = explain("FLX-3108")
    assert "flux-manifest.json" in text
    assert "*.flux.yml" in text


def test_an_unparseable_document_reports_file_line_and_column(bundle_copy: Path) -> None:
    with (bundle_copy / "persona.flux.yml").open("a", encoding="utf-8") as handle:
        handle.write("\n  : broken\n")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    detail = raised.value.detail
    assert "persona.flux.yml" in detail
    assert "line " in detail and "column " in detail


def test_a_broken_document_suppresses_the_errors_derived_from_it(bundle_copy: Path) -> None:
    """The reference checks read what loaded, so they cannot be trusted here."""
    with (bundle_copy / "persona.flux.yml").open("a", encoding="utf-8") as handle:
        handle.write("\n  : broken\n")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    detail = raised.value.detail
    assert "no such document in bundle" not in detail
    assert "dangling reference" not in detail
    assert "skipped" in detail


def test_a_schema_failure_does_the_same(bundle_copy: Path) -> None:
    text = (bundle_copy / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "fidelity: deterministic" in text
    (bundle_copy / "simulation.flux.yml").write_text(
        text.replace("fidelity: deterministic", "fidelity: banana"), encoding="utf-8"
    )
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    detail = raised.value.detail
    assert "schema:" in detail
    assert "no such document in bundle" not in detail


def test_errors_are_listed_one_per_line(bundle_copy: Path) -> None:
    """A semicolon-joined run-on is not a list a person can read."""
    (bundle_copy / "extra.flux.yml").write_text("\n  : broken\n", encoding="utf-8")
    with (bundle_copy / "persona.flux.yml").open("a", encoding="utf-8") as handle:
        handle.write("\n  : broken\n")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    lines = [line for line in raised.value.detail.splitlines() if line.strip()]
    assert len(lines) >= 2
    assert all(line.startswith("  ") for line in lines)


#: More digits than CPython converts between an ``int`` and its text.
_TOO_MANY_DIGITS = "9" * 5000

#: Mapping a{n} merges a{n-1} twice: past the 100,000 pairs merge keys may copy.
_MERGE_CHAIN = (
    "\n".join(
        ["a0: &a0 {k0: 1, k1: 2}"]
        + [f"a{n}: &a{n} {{<<: [*a{n - 1}, *a{n - 1}]}}" for n in range(1, 30)]
    )
    + "\n"
)


@pytest.mark.parametrize(
    ("file_name", "old", "new", "code", "detail"),
    [
        pytest.param(
            "simulation.flux.yml",
            "rows: 64 ",
            f"rows: {_TOO_MANY_DIGITS} ",
            "FLX-1003",
            "simulation.flux.yml: an integer of more than 4300 digits is too long to read "
            "(line 23, column 15)",
            id="a-decimal-integer-the-validator-could-not-read",
        ),
        pytest.param(
            "simulation.flux.yml",
            "name: Payment Recovery Simulation",
            "name: 0x" + "f" * 3600,
            "FLX-1003",
            "simulation.flux.yml: an integer of more than 4300 digits is too long to read "
            "(line 4, column 7)",
            id="a-hex-integer-a-schema-error-would-print",
        ),
        pytest.param(
            "payment-recovery.fluid.yml",
            "name: Payment Recovery Moment",
            f"name: {_TOO_MANY_DIGITS}",
            "FLX-1003",
            "payment-recovery.fluid.yml: an integer of more than 4300 digits is too long to "
            "read (line 6, column 7)",
            id="in-the-contract",
        ),
        pytest.param(
            "flux.lock",
            "lockVersion: 1",
            f"lockVersion: {_TOO_MANY_DIGITS}",
            "FLX-1003",
            "flux.lock: an integer of more than 4300 digits is too long to read "
            "(line 2, column 14)",
            id="in-the-lockfile-the-supply-chain-check-reads",
        ),
        pytest.param(
            "flux.lock",
            "lockVersion: 1",
            "lockVersion: !!int ''",
            "FLX-3105",
            "flux.lock: a scalar read as an integer has no integer value (line 2, column 14)",
            id="an-empty-tagged-integer-in-the-lockfile",
        ),
        pytest.param(
            "simulation.flux.yml",
            "rows: 64 ",
            "rows: 0b_ ",
            "FLX-3105",
            "simulation.flux.yml: a scalar read as an integer has no integer value "
            "(line 23, column 15)",
            id="an-integer-spelling-with-no-digit",
        ),
    ],
)
def test_an_integer_no_read_can_form_is_refused_before_the_validator_reads_it(
    bundle_copy: Path, file_name: str, old: str, new: str, code: str, detail: str
) -> None:
    """The validator's own read catches YAML errors only, so each of these
    escaped it as a bare ``ValueError``: from PyYAML's ``int()``, or from the
    schema message that printed the hex value."""
    document = bundle_copy / file_name
    text = document.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{file_name} no longer holds {old!r} once"
    document.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == code
    assert raised.value.detail == detail


@pytest.mark.parametrize(
    "unread",
    [
        pytest.param(f"\nunread: {_TOO_MANY_DIGITS}\n".encode(), id="an-integer-too-long"),
        pytest.param(b"\nunread: \xff\n", id="a-byte-that-is-not-utf-8"),
        pytest.param(b"\n" + _MERGE_CHAIN.encode(), id="merge-keys-past-the-bound"),
    ],
)
def test_a_lockfile_the_supply_chain_check_never_reads_keeps_its_outcome(
    bundle_copy: Path, unread: bytes
) -> None:
    """With no versioned ref the validator never opens ``flux.lock``, so
    nothing in it can change whether the bundle loads."""
    simulation = bundle_copy / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    assert text.count("module-payment-recovery@^2.1") == 1, "the fixture's versioned ref moved"
    simulation.write_text(text.replace("@^2.1", ""), encoding="utf-8")
    lock = bundle_copy / "flux.lock"
    lock.write_bytes(lock.read_bytes() + unread)
    assert load_bundle(bundle_copy).flux_documents


def test_a_lockfile_the_supply_chain_check_reads_is_held_to_the_bounds_first(
    bundle_copy: Path,
) -> None:
    """The validator reads ``flux.lock`` with a loader that bounds nothing."""
    lock = bundle_copy / "flux.lock"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n" + _MERGE_CHAIN, encoding="utf-8")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    assert "flux.lock: merge keys (<<) copy more than" in (raised.value.detail or "")


#: The same chain 17 merges deep: past the bound at a15, and cheap to build
#: should a read miss it.
_SHORT_MERGE_CHAIN = "\n".join(_MERGE_CHAIN.splitlines()[:18]) + "\n"


def _validator_reads(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """The names of the files the validator's own read opens, in order."""
    opened: List[str] = []
    real = resolve_module.load_yaml_docs

    def recording(path: Path) -> Any:
        opened.append(path.name)
        return real(path)

    monkeypatch.setattr(resolve_module, "load_yaml_docs", recording)
    return opened


def test_a_character_yaml_does_not_allow_is_refused_before_the_validator_reads_it(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyYAML refuses such text when a loader is made from it, so the check
    measured nothing, while the validator, reading the file in pieces, would
    build every document before the one that holds the character."""
    opened = _validator_reads(monkeypatch)
    segment = bundle_copy / "segment.flux.yml"
    original = segment.read_text(encoding="utf-8")
    segment.write_text(original + "# bell \x07\n", encoding="utf-8")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    assert raised.value.detail == (
        "segment.flux.yml: unreadable YAML: unacceptable character #x0007: "
        f"special characters are not allowed (line {original.count(chr(10)) + 1}, column 8)"
    )
    assert "segment.flux.yml" not in opened


def test_a_file_that_does_not_decode_is_refused_before_the_validator_reads_it(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator decodes a file in pieces, so a byte that does not decode,
    after a document and a long comment, let it build that document unchecked:
    the check used to leave the file to it."""
    opened = _validator_reads(monkeypatch)
    segment = bundle_copy / "segment.flux.yml"
    tail = b"\n---\n# " + b"x" * 10_000 + b"\nb: \x81\n"
    segment.write_bytes(segment.read_bytes() + tail)
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    # Not UTF-8 text, nor text in most locales' encodings; Latin-1 reads it as a
    # control character, which is refused the same way.
    assert (raised.value.detail or "").startswith("segment.flux.yml: unreadable YAML")
    assert "segment.flux.yml" not in opened


@pytest.mark.parametrize(
    ("scalar", "why"),
    [
        ("!!bool maybe", "a scalar read as a boolean has no boolean value"),
        ("!!float ZZZ", "a scalar read as a float has no float value"),
    ],
)
def test_a_scalar_its_explicit_tag_cannot_build_is_refused_before_the_validator_reads_it(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch, scalar: str, why: str
) -> None:
    """The validator builds these with a plain SafeLoader, which raised a bare
    KeyError or ValueError for them."""
    opened = _validator_reads(monkeypatch)
    persona = bundle_copy / "persona.flux.yml"
    text = persona.read_text(encoding="utf-8").rstrip("\n")
    persona.write_text(f"{text}\n---\na: {scalar}\n", encoding="utf-8")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    line = text.count("\n") + 3
    assert raised.value.detail == f"persona.flux.yml: {why} (line {line}, column 4)"
    assert "persona.flux.yml" not in opened


@pytest.mark.parametrize("kind", ["directory", "device"])
def test_a_path_that_is_not_a_regular_file_is_refused_without_being_read(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Reading a FIFO could block, and reading a device such as /dev/zero
    would never end."""
    if kind == "device" and os.name != "posix":
        pytest.skip("a POSIX device")
    opened = _validator_reads(monkeypatch)
    path = bundle_copy / "zz.flux.yml"
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(os.devnull)
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    assert raised.value.detail == "zz.flux.yml: not a regular file, so it is not read"
    assert "zz.flux.yml" not in opened


def test_a_lockfile_that_is_not_a_regular_file_is_refused_when_the_check_would_read_it(
    bundle_copy: Path,
) -> None:
    """flux_spec reads ``flux.lock`` whenever the path exists, regular file or not."""
    lock = bundle_copy / "flux.lock"
    lock.unlink()
    lock.mkdir()
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    assert raised.value.detail == "flux.lock: not a regular file, so it is not read"


#: An em dash, and an N with a cedilla, whose UTF-8 bytes (C5 85) read in
#: Latin-1 as an A with a ring and a NEL, which YAML takes for a line break.
_EM_DASH = chr(0x2014)
_N_WITH_CEDILLA = chr(0x0145)


def _read_as_if_the_locale_were_latin_1(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``Path.open()`` with no encoding decode Latin-1, as a Latin-1 locale does.

    Each file of *bundle* first loses its em dashes: their UTF-8 bytes read as
    control characters in Latin-1, which no YAML read accepts.
    """
    for document in bundle.glob("*.yml"):
        text = document.read_text(encoding="utf-8").replace(_EM_DASH, "-")
        document.write_text(text, encoding="utf-8")
    real_open = Path.open

    def latin_1_by_default(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: Optional[str] = None,
        errors: Optional[str] = None,
        newline: Optional[str] = None,
    ) -> Any:
        if "b" not in mode and encoding is None:
            encoding = "latin-1"
        return real_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", latin_1_by_default)


def test_the_check_reads_a_file_as_utf8_as_the_validator_does_whatever_the_locale(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both decode a file as UTF-8 (flux-spec from 0.5.4), even where the locale's
    encoding is Latin-1, in which the UTF-8 bytes of an N with a cedilla end in a
    line break: each of these lines is a comment to both reads, so nothing hides
    from the check that the validator then reads."""
    _read_as_if_the_locale_were_latin_1(bundle_copy, monkeypatch)
    hidden = "".join(f"# {_N_WITH_CEDILLA}{line}\n" for line in _SHORT_MERGE_CHAIN.splitlines())
    segment = bundle_copy / "segment.flux.yml"
    segment.write_text(segment.read_text(encoding="utf-8") + hidden, encoding="utf-8")
    load_bundle(bundle_copy)


def test_a_file_that_is_not_utf8_is_refused_by_the_check_whatever_the_locale(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Latin-1 locale would read these bytes; UTF-8, which both reads decode, does
    not, so the check refuses the file before the validator reads it."""
    _read_as_if_the_locale_were_latin_1(bundle_copy, monkeypatch)
    segment = bundle_copy / "segment.flux.yml"
    segment.write_bytes(segment.read_bytes() + b"\nbad: [caf\xe9\n")
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    assert "segment.flux.yml: unreadable YAML (not utf-8 text)" in (raised.value.detail or "")


def test_the_exact_read_holds_every_file_of_a_bundle_to_one_budget(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator's read has one budget for the bundle, and so has the exact
    read: the two decode a file alike only where the locale's encoding is UTF-8."""
    budgets: List[Any] = []
    real = resolve_module.load_documents_exact

    def recording(path: Path, **kwargs: Any) -> Any:
        budgets.append(kwargs.get("budget"))
        return real(path, **kwargs)

    monkeypatch.setattr(resolve_module, "load_documents_exact", recording)
    load_bundle(bundle_copy)
    assert len(budgets) > 1 and budgets[0] is not None
    assert all(budget is budgets[0] for budget in budgets)


def _sizes(bundle: Path) -> Dict[str, int]:
    """Every regular file of *bundle* by name, with its size."""
    return by_file(bundle, lambda path: path.stat().st_size)


def test_the_size_caps_are_4_mib_a_file_and_flux_specs_archive_cap_a_bundle() -> None:
    from flux_engine.spec import bundle as spec_bundle

    assert resolve_module.MAX_FILE_BYTES == 4 * 1024 * 1024
    assert resolve_module.MAX_BUNDLE_READ_BYTES == 256 * 1024 * 1024
    assert resolve_module.MAX_BUNDLE_READ_BYTES == spec_bundle.MAX_BUNDLE_BYTES


def test_a_file_past_the_size_cap_is_refused_by_its_size_before_a_file_is_read(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every read of a bundle file takes it whole, so a file past the cap is
    refused by the size ``stat`` gives, before the validator opens any file."""
    sizes = _sizes(bundle_copy)
    cap = max(sizes.values()) - 1
    name = next(name for name, size in sizes.items() if size > cap)
    monkeypatch.setattr(resolve_module, "MAX_FILE_BYTES", cap)
    opened = _validator_reads(monkeypatch)
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    assert (
        raised.value.detail
        == f"{name}: {sizes[name]} bytes, above the {cap} bytes a bundle file may hold"
    )
    assert opened == []


def test_a_file_at_the_size_cap_loads(bundle_copy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve_module, "MAX_FILE_BYTES", max(_sizes(bundle_copy).values()))
    assert load_bundle(bundle_copy).flux_documents


def test_the_size_cap_holds_a_pack_product_too(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is read whole for its digest like any file of the bundle."""
    sizes = _sizes(bundle_copy)
    monkeypatch.setattr(resolve_module, "MAX_FILE_BYTES", sizes["flux-manifest.json"] - 1)
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert (raised.value.detail or "").startswith(
        f"flux-manifest.json: {sizes['flux-manifest.json']} bytes, above"
    )


def test_documents_and_lockfile_past_the_bundle_cap_are_refused_before_a_file_is_read(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is not counted: the cap is on what the reads parse."""
    sizes = _sizes(bundle_copy)
    total = sum(size for name, size in sizes.items() if name != "flux-manifest.json")
    monkeypatch.setattr(resolve_module, "MAX_BUNDLE_READ_BYTES", total - 1)
    opened = _validator_reads(monkeypatch)
    with pytest.raises(FluxError) as raised:
        load_bundle(bundle_copy)
    assert raised.value.code.code == "FLX-3105"
    assert raised.value.detail == (
        f"the bundle's documents and lockfile: {total} bytes, above the {total - 1} "
        "bytes they may hold together"
    )
    assert opened == []
    monkeypatch.setattr(resolve_module, "MAX_BUNDLE_READ_BYTES", total)
    assert load_bundle(bundle_copy).flux_documents


def test_the_validators_read_holds_each_file_to_the_size_cap_by_itself(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a caller that loads a :class:`SeamBundle` without load_bundle's pass."""
    sizes = _sizes(bundle_copy)
    name = "simulation.flux.yml"
    monkeypatch.setattr(resolve_module, "MAX_FILE_BYTES", sizes[name] - 1)
    opened = _validator_reads(monkeypatch)
    seam_bundle = resolve_module.SeamBundle(bundle_copy, resolve_module.flux_schema_validator())
    with pytest.raises(FluxError) as raised:
        seam_bundle.load()
    assert (raised.value.detail or "").startswith(f"{name}: {sizes[name]} bytes, above")
    assert name not in opened


def test_the_exact_read_refuses_a_file_past_the_size_cap_before_reading_it(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = bundle_copy / "world.flux.yml"
    size = path.stat().st_size
    monkeypatch.setattr(resolve_module, "MAX_FILE_BYTES", size - 1)

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a file past the cap was read")

    monkeypatch.setattr(Path, "read_text", never)
    with pytest.raises(FluxError) as raised:
        resolve_module.load_documents_exact(path)
    assert raised.value.code.code == "FLX-3105"
    assert (
        raised.value.detail
        == f"world.flux.yml: {size} bytes, above the {size - 1} bytes a bundle file may hold"
    )


def _refused_within(seconds: float, read: Any, *args: Any) -> FluxError:
    """The FluxError *read* raises, failing the test if the read blocks or returns."""
    outcome: Dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["returned"] = read(*args)
        except FluxError as error:
            outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "the read blocked"
    assert "error" in outcome, f"the read returned {outcome.get('returned')!r}"
    return outcome["error"]


@pytest.mark.skipif(os.name != "posix", reason="a POSIX device and FIFO")
@pytest.mark.parametrize("kind", ["device", "fifo", "directory"])
def test_the_exact_read_refuses_a_path_that_is_not_a_regular_file(
    tmp_path: Path, kind: str
) -> None:
    """``/dev/zero`` behind a document's name would be read without end, and a
    FIFO would wait for a writer: neither is opened for reading."""
    path = tmp_path / "c.fluid.yml"
    if kind == "device":
        path.symlink_to(os.devnull)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    error = _refused_within(5, resolve_module.load_documents_exact, path)
    assert error.code.code == "FLX-3105"
    assert error.detail == "c.fluid.yml: not a regular file, so it is not read"


@pytest.mark.skipif(os.name != "posix", reason="a POSIX device")
def test_ship_refuses_a_contract_that_is_not_a_regular_file(tmp_path: Path) -> None:
    """``fluxsim ship`` digests the bundle's own contract without load_bundle in front."""
    from flux_engine.stages.ship import _bundle_contract_digest

    (tmp_path / "c.fluid.yml").symlink_to(os.devnull)
    error = _refused_within(5, _bundle_contract_digest, tmp_path, "payment-recovery")
    assert error.detail == "c.fluid.yml: not a regular file, so it is not read"


def test_a_file_that_grew_past_the_cap_once_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most one byte past the cap is read, whatever the file's stat said."""
    path = tmp_path / "world.flux.yml"
    path.write_bytes(b"x" * 9)
    monkeypatch.setattr(resolve_module, "MAX_FILE_BYTES", 8)
    real_stat = Path.stat

    def small_stat(self: Path, **kwargs: Any) -> os.stat_result:
        status = real_stat(self, **kwargs)
        fields = list(status[:10])
        fields[6] = 1  # st_size: as the file stood when it was checked
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", small_stat)
    with pytest.raises(FluxError) as raised:
        resolve_module.read_bundle_file(path)
    assert raised.value.detail == (
        "world.flux.yml: at least 9 bytes once read, above the 8 bytes a bundle file may hold"
    )


def test_resolving_reads_no_bundle_file_again(
    bundle_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract's bytes come from the loaded bundle, not a second read of the file."""
    loaded = load_bundle(bundle_copy)

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a bundle file was read again")

    monkeypatch.setattr(Path, "read_bytes", never)
    monkeypatch.setattr(resolve_module, "read_bundle_file", never)
    assert resolve_module.resolve_all_emits(loaded, simulation_id="sim-payment-recovery")


def test_a_healthy_bundle_still_loads(bundle_copy: Path) -> None:
    loaded = load_bundle(bundle_copy)
    assert loaded.flux_documents


def test_a_refusal_raised_by_a_check_comes_out_of_run_as_it_was_raised(bundle_copy: Path) -> None:
    """flux_spec's ``Bundle.run`` guards each step and reports what it raises. The engine's own
    refusal is not a report: it is raised, by the step that raised it, and no later check runs."""
    refusal = FluxError(FLX_3105, "flux.lock: refused (line 2, column 14)")
    later: List[str] = []

    class Refusing(SeamBundle):
        def check_refs(self) -> None:
            raise refusal

        def check_sums_and_sets(self) -> None:
            later.append("check_sums_and_sets")

    with pytest.raises(FluxError) as raised:
        Refusing(bundle_copy, flux_schema_validator()).run()
    assert raised.value is refusal
    assert later == []


@pytest.mark.parametrize(
    "exception",
    [
        TypeError("a shape this check did not expect"),
        KeyError("name"),
        ValueError("a value"),
        RecursionError("too deep"),
    ],
)
def test_any_other_exception_of_a_check_is_left_to_flux_specs_guard(
    bundle_copy: Path, exception: Exception
) -> None:
    """Only a ``FluxError`` gets out. What flux_spec does with any other exception of a check, report
    it or, in a release without the guard, let it out, is what the engine's bundle does as well."""

    class Plain(Bundle):
        def check_refs(self) -> None:
            raise exception

    class Seam(SeamBundle):
        def check_refs(self) -> None:
            raise exception

    def outcome(bundle_class: Any) -> Any:
        try:
            return ("reported", bundle_class(bundle_copy, flux_schema_validator()).run())
        except Exception as raised:  # noqa: BLE001 (what a release without the guard does)
            return ("raised", raised)

    assert outcome(Seam) == outcome(Plain)
