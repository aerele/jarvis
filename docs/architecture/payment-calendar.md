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

### Slice 1a measurement (exact speed-ups)

Slice 1a makes the Payable read faster without changing its output (see "Speed overrides" below).
It was measured on a populated site (`jarvis_playground.localhost`, MariaDB; 11,386 live Payable
and 33,618 live Receivable ledger rows), read-only as Administrator inside
`snapshot.consistent_read()`, one fresh process per run. "Before" is the frozen oracle
(`jarvis/tests/payment_calendar_oracle_1a.py`, a copy of the code before slice 1a); "after" is
`read_obligations`. The measurement ran on the final tree, after the party pre-seed amendment (see
"Speed overrides" below). Wall time and peak RSS are medians of 3 runs (month scope: 5 runs). The
SQL count is the number of `frappe.db.sql` calls made inside the read; it is the median too. The
count is stable (month "after" 102 and legacy "after" 107 in every run). The one outlier, 816
instead of 476 for the month "before" in the first process of the series, came from a cold Frappe
metadata cache: that process loaded 26 DocType definitions (13 queries each) that later processes
read from cache. An earlier series had the same effect on one "after" run (243 instead of 102).
Peak RSS is the whole process (about 87-95 MiB of it is the process before the read starts).

| Side, scope | Rows | Wall time before / after | SQL calls before / after | Peak RSS before / after |
| --- | --- | --- | --- | --- |
| Payable, current month | 192 | 2.46 s / 1.88 s | 476 / 102 | 143.7 / 148.2 MiB |
| Payable, legacy (no month) | 2,071 | 6.90 s / 2.14 s | 4,282 / 107 | 146.5 / 150.4 MiB |
| Receivable, either scope | refused | `too_large` on both | 38-51 / 39-53 | 180-181 / 179-180 MiB |

The canonical result (rows without their ids, excluded rows, notes, totals, completeness and scope
reasons) was identical before and after in every Payable run. Receivable on this site is refused
with `too_large` before and after, so no figure compares an output. Receivable "after" makes one or
two more calls than "before" (the bounded size probe is one query); the refusal is the same. The
month scope saves SQL calls, not wall time: its time is dominated by the upstream ledger queries,
which are unchanged, and the "before" and "after" runs overlap (1.87-3.04 s against 1.78-2.45 s), so
the median gap is not a claimed gain. The legacy scope reads the schedule of every invoice, so it
gains the most.

## Speed overrides fall back to upstream, never refuse

`CalendarReport` overrides one ERPNext report method only to read faster, with the same output.
`get_payment_terms` reads the Payment Schedule rows of up to 500 invoices in one query instead of
two queries per invoice, and reproduces upstream's body (including the `is_return = 0` filter and
the term order) around that read. It is the only pinned override.

Party details are pre-seeded, not overridden. `build_data` (v16 only) fills `party_details` for
every party in `voucher_balance` before upstream's `build_data` runs: one query per chunk of up to
500 parties, on Customer for Receivable or Supplier for Payable, with the same fields as upstream's
`get_party_details` and `None` for a party without a record. On MariaDB a party id that differs
only in letter case matches the record, as upstream's lookup does there; on Postgres the match is
exact. Upstream's `get_party_details` then only reads the dict, so no upstream body is copied and no
pin is needed. The seed runs after `set_defaults` (which resets `party_details` at the start of
`run`) and after `get_data` has filled `voucher_balance`. If upstream stops reading `party_details`,
the seed is unused and upstream looks parties up itself. The `native()` clone is a shallow copy and
shares the seeded dict. The seed may read a few parties that upstream would not have read; the
output is the same.

On v16 the ledger query also gets `name` as its last ORDER BY key. Upstream orders by posting
date and party only, so lines that tie came back in an undefined order. This needs no hash pin
because no upstream body is copied.

Two more changes copy no upstream body either, so they need no pin. Both apply on v16 only; v15
keeps today's code.

- One-pass repair rebuild (`build_data`). The old loop rebuilt `self.data` once per repaired
  invoice, which is quadratic. The loop now collects the replacements in `pre` order and rebuilds
  `self.data` once: the surviving rows in their order, then the replacements. The result order is
  the same. The copies are also flat: the `voucher_balance` entries used for `pre` hold scalars and
  dates only, so `frappe._dict(row)` is a full copy there (v15 keeps `deepcopy`); `metadata` is
  copied per entry because upstream pops `due_date` from the entries; `err_journals` becomes a set
  because only membership is used.
- Bounded size probe before the finance-book scan (`_read_obligations`). The ledger size check is
  `_ledger_rows_exceed`, a count over a derived table of `SELECT 1 ... LIMIT limit + 1`, so it
  reads at most 100,001 rows and never counts the whole table. On v16 it runs before
  `validate_obligation_books`. Visible change: a company that is both multi-book and too large is
  now refused as `too_large`; it was `multibook_unsupported`. Both refuse the read, and the refusal
  does not hide anything. On v15 the finance-book check still runs first.

The collect log line (`collect complete ...`, INFO level) ends with `fast_path=True` or
`fast_path=False`. It is an INFO field, so production (ERROR level) does not write it; the
`speed_fallback` ERROR line is the production signal. It reads `sources.FAST_PATH` when the line is
written. The flag is process-local and sticky: it turns false at the first fallback in a worker
process and stays false until that worker restarts. It covers only the one pinned override
(`get_payment_terms`), not the other changes of this slice. It is a log field
only; it is not part of the stored result. An ERPNext upgrade needs a worker restart for the pins to
be checked again; a deploy restarts the workers.

Slice 1a proves the output with a differential test. `jarvis/tests/test_payment_calendar_speedups.py`
re-runs every scenario of `test_payment_calendar_sources` and `test_payment_calendar_reconciliation`
with `read_obligations` replaced by a comparator. The comparator calls the real function and the
frozen oracle with the same arguments, and fails on any difference in the result (rows sorted and
without ids, excluded rows, notes, totals, completeness, scope reasons) or in the refusal code. The
one accepted difference is the `too_large` / `multibook_unsupported` precedence change above.
A scenario opts out with `@no_oracle_compare(reason)`.

These overrides are separate from the fail-closed classification pins
(`reconciliation.check_allocation_contract`, `_check_contract`), which still refuse a read on an
unknown source. A speed override is guarded by the sha256 of the upstream method source,
`compatibility._SPEED_PINS[major][name]`. When the installed source is not pinned (a new ERPNext
release, or v15, which has no pins), the override calls the upstream method instead. The output
stays correct and only the speed is lost. The first fallback per method in a process logs one
`speed_fallback` line at ERROR level (see "Reading the logs") with the method name, the installed hash
and the ERPNext version, and sets `sources.FAST_PATH` to false. The logging is best effort: nothing in
it can raise into the read. An unreadable source is not remembered as unknown; its hash is read again
on the next call.

Bump procedure when ERPNext changes a pinned method:

1. Run `test_pinned_hashes_match_installed_source`; it fails and prints the new hash.
2. Audit the upstream diff of the method against the override; the output must stay identical.
3. Add the hash to `_SPEED_PINS` next to the old one, with the ERPNext commit reference in the comment.

The pin is computed against the ERPNext source of the CI base image (`version-16` head) and the
local release; accept both when they differ.

## Built-in reader (S1, not wired)

`jarvis/payment_calendar/builtin_reader.py` reads the same obligations as `sources.read_obligations`
without the full-history ledger scan and without the size guards (`MAX_LEDGER_ROWS`, `MAX_VOUCHERS`).
Nothing calls it except tests and the manual acceptance functions below: there is no flag, hook,
scheduler entry or API change, and a test (`TestReaderIsNotWired`) fails if a runtime module imports or
names `builtin_reader` or `eligibility`. Slice S2 adds the routing; until then today's read serves every
request.

**What it reads.** The reader is a subclass of today's `CalendarReport` and replaces only the ledger fetch
inside ERPNext's `get_data`. It then runs ERPNext's own `init_voucher_balance` and
`update_voucher_balance`, today's `build_data` (repair, settled overlay, excluded FX, every refusal) and
today's result assembly. Every figure therefore comes from ledger lines through ERPNext's code. Invoice
headers (`outstanding_amount`) only find which invoices to read; no figure comes from a header. The
reader takes no transaction control and writes nothing, so it passes inside `snapshot.consistent_read()`.

**Discovery sets.** The ledger lines of these invoices are read, by the indexed `against_voucher_no`:

- `open`: a header with a non-zero `outstanding_amount`, plus every invoice with a line dated after
  the anchor (the header counts those lines, the report does not).
- `orphans`: invoices on the side's accounts that are cancelled or posted after the anchor. The report
  has no row for them, so a payment against them lands on the payment's own row.
- `due_in_window`: today's settled-candidate rule. An invoice with a payment term, or a header due date,
  inside the window, whatever its settlement date. Settled invoices are found by this set.
- `fx`: invoices on a foreign-currency account with a non-zero residual, so today's excluded-FX rule
  decides them.
- `multi`: every invoice with lines on more than one (account, party) key over the full history (one
  aggregate), so the inherited counter refuses `multiple_invoice_accounts` exactly when today does.
- `returns`: computed last over the union of the others and closed in both directions (a return needs its
  original, an original needs its returns).

Every line whose `against_voucher_type` is not the side's invoice type is read too (Payment Entry,
Journal Entry, orders, Employee Advance and so on); the types come from the control aggregate. A re-key
probe then follows each read line that falls back to its own key: it looks the target key up by
`voucher_no` and reads its lines, to a fixed point of at most 8 rounds (`rekey_unresolved` beyond that).

**Control total.** One SQL aggregate per side with the report's own predicate (the side's accounts of
the company, `delinked = 0`, `posting_date <= anchor`; on Receivable also `party_type != 'Employee'`,
with SQL semantics, so a NULL party type is excluded as in the report). The sum is read as text and
parsed as Decimal. The reader checks that the sum of the key balances and the sum of the lines it read
both equal that total within half a unit of the last place of the currency. This proves that the lines
NOT read sum to zero. It does not say to which keys they belong; the closure and the probe above cover
that. A mismatch raises `ReaderFallback("control_total")`.

**MariaDB only.** The reader uses MariaDB features (SQL semantics of the control aggregate, collation
checks, the ledger indexes) and is not tested on another database. Elsewhere it must fall back.

**Eligibility** (`jarvis/payment_calendar/eligibility.py`, fail closed). The reader reads without the
report's per-user narrowing, so it serves only a user for whom that narrowing removes nothing. The
reasons for `ineligible` are: an incomplete scope, a User Permission on a doctype the report links to,
a Company permission that excludes the company or a company restriction, an unaudited permission hook,
and a column match filter. A masked or permlevel-restricted field among those the reader reads on Sales
Invoice, Purchase Invoice, Journal Entry and Payment Entry has no reason of its own: it gives the same
`column_filter` reason as a column match filter. Any exception while checking gives
`eligibility_unknown`.

**Fallbacks.** The reader raises `ReaderFallback(reason)`, never a refusal. A fallback means "run today's
read"; it is not a `CalendarSourceUnavailable`, so it cannot be mistaken for a refusal. The reasons are
`ineligible`, `eligibility_unknown`, `unsupported_major`, `pin_drift`, `sales_person_scope`,
`control_total`, `rekey_unresolved`, `too_many_lines` and `reader_error` (a programming error in
reader-only code). `too_many_lines` means the ledger of the side has more lines than the reader may hold
in memory (a cap on the lines of the generic voucher types, on the total, and on the parties ranked, see
`builtin_reader`); it is a memory guard, not an error, and today's read serves the request instead. The
reader's own refusals are today's: it has the same input checks, books gate and error mapping, and the
same codes, except that it never gives `too_large`.

**How an operator sees a fallback.** The reader never logs by itself. `read_obligations(..., stats=d)`
fills `d["fallback"]` with the reason (and `d["fallback_detail"]`, a tuple of fixed strings such as the
names of drifted pins or the eligibility reason codes, never a data value) before it raises, also when the
failure comes before the engine exists. A caller that catches `ReaderFallback` can therefore log the reason,
the detail and the timings. No caller does this yet: S2 wires the routing and the log line, and until
then nothing is observable in production because the reader is not called.

**Known fallback causes.**

- A payment line against a closed invoice but on another party account lands on the payment's own
  key, which is not read. The control total then differs and the read falls back (`control_total`).
  The playground and drift runs must show 0 of these.
- A header whose stored `outstanding_amount` is wrong can hide an open invoice from discovery. The
  side-level control total catches it unless two stale headers cancel exactly.
  **Known limit (offsetting stale headers):** if one stale header hides an invoice of +X and another
  hides an invoice of -X on the same side, the control total still matches, the read is served, and both
  invoices are missing from it. No amount of a served row is wrong, but the two rows are absent. This is
  accepted for S1 (it needs two exactly offsetting stale headers on one side); the stale-header count of
  the playground run measures how likely it is, and a stale header is a data defect that ERPNext's own
  reports show too. A value never comes from a header.
- ERPNext or frappe major other than 16, or a pinned source that changed: `unsupported_major` and
  `pin_drift`.

**Pins and bump procedure.** `compatibility._READER_PINS["16"]` holds the sha256 of every upstream
source the reader copies or narrows (`get_data`, the two `fetch_ple_*` cursors, `build_delivery_note_map`,
`get_invoice_details`, the voucher-balance methods, the predicate methods, `build_data`,
`get_return_entries`, `update_voucher_outstanding` and the company restriction hooks). It is checked per
read; `reader_pin_drift()` lists the names that drifted. The hashes are the same on ERPNext 16.55.0 and
on the `version-16` head, so there is one hash each. When ERPNext changes a pinned source:

1. Run the pin test, `test_reader_pins_match_installed_source` (in `test_payment_calendar_compatibility`);
   it names the drifted function and prints the new hash.
2. Audit the upstream diff against the reader's copy or narrowing; the reader differential
   (`test_payment_calendar_reader_differential`) and the seeded drift test must stay green.
3. Add the new hash next to the old one with the ERPNext commit in the comment.
4. Restart the web and background workers after the bump. A worker caches the pin result in its process,
   so a worker that was not restarted keeps the old verdict (a stale `pin_drift`, or a missed one).

Only a `"16"` entry exists. Entries for v15 come with the v15 backport.

**Row ids equal today's.** An id encodes the row's position in the whole result, which follows the order
in which `init_voucher_balance` first meets each key. It keys a line by its own voucher, so the reader
also reads the other ledger lines of every voucher whose key a read line creates (by `voucher_no`, the
indexed lookup) and feeds them to the init pass only. They never reach the update pass, the control total
or a row, so no amount changes; they count toward `TOTAL_LINE_CAP` (`too_many_lines` above it) and show
as `init_only_lines` in the stats. Ids, row order and amounts all equal today's read: the differential
accepts no difference. The settled overlay is ordered by the candidate order, not by a set's hash order (`_settled_in_window`), which differs between two sets of the same keys and between processes. An id still does not outlive a read (it moves when the ledger changes); no
consumer keeps one across reads.

**Manual acceptance** (outputs outside the repository, companies as HMAC digests, rows by digest and
field name, never a party or voucher name). The functions live in
`jarvis/tests/payment_calendar_reader_acceptance.py`; each checks its site first and returns a short
summary, the full result is the JSON file.

| Function | Site | What it proves |
| --- | --- | --- |
| `playground` | `jarvis_playground.localhost`, session READ ONLY; `skip_eligibility` (below) | AC-10: both sides in one read view each, 0 diffs against today's read with guards lifted, no fallback, control total passes, equal settled-overlay counts, `check_source_contract` accepts the result; counts invoices whose header differs from the ledger |
| `drift` | `test_jarvis` | AC-11: a seeded run of real controllers, both sides at every drain (1-20 operations), 0 diffs, 0 fallbacks, control total passes, isolation probe equal (it counts companies too); rolled back. Drains run under frozen time, so the drain records carry no timings. The shared ledger company is created and committed first, outside the write guards (on a fresh site HRMS runs an ALTER TABLE that commits by itself) |
| `timing` | `jarvis_playground.localhost`, session READ ONLY; `skip_eligibility` (below) | AC-12 at a real company: run 1 per side cold (cache cleared), later runs warm; per-step timings, including control aggregate, `due_window` (D6) and `multi` (D8); memory of each read |
| `timing_guard_size` | `test_jarvis` | AC-12 (no-regression rule) at 10,000 vouchers and 100,000 ledger rows per side, settled due dates inside the window, unreferenced Payment Entry and Journal Entry lines; the fixture is raw-inserted in a savepoint and rolled back. Per side and run it times the reader and today's `read_obligations` (size guards lifted as in the differential) back to back, each in a forked child, alternating the order (reader first on odd runs, today first on even runs); the summary has `no_regression` and `no_regression_median` |

**`skip_eligibility`.** `playground` and `timing` take an acceptance-only option `skip_eligibility` (default `False`). When `True` the function refuses unless the session user is Administrator, then runs the reader with its eligibility call patched to report no reason for the duration of the read; the summary and the output carry `eligibility_skipped: true`. Every other guard stays (site check first, READ ONLY session, `consistent_read`, no writes, HMAC output). Why: on the playground site another app registers a `has_permission` hook (`<other_app>.access.has_permission`) for a voucher or party doctype. The eligibility gate cannot audit it and, correctly, fails closed, so every user is ineligible and both sides fall back with `ineligible` and compare nothing. The AC-10 run is therefore an exactness check of the reader against today's read, not an eligibility check (the eligibility tests cover that); the timing run then has a trivial `eligibility` step.

**No-regression rule (AC-12, guard size).** At the guard size (worst case) the reader must be no slower than today's read measured under the same load; at a real company size it must take 30 s or less. A time of today's read taken at another moment is not comparable on a shared machine, so `timing_guard_size` measures both reads back to back for each run and side, in forked children (the same method as for the reader), and alternates which read goes first: the first read of a pair finds the MariaDB buffer pool colder than the second, and the alternation cancels that bias. Per run it records both seconds and both read growths (`today` holds today's record). `per_side` in the summary has the max and median of each, the `order` of the runs and the outcome of today's read.

**Memory of a read.** Each timed read runs in a forked child process, because the process lifetime peak (`ru_maxrss`) never goes down and would carry the fixture builder and earlier reads. A child starts with the parent's resident size, so the figure of a read is `read_growth_mb` = child peak minus resident size at the fork (`baseline_rss_mb` and `peak_rss_mb` are in the record too). The parent's `memory_mb` stages are lifetime peaks and only describe the fixture build. At a real company (read only) the child opens its own DB connection. At guard size the fixture is an open transaction of the parent, so the child borrows the parent's connection while the parent waits; it runs SELECTs only and ends with `os._exit`. Each child rebuilds its process-local caches, as a new request does; Redis caches stay warm.

```
# W = the worktree or app checkout under test; O = an output directory outside every git work tree
# (and not under a `public` folder); <company> = the company name on that site; <bench> = the bench folder.
W=<path of the jarvis checkout under test>
O=<output directory>
mkdir -p $O && cd <bench>
# AC-10 playground, read only (skip_eligibility: playground-only app hook; exactness check, not an eligibility check)
PYTHONPATH=$W bench --site jarvis_playground.localhost execute jarvis.tests.payment_calendar_reader_acceptance.playground --kwargs "{'company': '<company>', 'out': '$O/playground.json', 'skip_eligibility': True}"
# AC-11 drift, test site only, one seed per run
for s in 1 2 3; do PYTHONPATH=$W bench --site test_jarvis execute jarvis.tests.payment_calendar_reader_acceptance.drift --kwargs "{'seed': $s, 'ops': 300, 'out': '$O/drift-$s.json'}"; done
# AC-12 timing (a fresh process per command: the first run is cold)
PYTHONPATH=$W bench --site jarvis_playground.localhost execute jarvis.tests.payment_calendar_reader_acceptance.timing --kwargs "{'company': '<company>', 'out': '$O/timing-playground.json', 'runs': 3, 'skip_eligibility': True}"
PYTHONPATH=$W bench --site test_jarvis execute jarvis.tests.payment_calendar_reader_acceptance.timing_guard_size --kwargs "{'out': '$O/timing-guard.json'}"
```

The gate reads the JSON: `eligibility_skipped` is stated, `diffs == 0`, `fallbacks` empty, `control_passed`, `max_seconds <= 30` and
`served` (playground company); at guard size `no_regression` is true (max of the reader <= max of today's read on every side; `no_regression_median` is the same by median). "Cold" means the first read after `frappe.clear_cache()` in a fresh process; the MariaDB buffer
pool is not flushed. The accepted fallback rate is the stale-header count the playground run records.

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
ERROR: `worker_unavailable`, `job_lost` and `speed_fallback` (see "Reading the logs"; the production level hides WARNING). Each
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
| ERROR | `speed_fallback method=<name> hash=<sha or none> erpnext=<version>` | The speed override `get_payment_terms` found an ERPNext source that is not pinned and ran upstream instead. Reads stay correct and only slower. Logged once per method per worker process, on v16 only. Run the pin bump procedure in "Speed overrides fall back to upstream, never refuse". `hash=none` means the source could not be read. |
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
| `too_large` | A cap was passed (companies, accounts, rows or invoices; see the cap table). (v16: also returned for a multi-book company over the row cap.) |
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
