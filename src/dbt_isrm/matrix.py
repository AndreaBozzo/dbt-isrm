"""Persist the long-form matrix and the run log.

Both files accumulate across invocations: re-running a (version, fixture,
stage) cell replaces that cell's rows and leaves every other cell untouched,
so the matrix can be built and refreshed one cell at a time.
"""

from pathlib import Path

import polars as pl

from dbt_isrm.artifacts import inspect_info_schema
from dbt_isrm.models import CELL_KEYS, MATRIX_SCHEMA, RUNS_SCHEMA, RunRecord

MATRIX_SORT = [*CELL_KEYS, "info_schema_version", "table", "column_position"]


def matrix_rows(record: RunRecord, snapshots_dir: Path) -> pl.DataFrame:
    """Observations for one run; empty when it produced no Information Schema."""
    if record.artifact_root is None:
        return pl.DataFrame(schema=MATRIX_SCHEMA)
    stats = inspect_info_schema(snapshots_dir / record.artifact_root, relative_to=snapshots_dir)
    return stats.with_columns(
        run_id=pl.lit(record.run_id),
        dbt_version=pl.lit(record.dbt_version),
        fixture=pl.lit(record.fixture),
        stage=pl.lit(record.stage),
    ).select(list(MATRIX_SCHEMA))


def run_rows(record: RunRecord) -> pl.DataFrame:
    return pl.DataFrame([record.model_dump()], schema=RUNS_SCHEMA)


def replace_cells(
    path: Path, new: pl.DataFrame, cells: pl.DataFrame, sort: list[str]
) -> pl.DataFrame:
    """Drop `cells` from the file at `path`, add `new`, and write Parquet + JSON.

    `cells` lists the (version, fixture, stage) keys being replaced. It is
    separate from `new` so that a run yielding no rows (a failure without
    artifacts) still clears the rows a previous run left for that cell.
    """
    if path.exists():
        existing = pl.read_parquet(path).join(cells.select(CELL_KEYS), on=CELL_KEYS, how="anti")
        combined = pl.concat([existing, new], how="vertical")
    else:
        combined = new
    combined = combined.sort(sort)
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.write_parquet(path)
    combined.write_json(path.with_suffix(".json"))
    return combined


def store(record: RunRecord, results_dir: Path, snapshots_dir: Path) -> None:
    """Add one finished run to `runs.parquet` and `matrix.parquet`."""
    runs = run_rows(record)
    replace_cells(results_dir / "runs.parquet", runs, runs, [*CELL_KEYS])
    replace_cells(
        results_dir / "matrix.parquet", matrix_rows(record, snapshots_dir), runs, MATRIX_SORT
    )
