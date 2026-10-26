import { computed, onScopeDispose, ref, shallowRef } from "vue";
import * as calendarApi from "@/api/paymentCalendar";
import { siteTimezone } from "@/utils/datetime";
import {
	addMonths,
	isSelectable,
	msUntilSiteMidnight,
	siteToday,
} from "@/lib/paymentCalendar/dates";

const emptyPages = () => ({
	Payable: { rows: 1, parties: 1 },
	Receivable: { rows: 1, parties: 1 },
});
const safeError = (result) => ({
	code: result?.code || "internal_error",
	state: result?.state || "failed",
	retryable: result?.retryable !== false,
});

export function usePaymentCalendar({
	api = calendarApi,
	timezone = siteTimezone,
	initialDate = "",
} = {}) {
	const phase = ref("loading"),
		companies = ref([]),
		company = ref(""),
		accounts = ref([]),
		caps = shallowRef(null);
	const snapshot = shallowRef(null),
		snapshotId = ref(null),
		selectedDate = ref(""),
		month = ref("");
	const detail = shallowRef(null),
		detailLoading = ref(false),
		upcoming = ref([]),
		upcomingLoading = ref(false);
	const activeView = ref("payees"),
		pages = ref(emptyPages()),
		error = shallowRef(null),
		notice = ref("");
	const floorSaving = ref(false),
		floorError = shallowRef(null),
		preferenceSaving = ref(false),
		preferenceError = shallowRef(null);
	const bucket = ref(null),
		bucketData = shallowRef(null),
		bucketLoading = ref(false),
		bucketPages = ref({ rows: 1, parties: 1 });
	const side = computed(() => (activeView.value === "customers" ? "Receivable" : "Payable"));
	const day = computed(() => snapshot.value?.projection?.days?.[selectedDate.value] || null);
	let generation = 0,
		detailSequence = 0,
		upcomingSequence = 0,
		bucketSequence = 0,
		disposed = false;
	let pollTimer,
		expiryTimer,
		midnightTimer,
		pollStarted = 0,
		recoveries = 0;
	let preferencePending = null,
		preferenceDraining = false;
	const handledMidnights = new Set();
	const current = (token) => !disposed && generation === token;
	const sameSelection = (value) =>
		value.company === company.value &&
		JSON.stringify(value.accounts) === JSON.stringify(accounts.value);
	const stopTimers = () => {
		clearTimeout(pollTimer);
		clearTimeout(expiryTimer);
		clearTimeout(midnightTimer);
	};

	function clearFinance() {
		snapshot.value = null;
		snapshotId.value = null;
		detail.value = null;
		upcoming.value = [];
		bucket.value = null;
		bucketData.value = null;
		detailLoading.value = false;
		upcomingLoading.value = false;
		bucketLoading.value = false;
		detailSequence++;
		upcomingSequence++;
		bucketSequence++;
	}
	function begin(nextPhase) {
		generation++;
		stopTimers();
		clearFinance();
		error.value = null;
		phase.value = nextPhase;
		return generation;
	}
	function fail(result, token = generation) {
		if (!current(token)) return;
		if (result?.state === "expired") return expire({ result });
		if (result?.state === "forbidden") {
			caps.value = null;
			accounts.value = [];
		}
		begin(
			result?.state === "forbidden"
				? "forbidden"
				: result?.state === "expired"
				? "expired"
				: "failed"
		);
		error.value = safeError(result);
	}

	// One write at a time makes the latest selection the last server-side effect,
	// including when the response to an earlier save was delayed or lost.
	function saveSelection() {
		if (!company.value) return;
		preferencePending = { company: company.value, accounts: [...accounts.value] };
		if (preferenceDraining) return;
		preferenceDraining = true;
		void (async () => {
			while (preferencePending) {
				const value = preferencePending;
				preferencePending = null;
				if (!disposed) preferenceSaving.value = true;
				const result = await api.setPreferences(value.company, value.accounts);
				if (!disposed && !preferencePending && sameSelection(value))
					preferenceError.value = result?.ok ? null : safeError(result);
			}
			preferenceDraining = false;
			if (!disposed) preferenceSaving.value = false;
		})();
	}

	async function initialize() {
		const token = begin("loading");
		const preferenceRead = api.getPreferences();
		const result = await api.getCapabilities();
		if (!current(token)) return;
		if (!result?.ok) return fail(result, token);
		companies.value = result.data.companies || [];
		phase.value = companies.value.length ? "choose_company" : "forbidden";
		const preferences = await preferenceRead;
		if (!current(token)) return;
		if (
			preferences?.ok &&
			preferences.data &&
			companies.value.some((value) => value.name === preferences.data.company)
		)
			await chooseCompany(preferences.data.company, {
				initialAccounts: preferences.data.accounts,
				save: false,
			});
	}

	async function chooseCompany(value, { initialAccounts = [], save = true } = {}) {
		recoveries = 0;
		notice.value = "";
		const token = begin("loading_company");
		company.value = value;
		accounts.value = [];
		caps.value = null;
		pages.value = emptyPages();
		if (!value) {
			phase.value = "choose_company";
			return;
		}
		const result = await api.getCapabilities(value);
		if (!current(token)) return;
		if (!result?.ok) return fail(result, token);
		caps.value = result.data;
		const allowed = new Set((result.data.accounts || []).map((account) => account.name));
		accounts.value = [...new Set(initialAccounts)].filter((name) => allowed.has(name)).sort();
		if (save) saveSelection();
		await refresh();
	}
	async function setAccounts(value) {
		if (!Array.isArray(value) || value.length > 50) return;
		const allowed = new Set((caps.value?.accounts || []).map((account) => account.name));
		accounts.value = [...new Set(value)].filter((name) => allowed.has(name)).sort();
		saveSelection();
		await refresh();
	}

	async function reloadCapabilities(token = generation) {
		const result = await api.getCapabilities(company.value);
		if (!current(token)) return null;
		if (!result?.ok) {
			if (result?.state === "forbidden") {
				caps.value = null;
				accounts.value = [];
			}
			return result;
		}
		caps.value = result.data;
		return result;
	}
	async function accessFailure(result, token) {
		if (!current(token)) return;
		const previousAccounts = [...accounts.value];
		if (result?.state === "forbidden" || result?.code === "scope_changed") caps.value = null;
		if (result?.state === "forbidden") accounts.value = [];
		if (
			(result?.code === "scope_changed" || result?.state === "forbidden") &&
			recoveries < 1
		) {
			recoveries++;
			const recovery = begin("loading_company");
			const capability = await reloadCapabilities(recovery);
			if (!current(recovery)) return;
			if (capability?.ok) {
				const allowed = new Set((caps.value.accounts || []).map((value) => value.name));
				accounts.value = previousAccounts.filter((name) => allowed.has(name));
				saveSelection();
				return refresh({ automatic: true });
			}
			return fail(capability || result, recovery);
		}
		return fail(result, token);
	}

	async function refresh({ automatic = false } = {}) {
		if (!company.value || disposed) return;
		if (!automatic) recoveries = 0;
		const token = begin("requesting");
		if (!caps.value) {
			const capability = await reloadCapabilities(token);
			if (!current(token)) return;
			if (!capability?.ok) return fail(capability, token);
			const allowed = new Set((caps.value.accounts || []).map((account) => account.name));
			accounts.value = accounts.value.filter((name) => allowed.has(name));
		}
		const selection = { company: company.value, accounts: [...accounts.value] };
		const result = await api.requestSnapshot(selection.company, selection.accounts);
		if (!current(token)) return;
		if (!result?.ok) return accessFailure(result, token);
		const status = result.data;
		if (!status?.snapshot_id || status.state === "failed")
			return fail({ state: "failed", code: status?.code }, token);
		snapshotId.value = status.snapshot_id;
		pollStarted = performance.now();
		await poll(token, status.snapshot_id, selection, 0);
	}
	async function poll(token, id, selection, attempt) {
		if (!current(token)) return;
		if (performance.now() - pollStarted >= 900000)
			return fail({ state: "failed", code: "timeout" }, token);
		const result = await api.getSnapshotStatus(id);
		if (!current(token) || snapshotId.value !== id) return;
		if (!result?.ok) return accessFailure(result, token);
		const data = result.data;
		if (data?.state === "failed") return fail({ state: "failed", code: data.code }, token);
		if (data?.state === "complete") {
			if (
				data.scope?.company !== selection.company ||
				JSON.stringify([...data.scope.accounts].sort()) !==
					JSON.stringify(selection.accounts)
			)
				return fail({ state: "failed", code: "internal_error" }, token);
			snapshot.value = data;
			if (caps.value) caps.value = { ...caps.value, settings: data.floor };
			phase.value = "ready";
			notice.value = "";
			recoveries = 0;
			const requested = selectedDate.value || initialDate;
			selectedDate.value = isSelectable(requested, data.scope)
				? requested
				: data.scope.anchor;
			if (requested && requested !== selectedDate.value)
				notice.value = "That date is outside the calendar range. Showing today.";
			if (
				!month.value ||
				month.value < data.scope.selectable_start.slice(0, 7) ||
				month.value > data.scope.selectable_end.slice(0, 7)
			)
				month.value = selectedDate.value.slice(0, 7);
			if (Date.parse(data.expires_at) <= Date.now()) return expire();
			armTimers();
			await loadPanels(token);
			return;
		}
		if (!data || !["queued", "running"].includes(data.state))
			return fail({ state: "expired", code: "expired" }, token);
		phase.value = data.state;
		const delay = document.hidden ? 15000 : [1000, 2000, 3000, 5000][Math.min(attempt, 3)];
		pollTimer = setTimeout(() => {
			void poll(token, id, selection, attempt + 1);
		}, delay);
	}

	async function expire({ result, verifyAccess = true } = {}) {
		const token = begin("expired");
		error.value = safeError(result || { code: "expired", state: "expired" });
		if (!verifyAccess) return;
		// A permissions hook purges snapshots too. An expired ID alone does not
		// prove that the old account/configuration controls are still permitted.
		caps.value = null;
		const capability = await reloadCapabilities(token);
		if (!current(token)) return;
		if (!capability?.ok) return fail(capability, token);
		const allowed = new Set((caps.value.accounts || []).map((account) => account.name));
		const permitted = accounts.value.filter((name) => allowed.has(name));
		if (permitted.length !== accounts.value.length) {
			accounts.value = permitted;
			saveSelection();
		}
	}
	function armTimers() {
		clearTimeout(expiryTimer);
		clearTimeout(midnightTimer);
		if (!snapshot.value) return;
		expiryTimer = setTimeout(
			expire,
			Math.max(1, Date.parse(snapshot.value.expires_at) - Date.now())
		);
		midnightTimer = setTimeout(checkClock, msUntilSiteMidnight(timezone()) + 20);
	}
	function checkClock() {
		if (!snapshot.value || disposed) return;
		const today = siteToday(timezone()),
			anchor = snapshot.value.scope.anchor;
		const key = `${today}:${anchor}`;
		if (today && today !== anchor && !handledMidnights.has(key)) {
			handledMidnights.add(key);
			notice.value = "The site date changed. Refreshing the calendar.";
			void refresh({ automatic: true });
		} else if (Date.parse(snapshot.value.expires_at) <= Date.now()) expire();
		else armTimers();
	}

	async function selectDate(value) {
		if (!snapshot.value || !isSelectable(value, snapshot.value.scope)) return;
		selectedDate.value = value;
		month.value = value.slice(0, 7);
		pages.value = emptyPages();
		await loadPanels(generation);
	}
	function browseMonth(offset) {
		if (!snapshot.value) return;
		const next = addMonths(month.value, offset);
		if (
			next >= snapshot.value.scope.selectable_start.slice(0, 7) &&
			next <= snapshot.value.scope.selectable_end.slice(0, 7)
		)
			month.value = next;
	}
	function goToday() {
		return snapshot.value && selectDate(snapshot.value.scope.anchor);
	}

	async function loadPanels(token) {
		await Promise.all([loadDetails(token), loadUpcoming(token)]);
	}
	async function loadDetails(token = generation) {
		const data = snapshot.value;
		if (!data || !current(token)) return;
		const seq = ++detailSequence,
			date = selectedDate.value,
			selectedSide = side.value;
		detail.value = null;
		if (data.unavailable?.[selectedSide]) {
			detailLoading.value = false;
			return;
		}
		detailLoading.value = true;
		const page = pages.value[selectedSide];
		const result = await api.getDayDetail(
			data.snapshot_id,
			date,
			selectedSide,
			page.rows,
			page.parties
		);
		if (
			!current(token) ||
			seq !== detailSequence ||
			snapshotId.value !== data.snapshot_id ||
			selectedDate.value !== date
		)
			return;
		detailLoading.value = false;
		if (!result?.ok) return accessFailure(result, token);
		detail.value = result.data;
	}
	async function loadUpcoming(token = generation) {
		const data = snapshot.value;
		if (!data || !current(token)) return;
		const seq = ++upcomingSequence,
			date = selectedDate.value;
		upcoming.value = [];
		if (data.unavailable?.Payable) {
			upcomingLoading.value = false;
			return;
		}
		upcomingLoading.value = true;
		const result = await api.getUpcoming(data.snapshot_id, date);
		if (
			!current(token) ||
			seq !== upcomingSequence ||
			snapshotId.value !== data.snapshot_id ||
			selectedDate.value !== date
		)
			return;
		upcomingLoading.value = false;
		if (!result?.ok) return accessFailure(result, token);
		upcoming.value = result.data.rows;
	}
	async function setDetailView(value) {
		if (!["payees", "terms", "customers"].includes(value)) return;
		activeView.value = value;
		await loadDetails();
	}
	async function changeDetailPage(kind, value) {
		if (!["rows", "parties"].includes(kind) || !Number.isInteger(value) || value < 1) return;
		pages.value[side.value][kind] = value;
		await loadDetails();
	}

	async function openBucket(selectedSide, name) {
		if (!snapshot.value) return;
		bucket.value = { side: selectedSide, name };
		bucketPages.value = { rows: 1, parties: 1 };
		await loadBucket();
	}
	function closeBucket() {
		bucketSequence++;
		bucket.value = null;
		bucketData.value = null;
	}
	async function loadBucket() {
		if (!snapshot.value || !bucket.value) return;
		const token = generation,
			seq = ++bucketSequence,
			data = snapshot.value,
			choice = bucket.value;
		bucketData.value = null;
		bucketLoading.value = true;
		const result = await api.getBucketDetail(
			data.snapshot_id,
			choice.name,
			choice.side,
			bucketPages.value.rows,
			bucketPages.value.parties
		);
		if (!current(token) || seq !== bucketSequence || snapshotId.value !== data.snapshot_id)
			return;
		bucketLoading.value = false;
		if (!result?.ok) return accessFailure(result, token);
		bucketData.value = result.data;
	}
	async function changeBucketPage(kind, value) {
		if (!["rows", "parties"].includes(kind) || !Number.isInteger(value) || value < 1) return;
		bucketPages.value[kind] = value;
		await loadBucket();
	}

	async function saveFloor(value) {
		if (floorSaving.value || !caps.value?.can_configure) return { ok: false };
		const token = generation,
			chosenCompany = company.value,
			settings = caps.value.settings;
		floorSaving.value = true;
		floorError.value = null;
		try {
			const result = await api.configureFloor(
				chosenCompany,
				value,
				settings.revision,
				settings.configuration_id
			);
			if (!current(token) || company.value !== chosenCompany)
				return { ok: false, state: "superseded" };
			if (result?.ok) {
				caps.value = { ...caps.value, settings: result.data };
				// The write is acknowledged: close its dialog while the new snapshot
				// prepares. Do not hold a successful form open for a large report job.
				void refresh();
			} else {
				floorError.value = safeError(result);
				await expire({ verifyAccess: false });
				await reloadCapabilities();
			}
			return result;
		} finally {
			if (!disposed) floorSaving.value = false;
		}
	}

	window.addEventListener("focus", checkClock);
	document.addEventListener("visibilitychange", checkClock);
	onScopeDispose(() => {
		disposed = true;
		generation++;
		stopTimers();
		clearFinance();
		window.removeEventListener("focus", checkClock);
		document.removeEventListener("visibilitychange", checkClock);
	});
	return {
		phase,
		companies,
		company,
		accounts,
		caps,
		snapshot,
		snapshotId,
		selectedDate,
		month,
		day,
		detail,
		detailLoading,
		upcoming,
		upcomingLoading,
		activeView,
		side,
		pages,
		error,
		notice,
		floorSaving,
		floorError,
		preferenceSaving,
		preferenceError,
		bucket,
		bucketData,
		bucketLoading,
		bucketPages,
		initialize,
		chooseCompany,
		setAccounts,
		refresh,
		selectDate,
		browseMonth,
		goToday,
		setDetailView,
		changeDetailPage,
		openBucket,
		closeBucket,
		changeBucketPage,
		saveFloor,
		reloadCapabilities,
		saveSelection,
	};
}
