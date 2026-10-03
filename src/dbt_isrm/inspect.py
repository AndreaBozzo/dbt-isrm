"""Read-only views over matrix.parquet for one dbt version."""

import polars as pl

FIELD_COLUMNS = [
    "fixture",
    "stage",
    "dtype",
    "row_count",
    "null_count",
    "empty_count",
    "populated_count",
    "population_ratio",
]


def classify_fields(matrix: pl.DataFrame) -> pl.DataFrame:
    """One row per (info_schema_version, table, column) with a population class.

    Only runs where the table had rows count: a column of an always-empty table
    is `no rows observed`, not `never populated`.
    """
    observed = matrix.filter(pl.col("row_count") > 0)
    classes = (
        observed.group_by("info_schema_version", "table", "column")
        .agg(
            populated=pl.col("populated_count").sum(),
            full=(pl.col("populated_count") == pl.col("row_count")).all(),
        )
        .select(
            "info_schema_version",
            "table",
            "column",
            population=pl.when(pl.col("full"))
            .then(pl.lit("fully populated"))
            .when(pl.col("populated") == 0)
            .then(pl.lit("never populated"))
            .otherwise(pl.lit("partially populated")),
        )
    )
    every = matrix.select("info_schema_version", "table", "column").unique()
    return (
        every.join(classes, on=["info_schema_version", "table", "column"], how="left")
        .with_columns(pl.col("population").fill_null("no rows observed"))
        .sort("info_schema_version", "table", "column")
    )


def summary(matrix: pl.DataFrame, runs: pl.DataFrame, version: str) -> str:
    matrix = matrix.filter(pl.col("dbt_version") == version)
    runs = runs.filter(pl.col("dbt_version") == version)
    if runs.is_empty():
        raise LookupError(f"no runs recorded for dbt {version}")

    failed = runs.filter((pl.col("exit_code") != 0) | pl.col("exit_code").is_null())
    fields = classify_fields(matrix)
    counts = dict(fields.group_by("population").len().iter_rows())

    lines = [
        f"dbt {version}",
        "",
        f"runs:    {runs.height} ({failed.height} non-zero exit)",
        f"tables:  {fields.select('info_schema_version', 'table').n_unique()}",
        f"columns: {fields.height}",
        "",
    ]
    for label in ("fully populated", "partially populated", "never populated", "no rows observed"):
        lines.append(f"{label + ':':<22}{counts.get(label, 0)}")
    lines.append("")
    lines.append("'populated' excludes NULL and empty values: '', '[]', '{}', 'null', [].")
    if not failed.is_empty():
        lines += ["", "non-zero exits:"]
        for row in failed.sort("fixture", "stage").iter_rows(named=True):
            lines.append(f"  {row['fixture']} / {row['stage']}: exit {row['exit_code']}")
    return "\n".join(lines)


def resolve_field(matrix: pl.DataFrame, field: str) -> tuple[str, str]:
    """Accept `dbt.node_columns.constraints` or `node_columns.constraints`."""
    table, sep, column = field.rpartition(".")
    if not sep:
        raise ValueError(f"expected <table>.<column>, got {field!r}")
    tables = matrix.select("table").unique().to_series().to_list()
    matches = sorted(t for t in tables if t == table or t.endswith("." + table))
    if len(matches) != 1:
        found = ", ".join(matches) if matches else "none"
        raise LookupError(f"table {table!r} matches {found}")
    return matches[0], column


def field_view(matrix: pl.DataFrame, version: str, field: str) -> str:
    matrix = matrix.filter(pl.col("dbt_version") == version)
    table, column = resolve_field(matrix, field)
    rows = (
        matrix.filter((pl.col("table") == table) & (pl.col("column") == column))
        .sort("fixture", "stage")
        .select(FIELD_COLUMNS)
    )
    if rows.is_empty():
        raise LookupError(f"no column {column!r} in {table} for dbt {version}")
    return f"dbt {version}  {table}.{column}\n\n" + format_table(rows)


def format_table(df: pl.DataFrame) -> str:
    def cell(value: object) -> str:
        if value is None:
            return "-"
        if isinstance(value, float):
            return f"{value:.0%}"
        return str(value)

    labels = {"row_count": "rows", "population_ratio": "ratio"}
    header = [labels.get(c, c.removesuffix("_count")) for c in df.columns]
    body = [[cell(v) for v in row] for row in df.iter_rows()]
    widths = [max(len(r[i]) for r in [header, *body]) for i in range(len(header))]
    return "\n".join(
        "  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip()
        for r in [header, *body]
    )
