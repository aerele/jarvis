import { call } from "frappe-ui";

const PREFIX = "jarvis.payment_calendar.api.";

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
export const requestSnapshot = (company, accounts) =>
	request("request_snapshot", { company, accounts });
export const getSnapshotStatus = (snapshot_id) => request("get_snapshot_status", { snapshot_id });
export const getDayDetail = (snapshot_id, date, side, page = 1, party_page = 1) =>
	request("get_day_detail", { snapshot_id, date, side, page, party_page, page_size: 25 });
export const getUpcoming = (snapshot_id, date) =>
	request("get_upcoming", { snapshot_id, date, page: 1, page_size: 30 });
export const getBucketDetail = (snapshot_id, bucket, side, page = 1, party_page = 1) =>
	request("get_bucket_detail", { snapshot_id, bucket, side, page, party_page, page_size: 25 });
