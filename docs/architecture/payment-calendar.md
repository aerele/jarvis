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

With the reader switch `on` and the company served (see "Bank read: one pass and one Payment Ledger scan per
view"), a snapshot that selected bank accounts adds two more fields at the end of the line:
`bank_path=one_pass` (or `today`) and `bank_ms=<n>`. They hold no name. With the switch off, or with no
selected account, the line is the text above, unchanged.

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

## Built-in reader (S1)

`jarvis/payment_calendar/builtin_reader.py` reads the same obligations as `sources.read_obligations`
without the full-history ledger scan and without the size guards (`MAX_LEDGER_ROWS`, `MAX_VOUCHERS`).
Only the S2 router and the shadow job (see "Reader routing (S2)") call it, besides tests and the manual
acceptance functions below, and a test (`TestReaderCallers`) fails if any other runtime module imports or
names `builtin_reader` or `eligibility`. With the mode `off` (the default) today's read serves every request.

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
a Company permission that excludes the company or a company restriction, a permission query hook or a
Server Script permission query on a doctype the report reads, and a column match filter (a Company
permission that allows the company adds one on the Account and Project columns, so it refuses too). A
`has_permission` hook is no reason: its answer is applied to every row afterwards (invariant I1, below). A masked or permlevel-restricted field among those the reader reads on Sales
Invoice, Purchase Invoice, Journal Entry and Payment Entry has no reason of its own: it gives the same
`column_filter` reason as a column match filter. Any exception while checking gives
`eligibility_unknown`.

**Fallbacks.** The reader raises `ReaderFallback(reason)`, never a refusal. A fallback means "run today's
read"; it is not a `CalendarSourceUnavailable`, so it cannot be mistaken for a refusal. The reasons are
`ineligible`, `eligibility_unknown`, `unsupported_major`, `pin_drift`, `sales_person_scope`,
`control_total`, `rekey_unresolved`, `too_many_lines`, `deadline` (the reader passed its limit, also inside
the permission filter), `assemble_budget` (more rows than `READER_MAX_ROWS` for the permission filter to
hold) and `reader_error` (a programming error in reader-only code). `too_many_lines` means the ledger of the side has more lines than the reader may hold
in memory (a cap on the lines of the generic voucher types, on the total, and on the parties ranked, see
`builtin_reader`); it is a memory guard, not an error, and today's read serves the request instead. The
reader's own refusals are today's: it has the same input checks, books gate and error mapping, and the
same codes, except that it never gives `too_large`.

**How an operator sees a fallback.** The reader never logs by itself. `read_obligations(..., stats=d)`
fills `d["fallback"]` with the reason (and `d["fallback_detail"]`, a tuple of fixed strings such as the
names of drifted pins or the eligibility reason codes, never a data value) before it raises, also when the
failure comes before the engine exists. A caller that catches `ReaderFallback` can therefore log the reason,
the detail and the timings. `routing.read_side` is that caller: it records the reason, the detail, the
cause class and the timings, and `read_log.reader_outcomes` counts and logs them (see "Reader routing (S2)").

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

Only a `"16"` entry exists. v15 entries come with S6; the v15 code is inactive until then.

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

**`skip_eligibility`.** `playground` and `timing` take an acceptance-only option `skip_eligibility` (default `False`). When `True` the function refuses unless the session user is Administrator, then runs the reader with its eligibility call patched to report no reason for the duration of the read; the summary and the output carry `eligibility_skipped: true`. Every other guard stays (site check first, READ ONLY session, `consistent_read`, no writes, HMAC output). Why: on a site where another app registers a permission query hook (`permission_query_conditions`, for example `<other_app>.access.get_permission_query_conditions`) for a voucher or party doctype, the eligibility gate cannot audit it and, correctly, fails closed, so every user is ineligible and both sides fall back with `ineligible` and compare nothing. (A `has_permission` hook needs no bypass: since I1 it is not an eligibility rule, because its answer is applied to every row afterwards.) The AC-10 run is therefore an exactness check of the reader against today's read, not an eligibility check (the eligibility tests cover that); the timing run then has a trivial `eligibility` step.

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

## Reader routing (S2)

Slice S2 wires the built-in reader into `snapshot.collect` for one case: today's read refused `too_large`. A
side that today serves is read exactly as before. The router is `routing.read_side`; the nightly comparison is
`shadow.py`. `sources.py` does not change, and S2 never emits `history_too_large`.

**Order of conditions.** `collect` calls `routing.read_side(read_obligations, ...)` for each side with the
job's start time and an outcomes list. The first condition that fails ends in a fresh `too_large` (raised
outside the `except` that caught today's refusal, so no frame of today's engine stays reachable):

1. Today's read runs. If it serves, the side is served and no setting is read. A refusal other than
   `too_large` is raised as it is.
2. The mode is read first, alone: `reader_settings(lists_when_off=False)` returns at once on `off` without reading
   the two lists. With `off`, or with a company that is not on the served list, nothing else runs: nothing is
   counted, logged or probed (`settings`, not counted).
3. The Frappe major must be 16 (`v15`) and the site must have an `encryption_key` (`no_key`). The router never
   calls `get_encryption_key`, which would write `site_config.json`.
4. Budget: at most 90 s since the job started (`budget`).
5. Failure memo: no memo for this company and side (`memo`).
6. In-flight marker (`jarvis:pcal:reader:inflight:<digest>:<side>`): a `SET NX` with the job's token on a key per
   company and side. A second job that finds it
   answers `busy` and remembers nothing, so two users who open the calendar together are not turned away for
   minutes because of one slow reader.
7. Probe: no more than `READER_LEDGER_CAP` ledger rows (`probe`); the cap is 100,000, the same figure as today's `sources.MAX_LEDGER_ROWS` and as the shadow's cap, so the reader serves no ledger that the shadow cannot compare. A probe that says
   "too big" sets the short memo; a probe that fails (`probe_error:<class>`) leaves today's refusal and sets no memo.
8. Slot: one of two site slots (`jarvis:pcal:reader:slot:<n>`, random token, released by a compare-and-delete
   script) is free (`busy`).
9. The reader runs, with a deadline of the smaller of 30 s and what is left of the 120 s view.

**In-flight marker and failure memo.** They are two keys. The in-flight marker and the slots live for
`SLOT_TTL_S` (the RQ job limit plus a margin, 420 s) so that a job killed mid-read cannot hold them longer.
The failure memo is set only after a bad read, never before it:

| Outcome of the reader | Memo |
| --- | --- |
| served, a refusal (today's codes), `ineligible`, `sales_person_scope` | none |
| `deadline`, `assemble_budget`, `too_many_lines`, an unexpected exception, a job timeout (RQ's exception raised inside the job) | 30 minutes (`MEMO_TTL_S`) |
| `control_total`, `rekey_unresolved`, `reader_error`, `pin_drift`, `unsupported_major`, `eligibility_unknown` | 5 minutes (`SHORT_MEMO_TTL_S`): the read repeats identically until something changes |
| `deadline` when the late view cut the reader's limit below 30 s | none: the reader was not slow |

A deterministic reader refusal (`CalendarSourceUnavailable`) deletes the memo, so each new job repeats the read.

A job that is hard-killed (the process dies, so no code runs after it) leaves only the in-flight marker and its slot, until their TTL (420 s): the next job for that company and side answers `busy`. It sets no memo, because the memo is written by a live process after a bad read. A job timeout is different: RQ raises it inside the job, so the router records it and sets the 30-minute memo.

**Errors and the lost view.** A job timeout (`TIMEOUTS`) always propagates: the job ends, the record says which
side the reader was on (`reader_exception:<class>`), and the memo is set. Every other error of the reader,
a database error included, refuses the side as today's `too_large` and is recorded by its class
(`reader_exception:<class>`; a failed statement is also `fallback:reader_error`, with the class as its cause).
The reader never needs to tell a lost connection from a failed statement: `consistent_read` checks at its exit
that the view is the same transaction and still open, and fails the job when it is not, whatever the reader
swallowed. A test proves it with a reader that swallows a lost connection.

**Modes and lists.** Jarvis Settings has three hidden fields, changed only by an Administrator
(`_ADMIN_ONLY_FIELDS`; a System Manager is refused even after a permission self-grant):

| Field | Meaning |
| --- | --- |
| `payment_calendar_reader` | `off` (default), `shadow` or `on` |
| `payment_calendar_reader_companies` | the served list: companies the reader serves when the mode is `on` |
| `payment_calendar_reader_shadow_companies` | the shadow list: companies the nightly job compares and never serves |

Each list is one exact company name per line; lines are stripped and blanks dropped. A save checks only the
lines added in that save, so a company deleted later never blocks an unrelated save. `reader_settings()` reads
fresh on every call and returns `off` for a missing field, an unknown value or any error. In `off` and
`shadow` no served side is read by the reader and no setting is read on a side that today serves. On a side
that today refuses `too_large` the router reads the mode (one SELECT) and the two lists only when the mode is
not `off`. The shadow job compares the union of both lists and also runs while the mode is `on`. A save that
changes only these fields is inert: it enqueues no LLM sync, and it skips the retry of a failed sync that any
other save performs.

**Caps and budgets.**

| Limit | Value |
| --- | --- |
| Budget before the reader starts | 90 s from the job start |
| Whole snapshot view | 120 s (the reader's limit is cut to what is left) |
| Reader limit per side | 30 s, plus a hard stop 5 s later inside the permission filter |
| Job permission stop (batch switch on only) | 280 s from the job start, around collect and the publish check (`jobs.JOB_PERMISSION_STOP_S`) |
| Probe cap | `READER_LEDGER_CAP` 100,000 ledger rows (= `sources.MAX_LEDGER_ROWS` = the shadow's cap) |
| Rows after the engine | `READER_MAX_ROWS` 50,000 (a memory guard: `assemble_budget`; AC-4 sets it from the snapshot bytes) |
| Slots | 2 per site |
| Failure memo | 30 minutes, or 5 minutes for a deterministic fallback or a probe over the cap |
| Slot and in-flight marker | 420 s |

**The exact deadline in the permission step.** The permission filter (`sources._assemble` -> `visible_names`)
always runs in full: that is invariant I1. Its time is held to the reader's limit exactly, not by an estimate.
With the batch permission switch off (the default; with it on, see "Batch permission check (S2b-batch)" below):
while `_assemble` runs and only then, `frappe.has_permission` is wrapped, and each per-document check raises
`ReaderFallback("deadline")` once the hard stop (the deadline plus 5 s, `ASSEMBLE_GRACE_S`) has passed. The
wrapper answers every check as before. It is installed only when a `has_permission` hook is registered, because
without one `visible_names` makes no per-document check, so a user with no hook pays nothing for it. No cost
estimate, calibration or hook timing exists any more. The worst view is about the deadline plus 5 s plus one
check, below the 300 s job limit. With the switch on nothing is wrapped: the same hard stop is a context stop
that the batch checks (see below).

**Invariant I1.** The reader's rows reach a user only through `sources._assemble` -> `visible_names`, which
runs every `has_permission` hook per voucher and party in that user's own session (the snapshot job runs as
the actor). For that reason `has_permission` hooks are not an eligibility rule. Any path that skips this
step (cache rows, shared generations, a stored snapshot served to other users) must bring the
`has_permission` rule back. Query hooks, Server Script permission queries, User Permissions, owner-only
roles, masked or permlevel-restricted fields and match filters on the report's columns stay: nothing after
the reader re-applies them. In S2, Company permissions with `applicable_for` set cause
`scoped_company_permission`, and permissions for all document types that allow the company still cause
`column_filter` through Account and Project match filters, so today's `too_large` stays for both. A support
person sees it as the counter
`ineligible:column_filter` in `shadow.status()` (below). A later slice may relax this.

`visible_names` may evaluate in batch; it must stay exactly equal to the per-document `frappe.has_permission`.
With the batch switch on, "runs every `has_permission` hook per voucher and party" is done by
`batch_permission.batch_visible` (see "Batch permission check (S2b-batch)" below), which returns the set that
the loop would return, for the session user. With the switch off the loop of today runs.

**Logs, counters and Error Log classes.** `read_log.reader_outcomes` runs in a `finally` after the view and
never raises. Every line, key and row names a company only by `company_digest` (a keyed HMAC of the site's
encryption key, domain `pcal-ops`, 16 hex) and holds fixed words and numbers: no `str(exc)`, no traceback.
`shadow.status()` maps a digest back to its company on the console.

Production logs at ERROR level: the INFO and WARNING lines (`reader_outcome`, `shadow_unit`,
`reader_view_slow` and the `*_unavailable` lines) are not visible there. The signals in production are the day
counters, the Error Log rows and `shadow.status()`.

- A day counter per company and class, `jarvis:pcal:reader:day:<digest>:<date>` (Redis hash, `HINCRBY` raw
  integers, TTL 35 days; the date comes from `routing._today()`, which `shadow._today()` uses too).
- Counted only (no Error Log row): `served`; the fallbacks `ineligible`, `ineligible:<reason>` (for example
  `ineligible:column_filter`) and `sales_person_scope`; `probe_error:<class>`; and the router steps `v15`,
  `no_key`, `budget`, `memo`, `probe`, `busy` and `cache`. The `settings` step is not counted.
- Error Log "Payment Calendar reader failure", one row per company, class and rolling 24 hours (not per calendar
  day), none when the cache cannot answer: `fallback:<reason>` for `control_total`, `rekey_unresolved`,
  `reader_error`, `pin_drift`, `unsupported_major`, `eligibility_unknown`, `too_many_lines`, `deadline` and
  `assemble_budget`; `reader_refused:<code>`; `reader_exception:<class>` (a timeout included). A row names the
  cause class of a `reader_error`.
- Error Log "Payment Calendar reader daily": `shadow.dispatch` writes it for yesterday when any counter other
  than `served` is above 0, once per company and date, before it checks the mode. The text is
  `read_log.daily_report`: every counter, `served` included, `failures` (the sum of the failure classes above and
  of `probe_error:<class>`), and `listed_but_missing=1` for a company on a list that no Company has (a renamed or
  deleted company, which is never served; `routing.listed_but_missing`). A company that is never served (too big,
  ineligible, busy, missing) is as visible as one that fails.
- Error Log "Payment Calendar reader shadow diff": one per company and night when a unit differs or fails with
  an unexpected error.
- Error Log "Payment Calendar reader shadow fallback": one per company, night and reason when a unit could not
  be compared because of `fallback:<reason>`, `ineligible`, `shadow_too_large` or `rss_cap`.
- Error Log "Payment Calendar reader shadow incomplete": a unit killed (`killed`), units cut by the night's
  budget (`time_cap`) or a company list that changed (`units_changed`, `units_changed_again`).

| Reason | Action |
| --- | --- |
| `ineligible`, `sales_person_scope` | none; the user's rules keep today's refusal. `ineligible:column_filter` is also what a Company-permission user gets, by design |
| `pin_drift`, `unsupported_major` | an ERPNext source changed: audit it and bump the pin; a bump needs a restart of the workers, and until then the side stays refused |
| `control_total`, `rekey_unresolved`, `reader_error`, `reader_exception:*`, `eligibility_unknown` | a defect or stale data: the row names the cause class; take the company off the served list and file it |
| `reader_refused:<code>` | the reader gave today's own refusal for this data; compare with today's read |
| `deadline`, `assemble_budget`, `too_many_lines` | the company is too big for the limits; figures stay "unavailable" |
| `busy`, `memo`, `budget`, `probe`, `cache` | expected under load; a memo clears in 30 minutes (5 for a deterministic one); `cache` means Redis did not answer |
| `v15`, `no_key` | the site cannot run the reader: nothing to do until it is a v16 site with an `encryption_key` |

**Shadow.** One cron entry, `"*/15 2-5 * * *"` -> `shadow.dispatch`, starts or resumes the night's chain from
02:00 to 05:59 site time. A unit is one company, one side and one scope (previous, current and next month and
the legacy window); each company also has one bank unit, `Bank:all` (see "Bank read: one pass and one Payment
Ledger scan per view"). One RQ job (`long` queue, 600 s, `job_id` `pcal-shadow:<date>:<n>`) runs one unit under
the lease `jarvis:pcal:shadow:lease` (900 s, so a job RQ kills late cannot outlive it), then enqueues the next.
The start company rotates by one each night, so a company late in the order is not starved every night by the
night's budget. A unit runs both reads, today's with its guards lifted and the reader, as Administrator in one
`consistent_read()` view, and stores a record (counts and digests, never names) after the view. The night's
budget is 1,800 s; later units record `time_cap`. A unit with an in-progress marker and no lease was killed and
is recorded as `killed`. The night hash stores a keyed digest of the company list: a list that changes during
the night restarts the night once; a second change stops it with an Error Log row.

- A unit above `SHADOW_LEDGER_CAP` (today's own ledger guard, 100,000 rows) is not compared
  (`shadow_too_large`): the oracle holds the whole ledger in memory on the one `long` worker. A process
  above 800 MB is checked before the oracle and after it (`rss_cap`). A reader that took past 30 s is
  `would_deadline`.
- A database error (a lost connection, a deadlock, a query timeout) is not a result: the unit is recorded
  `error_transient` in the state `retry` and is run again after the chain, up to two runs in all, within the
  night's budget. It writes no diff row and is not a diff; it counts as "not compared" in the gate.
- A job timeout leaves the marker and raises a fresh exception from the unit, so a traceback with locals (the
  runner stores one in developer mode) carries no company, party or voucher name.

The shadow proves data exactness for Administrator only; the per-user proof is the reply matrix test and the
visitor flow.

**The gate.** `shadow.summary(days=7)` passes a company when, for every side and scope, at least 7 nights hold a
`compared` record on the deployed reader build (`routing.reader_build()`: a hash of the figure-deciding
modules, listed in `routing.FIGURE_MODULES` (`banks`, `books`, `builtin_reader`, `compatibility`, `eligibility`, `errors`,
`permissions`, `reconciliation`, `scope` and `sources`), and the Frappe and ERPNext versions, computed once per process; a
test makes every `jarvis/payment_calendar/*.py` file a figure module or not), and the period has no diff or
error row, no `would_deadline` and no `deadline` or `assemble_budget` fallback. The period is tonight and the 7
nights before it, so a missing tonight never fails the gate. `missing` lists the side and scope that lack
nights, and `reasons` counts why the units that were not compared were not. A change to a figure module starts
the count again. Every company also needs the bank unit (`Bank:all`) `compared` on the same nights, with no
diff or error record; a company whose bank unit cannot be compared never passes (see "Companies that never
reach `Bank:all` `compared`" in "Bank read: one pass and one Payment Ledger scan per view").

The nights and the day counters live in `redis_cache` (no persistence, an LRU eviction policy): a Redis restart
or an eviction silently resets the count. That fails safe (the gate then says "missing"), but it is not
durable. Only the Error Log rows are in the database, so the pilot clock can restart. The gate does not check
that the nights saw new postings: the operator does.

**Status command.** For an operator without a console session:

```
bench --site <site> execute jarvis.payment_calendar.shadow.status
```

It prints the mode, the build, both lists, the digest that the Error Log rows carry for each company (digest =
company name; names appear here and nowhere in a log, key or row), today's and yesterday's counters, and the
gate per company with the side and scope that lack nights and the reasons. Use it to answer "why does this user
see unavailable" (`ineligible:*` counters), "is the shadow running" and "may this company be served".

**Runbook.**

- Deploy: migrate the site (or `frappe.reload_doc("jarvis", "doctype", "jarvis_settings")`) first, then restart
  the workers and the scheduler (the cron entry is new). Without the migrate the three fields are not in the
  meta: a `save()` ignores them silently and `reader_settings()` stays `off`. After the migrate check that the
  Scheduled Job Type for `jarvis.payment_calendar.shadow.dispatch` exists. A pin bump also needs a worker
  restart. A deploy by overlay patch (a diff applied to the live checkout) does not migrate: do the migrate or
  the reload step as well.
- Switch it on or off, as Administrator, through `save()` so that the Version history keeps the change. Never
  use `frappe.db.set_single_value`: it bypasses validation and history. Shadow first:

```
# bench --site <site> console
frappe.set_user("Administrator")
settings = frappe.get_single("Jarvis Settings")
settings.payment_calendar_reader = "shadow"   # off | shadow | on
settings.payment_calendar_reader_shadow_companies = "<company>"
settings.payment_calendar_reader_companies = ""
settings.save()
frappe.db.commit()
from jarvis.payment_calendar import routing
print(routing.reader_settings())   # read it back: the mode and both lists as the router will see them
```

  The playground, whose figures are already proven equal, goes straight to `on` with the company on the served
  list (the owner's decision), the same way:

```
settings = frappe.get_single("Jarvis Settings")
settings.payment_calendar_reader = "on"
settings.payment_calendar_reader_companies = "<company>"
settings.save()
frappe.db.commit()
print(routing.reader_settings())
```

- A customer company: shadow list with mode `shadow`, then wait for `shadow.summary()` (or `shadow.status`) to
  pass on the deployed build, check that the nights saw new postings, then move the company to the served list
  and set the mode to `on`.
- A shadow diff while the mode is `on`: remove the company from the served list at once (R0 below), then
  compare the diff row's digest and build with `shadow.status`. Nothing ties the served list to the gate by
  code, and the row can appear up to a day plus the next night after the cause.
- Rollback needs no restart for R0 and R1. R0: remove the company from the served list. R1: set the mode to
  `off`. The next job reads `off`; a snapshot already published stays until its `RESULT_TTL` (300 s) ends, so
  allow about 5 minutes. R2: revert the release, then migrate and restart: the revert removes the cron entry
  but the Scheduled Job Type row stays until a migrate, and the scheduler would enqueue a method that no
  longer exists every 15 minutes between 02:00 and 05:59. The Redis keys expire and the Error Log rows stay.
- Stacking: the reader (S1, #2796) must be merged, or backported to the hotfix branch, before S2. Check the
  `modified` line of `jarvis_settings.json` on each branch: it must be later than every other PR that edits
  that file, or a site that migrated the other one skips the new fields.
- Support note: figures can go back to "unavailable" after `deadline`, `assemble_budget` or `pin_drift`.

**Acceptance runs (S2).** Test code, never called at runtime; run through `bench execute` like the S1 runs.

| Function | Site | What it does |
| --- | --- | --- |
| `routed_playground(company, out, user)` | `jarvis_playground.localhost`, READ ONLY session | AC-9: both sides through the router as `user` (a visitor or Administrator) with the mode patched on, each compared with today's lifted read, ids included; records the path and the timings. The router's slot and memo keys are deleted at the end; nothing is written to the database |
| `shadow_once(company, out)` | `jarvis_playground.localhost`, READ ONLY session | AC-11: every shadow unit of the company once under the lease; writes no record, Error Log row or setting; deletes the lease key |
| `limits_at_guard_size(out, user)` | `test_jarvis` | AC-4: at 10,001 vouchers per side, as a non-Administrator user with a read-neutral `"*"` hook (patched `frappe.get_hooks`): a reader deadline of 1 s, both sides through the router (job time, peak memory, snapshot bytes), and the same at `READER_LEDGER_CAP` rows (peak memory, longest statement). Timings are recorded as numbers and flags, never asserted by a test. The fixture is rolled back |

```
PYTHONPATH=$W bench --site jarvis_playground.localhost execute jarvis.tests.payment_calendar_reader_acceptance.routed_playground --kwargs "{'company': '<company>', 'out': '$O/routed-playground.json', 'user': '<user>'}"
PYTHONPATH=$W bench --site jarvis_playground.localhost execute jarvis.tests.payment_calendar_reader_acceptance.shadow_once --kwargs "{'company': '<company>', 'out': '$O/shadow-once.json'}"
PYTHONPATH=$W bench --site test_jarvis execute jarvis.tests.payment_calendar_reader_acceptance.limits_at_guard_size --kwargs "{'out': '$O/limits-guard.json'}"
```

## Batch permission check (S2b-batch)

`permissions.visible_names` is the one visibility gate of every Payment Calendar read and publish: the
reader's `_assemble`, the voucher and party checks in `collect`, at publish and on every read
(`access._check_rows`). When a `has_permission` hook applies to the doctype, it checks every name that
`frappe.get_list` returned with one `frappe.has_permission(doctype, ptype, doc=name)` call: 4-26 ms per
document for a user with a Company User Permission. The batch replaces that loop with a faster evaluation
that returns the same set. It is behind a site switch that is **off by default**.

**Scope (what this does not do).** The reader serves a side only up to its ceilings: 100k live ledger rows
(the probe) and 50k assembled rows (`READER_MAX_ROWS`). This slice makes the permission filter fast and exact
inside those ceilings. The 1-lakh invoices per side x 10-year target is not met here: such a side is refused
by the probe at once, as today. That target stays with the cache slices (S4, S5) and the settled-parts slice (S6). S4 refuses
Company-UP users in the cache until a later amendment, so at that size no slice serves a Company-UP user yet.
**Child-row denial stays.** A Company-UP user does not see a Customer that has a Customer Credit Limit row
for another company, nor an invoice with a Tax Withholding Entry row for another company (in strict mode, or
with an empty one), because today's `has_permission` denies it. The batch is exactly today's rule (I1), so it
keeps this; a change of the rule would be a separate slice.

**How it works.** `visible_names` keeps its `get_list` step and its branch condition unchanged. When the
per-document branch is taken, it calls `batch_permission.batch_visible(doctype, allowed, ptype)`. The old
loop stays as `permissions._visible_names_per_doc` (the fallback, and the oracle of the tests).
`batch_permission` is the public entry (live gate, stop, stats, sample oracle, breaker, `status`, `reset_breaker`,
`differential`). The metadata plan is in `batch_permission_plan` and the evaluator in `batch_permission_eval`.

- The evaluator calls the real Frappe functions (`get_role_permissions`, `has_user_permission`, and the
  known `has_permission` hooks) on a read-only stand-in document (`_PermDoc`). Only the glue between them is
  transcribed from `frappe.permissions`, in Frappe's order and with its short circuits.
- Data comes from chunked SQL reads (500 names per chunk, as `get_list`): the parent columns, the child
  rows by a join on the child `parent` index, and the share rows. Table and column names come from the meta
  and information_schema, never from user input. These schema facts (table columns, whether a restricted
  master exists) live for one request or job, so no worker restart is needed after a migrate.
- Hooks are dispatched through `frappe.call` with the method strings of raw `frappe.get_hooks`, in the order
  of Frappe. A hook that the evaluator does not know runs on a real lazy document, per document.
- A document whose stand-in cannot answer (`ShimMiss`) gets the per-document call. An error from the batch's
  own SQL propagates; the function never returns a partial set.
- The first release serves users whose User Permission keys are a subset of `{"Company"}` (no User Permission,
  or Company permissions, scoped ones included).

**When it falls back to the loop (`permission_mode=per_doc`).** The reason is one fixed word; the result is
always the loop's, so it is exact and only slower.

| Reason | Meaning |
| --- | --- |
| `switch_off`, `breaker`, `breaker_unknown` | The live gate: the switch is off, a breaker key exists for the doctype, or Redis did not answer the breaker read |
| `tripped` | The batch tripped earlier in this job or request, so the rest of it takes the loop (the trip flag) |
| `small_input` | The user has no User Permission and the input has 4 names or fewer (`SMALL_INPUT`): the loop is cheaper. Not a result of the metadata plan (`batch_plan`): it is neither frozen for the job nor sampled |
| `scope_users` | The user has a User Permission on a doctype other than Company |
| `table_doctype`, `single_parent`, `virtual_parent`, `computed_table`, `virtual_child`, `no_table`, `no_column`, `custom_ptype`, `collation` | The doctype's metadata is outside what the evaluator reads exactly (any table field counts, relevant or not) |
| `controller_override` | A controller class of the doctype or of one of its child tables defines a method that changes how a document loads or reads a value, and is not on the allowlist |
| `entry_patched` | A pinned callable is not the audited one (see "Pins and identity") |
| `pin_drift` | A pinned source changed (see "Pins and identity"). It is checked first, before every other plan reason |
| `plan_error` | The plan could not be built (any unexpected error, for example an older Frappe API). One Error Log event `pcal_permission_plan_error`; a stop (timeout) still propagates |

**Time limits with the switch on (no cost estimate; the limit is exact).** With the switch off nothing below applies: the
reader wraps `frappe.has_permission` as above and a job has no permission stop (today's behaviour).

- Reader: `_permission_deadline` rebinds nothing. It enters `permission_stop(deadline + ASSEMBLE_GRACE_S)`
  around `sources._assemble`, and `check_permission_stop()` raises `ReaderFallback("deadline")` once that
  time has passed. The checks run before each `get_list` chunk, each batch chunk, each real hook call and each
  per-document call.
- Job: `run_snapshot` enters `permission_stop(job_start + JOB_PERMISSION_STOP_S)` (`JOB_PERMISSION_STOP_S =
  280`) around `collect` and the publish check. The stop raises `TimeoutError`, which is in `TIMEOUTS`: no
  router or reader frame catches it, the view rolls back, and the job fails `timeout` (the code of today's RQ
  kill), with the worker free at once. A test asserts that `JOB_PERMISSION_STOP_S` is larger than
  `JOB_BUDGET_S + READER_DEADLINE_S + ASSEMBLE_GRACE_S`, so the job stop is never active inside the reader.
- Reads (`verify_returned_rows`): no stop, as today; the batch only makes them faster.
- One mode per job: the first `batch_visible` call of a doctype in a job freezes (live gate, plan). A later
  call that differs (a breaker key set by another process, a mode change) poisons the view and fails the job
  `source_changed`. A read request has no freeze.

**Switch.** `payment_calendar_batch_permission` in `site_config.json`, default off. The value is read with Frappe's
`cint`: `1` and `"1"` allow the batch; `0`, `"0"`, `"false"`, `"off"` and an empty value give `switch_off`.

- A job reads the site config fresh at its start, so a change applies at the next job. A web request reads
  through a per-process cache with a 60 s TTL, so reads follow within 60 s. No restart is needed.
- The switch is read on every `batch_visible` call (the live gate), never from a memo.
- **Turn-on rule.** Turn it on per site only after the bounded differential (`differential`, below) is clean on
  that site's prod copy (`gate_passed` true) and the site passes the host check.
- **Host check.** Turn it on only on a self-hosted bench where (a) the operator has shell access for
  `bench --site <site> execute` and `set-config`, and (b) nothing outside the bench writes
  `site_config.json` (no hosting dashboard, configuration-management tool or container entrypoint). Write both
  answers in the site's turn-on record next to the differential result. On Frappe Cloud, shared benches, or
  any host that can push its own `site_config.json`, keep the switch off: a config push could set it to 1
  again after a trip, and the Redis breaker key alone is not durable (eviction, Redis restart). Those sites
  keep today's speed until a later amendment adds a database latch.
- **What runs with the switch off.** Only: the loop (moved to `_visible_names_per_doc`, same statements),
  `check_permission_stop()` calls that do nothing without an active stop, one `frappe.conf.get` and one trip
  flag read per per-document branch, and today's `_permission_deadline` body. No plan, no pin parse, no stat,
  no day counter, no event, no `site_count` call.
- **Deploy effect.** `batch_permission`, `batch_permission_plan` and `batch_permission_eval` are in
  `FIGURE_MODULES`, so `reader_build` (and `cache_build` once S4 lands) change at deploy, also with the switch
  off. A change to a figure module starts the count of shadow nights again (see "The gate" above).

**Pins and identity.** The batch is exact only while Frappe and ERPNext are the code that was audited. Two
checks send the batch to `per_doc` when that is not so; neither changes a result.

- **Pins.** `compatibility._PERMISSION_PINS` holds the sha256 of every Frappe and ERPNext definition that the
  per-document path runs and the evaluator transcribes or calls (and, when installed, the playground hook).
  `compatibility.permission_pin_drift()` lists the drifted names. It is separate from `reader_pin_drift()`:
  a permission pin drift never makes the reader fall back with `pin_drift`; it only gives the batch
  `permission_mode=per_doc reason=pin_drift`. A pinned file that cannot be read counts as drift. The
  parsed pins are memoised per process by (path, mtime, size): the first plan in a process parses the files
  (about 260 ms), later ones only `stat` them. Without the Redis cache below, a forked RQ job pays the parse once per job. Definition hashes
  are also cached in Redis under `jarvis:pcal:srcdefs:` for 24 h, keyed by file path, mtime and size, so a new
  worker process does not parse the pinned files again; an edited file never matches an old record.
- **Identity.** For each callable that the per-document path resolves at call time, the batch checks that the
  live object at that exact lookup name (for example `frappe.get_lazy_doc`, not
  `frappe.model.document.get_lazy_doc`) is the function defined in the pinned file. Methods resolve with
  `inspect.getattr_static` on the class that the per-document path instantiates. A wrapper from another app
  fails the check, also one made with `functools.wraps`. There is no allowlist of wrappers. Failure gives
  `permission_mode=per_doc reason=entry_patched`.
- **Lookup row ids.** `compatibility.PERMISSION_LOOKUP` lists every name that the per-document path resolves at
  call time, with an id per row. `status()` and the Error Log row name the failed ids. Kind B: used by the
  per-document path only (the batch bypasses or transcribes it). Kind S: the batch calls the same binding
  (checked anyway). Kind X: shared and not checked (`frappe.get_hooks`, `frappe.get_meta`). A child id carries
  the class name (`42:Name`). An id `allowlist:<Class>.<method>` is an allowlisted ERPNext controller method
  (`SalesInvoice.__init__`, `PurchaseInvoice.__init__`, `PaymentEntry.__init__`, `JournalEntry.__init__`,
  `SellingController.__setup__`, `BuyingController.__setup__`, `SubcontractingController.__init__`) that is no
  longer the pinned definition; a bare `allowlist` means the allowlist check itself could not run.
- **Operator note (dev bench).** An app that wraps a pinned Frappe callable keeps the batch at `per_doc`
  (`entry_patched`) in every process where the wrapper is bound. That is safe: the identity check runs in each
  process, and a job freezes one mode. On this dev bench optimus wraps `frappe.permissions.has_permission` and
  ugly_code wraps `frappe.get_all`, even on sites where those apps are not installed. Both install their wrap
  when their package is imported. `bench_helper.get_app_groups()` imports the `.commands` of every app on disk,
  so every `bench ...` process is wrapped (`bench worker`, `bench serve`, `bench execute`, `bench console`). A
  plain `frappe.init(site)` process is wrapped only when the Redis key `app_modules` is missing
  (`frappe/__init__.py:224`). A job or request init is clean unless its parent process was wrapped. So the
  workers started by `bench worker` and the bench web server stay at `per_doc`, and the mode can differ between
  processes. `status()` run through `bench execute` also reports the state of a wrapped process. The proof of
  the batch is the CI fixtures (which put the genuine bindings back) plus the playground runs (the speed check
  and the real-Chrome flow that shows `permission_mode=batch`). A playground run must come from a process where
  no wrapper is bound.

**Sample oracle and breaker.** Only in a job (`collect`), never on a read and never on a `small_input` call.
Per doctype up to 3 names are checked against a fresh `frappe.has_permission` call: first names that took a
non-default branch (a share, a child-row deny, owner-only, a hook candidate, `ShimMiss`), then a stride that
changes each run. The cap is 0.5 s per job. On a difference the batch runs again over the same input set and
the same chunk partition, with every batch memo cleared, and the sampled name is compared with a second fresh
per-document call.

| Outcome | What happens |
| --- | --- |
| Difference repeats (`mismatch`), the stop fires during the re-check (`mismatch_unresolved`), a second transient for one doctype in one site day (`transient_limit`), or the transient counter cannot be written (`transient_unknown`) | Confirmed: trip (below), an event `permission batch mismatch confirmed: doctype=<dt> reason=<reason> branch=<special|plain> direction=<batch_only|per_doc_only> user=<keyed digest> job=<id>` (operation `pcal_permission_batch_mismatch`), the day counters `permission_mismatch` and `breaker_trip`, the view is poisoned and the job fails `source_changed` (`mismatch_unresolved`: the stop's `timeout`). The side is never routed to the reader and never answered by the loop in place of the batch |
| Difference does not repeat (transient), first one for that doctype in the site day | No trip. An event `permission_mismatch_transient: doctype=<dt> branch=<...> direction=<...> user=<...> job=<...>` (operation `pcal_permission_batch_mismatch`), the view is poisoned, the job fails `source_changed`, and the day counter `permission_mismatch_transient:<doctype>` is increased. The limit is 2 per doctype per site day |

`branch` says whether the sampled name took a non-default branch (`special`) or not (`plain`); `direction` says which side allowed it; `user` is a keyed digest. The failure record of a job refused by the batch (the Error Log body of the failed job) gains `permission_detail` = `permission_mismatch`, or `permission_mode_changed` for a mode change.

**Trip.** At once, one ERROR line `permission_batch_trip doctype=<dt> reason=<reason> user=<digest> job=<id>` goes to
`logs/payment_calendar.log`. Then, in this order: (1) a Redis key `jarvis:pcal:permission_breaker:<doctype>`
with the time and the reason, **without TTL**; (2) `payment_calendar_batch_permission = 0` in the site config
(`update_site_config`, file lock); (3) a flag on `frappe.local` (it lives for that job or request only). A
failure of step (1) is queued as the event `breaker_write_failed:breaker_key` (Error Log operation
`pcal_permission_breaker`), and the other steps still run.

- **Latch key.** `jarvis:pcal:permission_breaker:_latch_write_failed` stops every doctype (reason `breaker`),
  not only the tripped one. It is set when the site-config write fails or is deferred. `status()` shows it as
  `latch_write_failed`.
- **Deferred config write.** A trip inside a job with less than 35 s before the job stop, and every
  `mismatch_unresolved` trip, does not write the site config at once (the write takes a file lock and cannot
  be cut). The Redis key, the latch key and the trip flag stop the batch at once, and the event
  `breaker_write_deferred:site_config` is queued. The job writes the config at its end, after the event
  flush. On success the latch key is deleted. On failure it stays, and the event
  `breaker_write_failed:site_config` is written. If the worker dies before that, the latch key also stays. The deferred write runs at the end of the job, still
  inside the RQ hard timeout. If the worker is killed in the middle of that write, `site_config.json` can be
  left empty: the latch key still stops the batch. Restore the file from a backup (see "If a trip left
  `site_config.json` empty or invalid" in the runbook below).
- **Effect.** A trip turns the batch off for the whole site, for every doctype, and it stays off until an
  operator resets it. The site config survives a Redis eviction or restart and the 5-minute watchdog clear
  (the scheduler job that clears the Redis cache; keys under the `persistent_cache_keys` prefix `jarvis:pcal:`
  survive it). The Redis key propagates the trip to other processes before the 60 s cache and records which
  doctype tripped.
- **Non-atomic write.** The config write truncates and then writes the file, so a process that reads the
  config in that window can fail once. This is the same window as `bench set-config`, and a trip is rare.

**Operator visibility.** Nothing is written with the switch off. With the switch on:

- Per call, a name-free record on `frappe.local`: `doctype`, `permission_mode`, `reason`, `names`, `ms`,
  `real_hook_calls`, `candidates`, `shim_miss`, `samples`, `mismatches` and `permission_refusal` (`mismatch`,
  `mismatch_unresolved`, `transient_limit`, `transient_unknown`, `mismatch_transient`, `mode_changed`). A doctype
  name is metadata; no document or user name appears.
- Log fields. The reader's counts (`permission_calls`, `permission_mode_<mode>`, `permission_reason_<reason>`,
  and the sums of the fields above) go into the reader record. With the switch on, the `reader_outcome` line of
  a side ends with `permission_mode=<batch|per_doc|mixed> permission_reason=<reason[,reason]|->`. A job also
  writes one INFO line `permission_batch permission_calls=N ...` with the same counts, only when calls were
  recorded. With the switch off neither the fields nor the line appear.
- Site-level day counters through `read_log.site_count(klass, date)` (it returns the new value; keys under
  `jarvis:pcal:`, TTL `read_log.COUNTER_TTL_S`): `permission_batch:<reason>`, `permission_mismatch`,
  `permission_mismatch_transient:<doctype>`, `breaker_trip`. `permission_batch:<reason>` counts each recorded
  per-document call by its reason (also `breaker`, `breaker_unknown`, `tripped`, `small_input`); a batch call
  adds none. With the plain switch off nothing is counted; a trip in the current job or request is counted.
- Error Log. One row per distinct event, with the title `Payment Calendar batch permission <operation>`; search
  the Error Log on the title (the `method` field). Operations: `pcal_permission_pin_drift`,
  `pcal_permission_identity`, `pcal_permission_plan_error`, `pcal_permission_plan` (a doctype runs per document
  for a reason other than `scope_users`, `pin_drift`, `entry_patched` and `plan_error`; the message names the
  doctype and the reason), `pcal_permission_batch_mismatch` and `pcal_permission_breaker`. A repeat of the same
  message dedupes inside a window. The persistent operations (`pin_drift`, `identity`, `plan`) write at most
  one row per day per distinct message; the mismatch and breaker events keep the 300 s window. Outside a
  snapshot view (a read, the publish check) an event is written at once. Inside a job's view the insert is
  refused, so the batch queues the events on `frappe.local` and `run_snapshot` writes them in a `finally`,
  after the view; an event already written is skipped by that flush. The mode-change event is
  `permission_mode_changed: doctype=<dt> user=<digest> job=<id>` (operation `pcal_permission_breaker`).
- Log lines in `logs/payment_calendar.log`: ERROR `permission_batch_trip doctype= reason= user= job=`; INFO
  `permission_batch_reset doctype= user= breakers= counters=` (written by `reset_breaker`); WARNING
  `permission_breaker_unreadable error=<Class>` (the class of the error behind `breaker_unknown`);
  `permission_event_unavailable operation= error=<Class>` (an event that could not be written: ERROR for the
  mismatch and breaker operations, else WARNING); ERROR `permission_differential_rollback_failed error=<Class>`.
- `bench --site <site> execute jarvis.payment_calendar.batch_permission.status` (kwarg `user`, default the
  session user) returns `{switch, breakers, latch_write_failed, pin_drift, identity_failures, counters, plans}`:
  the live switch (read with `cint`), the breaker keys (each with `doctype`, `reason` and `set_at`), the
  failed-latch flag, the drifted pin names, the identity failures (lookup row ids), today's `permission_*` and
  `breaker_*` day counters (read only, nothing is counted) and, for the user, `plans`
  (`{doctype: {mode, reason}}` for the six default doctypes). `plans` shows a doctype that stays per document
  for a reason that nothing else reports. No document or user name appears. `bench execute` runs as
  Administrator, so without `user` the `plans` block shows the Administrator shortcut. Pass the user to inspect:
  `bench --site <site> execute jarvis.payment_calendar.batch_permission.status --kwargs "{'user': '<user>'}"`.

**Runbook (batch permission).**

| Task | How |
| --- | --- |
| Turn on (per site) | Check the host (host check above) and write it in the turn-on record. Run the differential on the site's prod copy; it must pass. Then `bench --site <site> set-config payment_calendar_batch_permission 1`. Jobs pick it up at the next job, reads within 60 s. Run `status()` and read the first jobs' `permission_mode` |
| Turn off | `bench --site <site> set-config payment_calendar_batch_permission 0`. Same pickup time. The loop of today runs again; nothing else to undo |
| Status | `bench --site <site> execute jarvis.payment_calendar.batch_permission.status` |
| Differential, pin drift, identity failure, mismatch, reset | The entries below |

**Differential (gate).** `bench --site <site> execute jarvis.payment_calendar.batch_permission.differential`
with these kwargs:

- `users` (a list or tuple; default: every user with a User Permission, last login first, and 5 without; pass the visitor here; a string is a `TypeError`), `names` (a dict `{user: {doctype: [names]}}` with the names the calendar passes to `visible_names`, captured by the outside pilot script), `expect_batch`, `time_box_s` (default 4 h) and `show_users` (default `False`).
- Signature: `differential(users=None, *, names=None, expect_batch=..., time_box_s=4*3600, show_users=False)`. Run it in its own `bench execute`.
- Read-only: SELECT only, rolls back, writes no breaker key, site config, counter or Error Log row. The plan events that it would have written (pin drift, identity) come back in `plan_events` instead. An error in the rollback is logged (ERROR `permission_differential_rollback_failed error=<Class>`) and does not hide an earlier error.
- It calls the evaluator directly, so it runs with the switch at 0 and after a trip.
- Compared set per user and doctype: the supplied names plus SQL selectors `owner` (documents they own), `shared` (a read share for the user or for everyone), `child_row` (a relevant child row whose User Permission value is outside the allowed values or empty) and `restricted_master` (a link to a master with `restrict_to_companies = 1`).
- Each selector is capped at `DIFFERENTIAL_NAME_LIMIT` (50,000); a cut goes to `names_cut` and fails the gate.
- The time box is checked between users and between doctypes.
- It compares the batch with `frappe.has_permission` per name and returns `users` (per user and doctype: mode, reason, `batch_evaluated`, compared, supplied, selected, differences, branches), `gate_failed` (user, doctype, reason; reasons are the plan reason of a doctype that did not run as `batch`, `not_evaluated`, `doctype_missing` and `not_exercised`; a missing doctype may show `plan_error`), `not_compared` (Administrator, Guest and users with `scope_users`), `not_done` (users the box did not reach), `names_cut`, `differences` (total), `elapsed_s`, `gate_passed` and `plan_events`. Users appear as a keyed digest unless `show_users=True`.
- **Gate: `gate_passed` is true** (no `gate_failed`, no differences, no `names_cut`, no `not_done`). A pass needs at least one compared user and every `expect_batch` doctype compared with names above 0 for some user (`not_exercised` otherwise).
- A doctype in `expect_batch` (default Sales Invoice, Purchase Invoice, Payment Entry, Journal Entry, Customer, Supplier) that did not run as `batch` for a user whose User Permission keys are a subset of `{"Company"}` fails the gate with its reason.
- Run it on the prod copy of each pilot site.
- In a process where a wrapper is bound to a pinned Frappe callable (`entry_patched`, see "Operator note") the gate cannot pass, so run it from a process where no wrapper is bound.

**Pin drift.**

- `status()` or `compatibility.permission_pin_drift()` names the drifted definitions. One Error Log row with the
  title `Payment Calendar batch permission pcal_permission_pin_drift` (search the title).
- Results stay exact; the batch is `per_doc` (`pin_drift`).
- Audit the upstream diff against the evaluator's transcribed glue. Find that code with
  `grep -n "Mirrors" jarvis/payment_calendar/batch_permission_eval.py jarvis/payment_calendar/batch_permission_plan.py`:
  each block cites `frappe/<file>:<fn> L<a>-<b>` and the pin. Then add the new hash next to the old one
  with the version in the comment (as for the reader pins).
- Run the permission pin test (`jarvis.tests.test_batch_permission_pins`, on a test site), restart the workers
  and re-run the differential.

**Identity failure.**

- One Error Log row with the title `Payment Calendar batch permission pcal_permission_identity` (search the
  title) names the failed lookup rows. `status()` reports the same row ids; they map to callables in
  `compatibility.PERMISSION_LOOKUP` (legend above).
- `entry_patched` is safe (`per_doc`). Find the app that rebinds the callable at import and remove or replace
  the patch. Example for `frappe.permissions.has_permission`: read
  `frappe.permissions.has_permission.__code__.co_filename` on the console. The same check applies to the other
  rows (for example `frappe.get_all`).
- Do not add an allowlist of wrappers.

**Mismatch (trip).**

- The job failed `source_changed`; its failure record carries `permission_detail` (`permission_mismatch` or
  `permission_mode_changed`). The Error Log has a row titled
  `Payment Calendar batch permission pcal_permission_batch_mismatch` (search the title); a failed or deferred
  latch write or a mode change adds `... pcal_permission_breaker`. The log file has the ERROR line
  `permission_batch_trip`.
- The breaker key exists and the site config holds 0 (after a deferred write, it holds 0 at the end of the job). Users get today's speed from the next job.
- Find the cause, then do the reset below.

**Reset after a trip (self-hosted).**

0. When `status()` shows switch 1 together with a breaker key or `latch_write_failed` (the config write of a
   trip failed, was deferred and failed, or the worker died before it), first run
   `bench --site <site> set-config payment_calendar_batch_permission 0`, so the switch is off before you go on.
1. Run `bench --site <site> execute jarvis.payment_calendar.batch_permission.status` and read the doctype, the reason and the time.
2. Find the cause (pin, controller, data, or code fault); fix it or keep the switch off.
3. Run `bench --site <site> execute jarvis.payment_calendar.batch_permission.reset_breaker`.
4. Run the differential again on the site's prod copy; it must pass.
5. Run `bench --site <site> set-config payment_calendar_batch_permission 1`.
6. Restart the workers (`bench restart`, or the supervisor/systemd worker group). With the trip flag on `frappe.local` this is not needed for correctness, also in a no-fork worker; it clears the process memos (pin memo, `frappe.lazy_controllers`; the schema facts live for one request or job) so that the next job evaluates from fresh state.
7. Run `status()` again: switch 1, no breaker key, no pin drift, no identity failure.

`reset_breaker(doctype=None)` deletes every breaker key and the failed-latch flag, or only the key of `doctype`.
It also deletes the transient mismatch counters, returns `{breakers, latch_write_failed, transient_counters}` (what
it deleted) and writes the INFO line `permission_batch_reset`. It does not turn the switch on. After a Redis eviction the
trip stays in force through the site config (`switch_off`) until the operator sets the switch to 1.

**If a trip left `site_config.json` empty or invalid.** The config write truncates the file and then writes it,
so a crash in that window can leave it empty. Restore the file from the site backup, then go on at step 1.

**Revert the release.** Run `reset_breaker` before the revert, or delete the Redis keys
`jarvis:pcal:permission_breaker:*` after it (the old code has no `reset_breaker`). The site-config key
`payment_calendar_batch_permission` and the Error Log rows are inert for the old code.

**Frappe or ERPNext upgrade checklist.** The pins are strict, so an upgrade that changes a pinned definition sends
the batch to `per_doc` (`pin_drift`) until you act. After the upgrade: audit the changed definitions against the
evaluator, add the new hashes next to the old ones (with the version in the comment), run the permission pin
test, restart the workers, and re-run the differential (it must pass) before you rely on `permission_mode=batch`.

## Bank read: one pass and one Payment Ledger scan per view

The bank card and the "Show all accounts" dialog read company-currency book cash through
`banks.read_bank_balances`. Today it checks the finance books of the selected accounts (a GL scan and a scan
of the whole Payment Ledger), then calls `get_balance_on` once per account. Two speed-ups make that cheaper
and keep the figures and the refusals exactly equal. Both are behind the reader switch ("Reader routing (S2)")
and are **off by default**: with the switch off, or with a company that is not on the served list, today's
path runs.

**When they apply.** The reader mode is `on` and the company is on the served list (see "Reader routing
(S2)"), and the Frappe major is 16. In mode `shadow`, and in mode `off`, nothing serves from the new paths.
The mode and the lists are read once per view and shared by both speed-ups; a failed read counts as `off`.
This read happens in **every** snapshot view, also with the mode `off` and also on v15: it is one extra
`tabSingles` read per view, made before the company is checked. If that read fails with a database error
(a deadlock or a statement timeout), the answer is `off`, but the failed statement also poisons the view
(`routing._poison_view`), so a view that would have passed today can end as `source_changed`. This is rare and
transient; the next snapshot request reads again.

**One pass over the ledger.** For the selected accounts (at most 50, `banks.validate_bank_accounts`), one GL
query replaces the book scan and the per-account `get_balance_on` calls. It sums `ROUND(debit, p) -
ROUND(credit, p)` per account and finance book over the rows of the company with `is_cancelled = 0` and a
posting date up to the as-of date. `p` is the raw `get_currency_precision()` that `get_balance_on` rounds with
(0 is allowed); the reply still shows `p or 2` decimals. These rules decide the figure:

- A group whose finance book is not NULL, blank or the company default refuses `multibook_unsupported`, the
  code of today's gate. The Payment Ledger half of today's book gate still runs (see the next section).
- An account with no row is 0. The balance of an account is the exact decimal sum of its groups, converted
  once with `flt`, as `get_balance_on` does.
- Every account still gets `check_permission("read")`; a refused account raises the same error as today.
- The query body copies the audited `get_balance_on`. It runs only when the source hash of that function is
  one of the two audited 16.x hashes that include the default book (`compatibility.bank_one_pass_pinned`).
  Any other source, an unreadable source and v15 keep today's path and refuse nothing. A new ERPNext revision
  needs an audit and a pin bump, like the other pins; until then today's path runs.
- `IGNORE INDEX (company)` is a query-plan hint only. If the database says the key does not exist (MySQL error
  1176), the one pass returns nothing and the caller reads today's way, so a missing index never loses a
  figure. Any other database error propagates. The fallback is not silent in the shadow: the bank unit
  records `not_compared` with the reason `one_pass_unavailable`, so a site without the index never passes
  the gate for the bank unit.

**One Payment Ledger scan per view.** `snapshot.consistent_read` opens a view-scoped memo
(`books.book_memo`). Inside it, the first book check of a (company, as-of date) pair reads the Payment Ledger
book groups once (`account_type`, `finance_book` over the company's non-delinked rows up to the date). The
bank check uses all groups; a side check uses the groups of its account type. The view is one repeatable-read
snapshot, so every check sees the same rows and the answer equals today's by construction. The memo holds only
pairs of account type and book name. It never leaves the process, is never written, and is removed when the
view ends, also after an exception. The memo serves only when the switch is `on`, the company is served and
the source is v16. Outside a view, and in every other case, today's query runs. With the memo, one view runs
one Payment Ledger book query for the bank and both sides.

**Bank shadow unit `Bank:all`.** The nightly shadow gains a ninth unit per company, after the eight side
units. It reads the company's enabled Bank and Cash leaf accounts with today's `get_balance_on` path and with
the one pass, both in one `consistent_read()` view as Administrator. A company with more than 50 accounts
(`banks.MAX_BANK_ACCOUNTS`, the most one read takes) is not compared (`shadow_too_large`). It calls the
two paths directly, whatever the switch says, and it serves nothing. In mode `on` for a served company it also
compares the Payment Ledger book decision of the view memo with a direct query that never uses the memo; a
difference is recorded as a `diff` (with the counts `memo_refuses` and `direct_refuses`). The comparison is per account on the
text the reply shows. Outcomes:

| Outcome | Meaning |
| --- | --- |
| `compared` | Equal figures for every account |
| `diff` | At least one account differs, or one path refused where the other did not. The counts and the digests of the differing account names are recorded, never the names; an Error Log row is written once per company and night, like any shadow diff |
| `agree_refusal` | Both paths refuse with the same code, for example `multibook_unsupported` |
| `not_compared` | `fiscal_year_unavailable`, `shadow_too_large` (more than 50 accounts), `unpinned` (the `get_balance_on` source is not audited), `one_pass_unavailable` (error 1176, above), or `error:<class>` for an unexpected exception. `unpinned`, `one_pass_unavailable` and `shadow_too_large` also write one "Payment Calendar reader shadow fallback" Error Log row per company and night |

**Gate effect.** `shadow.summary` and `shadow.status` treat `Bank:all` as one more unit of every company on
the lists. A company passes the gate only when `Bank:all` also has the required number of `compared` nights
on the deployed build and no `diff` or error record. `missing` shows it as `Bank:all <n>/<nights>`. A company
without bank accounts compares an empty set and passes with `compared`. `agree_refusal` does not count as a
compared night.

**Deploy effect.** `banks` is in `routing.FIGURE_MODULES`, so `reader_build` changes at deploy, also with the
switch off. A change to a figure module starts the count of shadow nights again (see "The gate" in "Reader
routing (S2)").

- Deploy this change together with the other figure-module changes, so the count restarts once.
- Deploy outside the shadow window (02:00-05:59 site time) and never twice in one night. A deploy inside the
  window changes the number of units of a company (nine, not eight) while a chain runs: the night restarts
  once and writes a `units_changed` incomplete row, and a second change the same night stops it
  (`units_changed_again`). Records already written on the old build stay on the old build, so that night
  counts for no unit of the new build.
- Restart the workers and the scheduler at the deploy, as for any change of a figure module: `reader_build`
  is computed once per process, so a process that is not restarted still reports (and records) the old build.
  No migrate is needed: no DocType, field or index changes.

**Logs.** Three fields at the end of the collect line (`collect complete ...`) describe the bank read of a
served company, in this order: `bank_path=one_pass|today`, `bank_ms=<n>` and, only with `today`,
`bank_fallback=<cause>`. They are written only when the switch is `on`, the company is served and the snapshot
has selected accounts. With the switch off the line is byte-equal to the line before this change. No field holds
a name.

- **They are INFO diagnostics.** The production log level is ERROR (see "Reading the logs"), so a production
  file does not hold the collect line. To see it, lower the level for one process on a dev server, a staging
  site or a worker started for the purpose.
- **Causes** (`bank_fallback`): `unpinned` (the installed `get_balance_on` is not an audited source, or is
  wrapped or from another module), `v15` (the Frappe major is not 16), `missing_index` (MySQL error 1176: see
  "Index check"), `account_label` (the ledger names a selected account in a spelling that is not the selected
  name, so the one pass cannot map its rows).
- **The production signals** are two, both name-free and both visible at ERROR level or in Redis:
  1. One ERROR line per cause and worker process, `speed_fallback method=bank_one_pass:<cause> hash=<sha or
     none> erpnext=<version>` (the same line as the other speed overrides, see "Reading the logs"). It repeats
     only after a worker restart, so a cause that persists shows as one line per restart.
  2. A site day counter per cause, `jarvis:pcal:site:day:<date>:bank_today:<cause>` (one Redis string, `INCR`,
     35 days), counted once per snapshot that read `today` for a served company. It is the volume signal: a
     rising count on a company that should be served by the one pass is the alarm. Read it on the console with
     `frappe.cache.execute_command("GET", frappe.cache.make_key("jarvis:pcal:site:day:<date>:bank_today:unpinned"))`.
  `bank_ms` has no production signal; read it at INFO when the bank read is slow.

**Companies that never reach `Bank:all` `compared`.** The gate stays failed for a company whose bank unit
cannot be compared. `bench --site <site> execute jarvis.payment_calendar.shadow.status` prints the newest
bank-unit record and, under the gate, the `reasons` of the nights that were not compared. Act on the reason:

| Reason (`Bank:all`) | Cause | Action |
| --- | --- | --- |
| `fiscal_year_unavailable` | No fiscal year covers the night's date | Create the fiscal year. Without one the bank read refuses `fiscal_year_unavailable` too |
| `unpinned` | The installed `get_balance_on` is not an audited source | Audit it and bump the pin (see "Pin bump" below). Until then today's path runs and the company cannot pass |
| `one_pass_unavailable` | MySQL error 1176: the `company` index of `tabGL Entry` is missing | See "Index check" below |
| `agree_refusal` (an outcome, not a reason) | Both paths refuse alike, for example `multibook_unsupported` (a finance book other than blank or the company default holds rows) | Not a defect of the one pass, but the night does not count as `compared`: such a company cannot pass the gate while the refusal stands |
| `error:<class>` | An unexpected exception in a path | A defect: take the company off the served list and file it |

**Pin bump.** `compatibility._BANK_HELPER_HASHES` holds the audited sources of ERPNext's `get_balance_on`,
in ONE constant that both the bank-arguments adapter (`bank_balance_arguments`) and the one-pass read
(`bank_one_pass_blocker`) use. When ERPNext changes the function: audit the new source against the query in
`banks` (the filters, the per-row `ROUND`, the finance-book union, the permission and dimension checks), add the
new hash to `_BANK_HELPER_HASHES` next to the old ones, and run `test_payment_calendar_banks_one_pass` (the
`TestOnePassPin` tests fail first) and `test_payment_calendar_compatibility`. Also follow the pin bump of
"Speed overrides fall back to upstream, never refuse" for the other pins. A hash that the code does not know
gives today's path with the cause `unpinned`, never a refusal. The hash covers the body of `get_balance_on`
only: the functions it calls (`get_currency_precision`, the fiscal-year check) are not hashed, and the nightly
`Bank:all` comparison is what catches a change in them. A bump needs a worker restart.

**Index check.** The one-pass query carries `IGNORE INDEX (company)`. It needs the index named `company`
on `tabGL Entry` (ERPNext creates it from the `company` field, which has `search_index`). Check it:

```
bench --site <site> mariadb -e "SHOW INDEX FROM \`tabGL Entry\` WHERE Key_name = 'company'"
```

An empty result means error 1176 on the one pass. Restore the index through ERPNext's own schema sync
(migrate), not by hand, or leave the company on today's path.

**Mapping a digest to an account.** A bank-unit record and a diff row name accounts and the company only by
HMAC digests. In a console on the site, compute the digest of each bank account and compare:

```
# bench --site <site> console
from jarvis.payment_calendar import read_log, shadow, shadow_state
company = "<company>"
for name in shadow._bank_accounts(company):
    print(read_log.ops_digest(name), name)
digest = read_log.company_digest(company)
print(shadow_state.records(digest, "<night, YYYY-MM-DD>").get("Bank:all"))   # outcome, counts, digests
```

**Rollback.** No restart and no migrate. Remove the company from the served list or set the mode to `off`
(rollback steps R0 and R1 in the Runbook of "Reader routing (S2)"): the next job reads today's way, for the
bank and for both book checks, and a snapshot already published stays until its `RESULT_TTL` ends. With the
switch off nothing here runs except the one read of the mode per view. The figures do not change, because both
paths are exactly equal. Reverting the release (R2) changes `reader_build` back, so the nights restart again.
Records of `Bank:all` that remain in Redis after a revert are inert: no reader looks at a field that is not in
its unit list, the old build hash does not match them, and they expire with the night keys.

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
- Keep the reader switched off on v15 until S6 adds the v15 reader pins: reader mode `off`, both reader lists
  empty, and never set `payment_calendar_batch_permission` on v15. The reader answers `unsupported_major`
  there, so a side above the caps still shows `too_large`.
- A test that needs a Frappe 16 object, or a served reader read, carries `v16_only(<gate>)` from
  `jarvis/tests/payment_calendar_majors.py`, with one of its `GATE_*` constants. A test of a path that is active
  on v15 is never gated: fix the test so that it runs on v15. `test_payment_calendar_v15_inert.py` pins the
  gates.
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
| INFO | `collect complete duration_ms=<n> projections_ms=<n> payable_rows=<n> receivable_rows=<n> fast_path=<bool>` and, for a served company with selected accounts, `bank_path=<one_pass\|today> bank_ms=<n> [bank_fallback=<cause>]` | The end of one snapshot read. Hidden at the production level (ERROR). `bank_path=today` with a cause means the one-pass bank read did not run: see "Bank read: one pass and one Payment Ledger scan per view". The ERROR line `speed_fallback method=bank_one_pass:<cause>` and the site day counter `bank_today:<cause>` are the production signals. |
| WARNING | `bank_counter_unavailable` | The `bank_today:<cause>` day counter could not be written (Redis). The reply was not affected. |

**Error Log titles to filter on.**

| Title | Written by | Meaning |
| --- | --- | --- |
| `Payment Calendar API <method>` | `api._safe` | An unexpected error in an API call (`internal_error`). The body is JSON: `operation`, `exception_class`, `cause_class` (the class that was wrapped, for a hook error inside the identifier check), and `detail` (key names or a fixed code) when there is one. At most one row per operation and exception class in 300 s. |
| `Payment Calendar snapshot` / `Payment Calendar balances` | `jobs._fail_safely` | A job failed with a code other than `scope_changed`, `forbidden` or `expired`. The body has `code`, `kind`, `exception_class`, `stage` (`admission`, `sources`, `publish`) and `duration_ms`. |
| `Payment Calendar refusal loop` | `read_log.count_discard` | Three different snapshots of one user were refused and discarded inside 300 s. The body has the count and the last check kind. Look for a hook, a permission edit or a changing policy input that refuses every new snapshot. A refusal caused only by the site day changing is never counted. |
| `Payment Calendar batch permission <operation>` | `batch_permission.EVENT_TITLE` (class `batch_permission`) | A batch permission event. The `<operation>` is `pcal_permission_pin_drift`, `pcal_permission_identity`, `pcal_permission_plan_error`, `pcal_permission_plan`, `pcal_permission_batch_mismatch` or `pcal_permission_breaker`; search on the title. Written at once outside a snapshot view, and after the view inside a job (see "Batch permission check (S2b-batch)"). The body has no document or user name. Follow the runbook entry of that operation. |

**Codes.** A failed job and a failed call carry one `code` from `errors.SAFE_CODES`.

| `code` | Meaning |
| --- | --- |
| `internal_error` | An unexpected error. The Error Log row has the class and the stage. |
| `timeout` | The job passed the 300 s RQ timeout. With the batch permission switch on, also the 280 s job permission stop (`jobs.JOB_PERMISSION_STOP_S`), which ends the job first. |
| `source_changed` | The source view changed while the job read it (a write inside the repeatable-read view). With the batch permission switch on, also a confirmed or transient batch mismatch and a permission mode change inside a job (see "Batch permission check (S2b-batch)"). |
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
