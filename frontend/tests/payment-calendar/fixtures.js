import { vi } from "vitest";
export const SID = "a".repeat(32);
export const ok = (data) => ({ ok: true, data });
export const refusal = (code, state = "unavailable") => ({
	ok: false,
	code,
	state,
	retryable: state !== "forbidden",
});
export function deferred() {
	let resolve;
	const promise = new Promise((done) => {
		resolve = done;
	});
	return { promise, resolve };
}
export function complete(company = "Manufacturing", accounts = ["Bank A"], id = SID) {
	const days = {};
	for (const day of ["24", "25", "26", "27"]) {
		days[`2026-10-${day}`] = {
			date: `2026-10-${day}`,
			payable_remaining: "10.00",
			receivable_remaining: "20.00",
			payable_terms: 1,
			payable_invoices: 1,
			payable_parties: 1,
			payable_rows_due: 1,
			settlement: "Unpaid",
			projected_opening: "100.00",
			projected_closing: "110.00",
			risk: "green",
		};
	}
	return {
		snapshot_id: id,
		state: "complete",
		currency: "INR",
		generated_at: "2026-10-24T12:00:00+00:00",
		expires_at: "2026-10-24T12:05:00+00:00",
		scope: {
			company,
			accounts,
			anchor: "2026-10-24",
			selectable_start: "2025-10-01",
			selectable_end: "2027-09-30",
			window_start: "2025-10-01",
			window_end: "2027-10-30",
		},
		projection: {
			anchor: "2026-10-24",
			days,
			precision: 2,
			book_cash: "100.00",
			floor: "20.00",
			forecast_available: true,
			scope_reasons: [],
			due_now: { scheduled: "10.00", overdue: "0.00", undated: "0.00", total: "10.00" },
			excluded_collections: { overdue: "0.00", undated: "0.00", adjustments: "0.00" },
			reconciliation: {
				Payable: {
					canonical_total: "10.00",
					current_future: "10.00",
					overdue: "0.00",
					undated: "0.00",
					outside_horizon: "0.00",
					adjustments: "0.00",
				},
				Receivable: {
					canonical_total: "20.00",
					current_future: "20.00",
					overdue: "0.00",
					undated: "0.00",
					outside_horizon: "0.00",
					adjustments: "0.00",
				},
			},
		},
		floor: {
			configured: true,
			company,
			currency: "INR",
			safety_floor: "20.00",
			revision: 1,
			configuration_id: "floor",
		},
		unavailable: {},
		bank_code: null,
	};
}
export function detail(date = "2026-10-24", side = "Payable", page = 1, partyPage = 1) {
	return {
		rows: [
			{
				id: side + date,
				party_type: side === "Payable" ? "Supplier" : "Customer",
				party: "Party",
				party_name: "Cedar Trading",
				voucher_type: side === "Payable" ? "Purchase Invoice" : "Sales Invoice",
				voucher_no: date + "-page-" + page,
				due_date: date,
				term: "Net 30",
				original: "10.00",
				remaining: "10.00",
				paid: "0.00",
				credit: "0.00",
				discount: "0.00",
				settlement: "Unpaid",
				source_link: "/app/purchase-invoice/example",
			},
		],
		parties: [
			{
				party_type: "Supplier",
				party: "Party",
				party_name: "Cedar Trading",
				remaining: "10.00",
				terms: 1,
				invoices: 1,
			},
		],
		total: 30,
		party_total: 30,
		page,
		party_page: partyPage,
		page_size: 25,
		has_more: page === 1,
		generated_at: "2026-10-24T12:00:00+00:00",
	};
}
export function fakeApi() {
	let current = { company: "Manufacturing", accounts: ["Bank A"] };
	const companies = [
		{ name: "Manufacturing", default_currency: "INR" },
		{ name: "Services", default_currency: "INR" },
	];
	return {
		getCapabilities: vi.fn(async (company) =>
			ok({
				companies,
				accounts: company
					? [
							{
								name: company === "Services" ? "Bank B" : "Bank A",
								account_name: "Operating bank",
								account_currency: "INR",
							},
					  ]
					: [],
				settings: complete(company).floor,
				can_configure: true,
			})
		),
		getPreferences: vi.fn(async () => ok({ company: "Manufacturing", accounts: ["Bank A"] })),
		setPreferences: vi.fn(async (company, accounts) => ok({ company, accounts })),
		requestSnapshot: vi.fn(async (company, accounts) => {
			current = { company, accounts };
			return ok({
				snapshot_id: SID,
				state: "queued",
				queued_at: "2026-10-24T12:00:00+00:00",
			});
		}),
		getSnapshotStatus: vi.fn(async () => ok(complete(current.company, current.accounts))),
		getDayDetail: vi.fn(async (id, date, side, page, parties) =>
			ok(detail(date, side, page, parties))
		),
		getUpcoming: vi.fn(async () => ok({ rows: [], total: 0 })),
		getBucketDetail: vi.fn(async () => ok(detail())),
		configureFloor: vi.fn(async () => ok(complete().floor)),
	};
}
