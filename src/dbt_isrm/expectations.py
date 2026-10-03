"""Semantic expectations: a few explicit checks of values, not counts.

Population statistics cannot see a value that is present but wrong (an Arrow
type name in `data_type_inferred` counts as populated), so these checks read
the values themselves. Each is plain Python over one run's snapshot.

Classes:
- must: contracted or confirmed upstream; a failure is a violation.
- observed: baseline behavior; a change is reported, not judged.
- open: semantics unresolved; the result is recorded without a verdict.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import duckdb
import polars as pl

from dbt_isrm.models import SEMANTIC_SCHEMA, RunRecord

# The Information Schema layout version the checks read.
LAYOUT = "v1"

Check = Callable[[duckdb.DuckDBPyConnection, Path, RunRecord], tuple[bool, str]]


@dataclass(frozen=True)
class Expectation:
    id: str
    cls: Literal["must", "observed", "open"]
    evidence: str
    check: Check
    # None means every fixture / every stage.
    fixtures: tuple[str, ...] | None = None
    stages: tuple[str, ...] | None = None

    def applies(self, fixture: str, stage: str) -> bool:
        return (self.fixtures is None or fixture in self.fixtures) and (
            self.stages is None or stage in self.stages
        )


def _table(root: Path, name: str) -> str:
    path = (root / LAYOUT / f"{name}.parquet").as_posix().replace("'", "''")
    return f"read_parquet('{path}')"


def _constraint_types(raw: str | None) -> list[str] | None:
    """`[{"type": "not_null", ...}, ...]` (or a list of names) -> sorted type names."""
    if raw is None:
        return None
    try:
        items = json.loads(raw)
        return sorted(i["type"] if isinstance(i, dict) else str(i) for i in items)
    except (ValueError, TypeError, KeyError):
        # An unexpected encoding fails the check and shows the raw value.
        return [f"unparsed:{raw}"]


ORDERS = "model.columns_and_constraints.orders"
DECLARED_CONSTRAINTS = {"order_id": ["not_null", "primary_key"], "customer_id": ["not_null"]}


def check_constraints(con, root, record):
    rows = dict(
        con.execute(
            f"SELECT column_name, constraints FROM {_table(root, 'dbt.node_columns')} "
            "WHERE node_unique_id = ?",
            [ORDERS],
        ).fetchall()
    )
    found = {c: _constraint_types(rows.get(c)) for c in DECLARED_CONSTRAINTS}
    ok = all(set(want) <= set(found[c] or []) for c, want in DECLARED_CONSTRAINTS.items())
    detail = " ".join(f"{c}={'NULL' if v is None else v}" for c, v in found.items())
    return ok, detail


# DuckDB's own names for the declared types (`typeof()`).
ADAPTER_TYPES = {
    "order_id": "INTEGER",
    "customer_id": "INTEGER",
    "status": "VARCHAR",
    "amount": "DECIMAL(10,2)",
}


def check_inferred_types(con, root, record):
    rows = dict(
        con.execute(
            f"SELECT column_name, data_type_inferred FROM {_table(root, 'dbt.node_columns')} "
            "WHERE node_unique_id = ?",
            [ORDERS],
        ).fetchall()
    )

    def norm(v: str | None) -> str | None:
        return None if v is None else v.upper().replace(" ", "")

    ok = all(norm(rows.get(c)) == want for c, want in ADAPTER_TYPES.items())
    return ok, " ".join(f"{c}={rows.get(c) or 'NULL'}" for c in ADAPTER_TYPES)


def check_current_invocation(con, root, record):
    row = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE invocation_id = ?) "
        f"FROM {_table(root, 'dbt_rt.invocations')}",
        [record.invocation_id],
    ).fetchone()
    assert row is not None  # an aggregate without GROUP BY always returns one row
    total, current = row
    return current > 0, f"rows={total} current={'present' if current else 'absent'}"


EXPECTATIONS = (
    Expectation(
        id="constraints_exposed",
        cls="must",
        evidence="dbt-labs/dbt#16553 (confirmed bug; declared constraints missing)",
        check=check_constraints,
        fixtures=("columns_and_constraints",),
    ),
    Expectation(
        id="inferred_types_are_adapter_types",
        cls="open",
        evidence="dbt-labs/dbt#16515 (untriaged; Arrow type names)",
        check=check_inferred_types,
        fixtures=("columns_and_constraints",),
        stages=("compile_strict",),
    ),
    Expectation(
        id="current_invocation_recorded",
        cls="open",
        evidence="dbt-labs/dbt#16588 (untriaged; record written after the info schema)",
        check=check_current_invocation,
        # dbt says runtime results are written by compile, run and build only.
        stages=("compile", "compile_strict", "build"),
    ),
)


def evaluate(record: RunRecord, snapshots_dir: Path) -> pl.DataFrame:
    """Every applicable expectation for one run."""
    rows = []
    con = duckdb.connect()
    try:
        for e in EXPECTATIONS:
            if not e.applies(record.fixture, record.stage):
                continue
            if record.artifact_root is None:
                result, detail = "missing", "no information schema"
            else:
                try:
                    ok, detail = e.check(con, snapshots_dir / record.artifact_root, record)
                    result = "pass" if ok else "fail"
                except (duckdb.CatalogException, duckdb.BinderException, duckdb.IOException) as exc:
                    # The table or column the check reads is gone: a schema change.
                    result, detail = "missing", type(exc).__name__
            rows.append(
                {
                    "run_id": record.run_id,
                    "dbt_version": record.dbt_version,
                    "fixture": record.fixture,
                    "stage": record.stage,
                    "expectation_id": e.id,
                    "expectation_class": e.cls,
                    "result": result,
                    "detail": detail,
                }
            )
    finally:
        con.close()
    return pl.DataFrame(rows, schema=SEMANTIC_SCHEMA)
