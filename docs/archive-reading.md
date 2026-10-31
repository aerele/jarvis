# ZIP, GZ, and prepared report reading

ZIP and GZ reading is off by default. Set the site key `jarvis_archive_reading_enabled` to `true` only after the backend, plugin, and effective tool permissions have been checked. Turning this key off stops ordinary archive reads. It does not remove the independent native prepared report handoff or the shared XLSX integrity checks.

## Supported input

A ZIP can contain XLSX, CSV, TSV, JSON, MD, Markdown, and TXT files. A standalone GZ can wrap one of these formats. Directories are allowed in ZIP files. A bare `.gz` needs an explicit supported `payload_format`. The reader does not use the gzip header name to select a parser.

Nested archives, GZ members inside ZIP, TAR, encrypted members, links, and unsupported compression methods are refused. PDF and image members appear as unsupported inventory entries. No file is extracted to disk or saved as a new Frappe File.

The compressed input limit is 10 MiB. Each decoded file has a 10 MiB limit. Selected decoded files share a 32 MiB limit. XLSX internal streams share a separate 64 MiB limit, both per workbook and across the selected workbooks. ZIP allows 32 data files and 128 total entries. Unsupported macOS resource-fork entries stay in inventory but do not consume data-file slots. They remain unread and prevent a whole-archive completion claim. Selected data shares limits of 32 tables, 100,000 stored rows or nodes, 2 million cells, and 128 columns per table. A response has at most 200 displayed rows and 16,000 characters of worker data. The child has a 16-second CPU limit, 22-second wall limit, and 512 MiB address-space limit. These are refusal limits, not capacity guarantees for every file shape.

## Query flow

1. Call `query_file(file_id=...)` to get `archive-manifest-v1`. Inventory contains member IDs such as `m1` and `m2`, plus `archive_version`. It has no row count, view version, or cursor. Inventory does not read content.
2. Call `query_file` with the same exact File, `archive_version`, and selected `members`. Omit SQL. Inspect the source tables, header samples, and returned view `version`.
3. Use the same File, members, archive version, worksheet scope, payload format, and view version for a read-only SELECT. For example, `m1_sheet1` and `m2_sheet1` can be joined or combined in one SQLite query. All selected data loads before the query runs.
4. Read the result coverage. `coverage.complete` refers to the selected member and worksheet scope. `archive_complete` also requires every archive member and all worksheets. `unread_member_count=0` alone does not prove all worksheets were read. Display limits can hide result rows even when source loading is complete.

Headers and subtotals remain in source tables. The query must identify invoice rows, currencies, signs, and total rows. Use the decimal functions for money. JSON node counts and Markdown line/table counts are storage units, not invoice counts.

File names, cells, document text, and all returned source values are untrusted data. The plugin fences these values separately from fixed coverage guidance.

## Exact reading

`read_file` first returns the same inventory. Select one `member` and pin `archive_version` to start `archive-cursor-v1`. Follow the exact `next_read` arguments. They retain File ID, member, archive version, cursor, view version, payload format, and worksheet scope.

A final window does not prove earlier windows were read. It also does not cover other members. The existing task limit remains 20 windows or 100,000 returned characters. Use a query for counts and totals over larger sources. Report any unread ranges.

The server checks the whole selected compressed stream before it returns the first window. A damaged last member prevents a multi-member query from returning a partial total. Integrity checks read actual stream EOF, length, CRC, and ZIP extents. They do not trust ZIP size headers or `ZipFile.testzip()` alone.

## Native prepared reports

A File attached to a Prepared Report follows report access checks before any generic File decoding. A valid reference returns `prepared-report-handoff-v1` with the exact run ID for `query_report`. A denial cannot fall through to the generic decoder. A separately uploaded copy remains an ordinary File.

A completed `run_report` response now returns metadata with `read_status=not_read`. It does not decode rows or claim a row count. Use `query_report` for full source inspection, then use its pinned version for SELECT. A missing or corrupt attachment does not cause `run_report` to generate another report. The chat report card stays open until an actual query answer loads the complete permitted source. Existing Customer update previews and approvals remain in force.

## Deployment and recovery

Deploy the backend with the archive key off, then deploy the plugin. Apply the Admin standard-tier patch and refresh affected role profiles and agent bundles. Confirm both `query_file` and `query_report` in the effective permissions for a fresh run. Old AgentRun capability snapshots are not rewritten. The private bundle registry and its pinned capability digest must match the bench registry.

Preserve exact File IDs in ordinary and shared chat attachments. A URL shared by multiple File records needs an exact ID. A request with mismatched references is refused.

Keep archives off if an old plugin/backend pair drops scope arguments, if required capabilities are absent, or if supported-runtime checks are incomplete. A rollback must restore the matching backend/plugin pair. Shared XLSX integrity hardening is a separate change from the archive key.

Local evidence is recorded in the implementation work area. Native Frappe 15 execution and full model-driven chat journeys still need release verification. The pinned producer fixtures below cover selected export cases. These gaps do not permit a production-ready claim.

## Integrity and retention review

The internal XLSX stream validator also runs on ordinary Excel reads while the
archive switch is off. This closes a stream-truncation defect; disabling it would
restore that defect. It is a separate rollout concern from ZIP/GZ enablement.
The validator also limits a workbook to 4,096 ZIP entries. This is a resource
ceiling, not a statement that every valid workbook has fewer parts. Representative
Office export checks remain required; no permissive integrity fallback is used.

The offline test matrix commits eight pinned public fixtures from LibreOffice's
XLSX test corpus and one Google Sheets Quickstart export. Producer metadata covered Excel, Macintosh Excel, and LibreOffice. Both
plain query and exact-scan child paths passed. The Excel 2010 fixture exposed an
empty deflate thumbnail with no file-data bytes. The reader now accepts that
zero-byte ZIP case only when the verified size and CRC are zero. Nonempty streams
still require full EOF and integrity checks. A synthetic regression also proves
that the final worksheet row is preserved and incorrect size/CRC values fail.
The Google Sheets export contains the public Class Data sample linked in Google's
API Quickstart. SHA-256 values and immutable LibreOffice blob references are in
`jarvis/tests/fixtures/xlsx_producers/manifest.json`. Both real child paths run in
the tests with archives disabled. The matrix covers these files, not every valid
workbook or every Numbers, ClosedXML or ExcelJS export.

All source workers disable core dumps before reading input. The 512 MiB
address-space cap applies on Linux. macOS does not provide the same address-space
guarantee. See `source-reading-retention.md` for new tool-receipt minimization,
re-query behavior, historic records, and native-runner limitations.

URL-only lookup uses Frappe's permission-aware File listing. Exact attachment File
IDs remain the preferred reference, including native prepared-report handoff.
Missing, ambiguous, and unreadable URL references use the same denial text.

## Paired release order

Pause chat and agent traffic while deploying this release group. Deploy backend
#2245, #2247 and #2262 first; run `bench migrate` and restart backend workers. Then
deploy plugin #136, #137 and #138 and restart the gateway. Check the matching tool
contract digest, source-read hooks, a prepared-report handoff and an archive
canary. Keep archive reading off until those checks pass.

The 21 reader bundles bumped to 0.3.1 require `jarvis__query_report`. Deliver those
bundles and the Admin #794 provisioning patch only after the backend and plugin
register that tool. Refresh role profiles and check effective permissions on a
fresh run. Existing AgentRun capability snapshots are not rewritten. Resume
traffic only after the paired checks pass.

URL-only references remain refused when multiple readable File rows match. The
exact File ID disambiguates the attachment and preserves prepared-report access
checks. Updated attachment hints include that ID. Do not pick an arbitrary File
row to make a legacy URL succeed.
