# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The built-in sinks' factories: every setting is checked before a run starts."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks import registry

pytest.importorskip("pyarrow")

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["parquet", "arrow-ipc"])
@pytest.mark.parametrize("target", ["", " "])
def test_a_file_sink_without_a_directory_is_refused(name: str, target: str) -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"{name}:{target}")
    assert caught.value.code.code == "FLX-3504"
    assert f"--sink {name}:DIRECTORY" in str(caught.value)


def test_parquet_options_become_settings(tmp_path: Path) -> None:
    target = registry.open_target(
        f"parquet:{tmp_path}",
        {
            "row-group-size": "1000",
            "compression": "snappy",
            "rows-per-file": "5000",
            "verify": "yes",
        },
    )
    assert target.settings["row_group_size"] == 1000  # type: ignore[attr-defined]
    assert target.settings["compression"] == "snappy"  # type: ignore[attr-defined]
    assert target.settings["rows_per_file"] == 5000  # type: ignore[attr-defined]
    assert target.settings["verify"] is True  # type: ignore[attr-defined]
    assert not any(tmp_path.iterdir()), "checking the settings wrote nothing"


@pytest.mark.parametrize(
    "spec, options, needle",
    [
        ("parquet:{d}", {"colour": "blue"}, "takes no option colour"),
        ("parquet:{d}", {"compression": "lzo"}, "compression 'lzo'"),
        ("arrow-ipc:{d}", {"row-group-size": "10"}, "takes no option row-group-size"),
        ("parquet:{f}", {}, "is not a directory"),
    ],
)
def test_a_file_sink_refuses_bad_settings_before_the_run(
    tmp_path: Path, spec: str, options: Dict[str, str], needle: str
) -> None:
    taken = tmp_path / "file"
    taken.write_text("x")
    with pytest.raises(FluxError) as caught:
        registry.open_target(spec.format(d=tmp_path / "out", f=taken), options)
    assert caught.value.code.code == "FLX-3504"
    assert needle in str(caught.value)


class TestAdbcFactory:
    @pytest.fixture(autouse=True)
    def _needs_adbc(self) -> None:
        pytest.importorskip("adbc_driver_manager")
        pytest.importorskip("adbc_driver_sqlite")

    def test_a_driver_is_required(self) -> None:
        with pytest.raises(FluxError, match="needs a driver"):
            registry.open_target("adbc:")

    def test_the_uri_can_come_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FLUX_TEST_SINK_URI", str(tmp_path / "sim.sqlite"))
        target = registry.open_target("adbc:sqlite", {"uri-env": "FLUX_TEST_SINK_URI"})
        assert target.uri == str(tmp_path / "sim.sqlite")  # type: ignore[attr-defined]

    def test_an_unset_environment_variable_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FLUX_TEST_SINK_URI", raising=False)
        with pytest.raises(FluxError, match="FLUX_TEST_SINK_URI is not set"):
            registry.open_target("adbc:sqlite", {"uri-env": "FLUX_TEST_SINK_URI"})

    @pytest.mark.parametrize(
        "uri",
        [
            "postgresql://user:hunter2@db/sim",
            "user:hunter2@account/db/schema",
            "postgresql://db/sim?password=hunter2",
            "snowflake://account/db?private_key_path=/k",
        ],
    )
    def test_a_uri_with_a_secret_is_refused_on_the_command_line(self, uri: str) -> None:
        with pytest.raises(FluxError) as caught:
            registry.open_target("adbc:postgresql", {"uri": uri})
        assert caught.value.code.code == "FLX-3504"
        assert "uri-env" in str(caught.value)
        assert "hunter2" not in str(caught.value)

    @pytest.mark.parametrize("uri", ["postgresql://db/sim?sslmode=require", "sim.sqlite"])
    def test_a_uri_without_a_secret_is_fine(self, uri: str) -> None:
        options: Dict[str, str] = {"uri": uri}
        driver = "sqlite" if uri.endswith(".sqlite") else "postgresql"
        if driver == "postgresql":
            pytest.importorskip("adbc_driver_postgresql")
        registry.open_target(f"adbc:{driver}", options)

    def test_uri_and_uri_env_are_exclusive(self) -> None:
        with pytest.raises(FluxError, match="not both"):
            registry.open_target("adbc:sqlite", {"uri": "a", "uri-env": "B"})

    def test_a_missing_driver_fails_before_the_run(self) -> None:
        if importlib.util.find_spec("adbc_driver_postgresql") is not None:
            pytest.skip("the PostgreSQL driver is installed here")
        with pytest.raises(FluxError) as caught:
            registry.open_target("adbc:postgresql", {"uri": "postgresql://db/sim"})
        assert caught.value.code.code == "FLX-3501"
        assert "adbc-driver-postgresql" in str(caught.value)

    def test_driver_options_pass_through(self, tmp_path: Path) -> None:
        with pytest.raises(FluxError, match="takes no db options"):
            registry.open_target(
                "adbc:sqlite", {"uri": str(tmp_path / "x.sqlite"), "db.busy_timeout": "5"}
            )

    def test_settings_become_the_sinks(self, tmp_path: Path) -> None:
        target = registry.open_target(
            "adbc:sqlite",
            {
                "uri": str(tmp_path / "x.sqlite"),
                "prefix": "twin_",
                "mode": "replace",
                "batch-size": "500",
                "verify": "true",
            },
        )
        assert (target.prefix, target.mode, target.batch_size, target.verify) == (  # type: ignore[attr-defined]
            "twin_",
            "replace",
            500,
            True,
        )
        assert not (tmp_path / "x.sqlite").exists(), "checking the settings connected to nothing"


class TestAdbcDriverOptions:
    @pytest.fixture(autouse=True)
    def _needs_adbc(self) -> None:
        pytest.importorskip("adbc_driver_manager")

    def test_a_driver_option_can_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from flux_engine.sinks.adbc import _driver_options

        monkeypatch.setenv("FLUX_TEST_TOKEN", "t0ken-value")
        options = _driver_options(
            {"db.adbc.x.auth_token-env": "FLUX_TEST_TOKEN", "db.adbc.x.project": "p", "uri": "u"}
        )
        assert options == {"adbc.x.auth_token": "t0ken-value", "adbc.x.project": "p"}

    def test_an_unset_variable_for_a_driver_option_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from flux_engine.sinks.adbc import _driver_options

        monkeypatch.delenv("FLUX_TEST_TOKEN", raising=False)
        with pytest.raises(FluxError, match="FLUX_TEST_TOKEN is not set"):
            _driver_options({"db.adbc.x.auth_token-env": "FLUX_TEST_TOKEN"})

    @pytest.mark.parametrize(
        "key", ["adbc.snowflake.sql.auth_token", "password", "private_key", "client_secret"]
    )
    def test_a_driver_option_that_names_a_secret_is_refused_inline(self, key: str) -> None:
        from flux_engine.sinks.adbc import _driver_options

        with pytest.raises(FluxError) as caught:
            _driver_options({f"db.{key}": "hunter2-inline"})
        assert caught.value.code.code == "FLX-3504"
        assert f"db.{key}-env=NAME" in str(caught.value)
        assert "hunter2-inline" not in str(caught.value)


def test_two_exposes_that_would_share_a_table_are_refused(tmp_path: Path) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    from flux_engine.core.digest import digest_of_bytes
    from flux_engine.sinks.receipts import StreamContext

    target = registry.open_target("adbc:sqlite", {"uri": str(tmp_path / "x.sqlite")})

    def context(expose_id: str) -> StreamContext:
        return StreamContext(
            run_id="r1",
            expose_id=expose_id,
            rows=0,
            output_digest=digest_of_bytes(expose_id.encode(), subject="t"),
        )

    target.sink_for(context("telco.recovery"))
    with pytest.raises(FluxError) as caught:
        target.sink_for(context("telco_recovery"))
    assert caught.value.code.code == "FLX-3504"
    assert "'telco.recovery' and 'telco_recovery' would both land in" in str(caught.value)
    # SQLite and DuckDB do not tell table names apart by case.
    target.sink_for(context("Pay"))
    with pytest.raises(FluxError, match="'Pay' and 'pay' would both land in"):
        target.sink_for(context("pay"))


def test_the_adbc_target_keeps_its_credentials_out_of_its_repr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    monkeypatch.setenv("FLUX_TEST_SINK_URI", str(tmp_path / "hunter2-secret.sqlite"))
    target = registry.open_target("adbc:sqlite", {"uri-env": "FLUX_TEST_SINK_URI"})
    assert "hunter2-secret" not in repr(target)
