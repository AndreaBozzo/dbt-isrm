import polars as pl
import pytest

from dbt_isrm.diff import diff as diff_versions
from dbt_isrm.models import MATRIX_SCHEMA, RUNS_SCHEMA, SEMANTIC_SCHEMA
from dbt_isrm.report import render_markdown, render_text

NO_SEMANTIC = pl.DataFrame(schema=SEMANTIC_SCHEMA)


def diff(m, r, a, b, semantic=NO_SEMANTIC):
    return diff_versions(m, r, semantic, a, b)


def col(
    version,
    table,
    column,
    rows=1,
    null=0,
    empty=0,
    dtype="VARCHAR",
    fixture="f",
    stage="parse",
    isv="v1",
):
    populated = rows - null - empty
    return {
        "run_id": f"{version}-{fixture}-{stage}",
        "dbt_version": version,
        "fixture": fixture,
        "stage": stage,
        "info_schema_version": isv,
        "table": table,
        "column": column,
        "column_position": 0,
        "dtype": dtype,
        "row_count": rows,
        "non_null_count": rows - null,
        "null_count": null,
        "empty_count": empty,
        "populated_count": populated,
        "distinct_count": populated,
        "population_ratio": populated / rows if rows else None,
        # Differs between versions on purpose: paths must never show up as a change.
        "artifact_path": f"{version}/{fixture}/{stage}/info_schema/{isv}/{table}.parquet",
    }


def run(version, fixture="f", stage="parse", exit_code=0, artifacts=True, tables=1):
    return {
        "run_id": f"{version}-{fixture}-{stage}",
        "dbt_version": version,
        "fixture": fixture,
        "stage": stage,
        "dbt_args": [stage],
        "warehouse": False,
        "started_at": None,
        "duration_ms": 1000 if version == "A" else 7,
        "exit_code": exit_code,
        "timed_out": False,
        "stdout_path": "x",
        "stderr_path": "x",
        "artifact_root": f"{version}/{fixture}/{stage}/info_schema" if artifacts else None,
        "info_schema_versions": ["v1"] if artifacts else [],
        "table_count": tables,
    }


def frames(cols, runs):
    return pl.DataFrame(cols, schema=MATRIX_SCHEMA), pl.DataFrame(runs, schema=RUNS_SCHEMA)


def test_identical_versions_have_no_changes():
    m, r = frames(
        [col("A", "dbt.models", "name"), col("B", "dbt.models", "name")],
        [run("A"), run("B")],
    )
    d = diff(m, r, "A", "B")
    assert d.is_empty()
    assert "no observable changes" in render_text(d)


def test_schema_changes():
    m, r = frames(
        [
            col("A", "dbt.models", "name"),
            col("A", "dbt.models", "old"),
            col("A", "dbt.models", "kind", dtype="VARCHAR"),
            col("A", "dbt.gone", "x"),
            col("B", "dbt.models", "name"),
            col("B", "dbt.models", "new", dtype="BIGINT"),
            col("B", "dbt.models", "kind", dtype="VARCHAR[]"),
            col("B", "dbt.fresh", "a"),
            col("B", "dbt.fresh", "b"),
        ],
        [run("A"), run("B")],
    )
    d = diff(m, r, "A", "B")
    assert d.tables_added["table"].to_list() == ["dbt.fresh"]
    assert d.tables_removed["table"].to_list() == ["dbt.gone"]
    # Columns of added/removed tables are implied by the table change.
    assert d.columns_added.select("table", "column", "dtype").rows() == [
        ("dbt.models", "new", "BIGINT")
    ]
    assert d.columns_removed["column"].to_list() == ["old"]
    assert d.type_changes.select("column", "dtype_a", "dtype_b").rows() == [
        ("kind", "VARCHAR", "VARCHAR[]")
    ]
    text = render_text(d)
    assert "+ table dbt.fresh" in text and "- table dbt.gone" in text
    assert "+ dbt.models.new  BIGINT" in text and "- dbt.models.old" in text
    assert "dbt.models.kind\n  VARCHAR -> VARCHAR[]" in text


@pytest.mark.parametrize(
    ("a", "b", "kind"),
    [
        ({"rows": 4, "null": 4}, {"rows": 4}, "population gain"),
        ({"rows": 4}, {"rows": 4, "null": 1}, "population loss"),
        ({"rows": 2}, {"rows": 3}, "row count change"),
        ({"rows": 2, "null": 2}, {"rows": 2, "empty": 2}, "null/empty change"),
    ],
)
def test_population_change_kinds(a, b, kind):
    m, r = frames(
        [
            col("A", "dbt.node_columns", "constraints", **a),
            col("B", "dbt.node_columns", "constraints", **b),
        ],
        [run("A"), run("B")],
    )
    changes = diff(m, r, "A", "B").population_changes
    assert changes["kind"].to_list() == [kind]


def test_population_gain_text():
    m, r = frames(
        [
            col("A", "dbt.node_columns", "constraints", rows=3, null=3, stage="compile"),
            col("B", "dbt.node_columns", "constraints", rows=3, stage="compile"),
        ],
        [run("A", stage="compile"), run("B", stage="compile")],
    )
    assert (
        "dbt.node_columns.constraints\n"
        "  f / compile: 0/3 populated -> 3/3 populated  (population gain)"
    ) in render_text(diff(m, r, "A", "B"))


def test_failed_cell_is_an_execution_change_not_a_schema_change():
    m, r = frames(
        [
            col("A", "dbt.models", "name", stage="parse"),
            col("A", "dbt.models", "name", stage="build"),
            # Only `build` writes this table; B's build failed before writing anything.
            col("A", "dbt_rt.run_results", "status", stage="build"),
            col("B", "dbt.models", "name", stage="parse"),
        ],
        [
            run("A", stage="parse"),
            run("A", stage="build"),
            run("B", stage="parse"),
            run("B", stage="build", exit_code=1, artifacts=False, tables=0),
        ],
    )
    d = diff(m, r, "A", "B")
    assert d.compared_cells.rows() == [("f", "parse")]
    assert d.tables_removed.is_empty() and d.population_changes.is_empty()
    assert d.execution_changes.select("stage", "exit_code_a", "exit_code_b").rows() == [
        ("build", 0, 1)
    ]
    assert "f / build\n  exit 0, 1 tables -> exit 1, no artifacts" in render_text(d)


def test_never_populated_transitions():
    m, r = frames(
        [
            col("A", "dbt.models", "lost"),
            col("A", "dbt.models", "found", null=1),
            col("A", "dbt.models", "steady", null=1),
            col("B", "dbt.models", "lost", empty=1),
            col("B", "dbt.models", "found"),
            col("B", "dbt.models", "steady", null=1),
        ],
        [run("A"), run("B")],
    )
    d = diff(m, r, "A", "B")
    assert d.newly_never_populated["column"].to_list() == ["lost"]
    assert d.no_longer_never_populated["column"].to_list() == ["found"]


def test_cells_run_for_only_one_version_are_coverage_not_changes():
    m, r = frames(
        [
            col("A", "dbt.models", "name"),
            col("A", "dbt.models", "name", stage="build"),
            col("B", "dbt.models", "name"),
        ],
        [run("A"), run("A", stage="build"), run("B")],
    )
    d = diff(m, r, "A", "B")
    assert d.is_empty()
    assert d.cells_only_a.rows() == [("f", "build")]
    assert "only run for A: f/build" in render_text(d)


def test_diff_is_independent_of_input_order():
    cols = [
        col(v, "dbt.models", c, null=n, stage=s)
        for v in ("A", "B")
        for c in ("x", "y")
        for s in ("parse", "build")
        for n in ([0] if v == "A" else [1])
    ]
    runs = [run(v, stage=s) for v in ("A", "B") for s in ("parse", "build")]
    m, r = frames(cols, runs)
    forward = render_text(diff(m, r, "A", "B"))
    backward = render_text(diff(m.reverse(), r.reverse(), "A", "B"))
    assert forward == backward


def test_unknown_version_raises():
    m, r = frames([col("A", "dbt.models", "name")], [run("A")])
    with pytest.raises(LookupError, match="dbt B"):
        diff(m, r, "A", "B")


def test_markdown_report_has_every_section():
    m, r = frames(
        [
            col("A", "dbt.models", "name", null=1),
            col("B", "dbt.models", "name"),
            col("C", "dbt.models", "name"),
        ],
        [run("A"), run("B"), run("C")],
    )
    md = render_markdown(
        [diff(m, r, "A", "B"), diff(m, r, "B", "C")], ["f"], ["parse"], NO_SEMANTIC
    )
    for heading in [
        "# dbt ISRM Report",
        "## Versions compared",
        "## Fixtures",
        "## Stages",
        "## Schema changes",
        "## Population changes",
        "## Semantic assertion changes",
        "## Execution changes",
        "## Newly never-populated fields",
        "## Fields no longer never-populated",
    ]:
        assert heading in md
    assert "- A → B: 1/1 cells compared, changes" in md
    assert "- B → C: 1/1 cells compared, no observable changes" in md
    assert (
        "| `dbt.models.name` | f / parse | 0/1 populated | 1/1 populated | population gain |" in md
    )


def sem(version, result, detail, cls="must", expectation="constraints_exposed"):
    return {
        "run_id": f"{version}-f-parse",
        "dbt_version": version,
        "fixture": "f",
        "stage": "parse",
        "expectation_id": expectation,
        "expectation_class": cls,
        "result": result,
        "detail": detail,
    }


@pytest.mark.parametrize(
    ("cls", "a", "b", "kind"),
    [
        ("must", ("pass", "x"), ("fail", "y"), "regression"),
        ("must", ("fail", "y"), ("pass", "x"), "fixed"),
        ("must", ("fail", "y"), ("fail", "z"), "change"),
        ("open", ("pass", "x"), ("fail", "y"), "change"),
        ("open", ("fail", "y"), ("missing", "IOException"), "change"),
    ],
)
def test_semantic_change_kinds(cls, a, b, kind):
    m, r = frames(
        [col("A", "dbt.models", "name"), col("B", "dbt.models", "name")], [run("A"), run("B")]
    )
    s = pl.DataFrame([sem("A", *a, cls=cls), sem("B", *b, cls=cls)], schema=SEMANTIC_SCHEMA)
    d = diff(m, r, "A", "B", semantic=s)
    assert d.semantic_changes["kind"].to_list() == [kind]
    assert not d.is_empty()


def test_unchanged_semantic_result_is_not_a_change():
    m, r = frames(
        [col("A", "dbt.models", "name"), col("B", "dbt.models", "name")], [run("A"), run("B")]
    )
    s = pl.DataFrame([sem("A", "fail", "x"), sem("B", "fail", "x")], schema=SEMANTIC_SCHEMA)
    assert diff(m, r, "A", "B", semantic=s).is_empty()


def test_semantic_change_text_and_markdown():
    m, r = frames(
        [col("A", "dbt.models", "name"), col("B", "dbt.models", "name")], [run("A"), run("B")]
    )
    s = pl.DataFrame(
        [sem("A", "fail", "order_id=NULL"), sem("B", "pass", "order_id=['not_null']")],
        schema=SEMANTIC_SCHEMA,
    )
    d = diff(m, r, "A", "B", semantic=s)
    assert (
        "constraints_exposed (must)\n  f / parse  (fixed)\n"
        "    fail: order_id=NULL\n    -> pass: order_id=['not_null']"
    ) in render_text(d)
    md = render_markdown([d], ["f"], ["parse"], s, frozenset({"B"}))
    assert "- A → B (not in config): 1/1 cells compared, changes" in md
    assert "| `constraints_exposed` (must) | f / parse | fail: order_id=NULL |" in md
    assert "### Status in B" in md and "| `constraints_exposed` | must | 1/1 |" in md
