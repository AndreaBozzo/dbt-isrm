import itertools
from pathlib import Path
from typing import Annotated, NamedTuple

import polars as pl
import typer

from dbt_isrm import diff as diff_
from dbt_isrm import inspect as inspect_
from dbt_isrm import matrix, report
from dbt_isrm.config import (
    Config,
    load_config,
    new_releases,
    published_releases,
    select,
    version_key,
)
from dbt_isrm.runner import DbtUnavailable, ensure_dbt, run_dbt

app = typer.Typer(no_args_is_help=True, add_completion=False)

ConfigOption = Annotated[
    Path, typer.Option("--config", help="Matrix configuration file.", exists=True, dir_okay=False)
]
DEFAULT_CONFIG = Path("configs/default.yml")


def fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code)


@app.command()
def run(
    version: Annotated[list[str] | None, typer.Option("--version", help="dbt release.")] = None,
    fixture: Annotated[list[str] | None, typer.Option("--fixture")] = None,
    stage: Annotated[list[str] | None, typer.Option("--stage")] = None,
    discover: Annotated[
        bool,
        typer.Option(help="Also run stable releases on PyPI newer than the configured ones."),
    ] = False,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Run the configured matrix, or the part of it selected by the filters."""
    cfg = load_config(config)
    try:
        versions, fixtures, stages = select(cfg, version, fixture, stage)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if discover:
        found = [v for v in new_releases(published_releases(), cfg.versions) if v not in versions]
        typer.echo(f"discovered (not in config): {', '.join(found) or 'none'}")
        versions = [*versions, *found]

    for v in versions:
        try:
            build = ensure_dbt(v)
        except DbtUnavailable as exc:
            raise fail(str(exc), 2) from exc
        for f in fixtures:
            for s in stages:
                record = run_dbt(cfg, build, v, f, s)
                try:
                    matrix.store(record, cfg.results_dir, cfg.snapshots_dir)
                except matrix.IncompatibleResults as exc:
                    raise fail(str(exc), 2) from exc
                status = "timeout" if record.timed_out else f"exit {record.exit_code}"
                typer.echo(
                    f"{v:<8} {f:<24} {s:<15} {status:<8} "
                    f"tables={record.table_count:<3} {record.duration_ms / 1000:.1f}s"
                )

    typer.echo(f"\nwrote results to {cfg.results_dir}")


class Results(NamedTuple):
    matrix: pl.DataFrame
    runs: pl.DataFrame
    semantic: pl.DataFrame


def load_results(cfg: Config) -> Results:
    paths = [cfg.results_dir / f"{n}.parquet" for n in ("matrix", "runs", "semantic")]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise fail(f"{', '.join(missing)} not found in {cfg.results_dir}; run `dbt-isrm run`", 2)
    return Results(*(pl.read_parquet(p) for p in paths))


@app.command()
def inspect(
    version: Annotated[str, typer.Argument(help="dbt release, e.g. 2.0.6.")],
    field: Annotated[
        str | None, typer.Argument(help="<table>.<column>, e.g. node_columns.constraints.")
    ] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Summarize one release, or show one field across fixtures and stages."""
    res = load_results(load_config(config))
    try:
        if field is None:
            typer.echo(inspect_.summary(res.matrix, res.runs, res.semantic, version))
        else:
            typer.echo(inspect_.field_view(res.matrix, version, field))
    except (LookupError, ValueError) as exc:
        raise fail(str(exc)) from exc


@app.command()
def diff(
    a: Annotated[str, typer.Argument(help="Older dbt release.")],
    b: Annotated[str, typer.Argument(help="Newer dbt release.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Show what changed in the observable Information Schema between two releases."""
    res = load_results(load_config(config))
    try:
        typer.echo(report.render_text(diff_.diff(*res, a, b)))
    except LookupError as exc:
        raise fail(str(exc)) from exc


@app.command("report")
def report_(
    version: Annotated[
        list[str] | None,
        typer.Option("--version", help="Releases to compare, oldest first. Default: all recorded."),
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Default: <results>/report.md.")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Write report.md comparing each release with the one before it."""
    cfg = load_config(config)
    res = load_results(cfg)
    recorded = set(res.runs["dbt_version"].to_list())
    if version:
        versions = [v for v in version if v in recorded]
    else:
        versions = sorted(recorded, key=version_key)
    if len(versions) < 2:
        raise fail(f"need runs for at least two versions, have {versions}")

    diffs = [diff_.diff(*res, a, b) for a, b in itertools.pairwise(versions)]
    present = res.runs.filter(pl.col("dbt_version").is_in(versions))
    fixtures = [f for f in cfg.fixtures if f in set(present["fixture"].to_list())]
    stages = [s for s in cfg.stages if s in set(present["stage"].to_list())]
    unconfigured = frozenset(versions) - set(cfg.versions)

    path = output or cfg.results_dir / "report.md"
    md = report.render_markdown(diffs, fixtures, stages, res.semantic, unconfigured)
    path.write_bytes(md.encode("utf-8"))
    for d in diffs:
        new = " (not in config)" if d.b in unconfigured else ""
        typer.echo(f"{d.a} -> {d.b}{new}: {'no observable changes' if d.is_empty() else 'changes'}")
    typer.echo(f"wrote {path}")
