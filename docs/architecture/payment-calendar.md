# Payment Calendar source contract

The first change supplies internal sources and company configuration. It does
not expose a calendar endpoint, background job or customer dashboard. Accounting
documents and existing report results are unchanged.

## Supported source and completeness

The adapter targets ERPNext v16. Its allocation contract was checked against
ERPNext 16.50.0 (`7474d9e786277383de1242ab882f16856d17a9c9`). It calls the
canonical `ReceivablePayableReport` allocator rather than copying its maths.
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
open and settled rows. A restricted/custom scope sets `complete_for_company` to
false. Consumers must withhold whole-company cash-health colours for incomplete
obligations; permitted due rows alone do not establish liquidity. Unsupported
allocation/FX reconciliation and non-default finance books fail closed.

AP/AR and selected-bank sources accept only NULL, blank and the company default
finance book, using their canonical unfiltered paths. Account balances are signed
company-currency book values. Selected accounts must be readable, enabled leaf
Bank/Cash accounts of the same company. Fiscal coverage is required; missing
coverage cannot become zero cash. Request form fields are isolated from the
balance helper. Existing ledger/dimension gates remain authoritative.

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

These readers alone are **not a coherent snapshot API**. The next change must
wrap them on one primary repeatable-read, consistent read-only transaction and
reauthorize current settings/permissions before delivery. A committed-connection
discovery on the test site proved AP and bank values stayed 100/100 inside the old
snapshot after a payment, then became 70/70 in a fresh snapshot. AR, cancellation,
configuration races, unbuffered cursors and cached metadata still need the full
job-level integration tests. A common timestamp alone is insufficient.

There are no feature snapshots or user preferences to purge in this foundation.
Rollback leaves accounting data untouched; existing reports/chat remain usable.
Keep company floor configuration unless a separately reviewed removal is needed.
