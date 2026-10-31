# Landed-cost source checks

Use the dedicated landed-cost preview and create/update/submit tools. The check
uses whole linked Purchase Invoices, current read access, recorded treatment and
posted input-tax rows. It does not capitalize nonrecoverable GST. A missing,
unsupported, changed or unreadable treatment returns an input gap.

## Runtime support

The native vendor-invoice flow requires ERPNext 16 with `vendor_invoices` and the
`Landed Cost Vendor Invoice` child table. ERPNext 16.52.1 is the tested release
baseline. Older feature-equivalent builds can use the fallback when
`get_claimed_landed_cost` is absent. The fallback sums submitted child rows,
excludes the current voucher, and uses row locks for a write. It does not make a
v15 schema a native v16 schema. Use the separate v15 backport for that case.

Every linked invoice has a net-cost ceiling, including untaxed invoices with an
invoice-level discount. Add the current claim, native prior claims and legacy
claims before comparing with `base_net_total`. Preserve permission denials and
transaction retry errors. Other classifier faults return a safe input gap and
log only the exception class.

A linked, uncancelled adjustment on the expected input accounts invalidates the
original invoice's posting proof. Finance must reconcile it first. Adjustments
that carry no invoice reference cannot be attributed by this check. It does not
prove current statutory ITC eligibility or replace an accounts review.

## Generic tool RPCs

Known client/save/workflow/bulk mutation routes refuse an LCV target before
execution, including routes that can enqueue work. Use the dedicated tools.
Other synchronous tool RPC document writes pass the source-tax gate through
`before_validate` and `before_submit` hooks. A request-scoped context resets on
success or failure. Ordinary Desk saves do not enter this scope. Unknown
operator-authored asynchronous jobs do not inherit it.

Raw LCV controller helpers can return source money without the dedicated
preview's field checks. Controller calls use the dedicated landed-cost tools;
cancellation remains available through its normal confirmation path.

## Verification and release

CI fixtures exercise GST classification without an installed India Compliance
app. They supply recorded classifier outputs; they are not tax-law tests. A
native legacy-column fixture executes the real submitted-claim SQL, including
self exclusion, native-link exclusion, row locking and negative-claim refusal.
The native suite also runs the installed India Compliance integration checks.

The existing CI test-job ceiling changes from 25 to 35 minutes so full shards
can complete. This changes the job ceiling, not a parser or transaction budget.

Deploy from the reviewed develop change. Restart workers to load the new document
hooks. Run `bench migrate` as part of the normal app upgrade. No production field
or data migration is added here. Do not ship the v15 backport before this change
is merged and its supported-version checks pass. Rebase the PR onto current
develop before merge so the merged receipt-summary checks stay present.
