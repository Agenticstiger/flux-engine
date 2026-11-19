# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The harness's own logic, offline: no AWS, no DuckDB source, no FLUX run."""

from __future__ import annotations

import json
import math
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from flux_e2e import aws, bundle, judges, owner, report, rules, tpcds
from flux_e2e.aws import QueryResult


def _metric(value=None, status="F"):
    entry = {"definition": "x/1", "disclosure": "acro-default", "status": status}
    if value is not None:
        entry["value"] = value
    return entry


def _quantiles(values, status="F"):
    entry = {"definition": "quantiles/1", "disclosure": "acro-default", "status": status}
    if status == "F":
        entry["values"] = values
    return entry


# ---- bundle -----------------------------------------------------------------------


def test_histogram_weights_cover_every_row():
    points = [
        (Fraction(0), Decimal(1)),
        (Fraction(1, 4), Decimal(10)),
        (Fraction(1, 2), Decimal(20)),
        (Fraction(3, 4), Decimal(40)),
        (Fraction(1), Decimal(100)),
    ]
    edges, weights = bundle.histogram(points)
    assert edges == [Decimal(1), Decimal(10), Decimal(20), Decimal(40), Decimal(100)]
    assert weights == [Fraction(1, 4)] * 4


def test_histogram_folds_a_point_mass_and_clipped_tails_into_the_edge_bins():
    # min and max withheld; q25 == q50 is a point mass on the first edge.
    points = [
        (Fraction(1, 4), Decimal(5)),
        (Fraction(1, 2), Decimal(5)),
        (Fraction(3, 4), Decimal(9)),
    ]
    edges, weights = bundle.histogram(points)
    assert edges == [Decimal(5), Decimal(9)]
    assert weights == [Fraction(1)]


def test_histogram_needs_two_distinct_values():
    assert bundle.histogram([(Fraction(0), Decimal(3)), (Fraction(1), Decimal(3))]) is None
    assert bundle.histogram([]) is None


def _expose():
    return {
        "exposeId": "t",
        "semantics": {"entities": [{"name": "t", "type": "primary", "expr": "t_sk"}]},
        "contract": {
            "schema": [
                {"name": "t_sk", "type": "BIGINT"},
                {"name": "kind", "type": "enum('a', 'b')"},
                {"name": "label", "type": "STRING"},
                {"name": "day", "type": "DATE"},
                {"name": "price", "type": "DECIMAL(7,2)"},
            ]
        },
    }


def _table():
    return {
        "rowCount": 100,
        "columns": {
            "t_sk": {
                "metrics": {
                    "count": _metric(100),
                    "nullCount": _metric(status="A"),
                    "min": _metric(status="A"),
                    "max": _metric(status="A"),
                }
            },
            "kind": {"metrics": {"count": _metric(100), "nullCount": _metric(status="A")}},
            "label": {
                "metrics": {
                    "count": _metric(80),
                    "nullCount": _metric(20),
                    "distinctCount": _metric(3),
                }
            },
            "day": {
                "metrics": {
                    "min": _metric(0),
                    "max": _metric(86_400_000 * 10),
                    "quantiles": _quantiles(
                        {"0.25": 86_400_000, "0.5": 86_400_000 * 2, "0.75": 86_400_000 * 5}
                    ),
                }
            },
            "price": {
                "metrics": {
                    "min": _metric("1.50"),
                    "max": _metric(status="A"),
                    "quantiles": _quantiles({}, status="A"),
                }
            },
        },
    }


def test_plan_emit_reads_only_published_metrics_and_names_every_assumption():
    plan = bundle.plan_emit(_expose(), _table())
    assert plan.rows == 100
    notes = {note.column: note for note in plan.notes}
    assert plan.columns["t_sk"]["range"] == {"min": "1", "max": "100"}
    assert notes["t_sk"].source == "assumed"
    assert plan.columns["kind"] == {"support": "declared", "maxCardinality": 2}
    assert notes["kind"].source == "contract"
    assert plan.columns["label"]["support"] == {"values": ["label-1", "label-2", "label-3"]}
    assert plan.columns["label"]["nullFraction"] == "0.2"
    # A DATE bound is whole days since the epoch, the one spelling both readers accept.
    assert plan.columns["day"]["range"] == {"min": 0, "max": 10}
    assert plan.columns["day"]["bins"] == [0, 1, 2, 5, 10]
    assert plan.columns["price"] == {"levels": {"1.50": "1"}}


def test_simulation_pins_the_profile_and_the_contract():
    contract = {"id": "p", "fluidVersion": "0.7.7"}
    starter = {"fluxVersion": "0.5.0", "spec": {"worldRef": "w", "seed": 1, "emits": []}}
    plan = bundle.plan_emit(_expose(), _table())
    document = bundle.simulation(contract, "sha256:c", "sha256:p", [plan], seed=7, starter=starter)
    block = document["extensions"][bundle.NAMESPACE]
    assert block["profile"] == {"digest": "sha256:p"}
    assert block["emits"]["t"]["fidelity"]["properties"][0]["property"] == "mean"
    assert document["spec"]["seed"] == 7
    assert document["spec"]["emits"][0]["provenance"] == {"contractDigest": "sha256:c"}


def _calendar_expose(invariants):
    schema = [{"name": "d_date_sk", "type": "BIGINT"}, {"name": "d_date", "type": "DATE"}]
    block = {"schema": schema}
    if invariants is not None:
        block["invariants"] = invariants
    return {"exposeId": "date_dim", "contract": block}


def test_the_bundle_names_the_column_a_difference_form_generates():
    check = {"id": "date_of_key", "check": rules.RULES[0].text}
    assert bundle.invariant_settings(_calendar_expose([check])) == {
        "date_of_key": {"generates": "d_date"}
    }
    # A rule the owner left out, or a column the expose lacks, gets no setting.
    assert bundle.invariant_settings(_calendar_expose(None)) == {}
    expose = _calendar_expose([check])
    expose["contract"]["schema"].pop()
    assert bundle.invariant_settings(expose) == {}
    # bundle.py runs alone in the sandbox, so its table must match the owner's rules.
    assert bundle.GENERATES == {rule.id: rule.generates for rule in rules.RULES if rule.generates}


def test_a_column_a_difference_form_generates_declares_no_draw():
    # date_dim's d_date has one row per value, so the profile withholds its order
    # statistics. Declared as a placeholder day, it would leave the engine a range that
    # refuses every solved date; generated by its rule, it declares nothing.
    check = {"id": "date_of_key", "check": rules.RULES[0].text}
    table = {"rowCount": 10, "columns": {"d_date_sk": {"metrics": {}}, "d_date": {"metrics": {}}}}
    with_rule = bundle.plan_emit(_calendar_expose([check]), table)
    assert with_rule.columns["d_date"] == {}
    assert any(note.column == "d_date" and "rule" in note.how for note in with_rule.notes)
    without = bundle.plan_emit(_calendar_expose(None), table)
    assert without.columns["d_date"] != {}


def test_simulation_carries_each_emit_s_rule_settings():
    contract = {
        "id": "p",
        "fluidVersion": "0.7.7",
        "exposes": [_calendar_expose([{"id": "date_of_key", "check": rules.RULES[0].text}])],
    }
    starter = {"fluxVersion": "0.5.0", "spec": {"worldRef": "w", "seed": 1, "emits": []}}
    plans = [bundle.plan_emit(_expose(), _table())]
    plans.append(bundle.EmitPlan("date_dim", 10, "test"))
    plans[-1].columns["d_date_sk"] = {"range": {"min": "1", "max": "10"}}
    document = bundle.simulation(contract, "sha256:c", "sha256:p", plans, seed=7, starter=starter)
    emits = document["extensions"][bundle.NAMESPACE]["emits"]
    assert emits["date_dim"]["invariants"] == {"date_of_key": {"generates": "d_date"}}
    assert "invariants" not in emits["t"]


#: A calendar of 30 days computed as dsdgen computes it, keyed by the Julian day.
_CALENDAR = """
    SELECT 2451545 + i AS d_date_sk, DATE '2000-01-01' + i::INTEGER AS d_date,
           year(DATE '2000-01-01' + i::INTEGER)::BIGINT AS d_year,
           month(DATE '2000-01-01' + i::INTEGER)::BIGINT AS d_moy,
           ((year(DATE '2000-01-01' + i::INTEGER) - 1900) * 12
             + month(DATE '2000-01-01' + i::INTEGER) - 1)::BIGINT AS d_month_seq,
           (4 * (year(DATE '2000-01-01' + i::INTEGER) - 1900)
             + month(DATE '2000-01-01' + i::INTEGER) // 3 + 1)::BIGINT AS d_quarter_seq,
           ((2451545 + i - 2415015) // 7)::BIGINT AS d_week_seq,
           ((2451545 + i + 1) % 7)::BIGINT AS d_dow,
           dayname(DATE '2000-01-01' + i::INTEGER) AS d_day_name,
           CASE WHEN (2451545 + i + 1) % 7 IN (5, 6) THEN 'Y' ELSE 'N' END AS d_weekend,
           ((month(DATE '2000-01-01' + i::INTEGER) + 2) // 3)::BIGINT AS d_qoy,
           day(DATE '2000-01-01' + i::INTEGER)::BIGINT AS d_dom,
           (2451545 + i - day(DATE '2000-01-01' + i::INTEGER) + 1)::BIGINT AS d_first_dom
    FROM range(30) r(i)
"""


def test_the_contract_carries_each_rule_with_the_band_the_source_needs():
    import duckdb

    with duckdb.connect() as con:
        con.execute(f"CREATE TABLE date_dim AS {_CALENDAR}")
        # Twelve days with another day of the week need a band, rounded up to 15; three
        # days in another quarter would describe single rows, so that rule is left out.
        con.execute("UPDATE date_dim SET d_dow = (d_dow + 1) % 7 WHERE d_date_sk % 30 < 12")
        con.execute(
            "UPDATE date_dim SET d_day_name = dayname(d_date + (d_dow - (d_date_sk + 1) % 7)::INTEGER)"
        )
        con.execute(
            "UPDATE date_dim SET d_weekend = CASE WHEN d_dow IN (5, 6) THEN 'Y' ELSE 'N' END"
        )
        con.execute("UPDATE date_dim SET d_qoy = 4 WHERE d_date_sk % 30 < 3")
        schema, _ = owner._schema(con, "date_dim")
        written = rules.contract_invariants(con, "date_dim", schema, minimum_cell=10)
    by_id = {entry["id"]: entry for entry in written}
    assert by_id["date_of_key"] == {"id": "date_of_key", "check": rules.RULES[0].text}
    assert by_id["day_of_week"]["mustBeLessOrEqualTo"] == 15
    assert by_id["day_of_week"]["unit"] == "rows"
    assert "quarter_of_year" not in by_id
    # The first day of the month reads the day of the month, which this table has.
    assert by_id["first_day_of_month"]["derive"] == "d_first_dom"
    # The fiscal columns are not in this table, so their rules are left out.
    assert "fiscal_year" not in by_id
    for name in (
        "year_of_date",
        "month_of_date",
        "month_sequence",
        "quarter_sequence",
        "week_sequence",
        "day_name",
        "weekend",
    ):
        assert "mustBeLessOrEqualTo" not in by_id[name], name


def test_the_calendar_and_clock_rules_hold_on_dsdgen_s_own_tables(tmp_path: Path):
    import duckdb

    database = tmp_path / "source.duckdb"
    with duckdb.connect(str(database)) as con:
        try:
            con.execute("INSTALL tpcds; LOAD tpcds")
        except duckdb.Error as error:  # pragma: no cover - needs the extension once
            pytest.skip(f"DuckDB's tpcds extension is not available: {error}")
        con.execute("CALL dsdgen(sf=0.01, keys=true)")
        counts = {}
        for table in ("date_dim", "time_dim"):
            schema, _ = owner._schema(con, table)
            total = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for rule in rules.RULES:
                if rule.table == table:
                    counts[rule.id] = (rules.violating_rows(con, table, schema, rule), total)
    # dsdgen's day of the week differs from the key's on a few days at the two ends of the
    # calendar, which its band counts; every other rule holds on every row, the quarter's
    # included, since it carries dsdgen's own placement of the first day of a quarter.
    quirks = {"day_of_week"}
    assert {rule.id for rule in rules.RULES} == set(counts)
    for rule_id, (violating, total) in counts.items():
        if rule_id in quirks:
            assert 0 < violating < total // 100, rule_id
        else:
            assert violating == 0, rule_id


_TICKET_TO = list(rules.DEPENDENCIES[0].to_columns)


def _sales(con, rows):
    """A store_sales of tickets: ``rows`` is a list of (ticket, to-column value or None)."""
    columns = ", ".join(f"{name} BIGINT" for name in ["ss_ticket_number", *_TICKET_TO])
    con.execute(f"CREATE TABLE store_sales ({columns})")
    for ticket, value in rows:
        values = ", ".join("NULL" if value is None else str(value) for _ in _TICKET_TO)
        con.execute(f"INSERT INTO store_sales VALUES ({ticket}, {values})")


def test_a_dependency_is_counted_as_the_spec_counts_it_and_banded_by_its_share():
    import duckdb

    ticket = rules.DEPENDENCIES[0]
    with duckdb.connect() as con:
        # Two tickets of three lines each, one line of ticket 2 with another tuple, one line
        # of ticket 3 with NULLs (a NULL is a value), and a line whose ticket is NULL.
        _sales(con, [(1, 7), (1, 7), (1, 7), (2, 8), (2, 8), (2, 9), (3, 5), (3, None)])
        con.execute("INSERT INTO store_sales VALUES (NULL, 1, 1, 1, 1, 1, 1, 1)")
        assert rules.dependency_violating_rows(con, ticket) == 2
        assert rules.dependency_covered_rows(con, ticket) == 8
        schema, _ = owner._schema(con, "store_sales")
        # One to nine violating rows would describe single rows: the dependency is left out.
        written = rules.contract_invariants(con, "store_sales", schema, minimum_cell=10)
        assert [entry["id"] for entry in written if "dependency" in entry] == []
        written = rules.contract_invariants(con, "store_sales", schema, minimum_cell=2)
        (entry,) = [entry for entry in written if "dependency" in entry]
        # 2 violating rows round up to 5, an integer count of rows, as a rule's band is.
        assert entry["mustBeLessOrEqualTo"] == 5 and entry["unit"] == "rows"
        con.execute("DELETE FROM store_sales WHERE ss_ticket_number IN (2, 3)")
        con.execute("DELETE FROM store_sales WHERE ss_ticket_number IS NULL")
        assert rules.dependency_violating_rows(con, ticket) == 0
        written = rules.contract_invariants(con, "store_sales", schema, minimum_cell=10)
    (entry,) = [entry for entry in written if "dependency" in entry]
    assert entry == {
        "id": "ticket",
        "dependency": {"from": ["ss_ticket_number"], "to": _TICKET_TO},
    }


def test_the_generator_environment_rebuilds_the_checkouts_own_packages(tmp_path, monkeypatch):
    seen = []

    def stop(argv, **kwargs):
        seen.append(list(argv))
        raise KeyboardInterrupt  # stop after the sync command is built

    monkeypatch.setattr(generator.subprocess, "run", stop)
    with pytest.raises(KeyboardInterrupt):
        generator.build_venv(tmp_path, tmp_path / "venv")
    (sync,) = seen
    for name, _ in generator.LOCAL_PACKAGES:
        assert sync[sync.index(name) - 1] == "--reinstall-package"


def test_the_generator_environment_must_hold_the_checkouts_code(tmp_path):
    checkout, installed = tmp_path / "repo" / "pkg", tmp_path / "site" / "pkg"
    for directory in (checkout, installed):
        (directory / "sub").mkdir(parents=True)
        (directory / "a.py").write_text("x = 1\n")
        (directory / "sub" / "b.py").write_text("y = 2\n")
        (directory / "sub" / "__pycache__").mkdir()
        (directory / "sub" / "__pycache__" / "b.cpython-312.pyc").write_bytes(b"\0")
    assert generator.python_sources(checkout) == generator.python_sources(installed)
    (installed / "a.py").write_text("x = 0\n")
    assert generator.python_sources(checkout) != generator.python_sources(installed)


def test_the_contract_refuses_a_float_before_the_run_profiles_it():
    owner.refuse_floats({"a": [1, "0.5", {"b": True}]}, "the contract")
    with pytest.raises(ValueError, match=r"the contract\.exposes\[0\]\.band holds the float 8\.6"):
        owner.refuse_floats({"exposes": [{"band": 8.6}]}, "the contract")


def test_the_ticket_dependency_on_dsdgen_s_own_store_sales(tmp_path: Path):
    import duckdb

    database = tmp_path / "source.duckdb"
    with duckdb.connect(str(database)) as con:
        try:
            con.execute("INSTALL tpcds; LOAD tpcds")
        except duckdb.Error as error:  # pragma: no cover - needs the extension once
            pytest.skip(f"DuckDB's tpcds extension is not available: {error}")
        con.execute("CALL dsdgen(sf=0.01, keys=true)")
        covered = rules.dependency_covered_rows(con, rules.DEPENDENCIES[0])
        violating = rules.dependency_violating_rows(con, rules.DEPENDENCIES[0])
        # Every non-NULL value of every 'to' column is one per ticket.
        for name in _TICKET_TO:
            split = con.execute(
                f"SELECT count(*) FROM (SELECT ss_ticket_number FROM store_sales "
                f"WHERE {name} IS NOT NULL GROUP BY ss_ticket_number "
                f"HAVING count(DISTINCT {name}) > 1)"
            ).fetchone()[0]
            assert split == 0, name
        schema, _ = owner._schema(con, "store_sales")
        written = rules.contract_invariants(con, "store_sales", schema, minimum_cell=10)
        nulls = {
            name: con.execute(
                f"SELECT avg(CASE WHEN {name} IS NULL THEN 1 ELSE 0 END) FROM store_sales"
            ).fetchone()[0]
            for name in _TICKET_TO
        }
    # dsdgen picks some lines and NULLs a random subset of each picked line's columns, and the
    # dependency counts a NULL as a value, so the picked lines are its violating rows. The
    # owner writes the dependency with the share the source needs, which it then meets.
    assert 0 < violating < covered // 5
    (entry,) = [entry for entry in written if "dependency" in entry]
    assert entry["id"] == "ticket" and entry["unit"] == "rows"
    band = entry["mustBeLessOrEqualTo"]
    assert isinstance(band, int) and band % 5 == 0 and violating <= band < violating + 5
    # The NULLs fall on the same lines: independent, they would make several times as many
    # rows violate. FLUX can meet the band only if no column's NULLs alone exceed it
    # (EXPR-SPEC-2 section 8), and it then puts the NULLs on shared NULL rows.
    independent = 1 - math.prod(1 - share for share in nulls.values())
    assert independent * covered > 2 * band
    assert max(nulls.values()) * covered < band


def _calendar_with_columns(names):
    schema = [{"name": name, "type": "DATE" if name == "d_date" else "BIGINT"} for name in names]
    return {"exposeId": "date_dim", "contract": {"schema": schema}}


def test_the_bundle_writes_the_three_flux_side_rules():
    # bundle.py runs alone, so these are its own constants, and every rule is a flux-expr/3 text
    # that names the column it derives.
    columns = ("d_date_sk", "d_date", "d_year", "d_qoy", "d_dom", "d_same_day_ly", "d_quarter_name")
    written = bundle.flux_side_rules(_calendar_with_columns(columns))
    assert [rule["id"] for rule in written] == [
        "day_of_month",
        "same_day_last_year",
        "quarter_name",
    ]
    assert {rule["language"] for rule in written} == {"flux-expr/3"}
    assert {rule["derive"]: rule["expr"] for rule in written} == {
        "d_dom": "EXTRACT(DAY FROM d_date)",
        "d_same_day_ly": "d_date_sk - DAYS_BETWEEN(ADD_YEARS(d_date, -1), d_date)",
        "d_quarter_name": "d_year || 'Q' || d_qoy",
    }
    # A column the expose lacks, or one a contract rule decides, gets no FLUX-side rule.
    assert bundle.flux_side_rules(_calendar_with_columns(("d_date_sk", "d_date", "d_dom"))) == [
        written[0]
    ]
    decided = _calendar_with_columns(columns)
    decided["contract"]["invariants"] = [{"id": "dom", "derive": "d_dom", "expr": "1"}]
    assert "day_of_month" not in {rule["id"] for rule in bundle.flux_side_rules(decided)}
    other = _calendar_with_columns(columns)
    other["exposeId"] = "item"
    assert bundle.flux_side_rules(other) == []


def test_simulation_declares_the_flux_side_rules_under_the_emit():
    columns = ("d_date_sk", "d_date", "d_dom")
    contract = {
        "id": "p",
        "fluidVersion": "0.7.7",
        "exposes": [_calendar_with_columns(columns)],
    }
    starter = {"fluxVersion": "0.5.0", "spec": {"worldRef": "w", "seed": 1, "emits": []}}
    plan = bundle.EmitPlan("date_dim", 10, "test")
    plan.columns["d_date_sk"] = {"range": {"min": "1", "max": "10"}}
    document = bundle.simulation(contract, "sha256:c", "sha256:p", [plan], seed=7, starter=starter)
    emit = document["extensions"][bundle.NAMESPACE]["emits"]["date_dim"]
    assert [rule["id"] for rule in emit["rules"]] == ["day_of_month"]


def test_the_flux_side_rules_hold_on_dsdgen_s_own_date_dim(tmp_path: Path):
    import duckdb

    # The three rules by their DuckDB equivalents: the day of the month, the key a year back
    # (with the day clamped where the earlier year has no such day), and the quarter's name.
    equivalents = {
        "day_of_month": "d_dom IS DISTINCT FROM extract(day FROM d_date)",
        "same_day_last_year": (
            "d_same_day_ly IS DISTINCT FROM "
            "d_date_sk - date_diff('day', CAST(d_date - INTERVAL 1 YEAR AS DATE), d_date)"
        ),
        "quarter_name": "d_quarter_name IS DISTINCT FROM d_year || 'Q' || d_qoy",
    }
    assert {rule["id"] for _, rule, _ in bundle.FLUX_RULES} == set(equivalents)
    with duckdb.connect(str(tmp_path / "source.duckdb")) as con:
        try:
            con.execute("INSTALL tpcds; LOAD tpcds")
        except duckdb.Error as error:  # pragma: no cover - needs the extension once
            pytest.skip(f"DuckDB's tpcds extension is not available: {error}")
        con.execute("CALL dsdgen(sf=0.01, keys=true)")
        total = con.execute("SELECT count(*) FROM date_dim").fetchone()[0]
        violating = {
            rule_id: con.execute(f"SELECT count(*) FROM date_dim WHERE {where}").fetchone()[0]
            for rule_id, where in equivalents.items()
        }
    assert total > 70000
    assert violating == {rule_id: 0 for rule_id in equivalents}


# ---- judges -----------------------------------------------------------------------


def _result(columns, rows, error=None):
    return QueryResult(rows=rows, columns=columns, types=[], bytes_scanned=10, error=error)


def test_a_one_row_answer_compares_every_aggregate():
    source = _result(["a", "b"], [(10, Decimal("4.0"))])
    synthetic = _result(["a", "b"], [(12, Decimal("4.0"))])
    out = judges.compare_query(source, synthetic)
    assert out["status"] == "compared"
    assert out["aggregateError"] == pytest.approx(0.1)


def test_a_grouped_answer_compares_totals_and_shares_by_key():
    source = _result(["year", "brand", "total"], [(2000, "x", 30.0), (2000, "y", 70.0)])
    synthetic = _result(["year", "brand", "total"], [(2000, "x", 50.0), (2000, "z", 50.0)])
    out = judges.compare_query(source, synthetic, ["key", "key", "measure"])
    assert out["keys"] == ["year", "brand"] and out["measures"] == ["total"]
    assert out["aggregateError"] == 0.0
    assert out["shareDistance"] == pytest.approx(0.7)
    assert out["keyOverlap"] == pytest.approx(1 / 3)
    assert out["keysDisjoint"] is False


def test_an_empty_synthetic_answer_is_the_worst_score():
    out = judges.compare_query(_result(["a"], [(1,)]), _result(["a"], []))
    assert out["status"] == "empty synthetic"
    assert out["aggregateError"] == 1.0


def test_an_athena_error_is_reported_not_raised():
    out = judges.compare_query(_result([], [], error="boom"), _result(["a"], [(1,)]))
    assert out["status"] == "error source"
    out = judges.compare_query(_result(["a"], [(1,)]), _result([], [], error="multiple rows"))
    assert out["status"] == "error synthetic"


def test_key_sql_covers_composite_keys():
    jobs = dict(
        judges.key_sql(
            {"store_sales": ["ss_item_sk", "ss_ticket_number"]}, ["store_sales", "store_returns"]
        )
    )
    assert "SELECT DISTINCT ss_item_sk, ss_ticket_number FROM store_sales" in jobs["pk:store_sales"]
    composite = jobs["fk:store_returns_sr_item_sk_sr_ticket_number_to_store_sales"]
    assert "c.sr_item_sk = p.ss_item_sk AND c.sr_ticket_number = p.ss_ticket_number" in composite


def test_read_keys_turns_counts_into_rates():
    results = {
        "pk:t": _result(["rows_total", "keys_distinct", "keys_null"], [(10, 8, 0)]),
        "fk:c_to_t": _result(["references_total", "orphans"], [(4, 1)]),
    }
    out = judges.read_keys(results)
    assert out["primaryKeys"]["t"]["duplicateRate"] == pytest.approx(0.2)
    assert out["foreignKeys"]["c_to_t"]["orphanRate"] == 0.25
    assert out["summary"]["foreignKeysWithoutOrphans"] == 0


def test_the_store_scope_reads_38_vendored_queries():
    queries = judges.load_queries(tpcds.STORE_STAR)
    assert len(queries) == 38
    assert all("TABLE_SUFFIX" not in sql for _, sql in queries)


def test_read_verify_counts_flags_and_refusals():
    document = {
        "exitCode": 1,
        "artifacts": {
            "rowConformance": {"ok": True},
            "fidelity": {"t": {"status": "failed", "refusals": [{"code": "FLX-5037"}]}},
            "scorecard": {
                "document": {
                    "overall": {"status": "not measured", "score": None},
                    "dimensions": [{"dimension": "structure", "status": "not measured"}],
                    "shapeFlags": {
                        "tests": 3,
                        "tables": [
                            {
                                "columns": [
                                    {"status": "checked", "flags": [{"alert": "CONSTANT"}]},
                                    {"status": "not read"},
                                ]
                            }
                        ],
                    },
                }
            },
        },
    }
    out = judges.read_verify(document)
    assert out["shapeFlags"] == {"CONSTANT": 1}
    assert out["shapeColumnsUnread"] == 1
    assert out["refusalCodes"] == {"FLX-5037": 1}


# ---- owner, report, names ---------------------------------------------------------


_HEAD = {"_producer": "p", "_schemaURL": "s", "encoding": {}}


def _profile(table, rule="acro-default"):
    return {**_HEAD, "disclosureRules": {rule: {}}, "tables": {table: {"columns": {}}}}


def test_merge_profiles_joins_tables_and_refuses_a_different_rule(tmp_path: Path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps(_profile("a")))
    second.write_text(json.dumps(_profile("b")))
    digest = owner.merge_profiles([first, second], tmp_path / "m.json")
    assert digest.startswith("sha256:")
    assert set(json.loads((tmp_path / "m.json").read_text())["tables"]) == {"a", "b"}
    second.write_text(json.dumps(_profile("b", rule="other")))
    with pytest.raises(ValueError):
        owner.merge_profiles([first, second], tmp_path / "m.json")


def test_sdmetrics_metadata_keeps_single_column_keys_only(tmp_path: Path):
    contract = {
        "exposes": [
            {
                "exposeId": "store_sales",
                "contract": {
                    "schema": [
                        {"name": "ss_store_sk", "type": "BIGINT"},
                        {"name": "ss_quantity", "type": "BIGINT"},
                    ]
                },
                "semantics": {"relationships": [{"fromColumns": ["ss_store_sk"]}]},
            },
            {
                "exposeId": "store",
                "contract": {
                    "schema": [
                        {"name": "s_store_sk", "type": "BIGINT"},
                        {"name": "s_state", "type": "STRING"},
                    ]
                },
                "semantics": {
                    "entities": [{"name": "store", "type": "primary", "expr": "s_store_sk"}]
                },
            },
        ]
    }
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(contract))
    metadata = report.sdmetrics_metadata(path, ["store_sales", "store"])
    assert metadata["tables"]["store"]["primary_key"] == "s_store_sk"
    assert "primary_key" not in metadata["tables"]["store_sales"]
    assert metadata["relationships"] == [
        {
            "parent_table_name": "store",
            "parent_primary_key": "s_store_sk",
            "child_table_name": "store_sales",
            "child_foreign_key": "ss_store_sk",
        }
    ]


def test_names_carry_the_lane_prefix():
    names = aws.Names(aws.check_run_id("20261117-120000"))
    assert names.bucket == "fluid-lane-flux-e2e-20261117-120000"
    assert names.database("source") == "fluid_lane_flux_e2e_20261117_120000_source"
    with pytest.raises(ValueError):
        aws.check_run_id("Bad_ID")


def test_live_steps_need_the_flag(monkeypatch):
    monkeypatch.delenv(aws.LIVE_FLAG, raising=False)
    with pytest.raises(aws.NotLive):
        aws.require_live()


def test_a_foreign_key_that_is_the_primary_key_is_one_to_one():
    semantics = owner._semantics(
        "store_returns", ["sr_item_sk", "sr_ticket_number"], tpcds.STORE_STAR
    )
    cardinality = {
        tuple(relationship["fromColumns"]): relationship["cardinality"]
        for relationship in semantics["relationships"]
    }
    assert cardinality[("sr_item_sk", "sr_ticket_number")] == "one_to_one"
    assert cardinality[("sr_item_sk",)] == "many_to_one"
    assert set(cardinality.values()) == {"one_to_one", "many_to_one"}
    sales = owner._semantics("store_sales", ["ss_item_sk", "ss_ticket_number"], tpcds.STORE_STAR)
    assert {r["cardinality"] for r in sales["relationships"]} == {"many_to_one"}


def _levels_table(con, name, levels):
    con.execute(f"CREATE TABLE {name} (v VARCHAR)")
    con.executemany(f"INSERT INTO {name} VALUES (?)", [[v] for v in levels for _ in range(10)])


def _fluid_077_enum_patterns():
    """The column type patterns of forge-cli's FLUID 0.7.7 schema that spell an enum."""
    import re
    from importlib.resources import files

    path = files("fluid_build") / "schemas" / "fluid-schema-0.7.7.json"
    found = []

    def walk(node):
        if isinstance(node, dict):
            pattern = node.get("pattern")
            if isinstance(pattern, str) and r"enum\s*\(" in pattern:
                found.append(re.compile(pattern))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(path.read_text(encoding="utf-8")))
    assert found
    return found


def test_an_enum_level_may_hold_any_character_but_a_control_character():
    import duckdb
    from flux_stats.fidelity import declared_enum_values

    levels = (" padded ", "(x)", ">10000", "Dr.", "N/A", "O'Brien", "a, b")
    con = duckdb.connect()
    _levels_table(con, "kept", levels)
    _levels_table(con, "control", ("a", "tab\there"))
    _levels_table(con, "decomposed", ("a", "e\u0301"))

    found = owner._enum_levels(con, "kept", "v")
    assert found == tuple(sorted(levels))
    declared = owner._enum_type(found)
    assert "'O''Brien'" in declared
    assert declared_enum_values(declared) == found
    assert bundle._enum_levels(declared) == list(found)
    assert any(pattern.search(declared) for pattern in _fluid_077_enum_patterns())
    # FLUID 0.7.7 cannot spell a control character, and the engine reads a level
    # in NFC: either keeps the column a plain string.
    assert owner._enum_levels(con, "control", "v") == ()
    assert owner._enum_levels(con, "decomposed", "v") == ()


def test_every_foreign_key_stays_inside_the_scope():
    keys = tpcds.foreign_keys(tpcds.STORE_STAR)
    assert len(keys) == len(tpcds.FOREIGN_KEYS)
    assert all(fk.target in tpcds.STORE_STAR for fk in keys)
    assert tpcds.foreign_keys(["store_sales", "item"]) == [
        fk for fk in tpcds.FOREIGN_KEYS if fk.table == "store_sales" and fk.target == "item"
    ]


def test_a_parent_key_takes_the_range_of_the_foreign_keys_referencing_it():
    contract = {
        "exposes": [
            {
                "exposeId": "fact",
                "semantics": {
                    "relationships": [
                        {"to": "t", "fromColumns": ["f_t"], "toColumns": ["t_sk"]},
                        {"to": "t", "fromColumns": ["a", "b"], "toColumns": ["x", "y"]},
                    ]
                },
                "contract": {"schema": [{"name": "f_t", "type": "BIGINT"}]},
            },
            {"exposeId": "t"},
        ]
    }
    tables = {
        "fact": {"columns": {"f_t": {"metrics": {"min": _metric("100"), "max": _metric("250")}}}},
        "t": {"columns": {}},
    }
    referencing = bundle.referencing_ranges(contract, tables)
    assert referencing == {"t": {"t_sk": [Decimal(100), Decimal(250)]}}
    plan = bundle.plan_emit(_expose(), _table(), referencing["t"])
    assert plan.columns["t_sk"]["range"] == {"min": "100", "max": "250"}
    assert {note.column: note.source for note in plan.notes}["t_sk"] == "assumed"


def _child(name, metrics, *, key=False):
    """A child table "name" whose one column "f" references t.t_sk."""
    semantics = {"relationships": [{"to": "t", "fromColumns": ["f"], "toColumns": ["t_sk"]}]}
    if key:
        semantics["entities"] = [{"name": name, "type": "primary", "expr": "f"}]
    schema = [{"name": "f", "type": "BIGINT"}]
    expose = {"exposeId": name, "semantics": semantics, "contract": {"schema": schema}}
    return expose, {"rowCount": 50, "columns": {"f": {"metrics": metrics}}}


def test_every_key_a_foreign_key_draws_lies_inside_its_parent_key_range():
    # One child per way a referencing column can be declared: a histogram, a range
    # around its mean (a small table's min withheld), the mean alone, one published
    # value, and a child whose one-column key is the foreign key.
    mean = {"sum": _metric("1020"), "count": _metric(10)}
    children = [
        _child("hist", {"min": _metric("100"), "max": _metric("250")}),
        _child("around", {**mean, "min": _metric("90", status="A"), "distinctCount": _metric(11)}),
        _child("mean", mean),
        _child("one", {"min": _metric("50")}),
        _child("keyed", mean, key=True),
    ]

    def parent_range(kids):
        contract = {"exposes": [expose for expose, _ in kids] + [_expose()]}
        referencing = bundle.referencing_ranges(contract, {e["exposeId"]: t for e, t in kids})
        parent = bundle.plan_emit(_expose(), {**_table(), "rowCount": 5}, referencing["t"])
        return tuple(Decimal(parent.columns["t_sk"]["range"][end]) for end in ("min", "max"))

    low, high = parent_range(children)
    for expose, table in children:
        drawn = bundle._declared_span(bundle.plan_emit(expose, table).columns["f"])
        assert drawn and all(low <= value <= high for value in drawn), (expose["exposeId"], drawn)
    # A column with nothing published is a placeholder and does not stretch the range.
    assert parent_range([*children, _child("none", {})]) == (low, high)


def test_the_sandbox_denies_the_run_directory_and_allows_only_its_own(tmp_path: Path):
    from flux_e2e import generator

    root = tmp_path / "run" / "generator"
    root.mkdir(parents=True)
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(Path(__import__("sys").executable))
    text = generator.Sandbox(root, venv.parent, deny=[root.parent]).write_profile()
    if text is None:
        pytest.skip("sandbox-exec is macOS only")
    deny = text.index("(deny file-read-data")
    allow = text.index("(allow file-read-data")
    run_dir = str(Path(__import__("os").path.realpath(root.parent)))
    assert "(deny network*)" in text
    assert deny < text.index(run_dir) < allow
    assert text.index(str(Path(__import__("os").path.realpath(root))), allow) > allow


def test_athena_integers_are_numbers():
    import numpy as np
    import pandas as pd

    frame = pd.DataFrame({"k": ["a"], "n": pd.array([1000], dtype="Int64")})
    (row,) = frame.itertuples(index=False)
    assert isinstance(row[1], np.integer)
    source = _result(["k", "n"], [tuple(row)])
    synthetic = _result(["k", "n"], [("a", np.int64(1010))])
    out = judges.compare_query(source, synthetic)
    assert out["measures"] == ["n"] and out["aggregateError"] == pytest.approx(0.01)
    assert judges._number(np.bool_(True)) is None and judges._number(True) is None


def test_a_zero_answer_scores_as_the_worst_not_as_missing():
    assert judges.relative_error(0.0, 5.0) == 1.0
    assert judges.relative_error(0.0, 0.0) == 0.0
    source = _result(["k", "n"], [("a", 0.0), ("b", 0.0)])
    synthetic = _result(["k", "n"], [("a", 3.0), ("b", 1.0)])
    out = judges.compare_query(source, synthetic, ["key", "measure"])
    assert out["aggregateError"] == 1.0 and out["shareDistance"] == 1.0
    summary = judges.summarise_queries({"q": out})
    assert summary["aggregateCompared"] == 1 and summary["aggregateErrorMedian"] == 1.0


def test_query_roles_read_the_outermost_select():
    sql = (
        "WITH t AS (SELECT a, sum(b) s FROM x GROUP BY a) "
        "SELECT d.d_year, i.i_brand brand, sum(ss_ext_sales_price) sum_agg, count(*) n "
        "FROM date_dim d, item i GROUP BY d.d_year, i.i_brand ORDER BY 1 LIMIT 100"
    )
    assert judges.query_roles(sql) == ["key", "key", "measure", "measure"]
    # A star over a subquery takes the subquery's roles; over a real table, none.
    assert judges.query_roles("SELECT * FROM (SELECT a, sum(b) s FROM x GROUP BY a) y") == [
        "key",
        "measure",
    ]
    assert judges.query_roles("SELECT * FROM t") is None
    assert judges.query_roles("SELECT c_customer_id FROM t") == [None]
    nested = (
        "SELECT c_last_name, ss_ticket_number, cnt FROM (SELECT ss_ticket_number, "
        "count(*) cnt FROM s GROUP BY ss_ticket_number) dn, customer"
    )
    assert judges.query_roles(nested) == [None, "key", "measure"]
    ranked = "SELECT x, rank() OVER (ORDER BY y) rk, avg(sum(y)) OVER () a FROM t GROUP BY x"
    assert judges.query_roles(ranked) == ["key", "key", "measure"]


def test_disjoint_keys_are_counted_not_averaged():
    source = _result(["name", "n"], [("Smith", 3.0)])
    synthetic = _result(["name", "n"], [("c_last_name-01", 3.0)])
    out = judges.compare_query(source, synthetic, ["key", "measure"])
    assert out["keysDisjoint"] is True
    summary = judges.summarise_queries({"q": out})
    assert summary["keysDisjoint"] == 1 and summary["shareDistanceMedian"] is None


def test_a_broken_source_query_or_key_check_is_a_harness_failure():
    per_query = {"q1": {"status": "error source"}, "q2": {"status": "error synthetic"}}
    keys = {
        "source": {"primaryKeys": {"t": {"error": "x"}}, "foreignKeys": {}},
        "synthetic": {"primaryKeys": {}, "foreignKeys": {"f": {"orphanRate": 0.1}}},
    }
    assert judges.harness_failures(per_query, keys) == ["q1", "source t"]


def test_null_fraction_falls_back_to_the_row_count():
    expose = {"exposeId": "t", "contract": {"schema": [{"name": "v", "type": "STRING"}]}}
    table = {
        "rowCount": 1000,
        "columns": {
            "v": {
                "metrics": {
                    "count": _metric(status="A"),
                    "nullCount": _metric(995),
                    "distinctCount": _metric(status="A"),
                }
            }
        },
    }
    assert bundle.plan_emit(expose, table).columns["v"]["nullFraction"] == "0.995"


def test_a_published_mean_keeps_totals_when_order_statistics_are_withheld():
    expose = {
        "exposeId": "t",
        "contract": {
            "schema": [{"name": "price", "type": "DECIMAL(7,2)"}, {"name": "qty", "type": "BIGINT"}]
        },
    }
    table = {
        "rowCount": 100,
        "columns": {
            "price": {
                "metrics": {"min": _metric("1.00"), "sum": _metric("500"), "count": _metric(100)}
            },
            "qty": {"metrics": {"sum": _metric("250"), "count": _metric(100)}},
        },
    }
    plan = bundle.plan_emit(expose, table)
    assert plan.columns["price"]["range"] == {"min": "1.00", "max": "9.00"}
    # An integer mean of 2.5 spans 2..3; a whole mean is one level.
    assert plan.columns["qty"]["range"] == {"min": "2", "max": "3"}
    table["columns"]["qty"]["metrics"]["sum"] = _metric("300")
    assert bundle.plan_emit(expose, table).columns["qty"]["levels"] == {"3": "1"}


def test_a_key_range_from_foreign_keys_is_at_least_row_count_wide():
    note = bundle._key_range("k", entry := {}, 1000, [Decimal(10), Decimal(50)])
    assert entry["range"] == {"min": "10", "max": "1009"}
    assert note.source == "assumed"
    # One referenced value still places the range: the key drawn there must exist.
    bundle._key_range("k", entry := {}, 5, [Decimal(102)])
    assert entry["range"] == {"min": "102", "max": "106"}


def test_digests_name_the_bundle_and_the_outputs(tmp_path: Path):
    from flux_e2e import generator

    (tmp_path / "bundle").mkdir()
    (tmp_path / "parquet" / "t").mkdir(parents=True)
    (tmp_path / "bundle" / "simulation.flux.yml").write_text("a")
    (tmp_path / "parquet" / "t" / "x.parquet").write_bytes(b"b")
    (tmp_path / "parquet" / "t" / "_manifest.json").write_text("{}")
    first = generator.digests(tmp_path)
    assert list(first["outputFiles"]) == ["t/x.parquet"]
    assert first == generator.digests(tmp_path)
    (tmp_path / "parquet" / "t" / "x.parquet").write_bytes(b"c")
    assert generator.digests(tmp_path)["outputs"] != first["outputs"]


def test_history_keeps_one_line_per_run(tmp_path: Path):
    history = tmp_path / "history.jsonl"
    record = {"runId": "r1", "config": {}, "steps": {}}
    report.write(tmp_path, record, history=history)
    report.write(tmp_path, record, history=history)
    report.write(tmp_path, {**record, "runId": "r2"}, history=history)
    assert [json.loads(line)["runId"] for line in history.read_text().splitlines()] == ["r1", "r2"]


class _FakeError(Exception):
    pass


class _FakeBoto:
    """Just enough of boto3 for the sweep: two buckets, one database, one workgroup."""

    region_name = "us-east-1"

    def __init__(self, failing: str = "") -> None:
        self.buckets = {"fluid-lane-flux-e2e-a", "fluid-lane-flux-e2e-b", "other"}
        self.databases = {"fluid_lane_flux_e2e_a_source", "keep_me"}
        self.workgroups = {"fluid-lane-flux-e2e-a", "primary"}
        self.failing = failing
        fake = self

        class Bucket:
            def __init__(self, name):
                self.name = name
                self.object_versions = self
                self.objects = self

            def all(self):
                return self

            def delete(self):
                if self.name == fake.failing:
                    raise _FakeError("AccessDenied")
                fake.buckets.discard(self.name)

        self.Bucket = Bucket

    def resource(self, name):
        return self

    def client(self, name):
        return self

    def list_buckets(self):
        return {"Buckets": [{"Name": name} for name in sorted(self.buckets)]}

    def get_bucket_location(self, Bucket):
        return {"LocationConstraint": None}

    def get_paginator(self, name):
        return self

    def paginate(self):
        return [{"DatabaseList": [{"Name": name} for name in sorted(self.databases)]}]

    def delete_database(self, Name):
        self.databases.discard(Name)

    def list_work_groups(self, **_):
        return {"WorkGroups": [{"Name": name} for name in sorted(self.workgroups)]}

    def delete_work_group(self, WorkGroup, RecursiveDeleteOption):
        self.workgroups.discard(WorkGroup)


def test_the_sweep_deletes_only_the_prefix_and_goes_on_past_a_failure(monkeypatch):
    monkeypatch.setenv(aws.LIVE_FLAG, "1")
    boto = _FakeBoto(failing="fluid-lane-flux-e2e-a")
    outcome = aws.sweep(boto, report=lambda _: None)
    assert boto.buckets == {"fluid-lane-flux-e2e-a", "other"}
    assert boto.databases == {"keep_me"} and boto.workgroups == {"primary"}
    assert outcome["remaining"]["buckets"] == ["fluid-lane-flux-e2e-a"]
    assert len(outcome["errors"]) == 1 and aws.left_over(outcome)
    assert not aws.left_over(aws.sweep(_FakeBoto(), report=lambda _: None))


def test_a_swept_run_publishes_again_and_says_what_was_left(tmp_path: Path, monkeypatch):
    from flux_e2e import cli

    run = cli.Run(tmp_path, "r-1234", "c0ffee")
    run.record["steps"]["publish"] = {"tables": {}}
    monkeypatch.setattr(
        cli.aws, "sweep", lambda boto: {"remaining": {"buckets": ["x"]}, "errors": []}
    )
    assert cli._sweep(run, object()) is True
    assert "publish" not in run.record["steps"] and run.record["published"] == {"tables": {}}


def test_a_resumed_run_refuses_other_settings(tmp_path: Path):
    from flux_e2e import cli

    args = cli.build_parser().parse_args(
        [
            "run",
            "--work-root",
            str(tmp_path),
            "--run-id",
            "r-1234",
            "--seed",
            "1",
            "--until",
            "source",
        ]
    )
    run = cli.Run(tmp_path, "r-1234", "c0ffee")
    run.record["config"] = {"scope": "store", "scaleFactor": 1.0, "seed": 2, "region": args.region}
    run.save()
    with pytest.raises(ValueError, match="different seed"):
        cli._run(args)


def test_an_aggregate_that_is_null_on_one_side_is_still_a_measure():
    # An aggregate over no matching rows: Athena returns one row of NULLs.
    source = _result(["_col0", "_col1"], [(Decimal("4.5"), 10.0)])
    synthetic = _result(["_col0", "_col1"], [(None, None)])
    out = judges.compare_query(source, synthetic, ["measure", "measure"])
    assert out["measures"] == ["_col0", "_col1"] and out["aggregateError"] == 1.0


def test_the_sandbox_refuses_an_interpreter_that_contains_the_run(tmp_path: Path):
    from flux_e2e import generator

    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(Path(__import__("sys").executable))
    run_dir = tmp_path / "venv" / "run"
    (run_dir / "generator").mkdir(parents=True)
    sandbox = generator.Sandbox(run_dir / "generator", venv.parent, deny=[run_dir])
    if __import__("sys").platform != "darwin":
        pytest.skip("sandbox-exec is macOS only")
    with pytest.raises(RuntimeError, match="contains"):
        sandbox.write_profile()


def _stopped_generator_run(tmp_path: Path, monkeypatch, work_root: Path, root: Path) -> str:
    """Run the generator up to its first sandboxed command; return the profile it wrote."""
    import sys

    from flux_e2e import generator

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(Path(sys.executable))
    contract, profile = tmp_path / "contract.yaml", tmp_path / "profile.json"
    contract.write_text("x", encoding="utf-8")
    profile.write_text("{}", encoding="utf-8")

    class _Stop(Exception):
        pass

    def stop(sandbox, step, argv, **_):
        raise _Stop(sandbox.profile_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(generator.Sandbox, "run", stop)
    with pytest.raises(_Stop) as stopped:
        generator.run_generator(root, venv, contract, profile, work_root=work_root, seed=1)
    return str(stopped.value)


def test_the_generator_is_denied_every_run_under_the_work_root(tmp_path: Path, monkeypatch):
    import os
    import re

    from flux_e2e import generator

    if not generator._can_confine():
        pytest.skip("sandbox-exec is macOS only")
    work_root = tmp_path / "work"
    other_run = work_root / "20261117-000000" / "source"
    other_run.mkdir(parents=True)
    root = work_root / "20261117-000001" / "generator"
    denied, allowed = _stopped_generator_run(tmp_path, monkeypatch, work_root, root).split(
        "(allow file-read-data", 1
    )
    assert f'(subpath "{os.path.realpath(work_root)}")' in denied
    assert f'(subpath "{os.path.realpath(root.parent)}")' in denied
    reopened = [Path(path) for path in re.findall(r'\(subpath "([^"]+)"\)', allowed)]
    assert reopened and not any(
        Path(os.path.realpath(other_run)).is_relative_to(path) for path in reopened
    )


@pytest.mark.parametrize("root", ["elsewhere/generator", "work"])
def test_the_generator_runs_only_inside_the_work_root(tmp_path: Path, monkeypatch, root: str):
    with pytest.raises(RuntimeError, match="not inside the work root"):
        _stopped_generator_run(tmp_path, monkeypatch, tmp_path / "work", tmp_path / root)


def _generate(tmp_path: Path, monkeypatch, write_notes) -> tuple:
    """cli._generate over a stand-in generator; return its result and what the generator got."""
    import argparse

    from flux_e2e import cli, generator

    run = cli.Run(tmp_path / "work", "20261117-000001", "commit")
    seen: dict = {}

    def fake_run(root, venv, contract, profile, **kwargs):
        seen.update(kwargs)
        root.mkdir(parents=True)
        write_notes(root / "authoring.json")
        return {"runId": "r", "sandboxExec": True, "commands": [], "verify": {}, "digests": {}}

    monkeypatch.setattr(generator, "build_venv", lambda repo, venv: {"distributions": []})
    monkeypatch.setattr(generator, "run_generator", fake_run)
    monkeypatch.setattr(cli.judges, "read_verify", lambda document: {})
    args = argparse.Namespace(seed=1, jobs=1, allow_unconfined=False)
    out = cli._generate(run, args, tmp_path / "c", tmp_path / "p", run.dir / "generator")
    return out, seen


def test_a_run_hands_its_generator_the_work_root(tmp_path: Path, monkeypatch):
    out, seen = _generate(
        tmp_path, monkeypatch, lambda notes: notes.write_text('{"n": 1}', encoding="utf-8")
    )
    assert seen["work_root"] == tmp_path / "work"
    assert out["authoring"] == {"n": 1}


def test_an_unresolved_identifier_column_is_a_key_by_its_name():
    source = _result(["ss_ticket_number", "cnt"], [(1001, 5), (1002, 7)])
    synthetic = _result(["ss_ticket_number", "cnt"], [(9001, 5), (9500, 7)])
    out = judges.compare_query(source, synthetic, [None, None])
    assert out["keys"] == ["ss_ticket_number"] and out["measures"] == ["cnt"]
    assert out["aggregateError"] == 0.0


def test_a_harness_error_on_the_synthetic_side_stops_the_judge():
    source = _result(["a"], [(1,)])
    synthetic = _result([], [], error="QueryFailed: Rate exceeded")
    out = judges.compare_query(source, synthetic)
    assert out["status"] == "error harness"
    keys = {"source": {"primaryKeys": {}, "foreignKeys": {}}}
    assert judges.harness_failures({"q": out}, keys) == ["q"]


def test_with_no_published_end_the_mean_spreads_over_the_distinct_count():
    expose = {"exposeId": "t", "contract": {"schema": [{"name": "ticket", "type": "BIGINT"}]}}
    table = {
        "rowCount": 1000,
        "columns": {
            "ticket": {
                "metrics": {
                    "sum": _metric("120047500"),
                    "count": _metric(1000),
                    "distinctCount": _metric(1001),
                    "quantiles": _quantiles({"0.5": "120000"}),
                }
            }
        },
    }
    plan = bundle.plan_emit(expose, table)
    # Mean 120047.5, 1001 values: 119547.5 .. 120547.5, floored and ceiled.
    assert plan.columns["ticket"]["range"] == {"min": "119547", "max": "120548"}


# ---- privacy ----------------------------------------------------------------------

from flux_e2e import anonymeter_judge, privacy  # noqa: E402


def test_the_holdout_is_a_keyed_hash_of_the_key():
    keys = list(range(1, 10_001))
    first = privacy.held_out(keys, 0.1, seed=17)
    assert first == privacy.held_out(list(reversed(keys)), 0.1, seed=17)
    assert 800 < len(first) < 1200
    assert first != privacy.held_out(keys, 0.1, seed=18)
    with pytest.raises(ValueError):
        privacy.held_out(keys, 1.0, seed=17)


def _tiny_source(path: Path) -> None:
    import duckdb

    with duckdb.connect(str(path)) as con:
        con.execute(
            "CREATE TABLE customer AS SELECT range AS c_customer_sk, 1990 AS c_birth_year, "
            "range % 150 AS c_current_addr_sk FROM range(1, 201)"
        )
        con.execute(
            "CREATE TABLE store_sales AS SELECT range % 200 + 1 AS ss_customer_sk, range AS ss_item_sk, "
            "range AS ss_ticket_number FROM range(0, 1000)"
        )
        # Every fifth return names no customer: it moves only when its sale moved.
        con.execute(
            "CREATE TABLE store_returns AS SELECT CASE WHEN range % 5 = 0 THEN NULL ELSE range % 200 + 1 END "
            "AS sr_customer_sk, range AS sr_item_sk, range AS sr_ticket_number FROM range(0, 1000, 2)"
        )


def test_split_holdout_moves_people_with_their_sales_and_returns(tmp_path: Path):
    import duckdb

    database = tmp_path / "s.duckdb"
    _tiny_source(database)
    outcome = privacy.split_holdout(database, 0.2, seed=3)
    held = set(privacy.held_out(list(range(1, 201)), 0.2, seed=3))
    assert outcome["heldOut"]["customer"] == len(held)
    assert 0 <= outcome["addressesOfHeldOutOnly"] <= len(held)
    with duckdb.connect(str(database), read_only=True) as con:
        kept = {row[0] for row in con.execute("SELECT c_customer_sk FROM customer").fetchall()}
        assert not kept & held and len(kept) + len(held) == 200
        assert (
            con.execute(
                "SELECT count(*) FROM store_sales WHERE ss_customer_sk IN (SELECT sk FROM holdout.held)"
            ).fetchone()[0]
            == 0
        )
        # No kept return refers to a moved sale.
        assert (
            con.execute(
                "SELECT count(*) FROM store_returns r JOIN holdout.store_sales s "
                "ON s.ss_item_sk = r.sr_item_sk AND s.ss_ticket_number = r.sr_ticket_number"
            ).fetchone()[0]
            == 0
        )
        moved = con.execute("SELECT count(*) FROM holdout.store_returns").fetchone()[0]
    assert moved == outcome["heldOut"]["store_returns"] > 0


def test_the_person_view_has_one_row_per_customer(tmp_path: Path):
    import duckdb

    con = duckdb.connect()
    con.execute(
        "COPY (SELECT * FROM (VALUES (1, 10, 100, 1980, 5, 'Y'), (2, 11, 101, 1990, 7, 'N')) "
        "t(c_customer_sk, c_current_addr_sk, c_current_cdemo_sk, c_birth_year, c_birth_month, "
        f"c_preferred_cust_flag)) TO '{tmp_path / 'customer.parquet'}'"
    )
    # Address 10 appears twice, as a synthetic copy may repeat a key.
    con.execute(
        "COPY (SELECT * FROM (VALUES (10, 'Oak', 'TN', '1'), (10, 'Elm', 'TX', '2'), (11, 'Ash', 'GA', '3')) "
        f"t(ca_address_sk, ca_city, ca_state, ca_zip)) TO '{tmp_path / 'customer_address.parquet'}'"
    )
    con.execute(
        "COPY (SELECT * FROM (VALUES (100, 'M', 'S', 'College', 500, 'Good'), (101, 'F', 'M', 'Primary', 900, 'Low'))"
        " t(cd_demo_sk, cd_gender, cd_marital_status, cd_education_status, cd_purchase_estimate, "
        f"cd_credit_rating)) TO '{tmp_path / 'customer_demographics.parquet'}'"
    )
    con.execute(
        "COPY (SELECT * FROM (VALUES (1, 2.50), (1, 3.00), (NULL, 9.00)) t(ss_customer_sk, ss_net_paid)) "
        f"TO '{tmp_path / 'store_sales.parquet'}'"
    )
    sources = {table: tmp_path / f"{table}.parquet" for table in privacy.PERSON_TABLES}
    out = tmp_path / "view.parquet"
    assert privacy.person_view(sources, out) == 2
    rows = con.execute(
        f"SELECT ca_city, total_spend, sales_count FROM read_parquet('{out}')"
    ).fetchall()
    assert rows == [("Elm", 5.5, 2.0), ("Ash", 0.0, 0.0)]
    columns = [
        row[0] for row in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{out}')").fetchall()
    ]
    assert columns == privacy.view_columns()


def test_each_criterion_reports_its_riskiest_attack():
    attacks = {
        "singlingOut.univariate": {"risk": 0.01, "ci": [0, 0.05], "beatsBaseline": True},
        "singlingOut.multivariate": {"risk": 0.03, "ci": [0, 0.09], "beatsBaseline": True},
        "linkability": {"error": "ValueError: no rows"},
        "inference.a": {"risk": 0.2, "ci": [0.1, 0.3], "beatsBaseline": False},
    }
    best = anonymeter_judge.headline(attacks)
    assert best["singlingOut"]["attack"] == "singlingOut.multivariate"
    assert best["linkability"]["risk"] is None
    assert best["linkability"]["of"] == best["linkability"]["failed"] == 1
    assert best["singlingOut"]["of"] == 2 and best["singlingOut"]["failed"] == 0
    assert best["inference"] == {
        "attack": "inference.a",
        "risk": 0.2,
        "ci": [0.1, 0.3],
        "beatsBaseline": False,
        "verbatimCopy": None,
        "of": 1,
        "failed": 0,
    }
    # An attack that beat its baseline outranks a higher one that did not.
    attacks["inference.b"] = {"risk": 0.05, "ci": [0, 0.1], "beatsBaseline": True}
    assert anonymeter_judge.headline(attacks)["inference"]["attack"] == "inference.b"
    # The wide linkage attack counts under linkage, beside the narrow one.
    attacks["linkability.wide"] = {"risk": 0.02, "ci": [0, 0.04], "beatsBaseline": True}
    linkage = anonymeter_judge.headline(attacks)["linkability"]
    assert linkage["attack"] == "linkability.wide" and linkage["of"] == 2
    assert linkage["failed"] == 1


def test_the_narrow_linkage_sets_stay_and_every_set_column_is_in_the_view():
    spec = privacy.spec(10, 1)
    assert spec["linkageSets"] == [
        ["c_birth_year", "c_birth_month", "cd_gender"],
        ["ca_city", "ca_state", "ca_zip"],
    ]
    columns = set(privacy.view_columns())
    for sets in (spec["linkageSets"], spec["wideLinkageSets"]):
        assert all(set(each) <= columns for each in sets)
    assert len(spec["wideLinkageSets"][0]) > 3 and len(spec["wideLinkageSets"][1]) > 3


def test_the_null_split_draws_built_from_people_outside_the_training_sample():
    import pandas as pd

    from flux_e2e import sdmetrics_judge

    frame = pd.DataFrame({"a": range(30)})
    training = frame.sample(n=10, random_state=0)
    others = sdmetrics_judge.disjoint_sample(frame, training, seed=1)
    assert len(others) == 10 and not set(others.index) & set(training.index)
    small = frame.head(15)
    assert sdmetrics_judge.disjoint_sample(small, small.head(5), seed=1) is not None
    assert sdmetrics_judge.disjoint_sample(small, small.head(10), seed=1) is None


def test_the_report_names_the_source_as_holding_no_real_people():
    record = {
        "runId": "r1",
        "config": {"holdout": 0.1},
        "steps": {
            "source": {"holdout": {"fraction": 0.1, "customers": 10, "seed": 1, "heldOut": {}}},
            "privacy": {
                "rows": {"ori": 9, "syn": 9, "control": 1},
                "spec": privacy.spec(10, 1),
                "anonymeter": {
                    "criteria": anonymeter_judge.CRITERIA,
                    "attacks": {
                        "linkability": {
                            "risk": 0.0,
                            "ci": [0.0, 0.1],
                            "beatsBaseline": False,
                            "verbatimCopy": {"error": "ValueError: too few"},
                        },
                        "inference.a": {"error": "ValueError: no rows"},
                    },
                    "headline": {
                        "linkability": {
                            "attack": "linkability",
                            "risk": 0.0,
                            "ci": [0.0, 0.1],
                            "beatsBaseline": False,
                            "of": 1,
                            "failed": 0,
                        },
                        "inference": {"attack": None, "risk": None, "of": 1, "failed": 1},
                    },
                },
                "distance": {
                    "overfitting": {"score": 0.9, "synthetic_data_percentages": {}},
                    "overfittingNull": {"score": 0.92, "synthetic_data_percentages": {}},
                    "baseline": {"score": 0.8},
                },
            },
        },
    }
    head = report.headline(record)
    assert head["privacyDistanceOverfitting"] == 0.9 and head["privacyDistanceNull"] == 0.92
    assert head["privacyAttackRisks"] == {"inference.a": None, "linkability": 0.0}
    assert head["privacyLinkability"]["ci"] == [0.0, 0.1] and head["privacySinglingOut"] is None
    assert head["privacyInference"]["risk"] is None and head["privacyInference"]["failed"] == 1
    text = report.render(record, head)
    assert "holds no real people" in text
    assert "error: ValueError: too few" in text
    assert "- inference: no risk measured; 1 of 1 attacks could not run" in text
    assert "did not beat its baseline; 0 of 1 attacks could not run" in text
    assert "- null split, the same metric between two disjoint samples" in text
    record["steps"]["privacy"]["distance"]["overfittingNull"] = {"skipped": "too few"}
    assert "- null split: not measured (too few)" in report.render(record, report.headline(record))


def test_a_link_in_the_generator_output_is_refused(tmp_path: Path):
    import os

    from flux_e2e import generator

    real = tmp_path / "source.parquet"
    real.write_bytes(b"real rows")
    folder = tmp_path / "generator" / "parquet" / "customer"
    folder.mkdir(parents=True)
    (folder / "part-0.parquet").write_bytes(b"synthetic rows")
    assert generator.output_files(tmp_path / "generator", "customer") == [folder / "part-0.parquet"]
    (folder / "part-1.parquet").symlink_to(real)
    with pytest.raises(RuntimeError, match="link"):
        generator.output_files(tmp_path / "generator", "customer")
    (folder / "part-1.parquet").unlink()
    os.link(real, folder / "part-1.parquet")
    with pytest.raises(RuntimeError, match="link"):
        generator.output_files(tmp_path / "generator", "customer")


def test_a_link_at_the_authoring_notes_is_refused(tmp_path: Path):
    import os

    from flux_e2e import generator

    root = tmp_path / "generator"
    root.mkdir()
    notes = root / "authoring.json"
    notes.write_text('{"emits": 1}', encoding="utf-8")
    assert generator.authoring_notes(root) == {"emits": 1}
    real = tmp_path / "source.json"
    real.write_text('{"rows": "real"}', encoding="utf-8")
    notes.unlink()
    notes.symlink_to(real)
    with pytest.raises(RuntimeError, match="link"):
        generator.authoring_notes(root)
    notes.unlink()
    os.link(real, notes)
    with pytest.raises(RuntimeError, match="link"):
        generator.authoring_notes(root)


def test_a_run_refuses_authoring_notes_left_as_a_link(tmp_path: Path, monkeypatch):
    real = tmp_path / "source.json"
    real.write_text('{"rows": "real"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="link"):
        _generate(tmp_path, monkeypatch, lambda notes: notes.symlink_to(real))


def test_the_privacy_judge_needs_a_holdout(tmp_path: Path):
    from flux_e2e import cli

    args = cli.build_parser().parse_args(
        [
            "run",
            "--work-root",
            str(tmp_path),
            "--run-id",
            "r-1234",
            "--anonymeter-python",
            "/bin/true",
        ]
    )
    with pytest.raises(ValueError, match="control set"):
        cli._run(args)


# ---- follow: the prototype for ADR 0011 -------------------------------------------------

from flux_e2e import follow, generator  # noqa: E402


def test_a_follow_table_is_published_only_when_every_cell_passes():
    assert follow.checked_cells([(12, 11), (43, 30)]) == [10, 45]
    assert follow.checked_cells([(12, 9), (43, 30)]) is None  # too few people
    assert follow.checked_cells([(9, 9), (43, 30)]) is None  # too few rows
    assert follow.checked_cells([(0, 0), (43, 30)]) is None  # a zero cell is disclosive
    assert follow.checked_cells([]) is None


def _follow_source(path: Path, people: int, lag: str = "(ss_item_sk * 7) % 190"):
    import duckdb

    with duckdb.connect(str(path)) as con:
        con.execute(
            "CREATE TABLE store_sales AS SELECT i AS ss_item_sk, i // 4 AS ss_ticket_number, "
            "(i // 10) % 50 AS ss_customer_sk, 2450000 + i // 4 AS ss_sold_date_sk, "
            "10 AS ss_quantity FROM range(2000) t(i)"
        )
        con.execute(
            "CREATE TABLE store_returns AS SELECT ss_item_sk AS sr_item_sk, "
            "ss_ticket_number AS sr_ticket_number, "
            f"CASE WHEN ss_item_sk % 4 = 0 THEN (ss_item_sk + 1) % {people} "
            f"ELSE ss_customer_sk % {people} END AS sr_customer_sk, "
            f"ss_sold_date_sk + {lag} AS sr_returned_date_sk, "
            "1 + ss_item_sk % 10 AS sr_return_quantity FROM store_sales"
        )


def test_owner_statistics_publish_shares_and_lags_that_pass(tmp_path: Path):
    _follow_source(tmp_path / "s.duckdb", people=50)
    published = follow.owner_statistics(tmp_path / "s.duckdb")["store_returns"]
    customer = published["customerSameAsSale"]
    assert customer["same"] + customer["other"] == 2000 and customer["other"] > 0
    lag = published["lagDays"]["counts"]
    assert len(lag) == 19 and all(count % 5 == 0 for count in lag)
    assert abs(sum(lag) - 2000) <= 2 * len(lag)  # each count is rounded to a multiple of 5
    assert published["quantityShare"]["counts"] == [200] * 10


def test_owner_statistics_withhold_a_table_with_too_few_people(tmp_path: Path):
    _follow_source(tmp_path / "s.duckdb", people=5)
    published = follow.owner_statistics(tmp_path / "s.duckdb")["store_returns"]
    assert published["customerSameAsSale"] == {"status": "D"}
    assert published["lagDays"] == {"status": "D"}
    assert published["quantityShare"] == {"status": "D"}


def test_an_empty_declared_bin_withholds_the_whole_histogram(tmp_path: Path):
    # Every lag lies in 5 to 44 days, so the bins from 50 days on hold no row: a zero
    # cell is disclosive, and the owner would declare narrower bins.
    _follow_source(tmp_path / "s.duckdb", people=50, lag="5 + ss_item_sk % 40")
    published = follow.owner_statistics(tmp_path / "s.duckdb")["store_returns"]
    assert published["lagDays"] == {"status": "D"}
    assert "counts" in published["quantityShare"]


def _synthetic_copy(root: Path):
    import duckdb

    tables = {
        "date_dim": "SELECT 2450000 + i AS d_date_sk, DATE '1970-01-01' AS d_date, 0::BIGINT AS "
        "d_year, 0::BIGINT AS d_qoy, 'x' AS d_day_name, 'x' AS d_weekend FROM range(400) t(i)",
        "time_dim": "SELECT 28800 + i AS t_time_sk, 0::BIGINT AS t_hour, 'x' AS t_meal_time "
        "FROM range(3600) t(i)",
        "store_sales": "SELECT i AS ss_item_sk, i // 4 AS ss_ticket_number, "
        "CASE WHEN i % 7 = 0 THEN NULL ELSE i % 9 END AS ss_customer_sk, "
        "2450000 + i % 30 AS ss_sold_date_sk, (1 + i % 10)::BIGINT AS ss_quantity, "
        "CAST(1.25 + i % 3 AS DECIMAL(7,2)) AS ss_sales_price FROM range(200) t(i)",
        "store_returns": "SELECT i * 2 AS sr_item_sk, (i * 2) // 4 AS sr_ticket_number, "
        "CASE WHEN i % 5 = 0 THEN NULL ELSE 1000 + i END AS sr_customer_sk, "
        "2450100 + i AS sr_returned_date_sk, 99::BIGINT AS sr_return_quantity, "
        "CAST(0 AS DECIMAL(7,2)) AS sr_return_amt FROM range(100) t(i)",
    }
    with duckdb.connect() as con:
        for table, select in tables.items():
            folder = root / "parquet" / table
            folder.mkdir(parents=True)
            con.execute(f"COPY ({select}) TO '{folder / 'part-0.parquet'}' (FORMAT parquet)")


def test_after_the_layers_every_declared_rule_holds(tmp_path: Path):
    import duckdb

    _synthetic_copy(tmp_path)
    statistics = {
        "store_returns": {
            "customerSameAsSale": {"same": 100, "other": 0},
            "lagDays": {"edges": [0, 10, 20], "counts": [10, 10]},
            "quantityShare": {"edges": ["0", "1/2", "1"], "counts": [10, 10]},
        }
    }
    done = follow.apply(tmp_path, statistics, ["parent", "reference", "ticket"])
    assert set(done) == {"reference", "ticket", "parent"}
    con = duckdb.connect()
    for table in ("date_dim", "time_dim", "store_sales", "store_returns"):
        files = [str(path) for path in generator.output_files(tmp_path, table)]
        assert len(files) == 1
        con.execute(f"CREATE VIEW {table} AS SELECT * FROM read_parquet({files})")
    one = lambda sql: con.execute(sql).fetchone()  # noqa: E731
    assert one(
        "SELECT count(*) FILTER (WHERE d_date <> DATE '1970-01-01' + "
        "CAST(d_date_sk - 2440588 AS INTEGER) OR d_year <> year(d_date)) FROM date_dim"
    ) == (0,)
    assert one("SELECT count(*) FILTER (WHERE t_hour <> t_time_sk // 3600) FROM time_dim") == (0,)
    assert one(
        "SELECT count(*) FROM (SELECT ss_ticket_number FROM store_sales GROUP BY 1 "
        "HAVING count(DISTINCT ss_sold_date_sk) > 1 OR count(DISTINCT ss_customer_sk) > 1)"
    ) == (0,)
    # A sale line's own null stays null.
    assert one("SELECT count(*) FILTER (WHERE ss_customer_sk IS NULL) FROM store_sales") == (29,)
    joined = (
        "FROM store_returns r JOIN store_sales s "
        "ON r.sr_item_sk = s.ss_item_sk AND r.sr_ticket_number = s.ss_ticket_number"
    )
    lag, quantity, amount, customer, kept_null = one(
        "SELECT count(*) FILTER (WHERE sr_returned_date_sk - ss_sold_date_sk NOT BETWEEN 0 AND 19), "
        "count(*) FILTER (WHERE sr_return_quantity > ss_quantity OR sr_return_quantity < 1), "
        "count(*) FILTER (WHERE sr_return_amt <> ss_sales_price * sr_return_quantity), "
        "count(*) FILTER (WHERE sr_customer_sk IS NOT NULL AND ss_customer_sk IS NOT NULL "
        "AND sr_customer_sk <> ss_customer_sk), "
        f"count(*) FILTER (WHERE sr_customer_sk IS NULL) {joined}"
    )
    assert (lag, quantity, amount, customer, kept_null) == (0, 0, 0, 0, 20)
    assert one(
        "SELECT count(*) FROM store_returns WHERE sr_returned_date_sk NOT IN "
        "(SELECT d_date_sk FROM date_dim)"
    ) == (0,)


def test_draws_under_two_labels_do_not_move_together():
    import duckdb

    a, b = follow._uniform("i", "'one'"), follow._uniform("i", "'two'")
    (correlation,) = (
        duckdb.connect().execute(f"SELECT corr({a}, {b}) FROM range(20000) t(i)").fetchone()
    )
    assert abs(correlation) < 0.03


def test_an_unknown_layer_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown layer 'keys'"):
        follow.apply(tmp_path, {}, ["keys"])


def _write_table(root: Path, table: str, select: str, name: str = "part-0.parquet"):
    import duckdb

    folder = root / "parquet" / table
    folder.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as con:
        con.execute(f"COPY ({select}) TO '{folder / name}' (FORMAT parquet)")


#: dsdgen's date_dim on five dates, quirks included: April 1 of a year that is not divisible
#: by four sits in the quarter before, and the weekend is Friday and Saturday.
_SOURCE_DAYS = [
    (2451545, 2000, 1, 1, 1, 6, "Saturday", 1200, 5218, 401, "2000Q1", "Y", 2451545, 2451180),
    (2451636, 2000, 4, 1, 2, 6, "Saturday", 1203, 5231, 402, "2000Q2", "Y", 2451636, 2451270),
    (2452001, 2001, 4, 1, 1, 0, "Sunday", 1215, 5283, 406, "2001Q1", "N", 2452001, 2451636),
    (2452275, 2001, 12, 31, 4, 1, "Monday", 1223, 5322, 409, "2001Q4", "N", 2452245, 2451910),
    (2452461, 2002, 7, 5, 3, 5, "Friday", 1230, 5349, 411, "2002Q3", "Y", 2452457, 2452096),
]
_SOURCE_SECONDS = [
    (0, 0, 0, 0, "AM", "third", "night", None),
    (25200, 7, 0, 0, "AM", "first", "morning", "breakfast"),
    (43199, 11, 59, 59, "AM", "first", "morning", None),
    (61200, 17, 0, 0, "PM", "second", "evening", "dinner"),
    (82800, 23, 0, 0, "PM", "third", "evening", None),
]


def test_the_reference_layer_gives_dsdgen_s_calendar_and_clock(tmp_path: Path):
    import duckdb

    keys = ", ".join(str(day[0]) for day in _SOURCE_DAYS)
    _write_table(
        tmp_path,
        "date_dim",
        "SELECT unnest(["
        + keys
        + "])::BIGINT AS d_date_sk, DATE '1970-01-01' AS d_date, "
        + ", ".join(
            f"0::BIGINT AS {name}"
            for name in (
                "d_year d_moy d_dom d_qoy d_dow d_month_seq d_week_seq d_quarter_seq "
                "d_first_dom d_same_day_ly"
            ).split()
        )
        + ", 'x' AS d_day_name, 'x' AS d_quarter_name, 'x' AS d_weekend",
    )
    seconds = ", ".join(str(second[0]) for second in _SOURCE_SECONDS)
    _write_table(
        tmp_path,
        "time_dim",
        "SELECT unnest([" + seconds + "])::BIGINT AS t_time_sk, 0::BIGINT AS t_hour, "
        "0::BIGINT AS t_minute, 0::BIGINT AS t_second, 'x' AS t_am_pm, 'x' AS t_shift, "
        "'x' AS t_sub_shift, 'x' AS t_meal_time",
    )
    follow.apply(tmp_path, {}, ["reference"])
    con = duckdb.connect()
    read = lambda table: [str(p) for p in generator.output_files(tmp_path, table)]  # noqa: E731
    days = con.execute(
        "SELECT d_date_sk, d_year, d_moy, d_dom, d_qoy, d_dow, d_day_name, d_month_seq, "
        "d_week_seq, d_quarter_seq, d_quarter_name, d_weekend, d_first_dom, d_same_day_ly "
        f"FROM read_parquet({read('date_dim')}) ORDER BY 1"
    ).fetchall()
    assert days == _SOURCE_DAYS
    clock = con.execute(
        "SELECT t_time_sk, t_hour, t_minute, t_second, t_am_pm, t_shift, t_sub_shift, "
        f"t_meal_time FROM read_parquet({read('time_dim')}) ORDER BY 1"
    ).fetchall()
    assert clock == _SOURCE_SECONDS


def test_a_returned_quantity_stays_inside_the_bin_drawn(tmp_path: Path):
    import duckdb

    # Fifteen sold: bin (0, 1/10] holds 1 only and bin (9/10, 1] holds 14 and 15. A share
    # drawn inside the first bin and rounded up would also give 2, which belongs to the next.
    _write_table(
        tmp_path,
        "store_sales",
        "SELECT i AS ss_item_sk, i AS ss_ticket_number, i % 7 AS ss_customer_sk, "
        "2450000 AS ss_sold_date_sk, 15::BIGINT AS ss_quantity, "
        "CAST(2.00 AS DECIMAL(7,2)) AS ss_sales_price FROM range(2000) t(i)",
    )
    _write_table(
        tmp_path,
        "store_returns",
        "SELECT i AS sr_item_sk, i AS sr_ticket_number, 1 AS sr_customer_sk, "
        "NULL::BIGINT AS sr_returned_date_sk, 5::BIGINT AS sr_return_quantity, "
        "CAST(0 AS DECIMAL(7,2)) AS sr_return_amt FROM range(2000) t(i)",
    )
    _write_table(tmp_path, "date_dim", "SELECT 2450000 AS d_date_sk")
    counts = [100] + [0] * 8 + [100]
    statistics = {
        "store_returns": {
            "customerSameAsSale": {"status": "D"},
            "lagDays": {"status": "D"},
            "quantityShare": {"edges": [str(f) for f in follow.QUANTITY_EDGES], "counts": counts},
        }
    }
    follow.apply(tmp_path, statistics, ["parent"])
    files = [str(p) for p in generator.output_files(tmp_path, "store_returns")]
    got = dict(
        duckdb.connect()
        .execute(f"SELECT sr_return_quantity, count(*) FROM read_parquet({files}) GROUP BY 1")
        .fetchall()
    )
    assert set(got) == {1, 14, 15}
    assert 0.4 < got[1] / 2000 < 0.6  # half the rows draw each bin
    # One bin with rows is a bin every row draws.
    statistics["store_returns"]["quantityShare"]["counts"] = [0] * 9 + [100]
    follow.apply(tmp_path, statistics, ["parent"])
    files = [str(p) for p in generator.output_files(tmp_path, "store_returns")]
    (only,) = (
        duckdb.connect()
        .execute(f"SELECT list(DISTINCT sr_return_quantity ORDER BY 1) FROM read_parquet({files})")
        .fetchone()
    )
    assert only == [14, 15]


def test_a_failing_coverage_cell_withholds_every_table(tmp_path: Path):
    import duckdb

    _follow_source(tmp_path / "s.duckdb", people=50)
    with duckdb.connect(str(tmp_path / "s.duckdb")) as con:
        # Three returns lose their date: the rows the lag table leaves out, and only those,
        # would be a cell of three found by subtracting two published totals.
        con.execute(
            "UPDATE store_returns SET sr_returned_date_sk = NULL WHERE sr_item_sk IN (1, 2, 3)"
        )
    published = follow.owner_statistics(tmp_path / "s.duckdb")["store_returns"]
    assert published == {
        "customerSameAsSale": {"status": "D"},
        "lagDays": {"status": "D"},
        "quantityShare": {"status": "D"},
    }


def test_a_generator_output_name_that_could_write_sql_is_refused(tmp_path: Path):
    _write_table(tmp_path, "date_dim", "SELECT 1 AS d_date_sk")
    folder = tmp_path / "parquet" / "date_dim"
    (folder / "part-0.parquet").rename(folder / "a'); --.parquet")
    with pytest.raises(ValueError, match="refusing a generator output"):
        follow.apply(tmp_path, {}, ["reference"])


def _finished_run(root: Path, run_id: str):
    run = root / run_id
    _synthetic_copy(run / "generator")
    (run / "source").mkdir()
    _follow_source(run / "source" / "tpcds.duckdb", people=50)
    (run / "owner").mkdir()
    (run / "privacy").mkdir()
    record = {
        "runId": run_id,
        "config": {"label": "base"},
        "published": {"source": {}},
        "steps": {
            name: {"digests": {"outputs": "sha256:old"}} if name == "generate" else {}
            for name in ("source", "generate", "privacy", "publish", "judge")
        },
    }
    (run / "run.json").write_text(json.dumps(record), encoding="utf-8")


def test_a_clone_is_rewritten_with_its_own_digests_and_steps_to_run(tmp_path: Path):
    _finished_run(tmp_path, "run-a")
    target = follow.clone_run(tmp_path, "run-a", "run-b", ["reference", "ticket", "parent"])
    record = json.loads((target / "run.json").read_text(encoding="utf-8"))
    assert record["runId"] == "run-b"
    assert sorted(record["steps"]) == ["generate", "source"]
    assert "published" not in record and not (target / "privacy").exists()
    assert record["steps"]["generate"]["digests"]["outputs"] != "sha256:old"
    assert record["follow"]["layers"] == ["reference", "ticket", "parent"]
    assert (tmp_path / "run-a" / "privacy").exists()  # the original is untouched


def test_a_clone_that_fails_is_removed(tmp_path: Path):
    _finished_run(tmp_path, "run-a")
    with pytest.raises(ValueError, match="unknown layer"):
        follow.clone_run(tmp_path, "run-a", "run-b", ["parnt"])
    assert not (tmp_path / "run-b").exists()
    for path in (tmp_path / "run-a" / "generator" / "parquet" / "date_dim").iterdir():
        path.unlink()
    with pytest.raises(ValueError, match="no date_dim output"):
        follow.clone_run(tmp_path, "run-a", "run-c", ["reference"])
    assert not (tmp_path / "run-c").exists()
    with pytest.raises(ValueError, match="no run run-x"):
        follow.clone_run(tmp_path, "run-x", "run-d", ["reference"])
