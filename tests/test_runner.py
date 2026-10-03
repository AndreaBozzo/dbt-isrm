"""Integration tests: these run a real dbt release (downloaded on first use)."""

import polars as pl
import pytest

from dbt_isrm import matrix
from dbt_isrm.config import Stage, load_config
from dbt_isrm.runner import DbtUnavailable, ensure_dbt, run_dbt

VERSION = "2.0.6"

pytestmark = pytest.mark.integration


@pytest.fixture
def config(repo, tmp_path):
    cfg = load_config(repo / "configs" / "default.yml")
    return cfg.model_copy(
        update={"results_dir": tmp_path / "results", "snapshots_dir": tmp_path / "snapshots"}
    )


def test_parse_to_matrix(config, repo):
    ensure_dbt(VERSION)
    record = run_dbt(config, VERSION, "basic_model", "parse")

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


def test_failed_run_is_recorded(config):
    ensure_dbt(VERSION)
    config.stages["bogus"] = Stage(args=["not-a-dbt-command"])
    record = run_dbt(config, VERSION, "basic_model", "bogus")

    assert record.exit_code not in (0, None)
    assert record.artifact_root is None and record.table_count == 0
    assert (config.snapshots_dir / record.stderr_path).stat().st_size > 0


def test_ensure_dbt_rejects_missing_release():
    with pytest.raises(DbtUnavailable):
        ensure_dbt("0.0.0-does-not-exist")
