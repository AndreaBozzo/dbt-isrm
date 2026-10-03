"""Matrix configuration: which dbt versions, fixtures and stages to run."""

import json
import re
import urllib.request
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


STABLE = re.compile(r"\d+\.\d+\.\d+")
PYPI_URL = "https://pypi.org/pypi/dbt/json"


def version_key(version: str) -> tuple:
    """Numeric order for stable releases; anything else sorts after, by name."""
    if STABLE.fullmatch(version):
        return (0, tuple(int(p) for p in version.split(".")), "")
    return (1, (), version)


def new_releases(published: list[str], configured: list[str]) -> list[str]:
    """Stable releases newer than every configured one, in its major versions.

    PyPI's `dbt` also carries unrelated 0.x/1.x history, hence the major filter.
    """
    stable = [v for v in configured if STABLE.fullmatch(v)]
    if not stable:
        return []
    newest = max(stable, key=version_key)
    majors = {v.split(".")[0] for v in stable}
    return sorted(
        (
            v
            for v in published
            if STABLE.fullmatch(v)
            and v.split(".")[0] in majors
            and version_key(v) > version_key(newest)
        ),
        key=version_key,
    )


def published_releases(url: str = PYPI_URL) -> list[str]:
    """Releases on PyPI with at least one file that is not yanked."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        releases = json.load(resp)["releases"]
    return [v for v, files in releases.items() if any(not f.get("yanked") for f in files)]
