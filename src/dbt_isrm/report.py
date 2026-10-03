"""Render diffs as terminal text (`dbt-isrm diff`) and as `report.md`."""

import polars as pl

from dbt_isrm.diff import Diff
from dbt_isrm.expectations import EXPECTATIONS


def field_name(row: dict, show_isv: bool) -> str:
    name = f"{row['table']}.{row['column']}" if "column" in row else row["table"]
    return f"{row['info_schema_version']}:{name}" if show_isv else name


def _show_isv(d: Diff) -> bool:
    """Prefix fields with `v1:` only when more than one layout version is involved."""
    frames = [
        d.tables_added,
        d.tables_removed,
        d.columns_added,
        d.columns_removed,
        d.type_changes,
        d.population_changes,
    ]
    versions = set()
    for f in frames:
        versions.update(f["info_schema_version"].to_list())
    return len(versions) > 1


def population_cell(row: dict, side: str) -> str:
    if row["kind"] == "null/empty change":
        return f"null {row[f'null_count_{side}']}, empty {row[f'empty_count_{side}']}"
    return f"{row[f'populated_count_{side}']}/{row[f'row_count_{side}']} populated"


def exec_cell(row: dict, side: str) -> str:
    if row[f"timed_out_{side}"]:
        status = "timeout"
    else:
        status = f"exit {row[f'exit_code_{side}']}"
    artifacts = (
        f"{row[f'table_count_{side}']} tables" if row[f"has_artifacts_{side}"] else "no artifacts"
    )
    return f"{status}, {artifacts}"


def semantic_cell(row: dict, side: str) -> str:
    return f"{row[f'result_{side}']}: {row[f'detail_{side}']}"


def render_text(d: Diff) -> str:
    isv = _show_isv(d)
    out = [
        f"dbt {d.a} -> {d.b}",
        f"compared {d.compared_cells.height} of {d.cells.height} common (fixture, stage) cells",
    ]
    for version, frame in ((d.a, d.cells_only_a), (d.b, d.cells_only_b)):
        if not frame.is_empty():
            cells = ", ".join(f"{f}/{s}" for f, s in frame.iter_rows())
            out.append(f"only run for {version}: {cells}")

    schema = (
        [f"+ table {field_name(r, isv)}" for r in d.tables_added.iter_rows(named=True)]
        + [f"- table {field_name(r, isv)}" for r in d.tables_removed.iter_rows(named=True)]
        + [f"+ {field_name(r, isv)}  {r['dtype']}" for r in d.columns_added.iter_rows(named=True)]
        + [f"- {field_name(r, isv)}" for r in d.columns_removed.iter_rows(named=True)]
    )
    sections: list[tuple[str, list[str]]] = [("SCHEMA", schema)]

    types = []
    for r in d.type_changes.iter_rows(named=True):
        types += [field_name(r, isv), f"  {r['dtype_a']} -> {r['dtype_b']}"]
    sections.append(("TYPE CHANGES", types))

    population: list[str] = []
    last = None
    for r in d.population_changes.iter_rows(named=True):
        name = field_name(r, isv)
        if name != last:
            population += ["", name] if population else [name]
            last = name
        population.append(
            f"  {r['fixture']} / {r['stage']}: "
            f"{population_cell(r, 'a')} -> {population_cell(r, 'b')}  ({r['kind']})"
        )
    sections.append(("POPULATION CHANGES", population))

    sections.append(
        (
            "NEWLY NEVER POPULATED",
            [field_name(r, isv) for r in d.newly_never_populated.iter_rows(named=True)],
        )
    )
    sections.append(
        (
            "NO LONGER NEVER POPULATED",
            [field_name(r, isv) for r in d.no_longer_never_populated.iter_rows(named=True)],
        )
    )

    execution = []
    for r in d.execution_changes.iter_rows(named=True):
        execution += [
            f"{r['fixture']} / {r['stage']}",
            f"  {exec_cell(r, 'a')} -> {exec_cell(r, 'b')}",
        ]
    sections.append(("EXECUTION CHANGES", execution))

    semantic: list[str] = []
    last = None
    for r in d.semantic_changes.iter_rows(named=True):
        name = f"{r['expectation_id']} ({r['expectation_class']})"
        if name != last:
            semantic += ["", name] if semantic else [name]
            last = name
        semantic += [
            f"  {r['fixture']} / {r['stage']}  ({r['kind']})",
            f"    {semantic_cell(r, 'a')}",
            f"    -> {semantic_cell(r, 'b')}",
        ]
    sections.append(("SEMANTIC CHANGES", semantic))

    for title, lines in sections:
        if lines:
            out += ["", title, "", *lines]
    if d.is_empty():
        out += ["", "no observable changes"]
    return "\n".join(out)


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    def esc(v: str) -> str:
        return v.replace("|", "\\|")

    return [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
        *("| " + " | ".join(esc(c) for c in row) + " |" for row in rows),
        "",
    ]


def _md_section(title: str, diffs: list[Diff], render) -> list[str]:
    out = [f"## {title}", ""]
    any_rows = False
    for d in diffs:
        lines = render(d, _show_isv(d))
        if lines:
            any_rows = True
            out += [f"### {d.a} → {d.b}", "", *lines]
    if not any_rows:
        out += ["None.", ""]
    return out


def _schema_md(d: Diff, isv: bool) -> list[str]:
    rows = (
        [
            ["table added", f"`{field_name(r, isv)}`", ""]
            for r in d.tables_added.iter_rows(named=True)
        ]
        + [
            ["table removed", f"`{field_name(r, isv)}`", ""]
            for r in d.tables_removed.iter_rows(named=True)
        ]
        + [
            ["column added", f"`{field_name(r, isv)}`", r["dtype"]]
            for r in d.columns_added.iter_rows(named=True)
        ]
        + [
            ["column removed", f"`{field_name(r, isv)}`", r["dtype"]]
            for r in d.columns_removed.iter_rows(named=True)
        ]
        + [
            ["type change", f"`{field_name(r, isv)}`", f"{r['dtype_a']} → {r['dtype_b']}"]
            for r in d.type_changes.iter_rows(named=True)
        ]
    )
    return _md_table(["change", "field", "type"], rows) if rows else []


def _population_md(d: Diff, isv: bool) -> list[str]:
    rows = [
        [
            f"`{field_name(r, isv)}`",
            f"{r['fixture']} / {r['stage']}",
            population_cell(r, "a"),
            population_cell(r, "b"),
            r["kind"],
        ]
        for r in d.population_changes.iter_rows(named=True)
    ]
    return _md_table(["field", "fixture / stage", d.a, d.b, "change"], rows) if rows else []


def _execution_md(d: Diff, isv: bool) -> list[str]:
    rows = [
        [f"{r['fixture']} / {r['stage']}", exec_cell(r, "a"), exec_cell(r, "b")]
        for r in d.execution_changes.iter_rows(named=True)
    ]
    return _md_table(["fixture / stage", d.a, d.b], rows) if rows else []


def _semantic_md(d: Diff, isv: bool) -> list[str]:
    rows = [
        [
            f"`{r['expectation_id']}` ({r['expectation_class']})",
            f"{r['fixture']} / {r['stage']}",
            semantic_cell(r, "a"),
            semantic_cell(r, "b"),
            r["kind"],
        ]
        for r in d.semantic_changes.iter_rows(named=True)
    ]
    return _md_table(["expectation", "fixture / stage", d.a, d.b, "change"], rows) if rows else []


def _semantic_status_md(semantic: pl.DataFrame, version: str) -> list[str]:
    """Where each expectation stands in one release, whether or not it changed."""
    rows = []
    for e in EXPECTATIONS:
        r = semantic.filter((pl.col("dbt_version") == version) & (pl.col("expectation_id") == e.id))
        if r.is_empty():
            continue
        passed = r.filter(pl.col("result") == "pass").height
        observed = "; ".join(sorted(set(r["detail"].to_list()))[:3])
        rows.append([f"`{e.id}`", e.cls, f"{passed}/{r.height}", observed, e.evidence])
    if not rows:
        return []
    return [
        f"### Status in {version}",
        "",
        *_md_table(["expectation", "class", "pass", "observed", "evidence"], rows),
    ]


def _fields_md(attr: str):
    def render(d: Diff, isv: bool) -> list[str]:
        frame: pl.DataFrame = getattr(d, attr)
        return [f"- `{field_name(r, isv)}`" for r in frame.iter_rows(named=True)] + (
            [""] if not frame.is_empty() else []
        )

    return render


def render_markdown(
    diffs: list[Diff],
    fixtures: list[str],
    stages: list[str],
    semantic: pl.DataFrame,
    unconfigured: frozenset[str] = frozenset(),
) -> str:
    out = [
        "# dbt ISRM Report",
        "",
        "Generated by dbt-isrm. Each release is compared with the one before it. A change is an",
        "observation, not a verdict: nothing is called a regression without an explicit",
        "expectation.",
        "",
        "## Versions compared",
        "",
    ]
    for d in diffs:
        note = "no observable changes" if d.is_empty() else "changes"
        new = " (not in config)" if d.b in unconfigured else ""
        out.append(
            f"- {d.a} → {d.b}{new}: {d.compared_cells.height}/{d.cells.height} cells compared, "
            f"{note}"
        )
    out += [
        "",
        "## Fixtures",
        "",
        *(f"- `{f}`" for f in fixtures),
        "",
        "## Stages",
        "",
        *(f"- `{s}`" for s in stages),
        "",
        "Populated means neither NULL nor empty (`''`, `'[]'`, `'{}'`, `'null'`, `[]`).",
        "Reproduce any cell with",
        "`dbt-isrm run --version <version> --fixture <fixture> --stage <stage>`.",
        "",
    ]
    out += _md_section("Schema changes", diffs, _schema_md)
    out += _md_section("Population changes", diffs, _population_md)
    out += _md_section("Semantic assertion changes", diffs, _semantic_md)
    out += _semantic_status_md(semantic, diffs[-1].b)
    out += [
        "Classes: `must` is contracted or confirmed upstream (pass → fail is a regression);",
        "`observed` and `open` are recorded without a verdict.",
        "",
    ]
    out += _md_section("Execution changes", diffs, _execution_md)
    out += _md_section(
        "Newly never-populated fields",
        diffs,
        _fields_md("newly_never_populated"),
    )
    out += _md_section(
        "Fields no longer never-populated",
        diffs,
        _fields_md("no_longer_never_populated"),
    )
    out += [
        "Never-populated: NULL or empty in every row of every compared cell where the table had",
        "rows.",
        "",
    ]
    return "\n".join(out)
