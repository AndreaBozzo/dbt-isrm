"""Matrix configuration: which dbt versions, fixtures and stages to run."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Stage(BaseModel):
    """One logical execution stage, e.g. `compile_strict`."""

    args: list[str]
    # Informational: whether the stage executes SQL against the warehouse.
    warehouse: bool = False
    # Where dbt writes the Information Schema, relative to the project copy.
    artifact_dir: str = "target/info_schema"
    timeout_s: int = 600
    env: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    versions: list[str]
    fixtures: list[str]
    stages: dict[str, Stage]
    common_args: list[str] = Field(default_factory=list)
    fixtures_dir: Path
    results_dir: Path
    snapshots_dir: Path


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = path.resolve().parent
    for key in ("fixtures_dir", "results_dir", "snapshots_dir"):
        raw[key] = (base / raw[key]).resolve()
    return Config.model_validate(raw)


def select(
    config: Config,
    versions: list[str] | None,
    fixtures: list[str] | None,
    stages: list[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """Apply CLI filters to the configured matrix.

    Unknown fixtures and stages are errors, so a typo cannot silently produce an
    empty matrix. Versions are not checked against the config: running a new
    release without editing the config is a deliberate use case.
    """
    for kind, wanted, known in (
        ("fixture", fixtures, config.fixtures),
        ("stage", stages, list(config.stages)),
    ):
        unknown = sorted(set(wanted or []) - set(known))
        if unknown:
            raise ValueError(f"unknown {kind}(s): {', '.join(unknown)}; known: {', '.join(known)}")
    return (
        versions or config.versions,
        [f for f in config.fixtures if not fixtures or f in fixtures],
        [s for s in config.stages if not stages or s in stages],
    )
