# Findings

Observations from dbt-isrm, with how far each has been followed upstream.

Matrix as of 2026-10-03: dbt 2.0.0–2.0.6 × 6 fixtures × 4 stages, 168 runs, all exit 0.
Upstream source read at `dbt-labs/dbt@7bbd974` (2026-10-02).

## 1. `node_columns.constraints` and `models.constraints` are always NULL

- **Reproduce:** `dbt-isrm run --version 2.0.6 --fixture columns_and_constraints --stage compile`,
  then `dbt-isrm inspect 2.0.6 node_columns.constraints`
- **Observation:** 4/4 rows NULL in every stage of every release 2.0.0–2.0.6, while
  `manifest.json` lists `not_null` + `primary_key` on `order_id` and `not_null` on
  `customer_id`. `models.primary_key` is populated (`[order_id]`).
- **Upstream:** known: [dbt-labs/dbt#16553](https://github.com/dbt-labs/dbt/issues/16553)
  (open, a maintainer says a fix PR is in flight). This is dbt-isrm's first reproduction of a
  known real-world behavior.
- **Next:** when a release fixes it, the diff should show a population gain on both fields.

## 2. `dbt_rt.invocations` never contains the current invocation

- **Reproduce:** in one project directory, run
  `dbt build --generate-info-schema --invocation-id <id>` three times.
- **Observation:** after run N, `dbt_rt.invocations` holds invocations 1..N−1 and never N, while
  `dbt_rt.run_results` already has rows for N. In the matrix, which uses a fresh project copy per
  run, the table is always empty.
- **Code:** `crates/dbt-main/src/dbt_lib.rs` writes the invocation record at exit
  (`ctx.write(status)` after `do_execute_fs` returns), but the Information Schema is written
  inside `do_execute_fs`. The `docs generate` path already works around the same ordering with a
  second ingest (comment near `ingest_metadata_into_index(..., "dbt docs generate")`); the
  general `--generate-info-schema` path has no such pass.
- **Consequence:** `run_results JOIN invocations USING (invocation_id)` (the join in
  `crates/dbt-index-core/src/db.rs`) drops the current run's results.
- **Upstream:** filed as [dbt-labs/dbt#16588](https://github.com/dbt-labs/dbt/issues/16588)
  (2026-10-03), reproduced on 2.0.0 and 2.0.6.

## 3. `data_type_inferred` holds Arrow type names on DuckDB too

- **Reproduce:** `columns_and_constraints / compile_strict`, query
  `dbt.node_columns.data_type_inferred`.
- **Observation:** `Int32`, `Utf8`, `Decimal128(10, 2)` for columns declared `integer`,
  `varchar`, `decimal(10, 2)`.
- **Upstream:** known for Snowflake:
  [dbt-labs/dbt#16515](https://github.com/dbt-labs/dbt/issues/16515). DuckDB repro
  [added](https://github.com/dbt-labs/dbt/issues/16515#issuecomment-5968924582) (2026-10-03).
- **Instrument note:** the matrix calls this column 100% populated. Count-level statistics can't
  see wrong-kind values; this is the strongest argument for M3's semantic checks.

## 4. `dbt.hooks` is populated, contrary to an open issue

- **Observation:** the `source` fixture's `on-run-start` hook yields 1 row in `dbt.hooks` in
  every stage of every release 2.0.0–2.0.6.
- **Upstream:** [dbt-labs/dbt#16041](https://github.com/dbt-labs/dbt/issues/16041) (open,
  2026-08-22) says the table is "always zero rows". It looks fixed before 2.0.0.
  [Commented](https://github.com/dbt-labs/dbt/issues/16041#issuecomment-5968924805)
  (2026-10-03).
- **Remaining:** hook rows have `materialized = snapshot`, but operations carry no `config` in
  `manifest.json`, so the value looks like a default (reported in the same comment).
  `schema_name = public` is inherited from `manifest.json`.

## 5. Strict analysis of a source-backed model falls back to `off`

- **Observation:** in `source / compile_strict`, dbt downloads the source schema before
  `on-run-start` creates the table, fails (`dbt1014`), and sets `static_analysis` to `off` for
  `stg_orders`. The run still exits 0.
- **Status:** expected given the ordering. The fixture is documented accordingly.

## Not findings

- **Empty `dbt.column_lineage`** was a fixture gap: no fixture had a model→model dependency under
  working strict analysis. `basic_model` now has `orders_summary`, and lineage is populated under
  `compile_strict` (2 rows) in every release.
- **"column-level lineage requires --static-analysis strict"** is printed under
  `--static-analysis strict` when there is no lineage to write. The wording is misleading but
  harmless.

## Research checkpoint (§28), 2026-10-03

1. **Which fields vary by execution stage?** `node_columns.data_type_inferred` (strict only),
   `node_columns` row count for models without YAML columns (strict only adds inferred columns),
   `column_lineage` (strict only), `dbt_rt.run_results` (build only).
2. **Which fields are consistently unpopulated?** 124 columns are never populated in 2.0.6 and
   470 sit in tables with no rows for these fixtures. Notable: `node_columns.constraints`,
   `models.constraints`, `node_columns.data_type_actual` (even after `build`; related:
   [#16514](https://github.com/dbt-labs/dbt/issues/16514)), and all of `dbt_rt.invocations`,
   `relations`, `adapter_queries`, `diagnostics`, `freshness`.
3. **Which behaviors differ across releases?** None. 2.0.0–2.0.6 are identical in schema,
   counts, and also in raw values once volatile columns are excluded (checked 2.0.0 vs 2.0.6 by
   hashing every table).
4. **Which correspond to known issues?** #1 (#16553), #3 (#16515); #4 contradicts #16041.
5. **Which are unexplained?** #2 before reading the code (now filed as #16588); the
   `materialized` value in #4.
6. **Traced into the Rust implementation?** Yes: #2, to the write order in `dbt_lib.rs`.
7. **Is this information not already obvious from dbt's own tests and issues?** Partly. #2 and
   the #16041 staleness are new. But release-over-release diffs have produced nothing so far,
   because the surface has been frozen since 2.0.0. The value is currently in the *stage ×
   fixture* view and in value-level checks, not the release diff.

**Implication:** don't expand fixtures or versions yet. The next useful step is narrow: M3
expectations on the handful of properties above (constraints, inferred types, invocations), so
the matrix flags the fix when it lands. The release diff will matter once a release changes
the surface; the scheduled CI run is there to catch that.
