# dbt-isrm

**dbt Information Schema Release Matrix.** Runs controlled dbt projects across releases and
execution stages, and records how the Information Schema (`target/info_schema/vN/*.parquet`)
changes.

A change is an observation, not a verdict. The goal is to make Information Schema behavior
reproducible enough to investigate upstream.

## Example

dbt 2.0.6, fixture `columns_and_constraints`. `manifest.json` lists three constraints; the
Information Schema exposes none ([dbt-labs/dbt#16553](https://github.com/dbt-labs/dbt/issues/16553)):

```text
$ dbt-isrm inspect 2.0.6 node_columns.constraints
fixture                  stage           dtype    rows  null  empty  populated  ratio
columns_and_constraints  build           VARCHAR  4     4     0      0          0%
columns_and_constraints  compile         VARCHAR  4     4     0      0          0%
columns_and_constraints  compile_strict  VARCHAR  4     4     0      0          0%
columns_and_constraints  parse           VARCHAR  4     4     0      0          0%
(rows for other fixtures omitted)
```

More in [FINDINGS.md](FINDINGS.md).

## Usage

Requires [uv](https://docs.astral.sh/uv/). Each dbt release runs in its own cached environment
(`uv tool run --from dbt==<version>`); nothing is installed globally.

```bash
uv sync
uv run dbt-isrm run                                   # configured matrix
uv run dbt-isrm run --discover                        # plus newer stable releases on PyPI
uv run dbt-isrm run --version 2.0.6 --fixture source --stage build
uv run dbt-isrm inspect 2.0.6                         # population and expectation summary
uv run dbt-isrm inspect 2.0.6 node_columns.constraints
uv run dbt-isrm diff 2.0.5 2.0.6                      # changes between two releases
uv run dbt-isrm report                                # results/report.md, all recorded releases
```

Filters combine and repeat. Versions need not be in the config. Re-running a
(version, fixture, stage) cell replaces only that cell.

## What is measured

Per column of every Information Schema table, per run: DuckDB type, rows, NULLs, empty values,
populated values, distinct values. **Empty** means `''`, `'[]'`, `'{}'`, `'null'` or a
zero-length list, which dbt writes for unset fields.

`diff` compares schema (tables, columns, types), row and population counts, exit status and
semantic results, only over cells that produced artifacts in both releases. Paths, timestamps
and run ids are never compared. Values are not profiled.

Counts cannot see a value that is present but wrong, so three **semantic expectations**
([`expectations.py`](src/dbt_isrm/expectations.py)) read values directly:

| expectation                        | class  | evidence                                                        |
| ---------------------------------- | ------ | --------------------------------------------------------------- |
| `constraints_exposed`              | must   | [#16553](https://github.com/dbt-labs/dbt/issues/16553)          |
| `inferred_types_are_adapter_types` | open   | [#16515](https://github.com/dbt-labs/dbt/issues/16515)          |
| `current_invocation_recorded`      | open   | [#16588](https://github.com/dbt-labs/dbt/issues/16588)          |

`must`: contracted or confirmed upstream; pass → fail is a regression. `open`: recorded without a
verdict.

Each run records its identity: a per-cell `--invocation-id` (identical across releases), raw
`dbt --version`, wheel tag, sha256 of the native binary, OS and architecture.

## Matrix

[`configs/default.yml`](configs/default.yml): dbt 2.0.0–2.0.6, six fixtures, four stages. New
releases are added to the config by hand; `--discover` (used by the weekly workflow) runs newer
stable releases meanwhile and the report marks them `(not in config)`.

| stage            | command                                |
| ---------------- | -------------------------------------- |
| `parse`          | `dbt parse`                            |
| `compile`        | `dbt compile`                          |
| `compile_strict` | `dbt compile --static-analysis strict` |
| `build`          | `dbt build` (local DuckDB)             |

| fixture                   | covers                                                     |
| ------------------------- | ---------------------------------------------------------- |
| `basic_model`             | two models with a dependency; DAG and column lineage       |
| `source`                  | source description, loader, `loaded_at_field`              |
| `columns_and_constraints` | contract, declared types, constraints                      |
| `meta_tags_groups`        | tags, meta, group, access                                  |
| `disabled_resources`      | enabled and disabled model, disabled source                |
| `exposure`                | owner, type, `depends_on`, tags, meta                      |

Fixtures are plain dbt projects; each runs on its own with `dbt build --profiles-dir .`. Every
matrix run uses a fresh temporary copy.

## Output

```text
results/matrix.parquet   one row per (run, table, column); canonical
results/matrix.json      same, as JSON
results/runs.parquet     one row per dbt invocation, failures included
results/semantic.parquet one row per (run, expectation)
results/report.md        release-over-release report
snapshots/<version>/<fixture>/<stage>/
    info_schema/  manifest.json  run_results.json  stdout.txt  stderr.txt
```

Both directories are git-ignored. The `matrix` workflow (weekly and on demand) uploads them as
an artifact.

## Development

```bash
uv run pytest                       # includes integration tests against dbt 2.0.6
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

## License

Apache-2.0
