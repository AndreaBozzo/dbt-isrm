import itertools
from pathlib import Path
from typing import Annotated

import polars as pl
import typer

from dbt_isrm import diff as diff_
from dbt_isrm import inspect as inspect_
from dbt_isrm import matrix, report
from dbt_isrm.config import Config, load_config, select
from dbt_isrm.runner import DbtUnavailable, ensure_dbt, run_dbt

app = typer.Typer(no_args_is_help=True, add_completion=False)

ConfigOption = Annotated[
    Path, typer.Option("--config", help="Matrix configuration file.", exists=True, dir_okay=False)
]
DEFAULT_CONFIG = Path("configs/default.yml")


@app.command()
def run(
    version: Annotated[list[str] | None, typer.Option("--version", help="dbt release.")] = None,
    fixture: Annotated[list[str] | None, typer.Option("--fixture")] = None,
    stage: Annotated[list[str] | None, typer.Option("--stage")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Run the configured matrix, or the part of it selected by the filters."""
    cfg = load_config(config)
    try:
        versions, fixtures, stages = select(cfg, version, fixture, stage)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    for v in versions:
        try:
            ensure_dbt(v)
        except DbtUnavailable as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(2) from exc
        for f in fixtures:
            for s in stages:
                record = run_dbt(cfg, v, f, s)
                matrix.store(record, cfg.results_dir, cfg.snapshots_dir)
                status = "timeout" if record.timed_out else f"exit {record.exit_code}"
                typer.echo(
                    f"{v:<8} {f:<24} {s:<15} {status:<8} "
                    f"tables={record.table_count:<3} {record.duration_ms / 1000:.1f}s"
                )

    typer.echo(f"\nwrote matrix.parquet and runs.parquet to {cfg.results_dir}")


def load_results(cfg: Config) -> tuple[pl.DataFrame, pl.DataFrame]:
    matrix_path, runs_path = cfg.results_dir / "matrix.parquet", cfg.results_dir / "runs.parquet"
    if not runs_path.exists():
        typer.echo(f"error: {runs_path} not found; run `dbt-isrm run` first", err=True)
        raise typer.Exit(2)
    return pl.read_parquet(matrix_path), pl.read_parquet(runs_path)


@app.command()
def inspect(
    version: Annotated[str, typer.Argument(help="dbt release, e.g. 2.0.6.")],
    field: Annotated[
        str | None, typer.Argument(help="<table>.<column>, e.g. node_columns.constraints.")
    ] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Summarize population for one release, or show one field across fixtures and stages."""
    data, runs = load_results(load_config(config))
    try:
        if field is None:
            typer.echo(inspect_.summary(data, runs, version))
        else:
            typer.echo(inspect_.field_view(data, version, field))
    except (LookupError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def diff(
    a: Annotated[str, typer.Argument(help="Older dbt release.")],
    b: Annotated[str, typer.Argument(help="Newer dbt release.")],
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Show what changed in the observable Information Schema between two releases."""
    data, runs = load_results(load_config(config))
    try:
        typer.echo(report.render_text(diff_.diff(data, runs, a, b)))
    except LookupError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command("report")
def report_(
    version: Annotated[
        list[str] | None,
        typer.Option("--version", help="Releases to compare, oldest first. Default: config."),
    ] = None,
    output: Annotated[Path | None, typer.Option(help="Default: <results>/report.md.")] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Write report.md comparing each release with the one before it."""
    cfg = load_config(config)
    data, runs = load_results(cfg)
    recorded = set(runs["dbt_version"].to_list())
    versions = [v for v in (version or cfg.versions) if v in recorded]
    if len(versions) < 2:
        typer.echo(f"error: need runs for at least two versions, have {versions}", err=True)
        raise typer.Exit(1)

    diffs = [diff_.diff(data, runs, a, b) for a, b in itertools.pairwise(versions)]
    present = runs.filter(pl.col("dbt_version").is_in(versions))
    fixtures = [f for f in cfg.fixtures if f in set(present["fixture"].to_list())]
    stages = [s for s in cfg.stages if s in set(present["stage"].to_list())]

    path = output or cfg.results_dir / "report.md"
    path.write_bytes(report.render_markdown(diffs, fixtures, stages).encode("utf-8"))
    for d in diffs:
        typer.echo(f"{d.a} -> {d.b}: {'no observable changes' if d.is_empty() else 'changes'}")
    typer.echo(f"wrote {path}")
