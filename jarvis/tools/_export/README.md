# Background exports (admin-v2 issue 733)

`query` probes one row beyond its requested limit and also checks the model's
35,000-character budget. Partial answers receive a random owner-bound Redis ID
lasting 24 hours. The cache contains the validated spec, without the default limit/offset (an explicit top-N limit survives a response-size truncation),
owner, creation time and fingerprint; never rows. `export_query` accepts exactly
one of that ID, a spec, or doctype/filters. Explicit spec limits remain top-N.

The existing immediate route remains, up to 20,000 rows (a site can only lower
that, see `jarvis_export_immediate_row_limit` below). A row ceiling or time-budget
refusal creates a durable `Jarvis Export Job` and an ordinary assistant message.
The job stores its own input, independent of Redis. A repeated request in the same
chat joins active work, or reuses a file made within ten minutes after recompiling
current permissions. Reuse also compares the permission-woven SQL and masking plan.
Highlight options are included in the dedup key to avoid returning a differently
formatted file for otherwise identical requests.

## Execution and safety

`streaming.prepare` compiles SQL under the owner, checks every contributing
DocType's Export permission, and loads labels, DocField/computed types, masks,
highlight rules and system formats. `streaming.write` does no database or
permission lookups. CSV writes rows directly; xlsxwriter constant-memory mode
buffers only the first 1,000 rows and then spools worksheet XML. Those rows fix each
column's kind and width, and every cell is then formatted by the immediate export's
own rule (`compat.xlsx_cell_style`): bold frozen header, filter, grouped numbers, the
site's date and time formats, tab named after the export. The site's float precision
no longer applies (decimals show as `#,##0.00`, as in an immediate export). An
abandoned workbook (too large, cancelled, failed) is never finalised: the spool is
dropped with the attempt directory instead of being zipped.
Conditional formatting uses the final range, not retained result rows.
Xlsxwriter is an explicit dependency on both Frappe lines.

Filter exports use the installed Frappe list compiler. Both release lines also
constrain child rows by parent type and the parent's record permissions; a
DocType-level Export grant never widens that record scope. Modern query-builder
SQL retains its bound parameters. Legacy SQL already contains escaped literals
and is executed without a parameter mapping, preserving literal percent signs.

The MariaDB adapter uses a dedicated PyMySQL unbuffered cursor even on a bench
whose normal driver is mysqlclient. SQL is compiler-generated,
with a cap-plus-one probe. Its second connection checks cancellation, migration
table/metadata-lock waiters, and the deadline every five seconds and writes the
heartbeat/row count. Only this attempt's dedicated connection ID can be killed.
The stream socket closes before any cursor can drain. The watchdog remains
active until the worker acknowledges cancellation, including a cancel/SELECT
startup race. Wall-clock checks also run every 1,000 rows and after finalization;
correctness does not depend on how MariaDB charges time spent sending results.

Progress on the card. Before streaming, the attempt counts the exact SQL the stream
will run (`MariaDBStream.count_rows`): on its own connection, after the watchdog starts
so a cancel or a site update kills the count too, under a server-side three second limit
(`SET STATEMENT max_statement_time`, the ten minute session limit is untouched). It counts
output rows, so permissions, GROUP BY, DISTINCT and the LIMIT all apply, and the result
becomes `total_rows` (0 means unknown). A count that times out or fails is silent: the
card keeps its activity bar and row count, with no percentage. A count above the row cap
ends the attempt "too large" at once, with the stream's own message; when the count does
not answer, the stream still stops at the cap. Join specs that select the same column name
from two tables unaliased fail the count (MySQL error 1060) and never get a percentage.
The card shows rows against `total_rows`, stops at 99% until the file is ready, and shows
no percentage before the first rows are written or when the count is stale. After the last
row `write` records `phase = finalizing` before it closes the workbook (the zip) and
publishes; the card then shows "Finalizing" with an activity bar. `total_rows` and `phase`
are shown only while the job is running; the ready write clears `phase` and keeps
`total_rows`. How often the count answers: ready jobs with a count over all ready jobs,
`SELECT SUM(total_rows > 0) / COUNT(*) FROM `tabJarvis Export Job` WHERE status = 'ready'`.
The outcome of each count is on the export telemetry line as `detail`: `count:ok`,
`count:1969` (timed out), `count:1317` (killed), `count:1060` (duplicate column),
`count:<errno or exception class>`, `count:none`, or `count:skipped` (the attempt ended
before counting). Never SQL.

Files stay in private attempt directories until complete. Final private files
have unique names and are attached only to the owner-scoped job. Registration
uses the supported File `copy_from_existing_file` lifecycle, avoiding a second
in-memory copy, content deduplication across users, and the upload-size ceiling.
The completed archive still must pass the generated-export size ceiling.
Cleanup deletes unshared generated bytes after commit; Frappe's content-hash
cleanup alone does not remove these prewritten files.

Each attempt has a random token. Worker updates/publication require that token
and a running, uncancelled job. A reaper revokes stale attempts before retrying;
late workers cannot publish. One retry is shared across worker death (immediate
after detection) and yielding to migration (five minutes). Ordinary failures,
timeouts and size/permission refusals do not retry.

Lifecycle timestamps (heartbeat, start, finish, retry and file expiry) are UTC
in storage and include an explicit UTC offset in responses. The watchdog uses
the same application clock as recovery, independent of MariaDB/session/site
time zones. Framework-managed creation timestamps retain Frappe's site-clock
semantics for the thirty-day job retention cutoff.

A short site database lock serializes deduplication and dispatch. Capacity is
one export per owner and `max(1, live long workers - 1)` per site. Zero workers
leaves jobs queued. A one-worker site cannot reserve capacity for other long
jobs. Maintenance mode blocks background admission/dispatch. Other database
engines refuse the background route and retain their immediate route.
Dispatch filters busy owners, cancellation requests and future retry times
before selecting the next job, so a large ineligible backlog cannot hide work.

## Delivery and retention

The shared Vue card batches job-state reads, listens for user-scoped realtime
updates, and refreshes on focus/reconnect. It shows row count/elapsed time rather
than a speculative percentage. Cancel is owner-only and normally observed within
five seconds. Completion, failure and pause messages use the existing
conversation-lock/message-sequence allocator with a per-job/state key. Message
and Desk `Alert` notification insert in the same transaction; `Alert` skips
email on both supported Frappe versions. Reaper reconciliation repairs missed
notifications. Initial messages keep a durable title/row/date fallback after
job deletion. Repeated calls record status demand; conservative English chat
patterns record status/text-cancel demand without storing message text.
Periodic polling includes active jobs only, plus a one-time refresh when a ready
file reaches expiry; hidden tabs skip periodic requests. Terminal cards still
refresh on focus/reconnect/realtime events. State reads batch job and File rows.

A job belongs to exactly one chat. Its initial card is created in the chat that asked,
and every state message goes only to that chat; the owner gets one Desk bell per
state. A repeated request in the same chat joins the active job (or reuses a file
finished in the last ten minutes) and inserts nothing. The same request from another
chat starts its own job and queues behind the first (one export per owner). A caller
with no chat (a macro, a delegate, a plain API call) cannot start a background export:
the call is refused with "Start it from a Jarvis chat so the file can be delivered
there", together with the original narrowing advice. Cancel from the card cancels the
job. Delivery locks the conversation, then the job; publication locks them in the same
order and cancels the job if its chat was deleted. Deleting the chat revokes the
attempt, removes its file and deletes the job.

Files are kept for 30 days by default (`jarvis_export_retention_days`; 0 keeps them
until the chat is deleted). A finished file is a private File in the "Jarvis Exports"
folder (`Home/Jarvis Exports`, created on `bench migrate`, or by the first publish on a
fresh install; if it cannot be created the file is filed under Attachments instead),
named `jarvis-export-*` and attached to its `Jarvis Export Job`, not to the chat.
Retention deletes only the File linked from the job row. Expiry is quiet: no chat
message and no bell. The card shows its own "Expired" state, and the job row is kept
for 30 more days after it expired, then deleted. Lowering the setting also applies to
files already stored (raising it never extends them). Jobs that did not produce a file
(too large, too slow, failed, cancelled) are deleted 30 days after creation; a
`ready` row never ages out, it leaves only through expiry or by deleting the chat.
Failures are written to the Error Log with titles starting `jarvis.export` (they name
the job and never contain its source filters). A failure inside a web request (the
enqueue or cancel call) goes to the `jarvis.export` log file instead. The existing
five-minute cron (`reap`) also recovers dead workers, removes stale attempt
directories and uniquely prefixed files orphaned between filesystem rename and
transaction commit, and dispatches queued work. Immediate exports have no new
retention policy.

## Deployment and verification

Order matters. On each bench:

1. Frappe 15 benches: run `bench setup requirements` (or
   `pip install 'xlsxwriter>=3.2,<4'`) before restarting. Without it, Excel background
   exports are refused with a clear message and CSV still works. Verify with
   `env/bin/python -c 'import xlsxwriter'`. (The dependency is declared in
   `pyproject.toml`; Frappe 16 already provides it.)
2. Run `bench migrate` before restarting web and workers (new DocType, scheduler
   hook, and the "Jarvis Exports" folder).
3. Build both frontends (`npm run build`).
4. Restart ALL workers including `long`, and confirm at least one `long` worker is up
   (`bench doctor` / the RQ worker list).
5. Deploy the plugin's `result_id` schema/description and the regenerated persona
   contracts together, after the benches have this code. The v15/v16 hotfix backports
   preserve their branch-specific code.
6. Smoke: run one background export per line (CSV, and xlsx on Frappe 15), confirm
   the card reaches Ready, and search the Error Log for `jarvis.export` (not the old
   "Background export failed").

Background exports share the `long` worker with chat; benches with a single `long`
worker should add one. Capacity is `max(1, live long workers - 1)`, so a one-worker
bench runs an export on that worker and chat turns wait behind it.

Before a migration, allow active exports to finish where practical; the lock
watchdog remains the backstop because migrate need not set maintenance mode.

Site configuration (`bench --site <site> set-config -p <key> <value>`; `-p` stores a
number rather than a string):

| Key | Default | Meaning |
| --- | --- | --- |
| `jarvis_background_export_disabled` | off | Kill switch. A truthy number turns background exports off; `0`, a `"0"` string or removing the key turns them on. Immediate exports are unaffected |
| `jarvis_export_retention_days` | 30 | Days a finished file is kept; 0 keeps it (and its job row) until the chat is deleted. Lowering it also applies to stored files |
| `jarvis_export_immediate_row_limit` | 20000 | Rows an export may hold before it hands off to a background export. Lower-only: values above 20,000 are clamped to 20,000, and an unusable value (not a positive whole number) means 20,000 |
| `jarvis_export_row_limit` | 200000 | Background row ceiling, up to 200000 |
| `jarvis_export_max_mb` | 50 | Final generated file ceiling, independent of upload limits |
| `jarvis_export_spool_max_mb` | 1024 | Separate local XML scratch-space ceiling |

With the kill switch on, new background exports are refused (the caller still gets the
narrowing advice), queued and paused jobs stay queued and are not dispatched, and an
attempt that was reserved just before the switch was flipped goes back to the queue
without spending its retry. An export that is already running finishes, and the
reaper, cancel and expiry keep working. Switching it off resumes the queue.

### Rollback drill

1. Set `jarvis_background_export_disabled` to 1 (`bench --site <site> set-config -p
   jarvis_background_export_disabled 1`): new exports are refused and the queue is
   frozen.
2. In `bench console`, set every `queued`/`paused` Jarvis Export Job to `cancelled`
   with the error "Background exports were turned off on this site."; commit.
   Otherwise they run days later after a redeploy. While this code is still deployed
   the reaper tells each chat.

   ```python
   from jarvis.tools._export.clock import now_datetime
   for name in frappe.get_all(
       "Jarvis Export Job", filters={"status": ["in", ["queued", "paused"]]}, pluck="name"
   ):
       frappe.db.set_value("Jarvis Export Job", name, {
           "status": "cancelled",
           "error": "Background exports were turned off on this site.",
           "finished_at": now_datetime(),
       })
   frappe.db.commit()
   ```
3. Wait until no row is `running` (Desk list, System Manager), or cancel those from
   their cards.
4. Decide on ready files: keep them, or delete the Files in "Home/Jarvis Exports".
   After a revert nothing expires them.
5. Revert the content publish (plugin and persona), then the bench. RQ `long` jobs
   that still reference the removed module fail harmlessly; none should be left after
   step 3.
6. Sweep `private/jarvis_exports` (attempt scratch) and any `jarvis-export-*` files
   left in `private/files` by hand.
7. A `migrate` on the reverted code removes the DocType record
   (`remove_orphan_doctypes`) but leaves the `tabJarvis Export Job` table; drop it by
   hand only if the feature is not coming back.

The worker has a ten-minute deadline and a fifteen-minute RQ timeout. CSV checks
bytes while writing. XLSX incrementally measures compressed XML and checks the
final ZIP before publication; the separate scratch ceiling prevents highly
compressible data from exhausting the local disk. A scratch-ceiling refusal is
explicit and never delivers a partial file.

Regression coverage lives in `test_background_exports.py`: row and character
truncation, owner/expiry checks, 200,000-row streaming with `frappe.db.sql` patched
to fail, formula safety, highlights, cap/size refusal, real SQL cancellation,
private File publication, job/reaper deduplication, one shared retry, capacity,
current-permission reuse, expiry and conversation deletion. Existing query and
export suites protect immediate behavior. `ExportCard.spec.js` covers batched
state, cancellation, download URLs and old-card fallback. Browser checks must
also cover desktop and phone widths, dark mode, reload and multiple tabs.

Known scope boundaries from the issue remain: query's separate model-facing
masking work, the immediate export's byte-identical-file behavior, PostgreSQL,
agent-driven cancellation/listing, and PWA Desk-bell support are separate work.
