"""Run one dbt release against one fixture and stage, in isolation.

Each release runs through `uv tool run --from dbt==<version>`, which keeps a
cached environment per version and never touches the caller's Python
environment. Each run executes in a fresh temporary copy of the fixture, so
no state (partial parse, DuckDB file, stale artifacts) leaks between runs.
"""

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dbt_isrm.artifacts import info_schema_versions
from dbt_isrm.config import Config
from dbt_isrm.models import DbtBuild, RunRecord

# dbt artifacts preserved next to the Information Schema, when present.
KEPT_TARGET_FILES = ("manifest.json", "run_results.json")

# Runs inside the release's own environment to identify the build that executes.
_BUILD_PROBE = """
import hashlib, importlib.metadata as m, json
d = m.distribution("dbt")
core = [f for f in d.files if str(f).startswith("dbt/_core")]
assert len(core) == 1, core
tag = [l.split(": ", 1)[1] for l in d.read_text("WHEEL").splitlines() if l.startswith("Tag: ")]
print(json.dumps({
    "wheel_tag": ",".join(tag),
    "binary_sha256": hashlib.sha256(core[0].locate().read_bytes()).hexdigest(),
}))
"""


class DbtUnavailable(RuntimeError):
    """The requested dbt release could not be installed or is not what it claims."""


def _tool_command(version: str, executable: str) -> list[str]:
    uv = shutil.which("uv")
    if uv is None:
        raise DbtUnavailable("`uv` was not found on PATH")
    return [uv, "tool", "run", "--quiet", "--from", f"dbt=={version}", executable]


def dbt_command(version: str) -> list[str]:
    return _tool_command(version, "dbt")


def invocation_id(fixture: str, stage: str) -> str:
    """Stable per cell, so corresponding runs of different releases share an id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dbt-isrm:{fixture}/{stage}"))


def ensure_dbt(version: str) -> DbtBuild:
    """Install the release if needed and check it reports the requested version.

    Runs before any fixture so that installation output never lands in a run's
    captured stderr, and so a wrong or missing release fails loudly instead of
    being recorded as a dbt failure.
    """
    proc = subprocess.run(
        [*dbt_command(version), "--version"], capture_output=True, text=True, timeout=900
    )
    match = re.search(r"^dbt (\S+)", proc.stdout, re.MULTILINE)
    if proc.returncode != 0 or match is None:
        raise DbtUnavailable(
            f"dbt {version} is not runnable (exit {proc.returncode}):\n{proc.stdout}{proc.stderr}"
        )
    if match.group(1) != version:
        raise DbtUnavailable(f"requested dbt {version} but it reports {match.group(1)}")

    probe = subprocess.run(
        [*_tool_command(version, "python"), "-c", _BUILD_PROBE],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if probe.returncode != 0:
        raise DbtUnavailable(f"could not identify the dbt {version} build:\n{probe.stderr}")
    return DbtBuild(version_output=proc.stdout.strip(), **json.loads(probe.stdout))


def cell_dir(config: Config, version: str, fixture: str, stage: str) -> Path:
    return config.snapshots_dir / version / fixture / stage


def run_dbt(config: Config, build: DbtBuild, version: str, fixture: str, stage: str) -> RunRecord:
    """Run one (version, fixture, stage) cell and snapshot its artifacts."""
    spec = config.stages[stage]
    fixture_dir = config.fixtures_dir / fixture
    if not (fixture_dir / "dbt_project.yml").is_file():
        raise FileNotFoundError(f"fixture {fixture!r} has no dbt_project.yml in {fixture_dir}")

    out = cell_dir(config, version, fixture, stage)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    invocation = invocation_id(fixture, stage)
    dbt_args = [*spec.args, *config.common_args, "--invocation-id", invocation]
    env = {**os.environ, **spec.env}

    with tempfile.TemporaryDirectory(prefix="isrm-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp) / fixture
        shutil.copytree(fixture_dir, work)

        started_at = datetime.now(UTC)
        t0 = time.perf_counter()
        timed_out = False
        try:
            # Bytes, not text: output is stored verbatim, without newline translation.
            proc = subprocess.run(
                [*dbt_command(version), *dbt_args],
                cwd=work,
                env=env,
                capture_output=True,
                timeout=spec.timeout_s,
            )
            exit_code: int | None = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out, exit_code = True, None
            stdout, stderr = exc.stdout or b"", exc.stderr or b""
        duration_ms = round((time.perf_counter() - t0) * 1000)

        (out / "stdout.txt").write_bytes(stdout)
        (out / "stderr.txt").write_bytes(stderr)

        # Snapshot whatever exists, including after a failed run: a partial
        # Information Schema is an observation too.
        info_schema = work / spec.artifact_dir
        if info_schema.is_dir():
            shutil.copytree(info_schema, out / "info_schema")
        target = work / "target"
        for name in KEPT_TARGET_FILES:
            if (target / name).is_file():
                shutil.copy2(target / name, out / name)

    versions = info_schema_versions(out / "info_schema")
    rel = lambda p: p.relative_to(config.snapshots_dir).as_posix()  # noqa: E731
    return RunRecord(
        run_id=uuid.uuid4().hex,
        dbt_version=version,
        fixture=fixture,
        stage=stage,
        invocation_id=invocation,
        dbt_args=dbt_args,
        dbt_version_output=build.version_output,
        dbt_wheel_tag=build.wheel_tag,
        dbt_binary_sha256=build.binary_sha256,
        os=f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        warehouse=spec.warehouse,
        started_at=started_at,
        duration_ms=duration_ms,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout_path=rel(out / "stdout.txt"),
        stderr_path=rel(out / "stderr.txt"),
        artifact_root=rel(out / "info_schema") if versions else None,
        info_schema_versions=[v.name for v in versions],
        table_count=sum(len(list(v.glob("*.parquet"))) for v in versions),
    )
