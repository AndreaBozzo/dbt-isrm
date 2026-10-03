"""Read the Information Schema Parquet files dbt writes and measure population.

Types and statistics come from DuckDB rather than Polars: DuckDB type names
(`VARCHAR[]`, `TIMESTAMP WITH TIME ZONE`) are what consumers of `views.sql` see,
and they do not drift when the Polars dependency is upgraded, which would
otherwise show up as a type change in every historical diff.
"""

from pathlib import Path

import duckdb
import polars as pl

from dbt_isrm.models import COLUMN_STATS_SCHEMA

# String values dbt writes for "nothing here" in serialized fields: an empty
# description is '', unset meta is '{}', unset version is the JSON 'null'.
EMPTY_STRINGS = ("", "[]", "{}", "null")


def info_schema_versions(info_schema_dir: Path) -> list[Path]:
    """The `vN` directories under an info_schema directory, in name order."""
    if not info_schema_dir.is_dir():
        return []
    return sorted(p for p in info_schema_dir.iterdir() if p.is_dir() and p.name.startswith("v"))


def table_name(parquet_path: Path) -> str:
    """`dbt.node_columns.parquet` -> `dbt.node_columns`."""
    return parquet_path.name.removesuffix(".parquet")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _empty_predicate(column: str, dtype: str) -> str | None:
    """SQL that is true for a non-null value carrying no information."""
    if dtype == "VARCHAR":
        values = ", ".join(f"'{v}'" for v in EMPTY_STRINGS)
        return f"{column} IN ({values})"
    if dtype.endswith("[]"):
        return f"len({column}) = 0"
    return None


def inspect_parquet(con: duckdb.DuckDBPyConnection, path: Path) -> list[dict]:
    """Per-column statistics for one Parquet file."""
    source = f"read_parquet('{path.as_posix().replace(chr(39), chr(39) * 2)}')"
    columns = con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()

    selects = ["count(*)"]
    for name, dtype, *_ in columns:
        col = _quote(name)
        empty = _empty_predicate(col, dtype)
        selects += [
            f"count({col})",
            f"count(*) FILTER (WHERE {empty})" if empty else "0",
            f"count(DISTINCT {col})",
        ]
    values = con.execute(f"SELECT {', '.join(selects)} FROM {source}").fetchone()
    assert values is not None  # an aggregate without GROUP BY always returns one row

    row_count = values[0]
    rows = []
    for position, (name, dtype, *_) in enumerate(columns):
        non_null, empty, distinct = values[1 + 3 * position : 4 + 3 * position]
        populated = non_null - empty
        rows.append(
            {
                "column": name,
                "column_position": position,
                "dtype": dtype,
                "row_count": row_count,
                "non_null_count": non_null,
                "null_count": row_count - non_null,
                "empty_count": empty,
                "populated_count": populated,
                "distinct_count": distinct,
                "population_ratio": populated / row_count if row_count else None,
            }
        )
    return rows


def inspect_info_schema(info_schema_dir: Path, relative_to: Path) -> pl.DataFrame:
    """Statistics for every column of every table under `info_schema_dir/vN/`.

    `artifact_path` is stored relative to `relative_to`.
    """
    rows = []
    con = duckdb.connect()
    try:
        for version_dir in info_schema_versions(info_schema_dir):
            for path in sorted(version_dir.glob("*.parquet")):
                artifact_path = path.relative_to(relative_to).as_posix()
                for row in inspect_parquet(con, path):
                    rows.append(
                        {
                            "info_schema_version": version_dir.name,
                            "table": table_name(path),
                            "artifact_path": artifact_path,
                            **row,
                        }
                    )
    finally:
        con.close()
    return pl.DataFrame(rows, schema=COLUMN_STATS_SCHEMA)
