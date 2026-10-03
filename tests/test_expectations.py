from datetime import UTC, datetime

from conftest import write_parquet

from dbt_isrm.expectations import evaluate
from dbt_isrm.models import RunRecord

ORDERS = "model.columns_and_constraints.orders"
CURRENT = "11111111-1111-5111-8111-111111111111"


def record(fixture="columns_and_constraints", stage="compile_strict", artifacts=True):
    return RunRecord(
        run_id="r",
        dbt_version="2.0.6",
        fixture=fixture,
        stage=stage,
        invocation_id=CURRENT,
        dbt_args=[],
        dbt_version_output="dbt 2.0.6",
        dbt_wheel_tag="t",
        dbt_binary_sha256="0" * 64,
        os="o",
        arch="a",
        warehouse=False,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        duration_ms=1,
        exit_code=0,
        timed_out=False,
        stdout_path="x",
        stderr_path="x",
        artifact_root="snap/info_schema" if artifacts else None,
        info_schema_versions=["v1"] if artifacts else [],
        table_count=0,
    )


def node_columns(tmp_path, rows):
    """rows: (column_name, constraints, data_type_inferred)"""
    values = (
        ", ".join(
            "('{}', '{}', {}, {})".format(
                ORDERS, c, "NULL" if k is None else f"'{k}'", "NULL" if t is None else f"'{t}'"
            )
            for c, k, t in rows
        )
        or "('', '', NULL::VARCHAR, NULL::VARCHAR)"
    )
    write_parquet(
        tmp_path / "snap/info_schema/v1/dbt.node_columns.parquet",
        f"SELECT * FROM (VALUES {values}) "
        "v(node_unique_id, column_name, constraints, data_type_inferred)"
        + ("" if rows else " WHERE false"),
    )


def invocations(tmp_path, ids):
    values = ", ".join(f"('{i}')" for i in ids) or "(NULL)"
    where = "" if ids else " WHERE false"
    write_parquet(
        tmp_path / "snap/info_schema/v1/dbt_rt.invocations.parquet",
        f"SELECT * FROM (VALUES {values}) v(invocation_id){where}",
    )


def results(tmp_path, rec):
    df = evaluate(rec, tmp_path)
    return {r["expectation_id"]: (r["result"], r["detail"]) for r in df.iter_rows(named=True)}


NN = '[{"type":"not_null"}]'
NN_PK = '[{"type":"primary_key"},{"type":"not_null","name":null}]'


def test_constraints_pass_when_declared_types_are_exposed(tmp_path):
    node_columns(tmp_path, [("order_id", NN_PK, None), ("customer_id", NN, None)])
    invocations(tmp_path, [CURRENT])
    got = results(tmp_path, record(stage="compile"))
    assert got["constraints_exposed"] == (
        "pass",
        "order_id=['not_null', 'primary_key'] customer_id=['not_null']",
    )


def test_constraints_fail_on_null_and_on_unparseable(tmp_path):
    node_columns(tmp_path, [("order_id", None, None), ("customer_id", "not json", None)])
    invocations(tmp_path, [])
    got = results(tmp_path, record(stage="compile"))
    assert got["constraints_exposed"] == (
        "fail",
        "order_id=NULL customer_id=['unparsed:not json']",
    )


def test_inferred_types_compare_to_duckdb_names(tmp_path):
    adapter = [
        ("order_id", None, "integer"),
        ("customer_id", None, "INTEGER"),
        ("status", None, "VARCHAR"),
        ("amount", None, "DECIMAL(10, 2)"),
    ]
    node_columns(tmp_path, adapter)
    invocations(tmp_path, [CURRENT])
    assert results(tmp_path, record())["inferred_types_are_adapter_types"][0] == "pass"


def test_inferred_types_fail_on_arrow_names(tmp_path):
    arrow = [
        ("order_id", None, "Int32"),
        ("customer_id", None, "Int32"),
        ("status", None, "Utf8"),
        ("amount", None, "Decimal128(10, 2)"),
    ]
    node_columns(tmp_path, arrow)
    invocations(tmp_path, [CURRENT])
    assert results(tmp_path, record())["inferred_types_are_adapter_types"] == (
        "fail",
        "order_id=Int32 customer_id=Int32 status=Utf8 amount=Decimal128(10, 2)",
    )


def test_current_invocation(tmp_path):
    node_columns(tmp_path, [])
    invocations(tmp_path, ["older-1", "older-2"])
    assert results(tmp_path, record())["current_invocation_recorded"] == (
        "fail",
        "rows=2 current=absent",
    )
    invocations(tmp_path, ["older-1", CURRENT])
    assert results(tmp_path, record())["current_invocation_recorded"] == (
        "pass",
        "rows=2 current=present",
    )


def test_missing_table_is_missing_not_a_crash(tmp_path):
    node_columns(tmp_path, [])  # no dbt_rt.invocations written
    got = results(tmp_path, record())
    assert got["current_invocation_recorded"][0] == "missing"


def test_no_artifacts_is_missing(tmp_path):
    got = results(tmp_path, record(artifacts=False))
    assert set(got) == {
        "constraints_exposed",
        "inferred_types_are_adapter_types",
        "current_invocation_recorded",
    }
    assert all(v == ("missing", "no information schema") for v in got.values())


def test_applicability_by_fixture_and_stage(tmp_path):
    node_columns(tmp_path, [])
    invocations(tmp_path, [])
    # parse writes no runtime results; another fixture declares no constraints.
    assert set(results(tmp_path, record(stage="parse"))) == {"constraints_exposed"}
    assert set(results(tmp_path, record(fixture="basic_model", stage="build"))) == {
        "current_invocation_recorded"
    }
