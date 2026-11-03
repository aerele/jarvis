# Read a complete Prepared Report

`run_report` returns a short preview of a prepared report. For a complete-source
count, total, search, or selection, use `query_report`. Use the ID in the
`run_report` response field `run`, or follow its `next_query` hint.

1. Call `query_report(prepared_report="<run ID>")` without SQL.
2. Read `source_columns`, `tables`, source counts, `as_of`, and saved `filters`.
   Samples show only a few rows. Copy the returned `version`.
3. Call the same run with that version and one read-only SQLite SELECT.

Example after inspection confirms `c3` is the outstanding amount:

```json
{
  "prepared_report": "<run ID>",
  "version": "<64-character inspection token>",
  "sql": "SELECT COUNT(*) AS rows, decimal_sum(c3) AS outstanding FROM report_rows"
}
```

The worker loads every stored row before SQL. It can therefore find the last row
without sending thousands of rows to the model. `_row` preserves source position.
`c1`, `c2`, etc. map to the original declared columns. Hidden columns and undeclared
keys are excluded. Nested cells use JSON text. Blank numeric cells use NULL. Numeric values support at most 60 decimal places and 120-digit arithmetic precision; larger values fail explicitly. `report_rows` excludes a known
appended total. `report_totals` holds that total separately. Do not add it again.
The total's currency label is cleared because Frappe can copy the first currency
into a mixed-currency total. Group detail rows by currency when required.
The reader identifies modern footers from their stored layout, even if the Report total setting changed later. Old positional snapshots without a stored no-total flag fail with a request to generate a fresh report.

Use `decimal_sum/avg/min/max`, `decimal_add/sub/mul/div`, and `decimal_cmp` for exact
money calculations. Standard `sum/avg/total` are denied. Numeric report columns use
exact decimal ordering for direct comparisons and sorting. Quote thresholds to
preserve precision. SQL arithmetic and CAST can round. The reader preserves the
stored numeric values; it cannot restore precision already lost during generation.

`source_rows = data_rows + total_rows`. These are stored row counts, not distinct
Customer counts. `coverage.complete` confirms the full source was loaded and
access was checked. `result_truncated` applies only to the displayed answer.
Use groups, filters, or `_row` ranges for large result sets. Default display limit:
100 rows; maximum: 200 rows and approximately 16,000 characters.

The reader uses one exact completed run owned by the current user. It checks
current Report access, stored Link filters (including JavaScript filter metadata),
company access, field masks, and relevant whole-source row identities. Dynamic
Customer links are included. It refuses the whole snapshot if current access no
longer covers it. It does not return a filtered subset with stale totals.
Masked values are replaced before SQLite insertion. SQL cannot select, filter,
sort, or aggregate masked columns. The source version includes the run, file SHA,
saved filters, schema, user, and current access policy. SQL requires this version.

The parser and SQLite run in one short-lived child with bounded messages. Full
report rows stay in that child. The parent receives schema, bounded distinct access
references, and a small answer. It validates access references before sending SQL.
The child has no site connection. SQLite denies writes, ERP reads, filesystem
access, PRAGMA, extensions, and unsupported functions. All report data is enclosed
in an escaped untrusted-data block in the plugin response. Logs contain structural
summaries, not SQL text, saved filters, run IDs, or Customer values.

Limits apply together: 10 MiB compressed gzip, 32 MiB expanded JSON, 100,000 source
rows including the total, two million cells, 128 columns, 2 MiB access references,
16 seconds of child CPU, 512 MiB child address space, and a 22-second request budget.
A report within the row limit can still exceed another limit. On failure, the tool
returns an explicit error without a complete answer. It creates no report job,
export File, notification, or Customer update. It keeps no SQLite cache. A fresh
query rebuilds the temporary database, which disappears when the child exits.

This is a stored snapshot. State its generation time and saved scope. It does not
represent current balances. If access or source changed, inspect the same run again.
If the snapshot cannot be read safely, generate a fresh report in Desk. Do not retry
the same cached run in a loop. Later Customer changes still use the existing preview,
approval, and batch limits. This reader does not change that flow.
