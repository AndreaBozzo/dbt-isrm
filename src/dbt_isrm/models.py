"""Record shapes shared by the runner, the matrix and the CLI.

Paths stored in records are POSIX paths relative to the snapshots directory,
so results do not depend on the machine that produced them.
"""

from datetime import datetime

import polars as pl
from pydantic import BaseModel


class DbtBuild(BaseModel):
    """What was actually executed for a requested dbt version."""

    # Raw `dbt --version` output; it carries no build identifier of its own.
    version_output: str
    # Wheel tag of the installed release, e.g. `cp311-abi3-win_amd64`.
    wheel_tag: str
    # sha256 of the native module (`dbt/_core.*`): the build that ran.
    binary_sha256: str


class RunRecord(BaseModel):
    """One dbt invocation. Failed invocations are recorded too."""

    run_id: str
    dbt_version: str
    fixture: str
    stage: str
    # Passed as `--invocation-id`; derived from (fixture, stage), so the same
    # cell gets the same id in every release.
    invocation_id: str
    dbt_args: list[str]
    dbt_version_output: str
    dbt_wheel_tag: str
    dbt_binary_sha256: str
    os: str
    arch: str
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
    "invocation_id": pl.String,
    "dbt_args": pl.List(pl.String),
    "dbt_version_output": pl.String,
    "dbt_wheel_tag": pl.String,
    "dbt_binary_sha256": pl.String,
    "os": pl.String,
    "arch": pl.String,
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

# One row per semantic expectation evaluated in one run.
SEMANTIC_SCHEMA = {
    "run_id": pl.String,
    "dbt_version": pl.String,
    "fixture": pl.String,
    "stage": pl.String,
    "expectation_id": pl.String,
    "expectation_class": pl.String,
    # pass | fail | missing (the table or column it reads does not exist)
    "result": pl.String,
    # The observed value, rendered deterministically so it can be diffed.
    "detail": pl.String,
}

# A (version, fixture, stage) cell is the unit of re-running.
CELL_KEYS = ["dbt_version", "fixture", "stage"]
