"""Integration tests: these run a real dbt release (downloaded on first use)."""

import duckdb
import polars as pl
import pytest

from dbt_isrm import matrix
from dbt_isrm.config import Stage, load_config
from dbt_isrm.runner import DbtUnavailable, ensure_dbt, invocation_id, run_dbt

VERSION = "2.0.6"

pytestmark = pytest.mark.integration


@pytest.fixture
def config(repo, tmp_path):
    cfg = load_config(repo / "configs" / "default.yml")
    return cfg.model_copy(
        update={"results_dir": tmp_path / "results", "snapshots_dir": tmp_path / "snapshots"}
    )


@pytest.fixture(scope="module")
def build():
    return ensure_dbt(VERSION)


def test_build_identity(build):
    assert build.version_output == f"dbt {VERSION}"
    assert build.wheel_tag.startswith("cp311-abi3-")
    assert len(build.binary_sha256) == 64


def test_parse_to_matrix(config, repo, build):
    record = run_dbt(config, build, VERSION, "basic_model", "parse")

    assert record.exit_code == 0 and not record.timed_out
    assert record.artifact_root == f"{VERSION}/basic_model/parse/info_schema"
    assert record.info_schema_versions == ["v1"]
    assert record.table_count > 0
    cell = config.snapshots_dir / VERSION / "basic_model" / "parse"
    assert (cell / "manifest.json").is_file()
    assert b"dbt" in (cell / "stdout.txt").read_bytes() + (cell / "stderr.txt").read_bytes()
    # The run happened in a copy: the fixture itself stays clean.
    assert not (repo / "fixtures" / "basic_model" / "target").exists()

    matrix.store(record, config.results_dir, config.snapshots_dir)
    m = pl.read_parquet(config.results_dir / "matrix.parquet")
    unique_id = m.filter((pl.col("table") == "dbt.models") & (pl.col("column") == "unique_id"))
    # basic_model has two models: orders and orders_summary.
    assert unique_id.select("row_count", "populated_count").rows() == [(2, 2)]


def test_invocation_id_reaches_dbt(config, build):
    record = run_dbt(config, build, VERSION, "basic_model", "build")
    assert record.exit_code == 0
    assert record.invocation_id == invocation_id("basic_model", "build")
    assert record.dbt_args[-2:] == ["--invocation-id", record.invocation_id]
    assert (record.dbt_version_output, record.dbt_binary_sha256) == (
        build.version_output,
        build.binary_sha256,
    )

    # dbt itself recorded the id we passed, so runtime tables are joinable to the run.
    path = config.snapshots_dir / record.artifact_root / "v1" / "dbt_rt.run_results.parquet"
    ids = duckdb.sql(f"SELECT DISTINCT invocation_id FROM read_parquet('{path.as_posix()}')")
    assert ids.fetchall() == [(record.invocation_id,)]

    matrix.store(record, config.results_dir, config.snapshots_dir)
    semantic = pl.read_parquet(config.results_dir / "semantic.parquet")
    assert semantic.filter(pl.col("result") == "missing").is_empty()
    assert semantic["expectation_id"].to_list() == ["current_invocation_recorded"]


def test_failed_run_is_recorded(config, build):
    config.stages["bogus"] = Stage(args=["not-a-dbt-command"])
    record = run_dbt(config, build, VERSION, "basic_model", "bogus")

    assert record.exit_code not in (0, None)
    assert record.artifact_root is None and record.table_count == 0
    assert (config.snapshots_dir / record.stderr_path).stat().st_size > 0


def test_ensure_dbt_rejects_missing_release():
    with pytest.raises(DbtUnavailable):
        ensure_dbt("0.0.0-does-not-exist")
