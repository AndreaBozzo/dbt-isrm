import duckdb
import polars as pl
from conftest import write_parquet

from dbt_isrm.artifacts import info_schema_versions, inspect_info_schema, inspect_parquet


def stats_by_column(path):
    con = duckdb.connect()
    try:
        return {row["column"]: row for row in inspect_parquet(con, path)}
    finally:
        con.close()


def test_varchar_empty_values_are_not_populated(tmp_path):
    path = write_parquet(
        tmp_path / "t.parquet",
        "SELECT * FROM (VALUES ('x'), (''), ('{}'), ('[]'), ('null'), (NULL)) v(s)",
    )
    s = stats_by_column(path)["s"]
    assert s["dtype"] == "VARCHAR"
    assert (s["row_count"], s["non_null_count"], s["null_count"]) == (6, 5, 1)
    assert (s["empty_count"], s["populated_count"]) == (4, 1)
    assert s["population_ratio"] == 1 / 6


def test_whitespace_and_json_values_count_as_populated(tmp_path):
    # Only the exact empty markers are empty; anything else carries information.
    path = write_parquet(
        tmp_path / "t.parquet", "SELECT * FROM (VALUES (' '), ('{\"a\":1}'), ('NULL')) v(s)"
    )
    s = stats_by_column(path)["s"]
    assert (s["empty_count"], s["populated_count"]) == (0, 3)


def test_list_empty_values_are_not_populated(tmp_path):
    path = write_parquet(
        tmp_path / "t.parquet",
        "SELECT * FROM (VALUES (['a']), ([]::VARCHAR[]), (NULL::VARCHAR[])) v(tags)",
    )
    tags = stats_by_column(path)["tags"]
    assert tags["dtype"] == "VARCHAR[]"
    assert (tags["null_count"], tags["empty_count"], tags["populated_count"]) == (1, 1, 1)


def test_scalar_types_have_no_empty_values(tmp_path):
    path = write_parquet(
        tmp_path / "t.parquet", "SELECT * FROM (VALUES (false, 0), (NULL, NULL)) v(b, n)"
    )
    s = stats_by_column(path)
    assert (s["b"]["dtype"], s["n"]["dtype"]) == ("BOOLEAN", "INTEGER")
    assert s["b"]["populated_count"] == 1 and s["b"]["empty_count"] == 0
    assert s["n"]["populated_count"] == 1


def test_zero_row_table_has_no_ratio(tmp_path):
    path = write_parquet(tmp_path / "t.parquet", "SELECT 'x' AS a, 1 AS b WHERE false")
    s = stats_by_column(path)
    assert s["a"]["row_count"] == 0
    assert s["a"]["population_ratio"] is None
    assert [s["a"]["column_position"], s["b"]["column_position"]] == [0, 1]


def test_quoted_and_reserved_column_names(tmp_path):
    path = write_parquet(tmp_path / "t.parquet", 'SELECT \'g\' AS "group", \'q\' AS "we""ird"')
    assert set(stats_by_column(path)) == {"group", 'we"ird'}


def test_inspect_info_schema_walks_version_dirs(tmp_path):
    root = tmp_path / "snap" / "info_schema"
    write_parquet(root / "v1" / "dbt.models.parquet", "SELECT 'm' AS unique_id, [] AS tags")
    write_parquet(root / "v2" / "dbt_rt.invocations.parquet", "SELECT 1 AS n WHERE false")
    (root / "views.sql").write_text("-- not a table")
    (root / "not_a_version").mkdir()

    assert [p.name for p in info_schema_versions(root)] == ["v1", "v2"]
    df = inspect_info_schema(root, relative_to=tmp_path)
    assert df.select("info_schema_version", "table", "column").rows() == [
        ("v1", "dbt.models", "unique_id"),
        ("v1", "dbt.models", "tags"),
        ("v2", "dbt_rt.invocations", "n"),
    ]
    assert df["artifact_path"][0] == "snap/info_schema/v1/dbt.models.parquet"


def test_missing_info_schema_yields_empty_frame(tmp_path):
    df = inspect_info_schema(tmp_path / "absent", relative_to=tmp_path)
    assert df.is_empty()
    assert df.schema["population_ratio"] == pl.Float64
