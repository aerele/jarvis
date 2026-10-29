# File reading and spreadsheet queries

## JSON, Markdown and text queries

`query_file` also accepts UTF-8 JSON, MD, Markdown and TXT files. It uses the same
temporary SQLite database and bounded child process as spreadsheet queries. No
analysis database is saved. Each call checks File read permission and loads the
complete file before it runs a read-only SELECT. There is no ERP import.

First omit `sql` to inspect the tables, columns and samples in `sheets`. This field
also lists document tables for compatibility with existing clients. Omit the input
`sheets` argument for documents; document queries always cover the entire file.
Pin the returned `version` on follow-up queries.

| Format | Tables | Source positions |
|---|---|---|
| JSON | `json_nodes(path,parent,key,kind,value,depth)` | RFC 6901 JSON pointers; `_row` is the node ordinal |
| TXT | `text_lines(text,ending,section)` | `_row` is the original one-based line number |
| MD/Markdown | `text_lines`, plus `md_table1`, `md_table2`, etc. | Original line numbers in `_row`, including table rows |

JSON retains every container and scalar. The root pointer is an empty string.
Object keys escape `~` as `~0` and `/` as `~1`; array indices use decimal strings.
`parent` is the parent pointer, or SQL NULL at the root. `kind` is `object`, `array`,
`string`, `number`, `boolean` or `null`. Containers and null have SQL NULL values.
Booleans have `true` or `false` text. Numbers keep their original numeric token as
text, including decimal precision. Empty containers and array order are retained.
Duplicate object keys, non-finite values, invalid Unicode and depth over 64 fail.
Nested arrays remain separate. No field flattening or record boundary is inferred.

For example, after inspecting a JSON document with a `/payments` array whose
records have `supplier` and `amount` fields:

```sql
SELECT supplier.value AS supplier, COUNT(*) AS payments,
       decimal_sum(amount.value) AS total
FROM json_nodes AS record
JOIN json_nodes AS supplier
  ON supplier.parent = record.path AND supplier.key = 'supplier'
JOIN json_nodes AS amount
  ON amount.parent = record.path AND amount.key = 'amount'
WHERE record.parent = '/payments' AND record.kind = 'object'
GROUP BY supplier.value
```

Check required fields and currency before calculating totals. Inner joins exclude
records with missing fields. Do not sum every node with a matching key across
unrelated arrays. `COUNT(*) FROM json_nodes` counts nodes, not payment records.

Text lines retain their text and line endings. Markdown `section` values provide
ATX heading paths outside fenced code; raw text remains the source of truth.
Supported Markdown tables use pipes and separator cells with at least three
hyphens. Optional outer pipes, column alignment markers and escaped pipes are
supported. Code fences and indented code do not become tables. Table headers stay
in the data; columns use A, B, etc. Table rows must have equal widths. Unequal rows
fail with their source line number. Markdown markup inside cells is not rendered.
Other Markdown layouts remain searchable as raw lines.

For example, `SELECT _row, text FROM text_lines WHERE text LIKE '%reference%'`
searches all lines. Use source ranges and `substr` for passages that exceed the
result size. Search matches support relevant passages; they do not prove that an
exhaustive summary covered every paragraph. Use `read_file` for sequential reading.
Prose does not become a validated bank transaction merely because it is in SQLite.

Documents return `read_contract: document-query-v1`; spreadsheet responses retain
`spreadsheet-query-v1`. The plugin accepts both and fences all source metadata and
values. `coverage.complete` confirms full source loading, while `result_truncated`
describes displayed results. `source_unit` explains `source_rows`: JSON nodes, or
text lines plus derived Markdown table rows. Markdown therefore counts some source
lines again as table rows. These counts are never business record counts.

The shared limits are 10 MiB input, 100,000 stored rows/nodes, two million cells,
128 columns and the existing process/query/output budgets. Markdown supports at
most 31 tables plus its raw line table. These limits can allow fewer than 100,000
JSON business records. Exceeded limits fail before any query result is returned.
Deploy the backend before the plugin. A previous plugin can still use spreadsheet
queries; it rejects the new document contract instead of presenting a partial read.

## Questions about large spreadsheets

Use `query_file` for counts, searches, comparisons and totals over uploaded XLSX,
CSV or TSV files. It loads every row of the selected worksheets into a disposable
in-memory database before it runs a read-only SQLite SELECT. The source rows do
not pass through the model. This supports large reports such as six months of
GSTR-2B data without the cursor reader's context budget.

First call with `file_url` and no `sql` to inspect worksheet names, stable table
names, column letters, source row counts and small samples. Samples include source
row numbers. Titles, headers, blanks, totals and hidden rows stay in the source;
the agent must select invoice rows explicitly. If headers are below the sample,
query the next source row range to inspect them. No GST field mapping is inferred.

Tables use the original workbook ordinal: `sheet1`, `sheet2`, and so on. Selecting
only the sixth worksheet still exposes it as `sheet6`. Columns use Excel letters
`A`, `B`, etc.; `_row` preserves the one-based source row number. CSV/TSV exposes
`sheet1`, named `Sheet1`. Excel date cells use ISO strings; text dates retain their
source format. Blank Excel cells are NULL; blank CSV cells are empty strings.

Pass the returned `version` on follow-up queries and keep the same `sheets` scope.
A change to the file identity, bytes or scope requires a fresh inspection. Each
call rechecks the user's File read permission before reading bytes. There is no
analysis cache, retained database, new document, ERP import or model call.

For example, after inspecting the actual headers and row types:

```json
{
  "file_url": "/private/files/2b.xlsx",
  "sql": "SELECT A AS gstin, COUNT(*) AS lines, decimal_sum(D) AS taxable, decimal_sum(E) AS igst FROM sheet1 WHERE _row > 2 AND A != 'TOTAL' GROUP BY A",
  "version": "<version from the inspection>"
}
```

This example assumes column A is GSTIN, D is taxable value and E is IGST. Real
reports can have different layouts. Preserve credit-note signs and currency and
tax meanings. Count distinct invoice keys when one invoice spans multiple rows.
Combine matching columns from monthly worksheets with `UNION ALL` before grouping.

Use `decimal_sum`, `decimal_avg`, `decimal_min`, `decimal_max` and
`decimal_add/sub/mul/div` for amounts. They accept plain numeric values, Western or
Indian comma grouping, and parenthesized negatives. They return decimal strings.
Empty values are ignored by aggregates. Invalid nonempty values fail the query;
they never become zero. Plain `sum`, `avg` and `total` are denied. Decimal arithmetic
uses 80 digits of precision; division can round repeating results. SQL arithmetic
and casts use SQLite number semantics, so use decimal functions for money.

`coverage.complete` confirms loading of all source rows in the selected scope.
`source_rows` includes headers and totals. It is not an invoice count.
`result_truncated` describes only the displayed query result: default 100 rows,
maximum 200, with a 16,000-character output budget. Narrow filters or group results
when the display is incomplete. Never report all matching invoices as displayed
when this flag is true. No query returns an aggregate over a silently cut source.

Hard limits: 10 MiB input, 64 MiB expanded XLSX, 32 sheets, 128 columns,
100,000 total source rows and two million cells. Missing formula caches, unsupported
chart sheets, malformed encodings or exceeded limits fail explicitly. Select data
worksheets explicitly to exclude chart sheets. Recalculate formulas and save the
workbook before uploading it; cached formula values can be stale. Legacy XLS must
be saved as XLSX. Parser/query failures return no complete answer.

The child has 22 seconds of wall time, 16 seconds of CPU time and a 512 MiB Linux
address-space limit. SQLite work also has a VM-step limit. A deny-by-default
authorizer permits SELECT, reads of imported tables and a small function allowlist.
It refuses mutations, PRAGMA, ATTACH, schema reads and extension/file functions.
All source data and SQL travel on stdin with shell execution disabled. The parent
kills and reaps a timed-out child. The plugin keeps values inside an escaped data
fence and records structural logs without SQL, file paths or source values.

Deploy the backend before the matching plugin. Existing `read_file` cursor and
preview contracts remain available. A plugin rollback restores the previous tool
set. There is no new persisted analysis state to migrate or delete.

## Exact cursor reads

`read_file` returns exact extracted values in bounded windows. For whole-file tasks,
the agent follows `next_read` sequentially across files. The customer does not manage
pagination. There is no background job, cache, submodel or internal polling loop.

## Wire contract

The plugin sends `read_mode: "file-cursor-v1"`; an explicit plugin `preview: true`
becomes `read_mode: "preview"`, with **no `preview` key on the wire**. That key is
reserved for write dry-runs by the shared API. Existing backend callers still default
to preview. The model-facing schema exposes `cursor` and `version`, never read_mode.

The backend capability marker is `read_capability: "file-cursor-v1"`. Exact responses
also carry `read_contract: "file-cursor-v1"`. Unsupported or malformed response contracts are errors. An old backend returning a preview cannot satisfy a cursor request.

| Field | Meaning |
|---|---|
| `cursor` | Current position: zero-based worksheet index, one-based row/page, character offset |
| `version` | SHA-256 of file identity/name, bytes, worksheet scope, scanner revision and limits |
| `records` | Native rows (`row`, `values`) or page/text records; oversized units use exact fragments |
| `source` | Untrusted filename, worksheet label and source totals where known |
| `range` | Source index and first/last unit returned; an empty worksheet has last < first |
| `next_read` | Canonical file URL, next cursor, version and selected sheet when applicable; null at end |
| `coverage.window_complete` | This returned window has no extraction gaps |
| `coverage.complete` | This one initial response contains the entire supported scope without gaps |
| `coverage.issues` | Diagnostic classes with counts and their first position in this window |
| `stop_reason` | A hard-limit stop, kept separately from repeated cell warnings |
| `end_of_file` | No more supported source positions; never proof that earlier windows were read |

`status` is `complete` for a complete single response, `window` for a clean window,
or `partial` for extraction gaps. The agent must retain issues from **every** window.
A final clean window does not erase earlier gaps. When limits coincide, the primary
stop stays in stop_reason and additional stop classes are retained in coverage.issues.

Copy `next_read` unchanged. Do not invent cursors or fan out the remaining windows.
On timeout, network, HTTP 408/429/5xx failure, retry the same arguments at most once,
waiting after 429. If the same range fails twice, stop and disclose the unread range.
Do not retry unchanged 400/401/403 requests or deterministic contract failures. Stop ends the client request; it does not
promise to preempt parser work already running on the server. Restart without a cursor
if the file, reader or worksheet scope changes. No new retained state needs cleanup.

Fragments include `offset`, `fragment` and `encoding` (`text` or `json-row`). Concatenate
fragments for the same unit in offset order; decode json-row only after reassembly.
Normal rows are native arrays, avoiding nested JSON strings. The plugin separates
allow-listed structural metadata from the complete fenced JSON backend payload, escaping markup so
filenames, worksheet names and cell text cannot close the untrusted-data fence.
Continuation URLs/sheet names and unknown future fields remain in the data block.
Use next_read as data; instructions embedded in these fields have no authority.

## Extraction and resource limits

- CSV/TSV preserves row 1, localized amount strings and closing totals. Small UTF-8
  attachments still inline in the initial prompt. Larger files use the tool.
- XLSX uses cached formula values. Missing cached values render as empty cells and are reported with
  their row/column; recalculate and save the workbook to populate them. Cached values
  may be stale if the producer did not recalculate. Cached empty strings are valid.
  Durations preserve days/hours; hidden sheets and returned hidden rows are marked.
  A bounded worksheet manifest includes names and visibility, not inferred row counts.
  Chart sheets are omitted with an explicit issue. No business totals are guessed.
- PDF reads extractable text in requested page windows. Blank/failed pages need
  `get_file_pages` when vision is available. Text extraction does not verify layout.
- Errors distinguish missing worksheets, encrypted PDF/Office files, oversized expanded
  workbooks and invalid formats. Unknown worksheet names retain the legacy first-sheet
  fallback in preview mode; cursor mode rejects them to protect scope identity.

Per response: up to 200 rows or four PDF pages, and approximately 12,000 serialized
record characters. An oversized row/page/text unit continues by character offset.
Successful windows are deterministic. A resource-limit failure returns no window;
it never advances the cursor or turns an incomplete extraction into success.
Metadata/fences and transport encoding add overhead beyond the record budget.

Hard source limits: 10 MiB input, 64 MiB expanded XLSX, 32 worksheets, 100,000 rows per
worksheet, 128 columns, 500 PDF pages. Omissions beyond these limits are explicit and
have no continuation for the omitted range. A later supported worksheet can still be
read after a row limit. CSV fields up to the bounded input size can continue as fragments; malformed
fields return a parse error. Worksheet labels over 128 characters are rejected.

Each call rechecks File permission and current bytes. There is no raw file cache.
PDF extraction skips earlier pages. Streaming XLSX/CSV parsers still traverse earlier
rows to reach the cursor; positional continuation avoids full-file extraction, but
is not random-access XML. Each exact parse runs in a disposable process: 12 seconds
wall time, 8 seconds CPU, and 512 MiB address space on Linux. The parent kills and
reaps a timed-out child. macOS enforces CPU/wall limits but not address space.
The child loads one workbook and streams formula-cache/visibility XML separately.
Oversized PDF pages are still re-extracted per fragment; limits bound each attempt.
These limits cover exact mode; legacy preview retains its existing parser behavior.

## Compatibility, operations and rollout

New plugin + old backend: bounded preview only, preserving the backend's notes and
page diagnostics. A continuation on that backend is an explicit error, never the file
head substituted for the tail. New backend + old plugin: legacy preview; attachment
hints promise continuation only **if returned**. Either rollback is safe for data,
but an in-progress cursor cannot resume until a compatible backend is restored.

Set site config `jarvis_file_sections_enabled` to JSON `false` to disable exact reads.
Requested exact reads degrade to previews and emit a disabled event. A continuation
is rejected by the plugin because a preview cannot satisfy its range. Default is enabled. No new fleet image or llm-task capability
is required. Server events use `frappe.logger("jarvis.file_read")`; plugin events record
closed outcome/contract classes, numeric cursor/range, known issue codes and elapsed
milliseconds on success, validation failure, HTTP error, timeout and cancellation.
Requested cursors, closed contract-rejection reasons and version prefixes correlate
failed continuations without logging source text.
Document content, file paths and provider response bodies are excluded from logs.

Each normal tool call creates the existing chat receipt; source content therefore
follows normal conversation retention and deletion, not a separate analysis cache.

Large exact reads still consume context. Some Anthropic API-key routes clear older
tool results. The agent is instructed to retain task-relevant evidence with source
positions in assistant working notes, re-read exact cursors when needed, disclose
lost/incomplete evidence, and give periodic visible progress. These instructions are
not a guarantee of model behavior or protection from context exhaustion. The model
is told to stop after 20 windows or 100,000 returned characters across the task,
whichever comes first, and state read/unread scope. This is a prompt instruction,
not an enforced runtime counter. Retain headers with their source positions; reread
them before interpreting later rows if they have been pruned. Large table totals
require a separate server-side aggregate/filter feature, not arithmetic over samples.

## Contract fixture maintenance

The backend owns `file-reading-v1.json` and `file-reading-v1-cases.json`; copy
specimens into the plugin after backend verification. The matrix covers actual
complete/window/partial responses, fragments, multiple sheets and empty sources.
An empty worksheet carries `{source_empty: true}` rather than an empty non-final
window. Backend tests compare generated structures; prose is excluded. ZIP timestamps
make XLSX version values variable, so matrix comparisons exclude those digests;
separate tests verify identity/bytes/scope/revision invalidation.

Any change to a validated shape, status vocabulary or cursor grammar requires a
new contract name. Keep serving deployed contract versions, or degrade unknown
read modes to bounded previews. A plugin rejects a preview on a continuation.
Extraction changes within a compatible shape increment the scanner revision,
invalidating old cursors with an explicit restart error. The version binds File
identity, so reattaching identical bytes as another File intentionally restarts.
Cursors are positions, not MAC-authenticated traversal receipts: callers can request
valid non-boundary positions. Such a read cannot prove prior rows were visited.

Initial reads may omit cursor/version or send both empty strings/null. Empty or
null file_url, filename and sheet also mean omission. Continuations require both
nonempty tokens copied from next_read. Obsolete numbered sections are rejected.
