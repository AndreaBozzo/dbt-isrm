"""Record shapes shared by the runner, the matrix and the CLI.

Paths stored in records are POSIX paths relative to the snapshots directory,
so results do not depend on the machine that produced them.
"""

from datetime import datetime

import polars as pl
from pydantic import BaseModel


class RunRecord(BaseModel):
    """One dbt invocation. Failed invocations are recorded too."""

    run_id: str
    dbt_version: str
    fixture: str
    stage: str
    dbt_args: list[str]
    warehouse: bool
    started_at: datetime
    duration_ms: int
    # None when the process was killed on timeout.
    exit_code: int | None
    timed_out: bool
    stdout_path: str
    stderr_path: str
    # None when the run produced no Information Schema.
    artifact_root: str | None
    info_schema_versions: list[str]
    table_count: int


RUNS_SCHEMA = {
    "run_id": pl.String,
    "dbt_version": pl.String,
    "fixture": pl.String,
    "stage": pl.String,
    "dbt_args": pl.List(pl.String),
    "warehouse": pl.Boolean,
    "started_at": pl.Datetime("us", "UTC"),
    "duration_ms": pl.Int64,
    "exit_code": pl.Int64,
    "timed_out": pl.Boolean,
    "stdout_path": pl.String,
    "stderr_path": pl.String,
    "artifact_root": pl.String,
    "info_schema_versions": pl.List(pl.String),
    "table_count": pl.Int64,
}

# One row per observable column of one Information Schema table in one run.
COLUMN_STATS_SCHEMA = {
    "info_schema_version": pl.String,
    "table": pl.String,
    "column": pl.String,
    "column_position": pl.Int64,
    "dtype": pl.String,
    "row_count": pl.Int64,
    "non_null_count": pl.Int64,
    "null_count": pl.Int64,
    "empty_count": pl.Int64,
    "populated_count": pl.Int64,
    "distinct_count": pl.Int64,
    "population_ratio": pl.Float64,
    "artifact_path": pl.String,
}

MATRIX_SCHEMA = {
    "run_id": pl.String,
    "dbt_version": pl.String,
    "fixture": pl.String,
    "stage": pl.String,
    **COLUMN_STATS_SCHEMA,
}

# A (version, fixture, stage) cell is the unit of re-running.
CELL_KEYS = ["dbt_version", "fixture", "stage"]
