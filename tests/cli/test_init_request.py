# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --contract C`` with no profile writes the profile request (ADR 0018)."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

import flux_engine.cli._request as request_module
from flux_engine.cli._init import run_init
from flux_stats.request import load_request_text

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ORDERS = FIXTURES / "author" / "orders.fluid.yaml"
STORE_STAR = FIXTURES / "related-star" / "store-star.fluid.yml"


def _request(tmp_path: Path, contract: Path, name: str = "b") -> Path:
    result = run_init(str(tmp_path / name), contract=str(contract))
    assert result.exit_code == 0, result.stderr
    return tmp_path / f"{name}.profile-request.yml"


def test_a_contract_alone_writes_a_request_beside_the_directory(tmp_path: Path) -> None:
    path = _request(tmp_path, ORDERS)
    assert path.is_file()
    assert not (tmp_path / "b").exists()
    request = load_request_text(path.read_text(encoding="utf-8"))
    assert [table.expose_id for table in request.tables] == ["orders"]
    assert request.tables[0].row_count


def test_the_request_asks_for_what_each_column_needs(tmp_path: Path) -> None:
    request = load_request_text(_request(tmp_path, ORDERS).read_text(encoding="utf-8"))
    columns = {column.name: column for column in request.tables[0].columns}
    assert any(column.domain == "number" for column in columns.values())
    for column in columns.values():
        if column.domain == "number":
            assert "mean/1" in column.statistics and "min" in column.metrics
        if column.domain == "category":
            assert "mean/1" not in column.statistics


def test_every_declared_relationship_is_asked_for_children_per_parent(tmp_path: Path) -> None:
    request = load_request_text(_request(tmp_path, STORE_STAR).read_text(encoding="utf-8"))
    pairs = {(rel.parent.table, rel.child.table) for rel in request.relationships}
    assert ("date_dim", "store_sales") in pairs and ("store_sales", "store_returns") in pairs
    assert all("mean/1" in rel.statistics for rel in request.relationships)


def test_the_request_is_the_same_bytes_twice(tmp_path: Path) -> None:
    first = _request(tmp_path, STORE_STAR, "one").read_bytes()
    second = _request(tmp_path, STORE_STAR, "two").read_bytes()
    assert first == second


def test_the_request_names_no_value_of_the_data(tmp_path: Path) -> None:
    text = _request(tmp_path, ORDERS).read_text(encoding="utf-8")
    assert "sha256:" in text and "rows" not in text.split("tables:")[0]


def test_an_existing_request_is_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "mine.yml"
    target.write_text("keep", encoding="utf-8")
    result = run_init(str(tmp_path / "b"), contract=str(ORDERS), request=str(target))
    assert result.exit_code == 1 and "never overwrites" in result.stderr
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_request_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    path = _request(tmp_path, ORDERS)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_link_at_the_request_name_is_not_followed(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("keep", encoding="utf-8")
    (tmp_path / "b.profile-request.yml").symlink_to(elsewhere)
    result = run_init(str(tmp_path / "b"), contract=str(ORDERS))
    assert result.exit_code == 1
    assert elsewhere.read_text(encoding="utf-8") == "keep"


def test_request_needs_a_contract_and_no_profile(tmp_path: Path) -> None:
    assert run_init(str(tmp_path / "b"), request="r.yml").exit_code == 2
    both = run_init(str(tmp_path / "b"), contract=str(ORDERS), profile="p.json", request="r.yml")
    assert both.exit_code == 2


def test_strict_without_a_profile_is_a_usage_error(tmp_path: Path) -> None:
    assert run_init(str(tmp_path / "b"), contract=str(ORDERS), strict=True).exit_code == 2


def test_a_contract_that_is_not_there_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    result = run_init(str(tmp_path / "b"), contract=str(tmp_path / "missing.yaml"))
    assert result.exit_code == 1
    assert not (tmp_path / "b.profile-request.yml").exists()


def test_every_rule_of_the_contract_is_a_fact(tmp_path: Path) -> None:
    from tests.stages.test_rules_run import _CONTRACT, _bundle

    contract = _bundle(tmp_path, name="rules") / _CONTRACT
    request = load_request_text(_request(tmp_path, contract).read_text(encoding="utf-8"))
    assert [(fact.name, fact.table, fact.rule) for fact in request.facts] == [
        ("date_dim.date_of_key", "date_dim", "date_of_key"),
        ("date_dim.year_of_date", "date_dim", "year_of_date"),
    ]


@pytest.mark.parametrize("spelling", [".", "..", "./", "sub/.."])
def test_a_directory_spelled_as_dot_or_dot_dot_gets_its_request_beside_the_resolved_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """``init . --contract C`` named no directory to sit beside, and stopped with a ValueError."""
    here = tmp_path / "work" / "bundle"
    (here / "sub").mkdir(parents=True)
    monkeypatch.chdir(here / "sub" if spelling == ".." else here)
    result = run_init(spelling, contract=str(ORDERS))
    assert result.exit_code == 0, result.stderr
    resolved = Path(spelling).resolve()
    expected = resolved.with_name(f"{resolved.name}.profile-request.yml")
    assert Path(result.artifacts["request"]) == expected
    assert expected.is_file()
    # beside the directory, never inside it and never in the working directory by accident
    assert not list(resolved.glob("*.profile-request.yml"))


def _orders_with_column(tmp_path: Path, name: str) -> Path:
    contract = tmp_path / "orders.fluid.yaml"
    text = ORDERS.read_text(encoding="utf-8")
    assert text.count("name: country\n") == 1
    contract.write_text(text.replace("name: country\n", f'name: "{name}"\n'), encoding="utf-8")
    return contract


@pytest.mark.parametrize(
    "name",
    ["first\\u00a0name", "first\\u2028name", "first\\u0007name", "x" * 513],
    ids=["no-break space", "line separator", "control", "513 characters"],
)
def test_a_column_name_the_request_reader_refuses_is_refused_at_init_naming_the_column(
    tmp_path: Path, name: str
) -> None:
    """``init`` never writes a request that ``flux_stats.request`` would refuse on load."""
    contract = _orders_with_column(tmp_path, name)
    result = run_init(str(tmp_path / "b"), contract=str(contract))
    assert result.exit_code == 1
    assert result.error_code == "FLX-5038"
    assert "orders" in result.stderr and "column" in result.stderr
    assert not (tmp_path / "b.profile-request.yml").exists()


def test_a_column_name_with_a_plain_space_is_written_and_read_back(tmp_path: Path) -> None:
    contract = _orders_with_column(tmp_path, "first name")
    path = _request(tmp_path, contract)
    request = load_request_text(path.read_text(encoding="utf-8"))
    assert "first name" in {column.name for column in request.tables[0].columns}


def test_the_request_written_is_the_request_read_back_whatever_the_contracts_column_order(
    tmp_path: Path,
) -> None:
    """A contract lists its columns in no name order; the request reads back equal to its source."""
    from flux_engine.author.bundle import _read_contract
    from flux_engine.core.digest import digest_of_document
    from flux_engine.seam.profile_request import request_for_contract
    from flux_stats.request import render_request

    document, _ = _read_contract(ORDERS)
    digest = digest_of_document(document, subject="contract").value
    asked = request_for_contract(document, contract_digest=digest)
    names = [column.name for column in asked.tables[0].columns]
    assert names == sorted(names) != ["order_id", "country", "amount", "created_at"]
    assert load_request_text(render_request(asked)) == asked


def _labelled(tmp_path: Path, label: str) -> Path:
    """The author fixture's contract with ``orders.country`` labelled *label*."""
    text = ORDERS.read_text(encoding="utf-8")
    anchor = "    - name: country\n      type: varchar\n"
    assert text.count(anchor) == 1
    path = tmp_path / "labelled.fluid.yaml"
    path.write_text(text.replace(anchor, f"{anchor}      sensitivity: {label}\n"), encoding="utf-8")
    return path


def test_the_request_step_names_a_column_whose_label_a_plain_profile_cannot_meet(
    tmp_path: Path,
) -> None:
    contract = _labelled(tmp_path, "pii")
    result = run_init(str(tmp_path / "b"), contract=str(contract))
    assert result.exit_code == 0, result.stderr
    (line,) = [line for line in result.stdout.splitlines() if line.startswith("grade:")]
    assert "orders.country is pii (needs P3 or stronger)" in line
    assert "every statistic it publishes under noise" in line
    assert "PROFILE-SPEC.md section 3a" in line and "FLX-4005" in line
    assert result.artifacts["grade"]["belowMinimum"] == [
        {"exposeId": "orders", "column": "country", "sensitivity": "pii", "minimum": "P3"}
    ]


def test_a_request_for_a_contract_with_no_floor_above_p1_has_no_grade_line(
    tmp_path: Path,
) -> None:
    for contract in (ORDERS, _labelled(tmp_path, "confidential")):
        target = tmp_path / f"{contract.stem}.yml"
        result = run_init(str(tmp_path / "b"), contract=str(contract), request=str(target))
        assert result.exit_code == 0, result.stderr
        assert "grade:" not in result.stdout
        assert result.artifacts["grade"]["belowMinimum"] == []


def test_the_request_file_starts_with_a_header_naming_what_it_is_and_its_spec(
    tmp_path: Path,
) -> None:
    text = _request(tmp_path, ORDERS).read_text(encoding="utf-8")
    first, second, rest = text.split("\n", 2)
    assert first.startswith("# A profile request")
    assert "docs/spec/PROFILE-REQUEST-SPEC.md" in second and second.startswith("# ")
    assert not rest.startswith("#")
    # the reader takes the comments, so the request is the one it was
    from flux_stats.request import render_request

    request = load_request_text(text)
    assert load_request_text(render_request(request)) == request


def test_a_directory_that_cannot_be_removed_does_not_change_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request cannot be written, so init removes what it wrote; a refused rmdir is not an error."""
    profile = FIXTURES / "author" / "orders.profile.json"

    def full(path: Path, data: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(request_module, "_write_new", full)
    plain = tmp_path / "plain"
    plain.mkdir()
    expected = run_init(str(plain / "b"), contract=str(ORDERS), profile=str(profile))
    assert expected.exit_code == 1 and "No space left on device" in expected.stderr

    def refuse(self: Path) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "rmdir", refuse)
    stubborn = tmp_path / "stubborn"
    stubborn.mkdir()
    result = run_init(str(stubborn / "b"), contract=str(ORDERS), profile=str(profile))
    assert result.exit_code == expected.exit_code
    assert result.stderr == expected.stderr.replace(str(plain), str(stubborn))
