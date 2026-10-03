"""Compare the observable Information Schema surface of two dbt releases.

Only aggregate observations are compared: schema, row counts, population
counts and execution status. Run ids, artifact paths, timestamps and durations
never enter a comparison, which is what makes the result deterministic.

Nothing here is called a regression. Without an explicit expectation, a
change is only a change.
"""

from dataclasses import dataclass

import polars as pl

from dbt_isrm.inspect import classify_fields

CELL = ["fixture", "stage"]
FIELD = ["info_schema_version", "table", "column"]
POPULATED = ("fully populated", "partially populated")


@dataclass(frozen=True)
class Diff:
    a: str
    b: str
    # (fixture, stage) cells run for both versions, and those with artifacts in both.
    cells: pl.DataFrame
    compared_cells: pl.DataFrame
    # Cells run for only one of the versions: coverage, not behavior.
    cells_only_a: pl.DataFrame
    cells_only_b: pl.DataFrame
    tables_added: pl.DataFrame
    tables_removed: pl.DataFrame
    columns_added: pl.DataFrame
    columns_removed: pl.DataFrame
    type_changes: pl.DataFrame
    population_changes: pl.DataFrame
    newly_never_populated: pl.DataFrame
    no_longer_never_populated: pl.DataFrame
    execution_changes: pl.DataFrame
    semantic_changes: pl.DataFrame

    def is_empty(self) -> bool:
        return all(
            getattr(self, name).is_empty()
            for name in (
                "tables_added",
                "tables_removed",
                "columns_added",
                "columns_removed",
                "type_changes",
                "population_changes",
                "newly_never_populated",
                "no_longer_never_populated",
                "execution_changes",
                "semantic_changes",
            )
        )


def _schema(matrix: pl.DataFrame) -> pl.DataFrame:
    """(field, dtype) observed in a version; dtypes joined if cells disagree."""
    return (
        matrix.group_by(FIELD)
        .agg(dtype=pl.col("dtype").unique().sort().str.join(" | "))
        .sort(FIELD)
    )


def _population_changes(a: pl.DataFrame, b: pl.DataFrame) -> pl.DataFrame:
    counts = ["row_count", "null_count", "empty_count", "populated_count"]
    joined = a.select(*CELL, *FIELD, *counts).join(
        b.select(*CELL, *FIELD, *counts), on=[*CELL, *FIELD], suffix="_b"
    )
    kind = (
        pl.when(pl.col("row_count") != pl.col("row_count_b"))
        .then(pl.lit("row count change"))
        .when(pl.col("populated_count_b") > pl.col("populated_count"))
        .then(pl.lit("population gain"))
        .when(pl.col("populated_count_b") < pl.col("populated_count"))
        .then(pl.lit("population loss"))
        .when(pl.col("null_count") != pl.col("null_count_b"))
        .then(pl.lit("null/empty change"))
    )
    return (
        joined.with_columns(kind=kind)
        .filter(pl.col("kind").is_not_null())
        .rename({c: f"{c}_a" for c in counts})
        .sort([*FIELD, *CELL])
    )


def _never_populated_changes(a: pl.DataFrame, b: pl.DataFrame) -> tuple[pl.DataFrame, ...]:
    ca = classify_fields(a).rename({"population": "population_a"})
    cb = classify_fields(b).rename({"population": "population_b"})
    both = ca.join(cb, on=FIELD)
    newly = both.filter(
        pl.col("population_a").is_in(POPULATED) & (pl.col("population_b") == "never populated")
    )
    no_longer = both.filter(
        (pl.col("population_a") == "never populated") & pl.col("population_b").is_in(POPULATED)
    )
    return newly.sort(FIELD), no_longer.sort(FIELD)


def _execution_changes(ra: pl.DataFrame, rb: pl.DataFrame) -> pl.DataFrame:
    cols = ["exit_code", "timed_out", "table_count"]
    joined = ra.select(*CELL, *cols, has_artifacts=pl.col("artifact_root").is_not_null()).join(
        rb.select(*CELL, *cols, has_artifacts=pl.col("artifact_root").is_not_null()),
        on=CELL,
        suffix="_b",
    )
    changed = pl.any_horizontal(
        pl.col(c).ne_missing(pl.col(f"{c}_b")) for c in [*cols, "has_artifacts"]
    )
    return joined.filter(changed).rename({c: f"{c}_a" for c in [*cols, "has_artifacts"]}).sort(CELL)


def _semantic_changes(sa: pl.DataFrame, sb: pl.DataFrame) -> pl.DataFrame:
    """Expectations whose result or observed value changed.

    Only a `must` expectation going from pass to fail is a regression.
    """
    key = [*CELL, "expectation_id"]
    joined = sa.select(*key, "expectation_class", "result", "detail").join(
        sb.select(*key, "result", "detail"), on=key, suffix="_b"
    )
    must = pl.col("expectation_class") == "must"
    kind = (
        pl.when(must & (pl.col("result") == "pass") & (pl.col("result_b") != "pass"))
        .then(pl.lit("regression"))
        .when(must & (pl.col("result") != "pass") & (pl.col("result_b") == "pass"))
        .then(pl.lit("fixed"))
        .otherwise(pl.lit("change"))
    )
    return (
        joined.filter(
            (pl.col("result") != pl.col("result_b")) | (pl.col("detail") != pl.col("detail_b"))
        )
        .with_columns(kind=kind)
        .rename({"result": "result_a", "detail": "detail_a"})
        .sort(["expectation_id", *CELL])
    )


def diff(matrix: pl.DataFrame, runs: pl.DataFrame, semantic: pl.DataFrame, a: str, b: str) -> Diff:
    ra = runs.filter(pl.col("dbt_version") == a)
    rb = runs.filter(pl.col("dbt_version") == b)
    for version, r in ((a, ra), (b, rb)):
        if r.is_empty():
            raise LookupError(f"no runs recorded for dbt {version}")

    cells = ra.select(CELL).join(rb.select(CELL), on=CELL).sort(CELL)
    compared = (
        ra.filter(pl.col("artifact_root").is_not_null())
        .select(CELL)
        .join(rb.filter(pl.col("artifact_root").is_not_null()).select(CELL), on=CELL)
        .sort(CELL)
    )

    # Behavior is compared only where both versions produced artifacts: a cell
    # that failed in one version is an execution change, not a schema change.
    ma = matrix.filter(pl.col("dbt_version") == a).join(compared, on=CELL)
    mb = matrix.filter(pl.col("dbt_version") == b).join(compared, on=CELL)

    sa, sb = _schema(ma), _schema(mb)
    tables_a = sa.select("info_schema_version", "table").unique()
    tables_b = sb.select("info_schema_version", "table").unique()
    tables_added = tables_b.join(tables_a, on=["info_schema_version", "table"], how="anti")
    tables_removed = tables_a.join(tables_b, on=["info_schema_version", "table"], how="anti")

    # Columns of an added or removed table are implied by the table change.
    in_both_tables = tables_a.join(tables_b, on=["info_schema_version", "table"])
    sa_kept = sa.join(in_both_tables, on=["info_schema_version", "table"])
    sb_kept = sb.join(in_both_tables, on=["info_schema_version", "table"])
    types = sa.join(sb, on=FIELD, suffix="_b")

    newly, no_longer = _never_populated_changes(ma, mb)
    return Diff(
        a=a,
        b=b,
        cells=cells,
        compared_cells=compared,
        cells_only_a=ra.select(CELL).join(cells, on=CELL, how="anti").sort(CELL),
        cells_only_b=rb.select(CELL).join(cells, on=CELL, how="anti").sort(CELL),
        tables_added=tables_added.sort("info_schema_version", "table"),
        tables_removed=tables_removed.sort("info_schema_version", "table"),
        columns_added=sb_kept.join(sa_kept, on=FIELD, how="anti").sort(FIELD),
        columns_removed=sa_kept.join(sb_kept, on=FIELD, how="anti").sort(FIELD),
        type_changes=types.filter(pl.col("dtype") != pl.col("dtype_b"))
        .rename({"dtype": "dtype_a"})
        .sort(FIELD),
        population_changes=_population_changes(ma, mb),
        newly_never_populated=newly,
        no_longer_never_populated=no_longer,
        execution_changes=_execution_changes(ra.join(cells, on=CELL), rb.join(cells, on=CELL)),
        semantic_changes=_semantic_changes(
            semantic.filter(pl.col("dbt_version") == a).join(cells, on=CELL),
            semantic.filter(pl.col("dbt_version") == b).join(cells, on=CELL),
        ),
    )
