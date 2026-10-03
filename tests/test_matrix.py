from datetime import UTC, datetime

import polars as pl
from conftest import write_parquet

from dbt_isrm import matrix
from dbt_isrm.inspect import classify_fields
from dbt_isrm.models import MATRIX_SCHEMA, RunRecord


def make_record(version, fixture, stage, artifact_root, exit_code=0):
    return RunRecord(
        run_id=f"{version}-{fixture}-{stage}",
        dbt_version=version,
        fixture=fixture,
        stage=stage,
        dbt_args=[stage],
        warehouse=False,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        duration_ms=1,
        exit_code=exit_code,
        timed_out=False,
        stdout_path="stdout.txt",
        stderr_path="stderr.txt",
        artifact_root=artifact_root,
        info_schema_versions=["v1"] if artifact_root else [],
        table_count=1 if artifact_root else 0,
    )


def snapshot(snapshots, version, fixture, stage, select_sql):
    root = snapshots / version / fixture / stage / "info_schema"
    write_parquet(root / "v1" / "dbt.models.parquet", select_sql)
    return root.relative_to(snapshots).as_posix()


def test_store_replaces_only_the_rerun_cell(tmp_path):
    results, snaps = tmp_path / "results", tmp_path / "snapshots"
    a = snapshot(snaps, "2.0.6", "f", "parse", "SELECT 'x' AS a")
    b = snapshot(snaps, "2.0.6", "f", "build", "SELECT 'y' AS a, 'z' AS b")
    matrix.store(make_record("2.0.6", "f", "parse", a), results, snaps)
    matrix.store(make_record("2.0.6", "f", "build", b), results, snaps)

    # Re-run `parse`, now with a NULL value: only its rows change.
    snapshot(snaps, "2.0.6", "f", "parse", "SELECT NULL::VARCHAR AS a")
    matrix.store(make_record("2.0.6", "f", "parse", a), results, snaps)

    m = pl.read_parquet(results / "matrix.parquet")
    assert m.schema == pl.Schema(MATRIX_SCHEMA)
    assert m.select("stage", "column", "populated_count").rows() == [
        ("build", "a", 1),
        ("build", "b", 1),
        ("parse", "a", 0),
    ]
    assert pl.read_parquet(results / "runs.parquet").height == 2
    assert (results / "matrix.json").exists()


def test_failed_rerun_clears_stale_matrix_rows(tmp_path):
    results, snaps = tmp_path / "results", tmp_path / "snapshots"
    a = snapshot(snaps, "2.0.6", "f", "parse", "SELECT 'x' AS a")
    matrix.store(make_record("2.0.6", "f", "parse", a), results, snaps)
    matrix.store(make_record("2.0.6", "f", "parse", None, exit_code=2), results, snaps)

    assert pl.read_parquet(results / "matrix.parquet").is_empty()
    runs = pl.read_parquet(results / "runs.parquet")
    assert runs.select("exit_code", "artifact_root").rows() == [(2, None)]


def test_store_is_deterministic_regardless_of_order(tmp_path):
    def build(order, root):
        results, snaps = root / "results", root / "snapshots"
        paths = {
            s: snapshot(snaps, "2.0.6", "f", s, "SELECT 'x' AS a, NULL::VARCHAR AS b")
            for s in order
        }
        for s in order:
            matrix.store(make_record("2.0.6", "f", s, paths[s]), results, snaps)
        return pl.read_parquet(results / "matrix.parquet")

    one = build(["parse", "build", "compile"], tmp_path / "one")
    two = build(["compile", "parse", "build"], tmp_path / "two")
    assert one.equals(two)


def test_classify_fields():
    rows = [
        # table, column, row_count, populated_count
        ("dbt.models", "full", 2, 2),
        ("dbt.models", "full", 1, 1),
        ("dbt.models", "partial", 2, 2),
        ("dbt.models", "partial", 2, 1),
        ("dbt.models", "never", 2, 0),
        ("dbt.models", "never", 0, 0),
        ("dbt.seeds", "unseen", 0, 0),
    ]
    m = pl.DataFrame(
        [
            {
                "info_schema_version": "v1",
                "table": t,
                "column": c,
                "row_count": r,
                "populated_count": p,
            }
            for t, c, r, p in rows
        ]
    )
    got = dict(classify_fields(m).select("column", "population").iter_rows())
    assert got == {
        "full": "fully populated",
        "partial": "partially populated",
        "never": "never populated",
        "unseen": "no rows observed",
    }
