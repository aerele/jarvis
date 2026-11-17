import { onScopeDispose, ref, shallowRef } from "vue";
import * as calendarApi from "@/api/paymentCalendar";
import { nextPollDelay } from "@/lib/paymentCalendar/polling";

// "Show all accounts": one async server job over the catalog (D5, B5). The server sends the
// total; the client only waits for the job, polls with the snapshot backoff (15 s while the tab
// is hidden) and reuses a completed job of the same company until its expires_at. A reuse
// reads the job's status once and shows that reply, so the server re-checks access. A
// scope_changed job is discarded and requested once more on its own.
const GIVE_UP_MS = 900000;
// Refusals after which a cached job can no longer be trusted.
const DISCARD_CODES = ["scope_changed", "expired", "not_found_or_expired", "forbidden"];

export function useAccountBalances({ api = calendarApi } = {}) {
	const state = ref("idle"), // idle | loading | ready | failed
		result = shallowRef(null),
		errorCode = ref("");
	let token = 0,
		timer = null,
		cache = null;

	onScopeDispose(stop);

	function stop() {
		token++;
		clearTimeout(timer);
		timer = null;
	}

	// Back to idle (dialog closed, view switched): the cached job stays for the next open.
	function reset() {
		stop();
		state.value = "idle";
		result.value = null;
		errorCode.value = "";
	}

	async function load(company) {
		stop();
		errorCode.value = "";
		if (cache?.company === company && Date.parse(cache.result.expires_at) > Date.now())
			return recheck(company, token, cache.jobId);
		await start(company, token, false);
	}

	// A cached job is shown only after the server confirmed it again. The reply decides:
	// complete is shown, a refusal fails, anything else means the job is gone: ask anew.
	async function recheck(company, mine, jobId) {
		cache = null;
		result.value = null;
		state.value = "loading";
		const response = await api.getAccountBalancesStatus(jobId);
		if (mine !== token) return;
		if (!response?.ok) return fail(response?.code || "network_error", mine);
		if (response.data?.state === "complete")
			return finish({ mine, company, jobId }, response.data);
		await start(company, mine, false);
	}

	// `retried` is true on the one automatic second request after a scope_changed.
	async function start(company, mine, retried) {
		cache = null;
		result.value = null;
		state.value = "loading";
		const response = await api.requestAccountBalances(company);
		if (mine !== token) return;
		if (!response?.ok) return fail(response?.code || "network_error", mine);
		const jobId = response.data?.job_id;
		if (!jobId) return fail("internal_error", mine);
		await poll({ mine, company, jobId, retried, started: Date.now(), attempt: 0 });
	}

	async function poll(job) {
		if (Date.now() - job.started >= GIVE_UP_MS) return fail("timeout", job.mine);
		const response = await api.getAccountBalancesStatus(job.jobId);
		if (job.mine !== token) return;
		const data = response?.ok ? response.data : null;
		if (data?.state === "complete") return finish(job, data);
		const code = response?.ok ? data?.code : response?.code;
		if (code === "scope_changed" && !job.retried) return start(job.company, job.mine, true);
		if (!response?.ok) return fail(response?.code || "network_error", job.mine);
		if (data?.state === "failed") return fail(data.code || "internal_error", job.mine);
		if (!["queued", "running"].includes(data?.state)) return fail("expired", job.mine);
		const delay = nextPollDelay(job.attempt);
		timer = setTimeout(() => void poll({ ...job, attempt: job.attempt + 1 }), delay);
	}

	function finish(job, data) {
		if (!Array.isArray(data.accounts) || typeof data.total !== "string")
			return fail("internal_error", job.mine);
		const finished = {
			generated_at: data.generated_at,
			expires_at: data.expires_at,
			currency: data.currency,
			precision: data.precision,
			accounts: data.accounts,
			total: data.total,
		};
		// Without an expiry there is nothing to reuse it against.
		cache = Date.parse(data.expires_at)
			? { company: job.company, jobId: job.jobId, result: finished }
			: null;
		result.value = finished;
		state.value = "ready";
	}

	function fail(code, mine) {
		if (mine !== token) return;
		if (DISCARD_CODES.includes(code)) cache = null;
		result.value = null;
		errorCode.value = code;
		state.value = "failed";
	}

	return { state, result, errorCode, load, reset };
}
