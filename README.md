# Jarvis

**The AI teammate inside ERPNext.**

Your ERP already knows what happened. Jarvis helps your team ask what it means,
prepare what should happen next, and repeat good work without losing human
judgement.

**Ask. Verify. Approve. Repeat.**

## What makes Jarvis different

Jarvis is not a chat window pointed at your database. Three things hold, always:

- **It acts as you.** Every question and every action runs under your own Frappe
  and ERPNext permissions. Jarvis can never see or touch a record you could not
  open yourself.
- **Nothing important happens without you.** Reading and explaining happen
  directly. Anything that writes, submits, or is hard to reverse waits for a
  person to review and approve it, with the evidence in view.
- **Your data stays on your bench.** Your ERP records never leave your server.
  The AI reaches them only through a single, permission-checked door, never a
  copy of your books.

## Features

### Ask your business in plain language

- Ask about cash, sales, purchases, stock, customers, suppliers, projects, or
  any other records you are allowed to see.
- Trace an answer back to the records, dates, and evidence behind it.
- Investigate a question, compare options, prepare a draft, and continue the
  work in one conversation.
- Speak a request or attach an image, PDF, spreadsheet, or business document
  when typing would leave out useful context.
- Name, star, search, and return to conversations without mixing unrelated
  decisions.

### Turn incoming documents into reviewed work

- Drop supplier bills, statements, price lists, and other source files into
  **File Box**.
- Let Jarvis read the source, prepare the corresponding work, and surface any
  uncertainty instead of quietly guessing.
- Use the **Approval Board** to inspect drafts, answer questions, approve the
  next step, or reject it with the source conversation still in reach.
- Preview spreadsheet imports before any rows are added.
- Export useful records, reports, and prepared documents when the work needs to
  leave the screen.

### Turn good judgement into team practice

- Add **Business Notes** for the terms, exceptions, and operating facts that
  make your company different.
- Use the **Wiki** to see what Jarvis knows, where it came from, who it applies
  to, and whether it needs correction.
- Save a business rule as a **Skill** so Jarvis follows it consistently next
  time.
- Save several steps as a **Macro**, run them again, or put them on a daily,
  weekly, or monthly schedule.
- Create a **Trigger** for work that should begin when an important ERPNext
  record changes.
- Install an **Agent** to check a defined area in the background and bring back
  findings or prepared work.
- Review suggested learning before it becomes a personal or shared rule.

### See the business and find the work faster

- Turn a question into a saved **Dashboard** and share the view with the right
  people.
- Search chats, dashboards, records, lists, and reports from one command
  palette.
- Filter, sort, and choose columns for busy work queues.
- Receive notifications when work finishes or needs a decision.
- Continue conversations, approvals, File Box work, and key business views from
  the mobile app.

### Control & administration

- Administrators may allow ordinary, reversible changes within a conversation;
  proposed work and its evidence remain visible for review.
- Connect a supported chat subscription you already pay for or an approved API
  key, choose the models available to the team, and set backups when needed.
- Review personal and team usage, limits, plan details, renewals, and activity
  from the workspace.

## How to Install

Jarvis supports Frappe 15 and 16.

### Self-hosted bench

Run these commands from your Frappe bench:

```bash
# On a Frappe 15 site, use --branch version-15 instead
bench get-app https://github.com/aerele/jarvis.git --branch version-16
bench --site your-site.example install-app jarvis
```

### Frappe Cloud

On a private bench group:

1. Open **Bench Group > Apps**, choose **Add App**, and add
   `https://github.com/aerele/jarvis.git` from the `version-16` branch
   (`version-15` for a Frappe 15 site).
2. Deploy the bench update to your site.
3. Open **Site > Apps**, choose **Install App**, and install **Jarvis**.

After installation, sign in as a System Manager, open `/jarvis/onboarding`, and
follow the setup shown on screen.

### Updating PDF chart support

Deploy the backend and its Python requirements (including Matplotlib) before the
matching plugin schema and PDF skill. No schema migration is required for numeric
charts. Check a synthetic chart export after restarting workers: first-use font
loading shares the document's 25-second render budget. The isolated chart worker
uses bundled DejaVu Sans and disables external font-discovery commands.
Font metadata is cached per serving process; restarts or worker
recycling reset it, and a separate command-line warm-up does not warm web workers.
A timeout saves no document; omitted charts are reported in
`notes` and excluded from `chart_count`. Check these fields before treating a
report as complete. An unavailable chart renderer requires checking the backend's
installed dependencies; unsupported chart text can be preserved in a table.

Once numeric charts have been exported, their private render sidecars also retain
the typed chart specs for later regeneration. The plugin and persona can be
rolled back first, but a backend rollback must retain typed-chart support or
explicitly reject replay of those sidecars. Do not downgrade to the legacy-only
chart parser: it can misread numeric charts as zero-width progress bars without a
warning. Existing generated PDF files remain usable.

### AP document review: requirements, design and release boundary

AP Invoice Exception Operator v0.2 reviews **existing draft Purchase Invoices**;
File Box already creates those drafts. It does not create duplicate invoices,
submit, pay, or turn a review decision into a posting instruction. Manual runs
only: preview saves findings, while live mode also creates immutable, deduplicated
Approval Board review requests. Only the named reviewer can decide; acknowledgement
does not resume a delegate. ERPNext remains responsible for posting validations.

The requirements re-check separates commercial controls from legal standards:

| Requirement / accounting consideration | Implemented control / explicit boundary |
| --- | --- |
| Occurrence, accuracy and completeness of invoice matching | Exact company/supplier/item/PO/receipt child links; cumulative submitted **and draft** allocations; net price per stock UOM; source precision and explicit saved price tolerance. A match is a review candidate, not proof that goods exist or the supplier bill is authentic. |
| Evidence and documentation | Immutable run input/policy snapshots, source digest, deterministic private assessment, traceable findings and reviewer decisions. ICAI [SA 500](https://kb.icai.org/pdfs/PDFFile5b3b56cb154046.31830553.pdf) and [SA 230](https://kb.icai.org/pdfs/PDFFile5b3b4ec0607a15.03386623.pdf) inform the evidence/documentation design where applicable; this is not a statutory audit opinion. |
| Cutoff, fiscal-year boundaries and historical accuracy | Explicit company/from/to/report dates. Draft selection uses posting dates; prior-period linked sources remain available. Later receipts cannot establish receipt at the draft posting date. Current records are not a reconstruction of historical knowledge; [IAS 10](https://www.ifrs.org/issued-standards/list-of-standards/ias-10-events-after-the-reporting-period/) treatment is not inferred from a match. |
| Inventory cost / FX measurement | [IAS 2](https://www.ifrs.org/issued-standards/list-of-standards/ias-2-inventories/) and [IAS 21](https://www.ifrs.org/issued-standards/list-of-standards/ias-21-the-effects-of-changes-in-foreign-exchange-rates/) may govern measurement under IFRS, but a document match does not establish valuation or exchange-rate compliance. Cross-currency matching is not evaluable. |
| Returns, reversals, amendments and cancellations | Cancelled invoices do not reserve quantities. Amended documents must have their own valid source chain. Returns, credits and stock-updating invoices require manual reconciliation; no unsigned subtraction or automatic release of capacity. |
| Duplicate bills | Exact company/supplier/bill-number candidates only; annual reuse and legitimate corrections require judgement. No claim of a confirmed duplicate payment or recovered cash. |
| Permissions and incomplete evidence | Full-company visibility is checked without exposing hidden rows. Missing datasets, field evidence, truncation and source changes cannot produce ready/clean results. Evidence is rechecked at writeback and acknowledgement. |
| Tax / entity-specific compliance | Jurisdiction, entity framework and GST/TDS applicability are **unconfirmed**. No tax rates, credit eligibility, withholding decisions or legal certification are implemented. These require a separately approved, effective-dated rule scope. |

The implementation plan was revised to remove imaginary OCR intake records,
duplicate-PI creation, fixed hidden rounding/tolerances, intake-window filtering of
linked sources, and GL-only freshness checks. Matching code stays in the private
bundle; the bench owns permission-bounded collection, provenance and review custody.
The plugin exposes only the new run-bound input read. No ERP mutation tool is granted.
Different-period runs never auto-resolve AP findings; human disposition is explicit.

Configure `company`, `from_date`, `to_date`, `report_date`, `policy_version`,
`price_tolerance_percent`, `basis="current_records"` and
`review_scope="document_matching_only"`. All are required. Configure offers visible,
unsaved starter settings: `AP-MATCH-v1` (a reference, not verified approval), 0% net-price
tolerance at source currency precision, current records and document matching only.
These are conservative application defaults, not thresholds mandated by accounting
standards; confirm them with the named reviewer. Saved settings and subsequent edits
are never replaced by an asynchronous suggestion.

Company suggestions use the user's readable ERPNext default (or a single readable
company). Fiscal-year choices use active, permission-checked ERPNext records applicable
to that company, including short years; no calendar or jurisdiction is assumed. A
unique year containing the existing period, or today's site date for an empty period,
is suggested. New date fields use its start through the earlier of year end and today;
the cutoff is capped at today. Explicitly selecting another year resets the date fields.
Missing, ambiguous, future or unreadable calendars never produce guessed periods.
Saved `fiscal_year`, when present, is validated against the company and posting-date
window and retained in the immutable run scope. Clear Fiscal year for a custom
cross-year period; older explicit-date configurations remain valid. Prior-year linked
orders and receipts are still included, regardless of this intake selector.

Configure shows field-level guidance from the
same server validator used at launch. Incomplete setup can be saved, but Run Now
stays unavailable until configuration is valid and saved; unsaved edits also block
launch. The capture is bounded to 500 documents per
source DocType and 10,000 total child rows; larger companies return not-evaluable,
not a partial-population clean result. Capacity expansion requires a separately
tested, complete allocation fetch; narrowing the input window alone is not safe.

Release is paired: migrate Jarvis's Run/Approval Request fields, deploy the matching
plugin tool contract, then deliver the private v0.2 bundle and matching ID-only
registry. Do not advertise availability by changing catalogue visibility alone.
Existing in-flight runs retain their launch tool contract and cannot gain new writes.

Verification: `jarvis.tests.test_purchase_match` exercises permission degradation,
scope/digest/omission rejection, stale evidence, preview/live separation and native
ERPNext PO → submitted receipt → draft PI → idempotent human review without posting.
Set `AP_ASSESSMENT_PATH` to the private bundle's `assessment.py` when running this
suite to test real native evidence through the shipped executable and bench validator.
The bundle's `test_assessment.py` imports the actual runtime and validates its wire
envelope; it replaces the former standalone staging oracle. UI, plugin tool-contract,
bundle-delivery and existing platform tests are additional release gates.

### AR collections review: scope, controls and release boundary

AR & Collections Operator v0.2 is **review and drafts only**, framework-neutral.
Manual preview creates findings; live mode additionally stages named-reviewer
requests. Reminder text stays inside the review evidence, without a recipient.
No Dunning, Communication, payment, journal, reconciliation, fee, interest or
write-off is created. Review acknowledgement never resumes the delegate.

The architecture re-check replaces the former model-reproduced staging oracle,
fixed two-decimal rounding, hidden risk score and escalating final notices with
a shipped deterministic private assessment and shared operator evidence custody.
Native Dunning is deliberately not used: its mapper can inherit fees/interest and
its currency/payment-schedule semantics require a separate approved write scope.

| Requirement / applicable consideration | Control / boundary |
| --- | --- |
| Receivable completeness and cutoff | Company-wide invoices, typed Payment Ledger Entries and settlement documents; opening receivables remain included across fiscal years. Current records only, not historical reconstruction. Future allocations and historical cutoffs cannot produce current reminder demands. |
| Accuracy and ageing | Reconcile exact Decimal ledger totals to native outstanding at source precision; use recorded payment-term due dates and allocations. Due today is not overdue. Unsupported FX, finance-book slices and unreconciled instalments require manual review. See ERPNext [payment terms](https://docs.frappe.io/erpnext/payment-terms). |
| Offsetting and currencies | Never invent offsets across customer, company, account or currency, or allocate unallocated receipts/credits. Under IFRS, [IAS 32](https://www.ifrs.org/issued-standards/list-of-standards/ias-32-financial-instruments-presentation/) sets separate offsetting conditions and [IAS 21](https://www.ifrs.org/issued-standards/list-of-standards/ias-21-the-effects-of-changes-in-foreign-exchange-rates/) governs FX measurement; neither is established by a collection candidate. |
| Impairment and credit risk | Oldest due date is operational ordering, not a loss estimate or credit-limit breach. [IFRS 9](https://www.ifrs.org/issued-standards/list-of-standards/ifrs-9-financial-instruments/) impairment requirements require a separate applicable framework and model. No ECL, default probability or recovery claims. |
| Reversals and adjustments | Ignore delinked allocations and cancelled invoices; verify active ledger vouchers are submitted. Amendments keep their own identity. Credits, returns, draft settlements and recent allocations hold follow-up for reconciliation, without assuming cash receipt or a tax deduction. |
| India adaptation | Enable the India checklist only when India Compliance is installed **and** the selected company is in India. Retain readable GST/IRN metadata as recorded; review credit/debit notes and withholding evidence. [India Compliance validations](https://docs.indiacompliance.app/docs/miscellaneous/transaction_validations) and [CBIC section 34](https://taxinformation.cbic.gov.in/content-page/explore-act/1000304/1000001) guide the document-review boundary. Installation does not determine Ind AS/AS applicability, e-invoice mandates, tax rates or filing compliance. |
| Evidence and authority | Run-bound input, settings, app context and output snapshots; source digest; complete reference set; freshness/read-access rechecks before writeback and acknowledgement; deduplicated immutable reviews. No automatic resolution of prior-period findings. Shared AP custody keeps the same AP behavior. |

Required settings: `company`, `from_date`, `to_date`, `report_date`, `policy_version`,
`settlement_hold_days`, `basis="current_records"`, `review_scope="review_and_drafts_only"`.
Use the shared fiscal-year picker. Its period is review context, never an opening-debt
filter. Visible suggestions are `AR-REVIEW-v1` and a three-**calendar**-day settlement
hold (0–30); these are application defaults, not statutory grace periods or verified
policy approval. Saved policies and later edits are preserved. Missing/ambiguous
fiscal calendars are not guessed. Historical reviews never prepare reminder text.

Disputes, contact authority, unrecorded receipts and external withholding certificates
are not inferred from ERP silence; a human must check them. Missing, unreadable,
truncated or changing inputs cannot imply a clean book. The first bounded collector
marks standalone non-invoice debit balances for manual native AR reconciliation;
an invoice-only result must not certify those opening journals. It
supports 500 documents per source, 10,000 ledger rows and 10,000 child rows. Exceeding
capacity degrades the review instead of silently sampling. Monetary finding totals
remain zero; the note identifies each balance and currency without mixing currencies.

Release requires paired bench registry/tool contract, plugin and private bundle delivery.
AP/AR v0.2 are Published in the private manifests and the paired bench registry.
Publication does not change fleet visibility, promote preview installations or grant ERP
writes. Existing v0.1 configurations require explicit review settings.
Run `jarvis.tests.test_receivables_review` with `AR_ASSESSMENT_PATH` pointing to the
private `assessment.py` for real submitted-invoice/payment-ledger/stale-review coverage.
The private bundle tests import that runtime directly, not a separate policy oracle.
