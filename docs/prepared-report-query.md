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

# Inline report snapshots (`rs_` ids)

`run_report` stores the rows of a large inline run in Redis. The agent reads them with
`query_report(prepared_report="rs_...")`, the same way as a Prepared Report. This section
is for the operator.

## Configuration

Set both keys in `site_config.json` (or `common_site_config.json`). Jarvis reads them on
every call. Do not restart a service and do not run `bench migrate`.

| Key | Default | Effect |
| --- | --- | --- |
| `jarvis_report_snapshots` | `1` | `0` turns snapshots off. `run_report` stops creating snapshots. An existing `rs_` id is refused at once: its rows stay in Redis until their TTL, but nobody can read them. |
| `jarvis_report_snapshot_reload` | `1` | `0` turns off the re-run of an expired snapshot. A snapshot that is still in Redis stays readable. An expired one gives the "run run_report again" error. Snapshots off also turns reload off. |

Each key uses `frappe.utils.cint`. A missing key means `1`. `0`, `"0"`, `""`, `null` and
any text that is not a number mean `0`. Any other number means `1`.

Fixed limits: rows live 60 minutes, the record that allows a reload lives 24 hours, a user
keeps at most 5 snapshots, and a user can start at most 6 reloads in a fixed hour. A reload
returns the tool error `snapshot_reloaded`. The agent then reads the fresh rows with its next call.

## Free the memory by hand

The snapshot keys are in `persistent_cache_keys` (`jarvis/hooks.py`). They survive the
5-minute watchdog cache clear and `bench clear-cache`. To free the memory before the TTL,
delete the keys by hand. Use the port of `redis_cache` from `common_site_config.json`.

```bash
redis-cli -p <redis_cache port> --scan --pattern '*|jarvis:report_snapshot*' \
  | xargs -r -n 100 redis-cli -p <redis_cache port> DEL
```

The pattern matches all five key families: content, meta, index, reload lock, reload rate.
First run the `--scan` part alone and read the keys. After a purge, the agent runs
`run_report` again when it needs the rows.

## Rollout watch

For the first 7 days, run the signals script on the bench every day:

```bash
bench --site <site> execute jarvis.scripts.report_snapshot_signals.run
bench --site <site> execute jarvis.scripts.report_snapshot_signals.run --kwargs "{'days': 7}"
```

The script reads the `jarvis.report_snapshot` log (the bench-wide copy, so every site),
changes nothing, and prints a JSON result. It gives per-site signals, one `pooled` entry for
the sites with fewer than 20 reloads, and one top-level `abort` list. Read the `warnings`
list first. A warning means the log is missing, empty, old, or cut by rotation
(`window_truncated`). Do not trust the result then. With `FRAPPE_STREAM_LOGGING` set, the
events go to stdout and the script finds no file.

Turn `jarvis_report_snapshot_reload` off when `abort` names any of these (the script
checks them with at least 20 reloads, except the eviction rule):

- S5: the reload p95 is above 12 seconds.
- Capped or busy reloads are above 10 % of reloads (busy counts once per snapshot and generation).
- S4 is below 90 %: too few reloads are followed by a query that runs SQL. S4 is undefined when no reload is ok.
- Failed reloads are above 20 %. The message lists the `failed:<class>` counts.
- Killed reloads: more than 5 % of attempts have a `reload_start` and no `reload_end`.
- Any `refused:prepared` outcome.
- Evicted reads are above 5 % of reads (checked with at least 20 reads, also on a small site).

Turn `jarvis_report_snapshots` off for any report of wrong rows or wrong access.

The result also holds the server-wide Redis `evicted_keys` and `uptime_in_seconds`. They are
a manual check only: compare two runs. Redis `allkeys-lru` can evict a snapshot or a reload
lock early. The worst case is one extra run or a reset reload cap, never wrong rows.
