import { call } from "frappe-ui";

const PREFIX = "jarvis.payment_calendar.api.";
// Keep in sync with DEFAULT_DIRECTION in usePaymentLedger.js and the server sort whitelist.
const SORT_FIELDS = ["remaining", "due_date", "party"];
const SORT_ORDERS = ["asc", "desc"];
const PAGE_SIZE = 25;

async function request(method, args = {}) {
	try {
		return await call(PREFIX + method, args);
	} catch (error) {
		const denied = [error?.status, error?.httpStatus, error?.statusCode].some((status) =>
			[401, 403].includes(status)
		);
		return {
			ok: false,
			state: denied ? "forbidden" : "failed",
			code: denied ? "forbidden" : "network_error",
			retryable: !denied,
		};
	}
}

export const getCapabilities = (company) =>
	request("get_capabilities", company ? { company } : {});
export const getPreferences = () => request("get_preferences");
export const setPreferences = (company, accounts) =>
	request("set_preferences", { company, accounts });
export const configureFloor = (company, value, expected_revision, configuration_id) =>
	request("configure_floor", { company, value, expected_revision, configuration_id });
export const requestSnapshot = (company, accounts, month) =>
	request("request_snapshot", { company, accounts, ...(month ? { month } : {}) });
export const getSnapshotStatus = (snapshot_id) => request("get_snapshot_status", { snapshot_id });
export const getDayDetail = (
	snapshot_id,
	date,
	side,
	page = 1,
	party_page = 1,
	sorting,
	page_size = PAGE_SIZE
) =>
	request("get_day_detail", {
		snapshot_id,
		date,
		side,
		page,
		party_page,
		page_size,
		parties: false, // the ledger never reads the party summary
		...sortArgs(sorting),
	});
export const getUpcoming = (snapshot_id, date) =>
	request("get_upcoming", { snapshot_id, date, page: 1, page_size: 30 });
// bucket: today | immediate | now | window | overdue | undated
export const getBucketDetail = (
	snapshot_id,
	bucket,
	side,
	page = 1,
	party_page = 1,
	sorting,
	page_size = PAGE_SIZE
) =>
	request("get_bucket_detail", {
		snapshot_id,
		bucket,
		side,
		page,
		party_page,
		page_size,
		parties: false, // the ledger never reads the party summary
		...sortArgs(sorting),
	});
export const requestAccountBalances = (company) =>
	request("request_account_balances", { company });
export const getAccountBalancesStatus = (job_id) =>
	request("get_account_balances_status", { job_id });

// Only whitelisted values are sent; omitted keys keep the server default.
function sortArgs(sorting) {
	const { sort, order } = sorting ?? {};
	return {
		...(SORT_FIELDS.includes(sort) ? { sort } : {}),
		...(SORT_ORDERS.includes(order) ? { order } : {}),
	};
}
