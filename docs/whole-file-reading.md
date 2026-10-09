# File cursor reading

`read_file` returns exact extracted values in bounded windows. For whole-file tasks,
the agent follows `next_read` sequentially across files. The customer does not manage
pagination. There is no background job, cache, submodel or internal polling loop.

## Wire contract

The plugin sends `read_mode: "file-cursor-v1"`; an explicit plugin `preview: true`
becomes `read_mode: "preview"`, with **no `preview` key on the wire**. That key is
reserved for write dry-runs by the shared API. Existing backend callers still default
to preview. The model-facing schema exposes `cursor` and `version`, never read_mode.

The backend capability marker is `read_capability: "file-cursor-v1"`. Exact responses
also carry `read_contract: "file-cursor-v1"`. These markers deliberately differ from
the previous, unmerged numbered-section prototype. Unsupported/malformed contracts
are errors. An old backend returning a preview cannot satisfy a cursor request.

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
On timeout/network/HTTP failure the requested range was not received: retry the same
arguments sequentially, waiting after 429. Stop ends the client request; it does not
promise to preempt parser work already running on the server. Restart without a cursor
if the file, reader or worksheet scope changes. No new retained state needs cleanup.

Fragments include `offset`, `fragment` and `encoding` (`text` or `json-row`). Concatenate
fragments for the same unit in offset order; decode json-row only after reassembly.
Normal rows are native arrays, avoiding nested JSON strings. The plugin separates
trusted continuation metadata from a fenced JSON source block, escaping markup so
filenames, worksheet names and cell text cannot close the untrusted-data fence.

## Extraction and resource limits

- CSV/TSV preserves row 1, localized amount strings and closing totals. Small UTF-8
  attachments still inline in the initial prompt. Larger files use the tool.
- XLSX uses cached formula values. Missing cached values are null and reported with
  their row/column; recalculate and save the workbook to populate them. Cached values
  may be stale if the producer did not recalculate. No business totals are guessed.
- PDF reads extractable text in requested page windows. Blank/failed pages need
  `get_file_pages` when vision is available. Text extraction does not verify layout.
- Errors distinguish missing worksheets, encrypted PDF/Office files, oversized expanded
  workbooks and invalid formats. Unknown worksheet names retain the legacy first-sheet
  fallback in preview mode; cursor mode rejects them to protect scope identity.

Per response: up to 200 rows or four PDF pages, and approximately 12,000 serialized
record characters. An oversized row/page/text unit continues by character offset.
There is **no total-window cap** and no wall-clock cutoff changing the reachable range.
Metadata/fences and transport encoding add overhead beyond the record budget.

Hard source limits: 10 MiB input, 64 MiB expanded XLSX, 32 worksheets, 100,000 rows per
worksheet, 128 columns, 500 PDF pages. Omissions beyond these limits are explicit and
have no continuation for the omitted range. A later supported worksheet can still be
read after a row limit. CSV parser field-size limits also apply; malformed/oversized
fields return a parse error. Worksheet labels over 128 characters are rejected.

Each call rechecks File permission and current bytes. There is no raw file cache.
PDF extraction skips earlier pages. Streaming XLSX/CSV parsers still traverse earlier
rows to reach the cursor; positional continuation avoids full-file extraction, but
is not random-access XML. Input/expansion limits bound parser resources; existing RPC
and turn timeouts remain in force. A pathological single parser operation can time out.

## Compatibility, operations and rollout

New plugin + old backend: bounded preview only, preserving the backend's notes and
page diagnostics. A continuation on that backend is an explicit error, never the file
head substituted for the tail. New backend + old plugin: legacy preview; attachment
hints promise continuation only **if returned**. Either rollback is safe for data,
but an in-progress cursor cannot resume until a compatible backend is restored.

Set site config `jarvis_file_sections_enabled` to JSON `false` to disable exact reads.
Previews remain available. Default is enabled. No new fleet image or llm-task capability
is required. Server events use `frappe.logger("jarvis.file_read")`; plugin events record
closed outcome/contract classes, numeric cursor/range, known issue codes and elapsed
milliseconds on success, validation failure, HTTP error, timeout and cancellation.
Document content, file paths and provider response bodies are excluded from logs.

Each normal tool call creates the existing chat receipt; source content therefore
follows normal conversation retention and deletion, not a separate analysis cache.

Large exact reads still consume context. Some Anthropic API-key routes clear older
tool results. The agent is instructed to retain task-relevant evidence with source
positions in assistant working notes, re-read exact cursors when needed, disclose
lost/incomplete evidence, and give periodic visible progress. These instructions are
not a guarantee of model behavior or protection from context exhaustion.

Before release, verify on a non-production live tenant: a multi-window read (including
an Anthropic API-key route), three attachments in one message, Stop/re-ask on desktop
and PWA, and a new plugin with a hotfix backend. Confirm tail evidence, preserved
working evidence after pruning, visible progress, and truthful partial results. Local
parser, API and mocked transport tests do not establish those live outcomes.

## Contract fixture maintenance

Both repositories test identical bytes in `file-reading-v1.json` (plugin
`tests/fixtures/`, backend `jarvis/tests/fixtures/`) with an asserted SHA-256:
`02b02d5933c92c7af789b57847a29cbc6b5be78b77e62ed11c94f2920071a915`.
The backend also compares the specimen with actual reader output. Update both copies
and both asserted hashes intentionally when changing the contract. Increment the
scanner revision for incompatible extraction semantics so old cursors fail closed.
