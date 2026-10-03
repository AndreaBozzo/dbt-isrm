from pathlib import Path

import duckdb
import pytest

REPO = Path(__file__).resolve().parent.parent


def write_parquet(path: Path, select_sql: str) -> Path:
    """Write the result of a DuckDB SELECT to a Parquet file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute(f"COPY ({select_sql}) TO '{path.as_posix()}' (FORMAT parquet)")
    finally:
        con.close()
    return path


@pytest.fixture
def repo() -> Path:
    return REPO
