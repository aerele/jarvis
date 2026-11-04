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

## API, retention and recovery

`jarvis.payment_calendar.api` exposes POST-only capabilities, preferences,
floor updates, snapshot request/status, day detail, upcoming and reconciliation
bucket detail. The server fixes the anchor to site today. Each opaque ID is bound
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

Every delivery refreshes current actor access, hashes actual policy/configuration
row contents, and rechecks **all** cached source rows through report filters and
voucher/party queries before releasing even aggregates. Floor identity/revision,
site midnight, currency/timezone/defaults, permissions and dimensions invalidate
old results. Private relationship proofs never enter public responses. Missing
sources produce unavailable amounts and no cash-health colour. Signed negative
adjustments reconcile separately and never create forecast cash flows.

Failures return fixed safe codes. Jobs catch exceptions before Frappe's automatic
retry/traceback logger can change the actor or retain financial exception text;
diagnostics contain only stage, exception class, code and duration. Company and
account selections travel in POST bodies; calendar URLs need only dates and
opaque IDs. Source-document links are returned only after current access checks.

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
