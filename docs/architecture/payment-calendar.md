# Payment Calendar source and snapshot contract

The source foundation and snapshot service supply company configuration and
permission-checked POST APIs. The native calendar is a separate UI change.
Accounting documents and existing report results are unchanged.

## Supported source and completeness

The adapter supports matching Frappe/ERPNext v15 and v16 pairs. Its allocation
contract was checked against ERPNext 15.124.1
(`4e0dce9ea8d351cd7023ad5ed66faf3cdec0680e`) and 16.50.0
(`7474d9e786277383de1242ab882f16856d17a9c9`), with Frappe 15.124.0
(`f1cf50cf9bbc641bb015adb7008bf2ea1c8483ab`) and 16.51.1
(`2871914b69b6e14a4b023d9b7cc52dc3e269cc1b`). Unknown or mismatched majors fail
closed. It calls the canonical `ReceivablePayableReport` allocator rather than
copying its maths. The inspected allocation methods are identical; the invoice
schedule classifier accepts only each version's audited controller source hash.
The v15 schedule differs in initial numeric normalization and its settings
helper, while the classified advance and withholding formulas are unchanged.
Standard Supplier/Customer Party Type mappings and company-side accounts are
required before invoking the report: ERPNext otherwise skips common filters.
Incomplete accounting setup fails closed. Method signatures and required runtime
attributes are checked; incompatible
internals and unknown ERPNext fetch modes fail with `CalendarSourceUnavailable`.
Both supported cursor modes use the canonical allocator. Float residues rounded
to zero are represented once, without duplicating settled terms. Row IDs are
local to a snapshot and must always be paired with that snapshot identity.

Open residuals include all source history through the accounting date. Settled
Purchase/Sales Invoice terms are captured before allocation mutates the source,
then retained within the requested due-date window. Settled Journal Entry bills
are outside this overlay; their open residuals still reconcile. A zero residual
is **Settled**, without a claim that cash cleared a bank. Credits, discounts and
negative unallocated balances retain their accounting meaning and sign.

Report permissions and explicit invoice document-query permissions apply to both
open and settled rows. Every configured accounting party type also passes its
master's read-or-select query scope. The allocated invoice residual is checked
against its independent pre-mutation ledger residual, so a malformed schedule
cannot redefine the total. A restricted/custom scope sets `complete_for_company` to
false. Consumers must withhold whole-company cash-health colours for incomplete
obligations; permitted due rows alone do not establish liquidity. Unsupported
allocation/FX reconciliation and non-default finance books fail closed.

AP/AR and selected-bank sources accept only NULL, blank and the company default
finance book, using their canonical unfiltered paths. Account balances are signed
company-currency book values. Selected accounts must be readable, enabled leaf
Bank/Cash accounts of the same company. Fiscal coverage is required; missing
coverage cannot become zero cash. Request form fields are isolated from the
balance helper. Existing ledger/dimension gates remain authoritative.
The v15 bank helper has no finance-book keyword arguments; the adapter omits
only those arguments after the same alternative-book rejection. The bank helper
also changed within v16: 16.50's default call includes all books, while 16.52
requires explicitly including the company default. Conversely, that flag excludes
blank books on 16.50. Exact audited helper-source hashes select each variant's
arguments; unknown helper implementations fail closed for bank balances. The
guard and canonical call together retain the same NULL/blank/default-book union.

Frappe/ERPNext v15 does not apply the v16 canonical Sales Person user-permission
filter. For v15 viewers with Sales Person restrictions, receivables are
unavailable with `restricted_obligations`; the adapter refuses them before source
execution and again during cached delivery. Permitted payables and bank balances
remain available, but whole-company forecast colours remain unavailable. This
restricted case does not claim the same permitted-row coverage as v16.

## Bounds and measured cost

Each side is limited to 100,000 active Payment Ledger rows through the anchor and
10,000 positive-invoiced source balances across **all history**, including settled
invoices. These are safety bounds, not visible-calendar row limits. Larger
companies receive `too_large`; no source is truncated to produce colours.

Synthetic transactional `test_jarvis` benchmarks on MariaDB 10.11.14:

| Case | Rows / invoices or terms | Wall time | Traced peak | SQL calls |
| --- | --- | --- | --- | --- |
| Open AP adapter | 50,501 / 2,501 | 20.384 s | 108.12 MiB | 5,378 |
| Settled AP at caps | 100,000 / 10,000 | 85.272 s | 170.19 MiB | 20,377 |
| Settled AR at caps | 100,000 / 10,000 | 79.029 s | 164.13 MiB | 20,087 |

Tracemalloc was enabled. Whole-process peak RSS including fixture creation was
493–511 MiB; it is not an isolated measurement of source-worker RSS. Both sides
must run sequentially in a long background job, with a 300-second initial worker
budget and deployment-specific memory/concurrency checks before pilot. No source
work belongs in a synchronous dashboard request. Restricted-user post-filters can
add queries; production capacity is not established by these fixtures.

## Company floor and coherent reads

`Jarvis Payment Calendar Settings` has a stable random ID and unique Company
link, so Company link renames do not orphan the floor. Its currency and revision
are server-owned. Missing configuration or changed company currency is
unconfigured, not a default zero. Writes, including ordinary Desk/REST document
saves, require Accounts Manager/System Manager plus Company write access.
Optimistic revisions and a Company row lock serialize concurrent first creation.
The read contract also returns `configuration_id`; consumers must bind both this
ID and the revision, since deletion/recreation starts a new revision sequence.

The snapshot job pins one primary connection with explicit repeatable-read and
`START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY`. Helper writes,
transaction controls and connection replacement fail closed. The generated
timestamp marks the start of that read view, not the later completion time.
Independent committed test connections cover supplier/customer payments,
cancellation and floor revision changes; post-read authorization refuses an old
floor even when the financial view itself is coherent.

Canonical helpers retain their cache-miss logic. For the sequential RQ source
window only, allowlisted Company/Accounts Settings/System Settings documents,
global defaults and fiscal-year caches miss locally and never fill shared caches.
Frappe v15 global defaults use the `defaults` hash's `__default` member; v16
uses the `defaults::__default` key and client cache. Each supported profile must
prove that its actual defaults path was bypassed. Field-mask metadata is included
where the framework provides native masks; v15 has no native mask column.
Role/User Permission fills remain private to that source window. Wrappers are
restored on every exit; this context must never run in a threaded web request.
Tripwires check the required helper-cache paths and source precision. Schema,
hooks and permission metadata remain part of the maintained versioned contract.

## Visibility

The SPA shows the Payment Calendar link (welcome screen) and card (Dashboards,
Saved tab) only when the boot flag `can_view_payment_calendar` is `true`.
`jarvis/www/jarvis.py` sets it from
`jarvis.payment_calendar.visibility.can_view_payment_calendar()`. A direct visit
to `/dashboards/payment-calendar` with the flag off redirects, without a
message, to `/dashboards#saved`. The flag is `true` only when all four rules
hold, checked in this order:

1. ERPNext is installed (`jarvis.chat.agent_installability.installed_apps`).
2. The Frappe/ERPNext pair passes `sources.check_contract()`. This covers an
   unsupported version pair and ERPNext source drift. Only a pass or a
   deterministic `CalendarSourceUnavailable` is remembered, per process and per
   version pair; a deploy restarts workers and resets it. Any other result
   (ERPNext cannot be imported, `AttributeError`, an `ImportError` from the
   Jarvis modules) hides the entry points but is not remembered, so the next
   page load tries again.
3. The user can run the standard Accounts Payable report: it exists, is enabled,
   is a standard Accounts script report, the user has a role it allows (Custom
   Role included) and report permission on its reference DocType. Accounts
   Receivable is not required.
4. The user can see at least one Company (`frappe.get_list`).

Every failure gives `false`. The flag is a UX hint only. The calendar API keeps
its own gate (`authorize_scope`, `require_report`), so a stale flag cannot grant
data; it can only show the in-page "Finance access required" or "unavailable"
state. Those states also remain for cases the boot check does not cover: more
than 500 visible companies, native field masks and `has_permission` hooks on
Company. A role change shows after the user reloads.

Quiet cases, with no log row: ERPNext absent, a missing or inaccessible report,
and a user who cannot read Company (`frappe.PermissionError` from the Company
list). Everything else writes one deferred Error Log row at most once an hour
per site and per kind (`jarvis.holiday_touch.report`). There are three kinds:

- `payment_calendar_visibility:contract`: the source contract. A
  `CalendarSourceUnavailable` is reported once, when it is first remembered,
  with a title that names the Frappe and ERPNext versions. An unexpected error
  or an ERPNext import failure is reported on each attempt (not remembered).
- `payment_calendar_visibility:check`: an unexpected exception in the other steps.
- `payment_calendar_visibility:boot`: an exception that reached the boot code
  in `jarvis/www/jarvis.py`, including an import error of the package.

`message_log` is saved and restored, so the check adds nothing to the page
response.

Support recipe, when a user reports a missing calendar:

```python
# bench --site <site> console
import frappe
frappe.set_user("<user>")
from jarvis.payment_calendar import sources, visibility
visibility.can_view_payment_calendar()
```

To see which step hides it, call the steps one by one:
`from jarvis.chat.agent_installability import installed_apps; "erpnext" in installed_apps()`
(rule 1), `visibility._source_contract_ok()`
(rule 2), `visibility._can_run_ap_report()` (rule 3) and
`frappe.get_list("Company", pluck="name", limit=1)` (rule 4). If rule 2 is
`False`, the Error Log row may be an hour old; call `sources.check_contract()` directly to see the
error. After a fix, `visibility.reset_contract_cache()` clears the remembered
result in the console process; restart workers to clear it for the site.

## Template contract (#821)

The calendar UI is built to the design template. The backend adds the pieces
that the template shows and the old contract did not provide. The old keys
stay; the changes below are the exceptions.

### Modules

| Module | Job |
| --- | --- |
| `catalog.py` | The one rule for "accounts this user may total": request gate, catalog list, chunked validation. |
| `balances.py` | The all-account balances job: worker read, status-read gate, flat status body. |
| `bank_labels.py` | Bank name and masked number, read at request time and never stored. |
| `detail.py` | Day and bucket replies: request validation (`Query`), `Page`, `render`, and `index_side`, which builds the stored day/bucket index. The API and the golden dataset both call it. |
| `access.py` | `authorize_scope`, the fingerprint inputs and the shared row check (`verify_returned_rows`). |
| `read_log.py` | Read-path logs: refusals, slow reads and the refusal-loop Error Log. |
| `jobs.py` | Admission, enqueue, worker entry points, safe failure logging for both job kinds. |
| `store.py` | Redis status, parts and hashes, tombstone, admission limits, `split_result`, `public_status`. |

`tests/payment_calendar_golden.py` builds the template dataset through
`snapshot_status_body`, `balances_status_body`, `detail.day_detail` and
`detail.bucket_detail`. The committed JSON in `frontend/tests/payment-calendar/golden/`
is regenerated with `JARVIS_UPDATE_GOLDEN=1`; a golden change needs the backend run.

### Snapshot status body

`get_snapshot_status` returns the lifecycle keys (`snapshot_id`, `state`, `code`,
`queued_at`, `started_at`, `completed_at`, `expires_at`) and, for a complete
snapshot, `projection`, `currency`, `floor`, `generated_at`, `bank_code`,
`unavailable`, `scope`, plus:

- `bank`: `{total, currency, precision, accounts: [{account, account_name,
  account_type, bank, reference, balance}]}` or null without selected accounts.
  The total and balances are the stored values. Names, types and bank labels are
  added when the status is read.
- `horizon`: the stored next-30-days summary `{start, end, forecast_available,
  scope_reasons, days: {date: {projected_closing, margin}}, lowest, first_below_floor}`.
  `lowest` is null unless every day has a closing. The key is always stored; its value is null
  only when the job ran without the horizon (`build_result(with_horizon=False)`, the Task 0 fallback).
- `projection.month_totals`: for a month scope `{receivable, payable, net,
  receivable_rows, payable_rows, complete}`; all values are null when `complete` is
  false (either side incomplete or unavailable). It is null for a scope without a month.
- A day `net` (receivable minus payable remaining) is null unless both sides are complete.

`request_snapshot` takes `month` (`"YYYY-MM"` or `"current"`). Results carry
`schema` 2. `store.split_result` is strict at publish: a result without `schema` 2, a
status key, `horizon` or `projection.month_totals` fails the job with `internal_error`.
No reader accepts an older shape. A snapshot that the code before 821-A completed has no parts:
every read of it answers `expired` once (the client requests a new one).

### Detail and bucket replies

`get_day_detail` and `get_bucket_detail` take `sort` (`remaining`, `due_date`,
`party`) and `order` (`asc`, `desc`). The default is `remaining` descending, which
changes the old order. Rows with no due date come last in both directions; ties
go to `id` ascending. The default order is the stored order, so a default-sort read
slices the stored index and does no sort. Another sort orders only the selected day
or bucket, before paging. Only the rows of the page are made public. Each row has `settled` (original minus remaining where that
is meaningful, else null).

Buckets of `get_bucket_detail`:

| Bucket | Rows | Total |
| --- | --- | --- |
| `today` | Payable, positive remaining, due on the anchor | KPI `due_now.scheduled` |
| `immediate` | Payable, positive remaining, overdue or undated | KPI `due_now.immediate` |
| `now` | Payable, positive remaining, due on or before the anchor, or undated | KPI `due_now.total` |
| `window` | Rows due inside the month window, settled-zero terms included | `month_totals`, null while incomplete |
| `overdue`, `undated`, `outside_horizon` | Positive remaining by due date | Signed reconciliation sum |
| `adjustments` | Negative remaining | Signed reconciliation sum |

`today`, `immediate` and `now` list payables only. A receivable request is
a validation error before any row is read.

### All-account balances ("Show all accounts")

`request_account_balances(company)` admits one balances job per actor over every
account the user may total (`catalog.permitted_accounts`). The scope carries the
catalog and the anchor. The job has its own admission slot (one active job; two for
snapshots), the same `long` queue and 300 s timeout. The worker reads balances at
the scope anchor in chunks of 50 inside one repeatable-read view and stores only
`{generated_at, anchor, currency, precision, accounts: [{account, balance}], total}`.
Names, types and bank labels are added at status-read time. A job that runs
150 s or longer writes a `slow` warning to the `payment_calendar` log.

`get_account_balances_status(job_id)` checks the actor's access on every read:

| Job state | Check |
| --- | --- |
| queued, running | Company gate and Accounts Payable report gate only (no account is read) |
| complete | The same gate, then one validation pass of the result accounts in chunks of 50 |

A refused account discards the job and answers `scope_changed`. So does a result whose `anchor`
is not the site's today (a read after midnight): the balances are as of another day. A balances job leaves
no tombstone, so the next read of that id answers `not_found_or_expired`. A source error from
the gate (`too_large`, `source_contract_unavailable`) keeps the job. The chunked
validation takes one call per 50 accounts. In the account catalog, a chunk that
fails is re-checked one account at a time, so one refused account does not hide
the other 49. Measured on `jarvis_playground` at 500 accounts the completed read
has p95 under 2 s; see the PR text for the numbers.

A balances id is never read as a snapshot id, and a snapshot id is never read
as a balances id; the other kind always answers `not_found_or_expired`.

### Read path (821-A)

Decision: check visibility once at publish, then check the rows each read returns. A hidden row
that the reply does not return does not refuse the read; a returned row that is now hidden does.

**Stage 2 storage.** A snapshot is not one key. `store.split_result(result, scope)` splits it, and
`store.publish_snapshot` writes every piece and flips the status to `complete` in one Lua script
(`_PUBLISH_PARTS`), with the same expiry (`RESULT_TTL`):

| Key (`jarvis:pcal:{owner}:<name>:{sid}`) | Type | Holds |
| --- | --- | --- |
| `status` | JSON | Job row: state, scope, fingerprint (unchanged). |
| `part-status` | JSON | Small reply data (about 40 KB): projection, currency, floor, bank, horizon, `unavailable`, plus the private `_permission_columns` and `_top_proofs`. |
| `part-payable`, `part-receivable` | JSON | The side rows in default order, without `_permission`, `excluded_rows` and `permission_columns`. An unavailable side has no part. |
| `proofs-payable`, `proofs-receivable` | hash | Field = row `id`, value = the row's proof JSON, verbatim. No hash for a side without rows. |
| `index-payable`, `index-receivable` | hash | Field = `day:<iso>` or `bucket:<name>`, value = `{rows: positions (ascending), parties: party summary, total}`. Every bucket of the side is written; a day appears when it has listed rows. |
| `discarded` | string | The tombstone (see below). |

Parts are passed verbatim in ARGV and never decoded in Lua (`cjson` would turn `[]` into `{}`).
The script deletes each hash before it writes it. `store._STORED` lists every key a result can use;
admit (stale entry), purge and discard delete all of them. `store.publish` refuses the snapshot kind;
balances keep one `result` key.

**Reads.** `_safe` refreshes the actor once, `_load` reads the status key (and the status part once
complete) and calls `authorize_scope(..., actor_refreshed=True)` without a `result`: the full policy
fingerprint, scope and mask checks, the report and obligation-side gates; no floor or
scope-reason lookup. Only `get_snapshot_status` on a complete snapshot runs the account gate.
A day or bucket read then:

1. checks the side, and validates the request (sort, order, date or bucket, page size, party flag)
   before any side key is read;
2. loads the side part and one index entry (`EXISTS` + `HGET` in one script);
3. builds the page from the entry: the default sort slices the positions, another sort orders only
   the selection; the party summary and total come from the entry;
4. loads the proofs of exactly the page rows: `EXISTS` + one `HMGET` in one script, none for an empty page;
5. checks the page rows and the listed parties (`access.verify_returned_rows`: the report row filter
   over the proofs, `visible_names` per voucher doctype and party type, `docstatus = 1`, company);
6. renders. Rows lose every private key only in `detail.render`.

Upcoming reads the status part only. Polls of a queued or running job read no side key. A complete
status checks the stored top-invoice proofs of every day (all parties of a multi-party Journal Entry).

**Refusal and discard.** A returned voucher or party that is not visible, a proof the report filter drops,
a fingerprint change or a lost scope refuses the whole read as `scope_changed` and discards the
snapshot: no partial page, because a filtered page would break the totals and leak the hidden amount.
A discard deletes every stored key and sets the tombstone `discarded:{sid}` (per owner, `RESULT_TTL`,
snapshot kind only). A later read of the same id by the owner gets `scope_changed`; another user gets
`expired`. Errors that are not refusals (a hook raising, a lookup failing, a missing stored proof, a proof
that names another row, a `ValidationError` from a hook) answer `internal_error` and keep the snapshot.
A side part, proofs hash or index hash that is gone is a discard (tombstone: `scope_changed`) or an
eviction (`expired`). A proof field missing from a present hash is a stored-data defect, never `{}`.

**Logs.** `payment_calendar` logger, no names. At WARNING: `read_refused endpoint=... kind=... side=...`,
`slow_read endpoint=... ms=... rows=... parties=...` above 300 ms and `part_missing endpoint=... part=...`; at
ERROR: `worker_unavailable` and `job_lost` (see "Reading the logs"; the production level hides WARNING). Each
read-path discard is counted once per snapshot in a 300 s window (`jarvis:pcal:{owner}:refusals`); the third
different snapshot writes one Error Log row, "Payment Calendar refusal loop", with counts and the check kind
only. A refusal caused only by the site day changing (`kind=anchor`) discards the snapshot but is not counted.
The counter is best effort and never changes a reply. An `internal_error` writes at most one Error Log row per
operation and exception class in 300 s (Frappe does not group Error Log rows).

**Fingerprint inputs.** `access.FINGERPRINT_INPUTS` names every hashed input and a test keeps the list
equal to the data. 821-A added `server_scripts` (Permission Query Server Scripts for the doctypes in
play), `server_script_enabled` and `domains` (Has Domain rows of Domain Settings). The doctype set the
policy rows are read for (`access._doctypes`) also holds the doctypes that the Accounts Payable and Receivable
Link columns point at (`Contact`, `Currency`, `Customer Group`, `DocType`, `Payment Term`, `Project`,
`Sales Person`, `Supplier Group`, `Territory`), because the report row filter applies user permissions on them.
A change to any input refuses every read and discards the snapshot. Merging the fingerprint queries is deferred.

**Limits.** A read loads and parses the side part (proofs are not in it) and at most one page of proofs. A non-default sort on the month window
sorts that selection (up to the side cap of 10,000 rows); it is measured, not gated. A snapshot
that the code before 821-A completed has no parts and answers `expired` once. A snapshot of this code with
an older fingerprint (an input was added) answers `scope_changed` once.

### Backport checklist (version-15-hotfix)

The hotfix lines differ from develop in these places. Carry each one in the v15 backport and run the v15-only
tests (`TestV15Backport` in `test_payment_calendar_read_path.py`, skipped on v16) in its CI:

- `row_filter_columns`: pass the report columns through `row_filter_columns(...)` in **both**
  `access._check_proofs` (the read-path check) and `sources._visible_rows` (the publish check) before they go to
  `get_filtered_data`. Without it every read by a user who is not a System Manager is refused.
- `frappe.get_hooks`, not `effective_hooks`: the hotfix lines have no `jarvis/permission_hooks.py`.
  `permissions.visible_names` and `incomplete_scope_reasons` read `frappe.get_hooks("has_permission")` and
  `("permission_query_conditions")`.
- Keep `require_obligation_scope` in the `sides` loop of `authorize_scope` (v15 refuses receivables for a user
  with a Sales Person permission, on every read).
- The tests that simulate a site `has_permission` hook must patch the lookup that the v15 code uses
  (`frappe.get_hooks`), not the develop helper.
- Carry `frontend/tests/payment-calendar/golden/` and keep `test_payment_calendar_golden` green and
  byte-identical.
- The two read-path timing tests print `PCAL_TIMING endpoint=<name> ms=<n>` (no assertion). Read the lines from
  the CI log of the backport PR (AC5).

### Caps and where they live

| Cap | Value | Where |
| --- | --- | --- |
| Visible companies | 500 (`too_large`) | `api.get_capabilities` |
| Accounts in the catalog / balances job | 500 (`too_large`) | `catalog.MAX_ACCOUNTS` |
| Accounts per snapshot scope or preference | 50 | `scope._account_ids`, `banks.validate_bank_accounts`, `preferences` |
| Accounts per validation or balance-read call | 50 | `catalog.CHUNK` |
| Detail page size / page number | 100 / 100,000 | `detail._page` |
| Rows whose proofs one read loads | at most the page size (100) | `store.load_proofs` |
| Slow read warning | 300 ms | `read_log.SLOW_MS` |
| Refusal-loop Error Log | 3 different discarded snapshots in 300 s | `read_log.LOOP_SNAPSHOTS`, `store.REFUSAL_WINDOW` |
| Snapshot result lifetime | 300 s | `store.RESULT_TTL` |
| Active jobs per actor | 2 snapshots, 1 balances | `store.ADMISSION_LIMITS` |
| Ledger rows / invoices per side | 100,000 / 10,000 | `sources.MAX_LEDGER_ROWS`, `sources.MAX_VOUCHERS` |
| Slow balances job warning | 150 s | `jobs.SLOW_JOB_MS` |

### Reading the logs

Two places hold diagnostics: the `payment_calendar` log file and the Error Log doctype. No line or row
carries a voucher, party, company, user or amount.

**Log file.** `sites/<site>/logs/payment_calendar.log` (the bench-wide copy is `logs/payment_calendar.log`).
A line looks like `<time> <LEVEL> payment_calendar <message>`. The production log level is ERROR
(`frappe.utils.logger.default_log_level`; a dev server uses WARNING), so a production file holds the ERROR
lines only. The WARNING lines below are hidden there by design: `slow_read`, a slow balances job and
`read_refused` are for a dev server, a staging site or a site whose process level was lowered. Do not depend on
them in production. The production signals are the ERROR lines and the Error Log rows.

| Level | Line | Meaning and first action |
| --- | --- | --- |
| ERROR | `worker_unavailable kind=<snapshot\|balances> reason=<class>` | The job could not be put on the `long` queue. `reason` is the exception class, or `not_queued` when the enqueue returned nothing. The user sees `worker_unavailable`. Check Redis and the `long` worker. |
| ERROR | `job_lost kind=<kind> age_s=<n>` | A queued or running job has no active RQ job (a worker crash, a flushed queue). Logged only when the status row was marked failed. Check the worker log of that time. |
| WARNING | `read_refused endpoint=<name> kind=<kind> side=<side or ->` | A read refused and discarded a snapshot. `kind`: `scope` (the stored scope no longer holds), `anchor` (the site day changed), `fingerprint` (a permission or setting input changed), `voucher`, `party`, `proof` (a returned row is now hidden or changed), `obligation` (`restricted_obligations`). |
| WARNING | `slow_read endpoint=<name> ms=<n> rows=<n> parties=<n>` | A read took longer than 300 ms (`read_log.SLOW_MS`). |
| WARNING | `part_missing endpoint=<name> part=<part>` | A snapshot status row exists but one stored key is gone (`payable`, `receivable`, `status`, `proofs-<side>`, `index-<side>`): a Redis eviction. The client sees `expired`. Many of these point at Redis memory pressure. |
| WARNING | `slow balances job complete duration_ms=<n> accounts=<n>` | A balances job ran 150 s or longer (RQ timeout 300 s). |
| WARNING | `<kind> job scope_changed\|forbidden\|expired exception_class=<class> stage=<stage>` | An expected job outcome: access or scope changed while the job ran. |
| WARNING | `refusal_counter_unavailable` | The refusal-loop counter failed (Redis). The reply was not affected. |

**Error Log titles to filter on.**

| Title | Written by | Meaning |
| --- | --- | --- |
| `Payment Calendar API <method>` | `api._safe` | An unexpected error in an API call (`internal_error`). The body is JSON: `operation`, `exception_class`, `cause_class` (the class that was wrapped, for a hook error inside the identifier check), and `detail` (key names or a fixed code) when there is one. At most one row per operation and exception class in 300 s. |
| `Payment Calendar snapshot` / `Payment Calendar balances` | `jobs._fail_safely` | A job failed with a code other than `scope_changed`, `forbidden` or `expired`. The body has `code`, `kind`, `exception_class`, `stage` (`admission`, `sources`, `publish`) and `duration_ms`. |
| `Payment Calendar refusal loop` | `read_log.count_discard` | Three different snapshots of one user were refused and discarded inside 300 s. The body has the count and the last check kind. Look for a hook, a permission edit or a changing policy input that refuses every new snapshot. A refusal caused only by the site day changing is never counted. |

**Codes.** A failed job and a failed call carry one `code` from `errors.SAFE_CODES`.

| `code` | Meaning |
| --- | --- |
| `internal_error` | An unexpected error. The Error Log row has the class and the stage. |
| `timeout` | The job passed the 300 s RQ timeout. |
| `source_changed` | The source view changed while the job read it (a write inside the repeatable-read view). |
| `scope_changed` | A permission, setting, account, day or stored proof no longer holds. The client requests a new snapshot. |
| `forbidden` | Company or report access is missing. |
| `expired` | The result is gone (300 s lifetime, a purge or an eviction). Also the code of an unknown or foreign id. |
| `source_contract_unavailable` | The ERPNext or Frappe version or the report does not match the audited contract. |
| `source_configuration_unavailable` | A site setting the calendar needs is missing (for example the company currency). |
| `fiscal_year_unavailable` | No fiscal year covers a balance read. |
| `multibook_unsupported` | Ledger rows exist in a finance book the calendar cannot combine. |
| `too_large` | A cap was passed (companies, accounts, rows or invoices; see the cap table). |
| `invalid_source_amount` | A source amount is not a valid number. |
| `reconciliation_mismatch` | The invoice rows and the ledger do not reconcile. |
| `currency_reconciliation_unavailable` | A foreign-currency invoice cannot be reconciled. |
| `settlement_schedule_mismatch` | A payment schedule does not match the settled amount. |
| `multiple_invoice_accounts` | One invoice posts to more than one control account. |
| `masked_source_unsupported` | A source field is masked for the user. |
| `restricted_obligations` | User permissions limit the obligations (a v15 Sales Person scope on receivables). The cash side would not be comparable. |
| `job_lost`, `worker_unavailable`, `worker_busy` | The job is missing from RQ, could not be queued, or the user already has the allowed number of active jobs. |

**Performance and test data cleanup.** The measurements of Task 0 and T6 ran on a throw-away site with data
padded to the caps. That data (the `PCAL-PERF` accounts and the invoices tagged `T0-821A`) is local to that
site and is not part of the app, a fixture or a patch: nothing ships it. Remove it on that site after the
measurement (cancel, then delete the tagged documents; `jarvis/tests/payment_calendar_fixtures.py` data is
rolled back by the tests). A site that still has it shows a large side near the 10,000-invoice cap; this is not
a defect of the calendar.

## API, retention and recovery

`jarvis.payment_calendar.api` exposes POST-only capabilities, preferences,
floor updates, snapshot request/status, day detail, upcoming, bucket detail and
the all-account balances job (`request_account_balances`,
`get_account_balances_status`). The server fixes the anchor to site today. Each opaque ID is bound
to its site, requesting actor, canonical company/accounts and full date window.
No endpoint accepts an actor or caller-supplied accounting filters. Detail and
counterparty pages are bounded to 100 rows; aggregates use the complete source.
All panels reuse the same snapshot rather than recomputing reports on clicks.

Atomic Redis admission precedes Frappe's long-queue job deduplication. Each run has
a 300-second worker timeout, 900-second queued status and 360-second running
lease. A completed result expires after 300 seconds. Refresh joins active work;
after completion it creates fresh work. Two active jobs per actor bound different
account/company combinations. RQ identity includes the run token, so an expired
run cannot block recovery behind an old worker registry entry. A skipped enqueue
fails immediately; polling detects a missing/terminal RQ job. A queued run with no
worker expires rather than presenting empty financial data. The `jarvis:pcal:`
namespace retains its own TTLs across the routine watchdog cache clear. Explicit
feature purges and actual key loss still fence late output. Admission prunes
expired owner-index entries, retaining bounded cleanup bookkeeping.

Every delivery refreshes current actor access and hashes actual policy/configuration
row contents. The full row visibility check (every cached source row through the report
filters and the voucher and party queries) runs once, at publish. A read checks the
visibility of the rows it returns (see "Read path (821-A)"). Floor identity/revision,
site midnight, currency/timezone/defaults, permissions and dimensions invalidate
old results. Private relationship proofs never enter public responses. Missing
sources produce unavailable amounts and no cash-health colour. Signed negative
adjustments reconcile separately and never create forecast cash flows.

Failures return fixed safe codes. Jobs catch exceptions before Frappe's automatic
retry/traceback logger can change the actor or retain financial exception text;
diagnostics contain only stage, exception class, code and duration. Company and
account selections travel in POST bodies; calendar URLs need only dates and
opaque IDs. Detail rows carry no source-document link (`source_link` was removed in #821).

Preferences contain only company/account IDs in the existing owner-scoped Jarvis
User Settings. Direct saves validate this schema too. User updates/permissions
that affect access purge indexed feature snapshots; disable/delete also clears preferences. Workspace
reset clears only these preferences/snapshots, preserving company floors and
other user preferences. Purge failure does not block user lifecycle: fresh access
checks still deny delivery, and Redis retention stays bounded. No global key scan
or accounting deletion is used.

A synthetic cached-delivery benchmark with 20,000 real permission-checked invoice
headers measured 1.510 s for status (332 KB response), 1.779 s for day detail and
1.523 s for upcoming on this test environment. This measures delivery/authorization,
not canonical accounting-source work. Custom per-document permission hooks can
add queries; deployment-specific latency remains a pilot check.

Rollback removes the calendar UI/APIs/jobs and expires feature caches, leaving
existing reports/chat and accounting data untouched. Keep company floor
configuration unless a separately reviewed removal is needed. Native UI/browser
journeys and deployment-specific worker capacity remain subsequent pilot gates.
