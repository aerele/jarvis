import { computed, onScopeDispose, ref, shallowRef } from "vue";
import * as calendarApi from "@/api/paymentCalendar";
import { siteTimezone } from "@/utils/datetime";
import {
	addMonths,
	isSelectable,
	msUntilSiteMidnight,
	siteToday,
} from "@/lib/paymentCalendar/dates";
import { nextPollDelay, safeError } from "@/lib/paymentCalendar/polling";
import { RecoveryBudget } from "@/lib/paymentCalendar/recoveryBudget";

const WORKER_BUSY_RETRY_MS = 60000;

export function usePaymentCalendar({
	api = calendarApi,
	timezone = siteTimezone,
	initialDate = "",
} = {}) {
	const refreshing = ref(false),
		waitingForWorker = ref(false),
		refreshIssue = shallowRef(null);
	const layout = shallowRef(null);
	// A loading frame contains identities and presentation metadata, never old balances.
	const loadingSnapshot = computed(() =>
		!snapshot.value && layout.value && (refreshing.value || refreshIssue.value)
			? {
					snapshot_id: null,
					currency: layout.value.currency,
					scope: layout.value.scope,
					generated_at: layout.value.generated_at,
					unavailable: {},
					bank_code: null,
					projection: {
						precision: layout.value.precision,
						days: {},
						forecast_available: false,
						payable_settlement_complete: false,
						book_cash: null,
						floor: null,
						due_now: { total: null, scheduled: null, overdue: null, undated: null },
						excluded_collections: { overdue: null, undated: null, adjustments: null },
						reconciliation: { Payable: null, Receivable: null },
						source_notes: {},
						scope_reasons: [],
					},
			  }
			: null
	);
	const phase = ref("loading"),
		companies = ref([]),
		company = ref(""),
		accounts = ref([]),
		caps = shallowRef(null);
	const snapshot = shallowRef(null),
		snapshotId = ref(null),
		selectedDate = ref(""),
		month = ref("");
	const upcoming = ref([]),
		upcomingLoading = ref(false);
	const error = shallowRef(null),
		notice = ref("");
	const floorSaving = ref(false),
		floorError = shallowRef(null),
		preferenceSaving = ref(false),
		preferenceError = shallowRef(null);
	const day = computed(() => snapshot.value?.projection?.days?.[selectedDate.value] || null);
	let generation = 0,
		upcomingSequence = 0,
		disposed = false;
	let pendingSnapshotId = null,
		refreshAt = 0,
		resumeRefresh = false,
		resumeRecovery = false;
	let refreshTimer,
		pollTimer,
		expiryTimer,
		midnightTimer,
		workerRetryTimer,
		pollStarted = 0;
	// Survives "ready": only a user action or the passing of time refills it.
	const recoveries = new RecoveryBudget();
	const limitReached = { state: "failed", code: "recovery_limit", retryable: true };
	let workerRetry = null;
	let preferencePending = null,
		preferenceDraining = false;
	const handledMidnights = new Set();
	// The ledger composable listens here; the core never reads its state.
	const invalidatedListeners = new Set(),
		abandonedListeners = new Set();
	const subscribe = (listeners) => (fn) => {
		listeners.add(fn);
		return () => listeners.delete(fn);
	};
	// reason: date | snapshot | scope | expiry | cleared
	// A listener bug must not break the core lifecycle or the other listeners.
	const notify = (listeners, ...args) =>
		[...listeners].forEach((fn) => {
			try {
				fn(...args);
			} catch (error) {
				console.error("Payment calendar listener failed", error);
			}
		});
	const emitInvalidated = (reason) => notify(invalidatedListeners, { reason });
	const emitAbandoned = () => notify(abandonedListeners);
	const current = (token) => !disposed && generation === token;
	const sameSelection = (value) =>
		value.company === company.value &&
		JSON.stringify(value.accounts) === JSON.stringify(accounts.value);
	const clearWorkerRetry = () => {
		clearTimeout(workerRetryTimer);
		workerRetryTimer = null;
		workerRetry = null;
	};
	const stopTimers = () => {
		clearTimeout(refreshTimer);
		clearTimeout(pollTimer);
		clearTimeout(expiryTimer);
		clearTimeout(midnightTimer);
		clearWorkerRetry();
	};

	function clearFinance(reason = "cleared") {
		snapshot.value = null;
		snapshotId.value = null;
		upcoming.value = [];
		upcomingLoading.value = false;
		upcomingSequence++;
		emitInvalidated(reason);
	}
	function begin(nextPhase, { preserve = false, keepLayout = false } = {}) {
		generation++;
		stopTimers();
		refreshing.value = false;
		waitingForWorker.value = false;
		refreshIssue.value = null;
		if (!preserve && !keepLayout) layout.value = null;
		pendingSnapshotId = null;
		refreshAt = 0;
		resumeRefresh = false;
		resumeRecovery = false;
		if (!preserve) clearFinance();
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
		recoveries.reset();
		notice.value = "";
		emitInvalidated("scope");
		const token = begin("loading_company");
		company.value = value;
		accounts.value = [];
		caps.value = null;
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
		emitInvalidated("scope");
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
		const recoverable =
			result?.code === "scope_changed" ||
			result?.state === "forbidden" ||
			result?.state === "expired";
		if (recoverable) {
			// Past the cap a forbidden reply stays "forbidden"; any other ends in the manual message.
			if (!recoveries.take())
				return fail(result?.state === "forbidden" ? result : limitReached, token);
			const recovery = begin("loading_company");
			const capability = await reloadCapabilities(recovery);
			if (!current(recovery)) return;
			if (capability?.ok) {
				const allowed = new Set((caps.value.accounts || []).map((value) => value.name));
				accounts.value = previousAccounts.filter((name) => allowed.has(name));
				saveSelection();
				// An automatic refresh never starts during a floor save: resume when it ends.
				if (floorSaving.value) {
					resumeRecovery = true;
					return;
				}
				return refresh({ automatic: true });
			}
			return fail(capability || result, recovery);
		}
		return fail(result, token);
	}

	function initialMonth() {
		const today = siteToday(timezone());
		return today &&
			isSelectable(initialDate, {
				selectable_start: addMonths(today.slice(0, 7), -12) + "-01",
				selectable_end: addMonths(today.slice(0, 7), 11) + "-31",
			})
			? initialDate.slice(0, 7)
			: "current";
	}

	async function refresh({ automatic = false, background = false, month: requestedMonth } = {}) {
		if (!company.value || disposed || (automatic && (refreshing.value || floorSaving.value)))
			return;
		if (!automatic && !background) recoveries.reset();
		const preserve =
			background && !!snapshot.value && Date.parse(snapshot.value.expires_at) > Date.now();
		const keepLayout = background && !!layout.value;
		const token = begin(preserve || keepLayout ? "ready" : "requesting", {
			preserve,
			keepLayout,
		});
		refreshing.value = background;
		if (preserve) armExpiry();
		if (!caps.value) {
			const capability = await reloadCapabilities(token);
			if (!current(token)) return;
			if (!capability?.ok) return fail(capability, token);
			const allowed = new Set((caps.value.accounts || []).map((account) => account.name));
			accounts.value = accounts.value.filter((name) => allowed.has(name));
		}
		const selection = {
			company: company.value,
			accounts: [...accounts.value],
			month: requestedMonth || month.value || initialMonth(),
			viewMonth: month.value,
		};
		pollStarted = performance.now();
		await requestSelection(token, selection);
	}
	function resumeWorkerRetry() {
		const retry = workerRetry;
		if (
			!retry ||
			!retry.ready ||
			document.hidden ||
			floorSaving.value ||
			!current(retry.token)
		)
			return false;
		clearTimeout(workerRetryTimer);
		workerRetryTimer = null;
		workerRetry = null;
		refreshing.value = true;
		waitingForWorker.value = false;
		refreshIssue.value = null;
		void requestSelection(retry.token, retry.selection, retry.attempt);
		return true;
	}
	function scheduleWorkerRetry(token, selection, attempt, delay) {
		clearWorkerRetry();
		workerRetry = {
			token,
			selection,
			attempt,
			ready: document.hidden,
		};
		if (workerRetry.ready) return;
		workerRetryTimer = setTimeout(() => {
			workerRetryTimer = null;
			if (!workerRetry || workerRetry.token !== token) return;
			workerRetry.ready = true;
			resumeWorkerRetry();
		}, delay);
	}
	async function requestSelection(token, selection, attempt = 0) {
		if (!current(token)) return;
		if (refreshing.value && selection.month !== "current" && month.value)
			selection = { ...selection, month: month.value };
		const result = await api.requestSnapshot(
			selection.company,
			selection.accounts,
			selection.month
		);
		if (!current(token)) return;
		if (!result?.ok && result?.code === "worker_busy") {
			waitingForWorker.value = true;
			if (performance.now() - pollStarted >= 60000) {
				if (!layout.value) return fail(result, token);
				refreshing.value = false;
				waitingForWorker.value = false;
				refreshIssue.value = safeError(result);
				upcomingLoading.value = false;
				emitAbandoned();
				scheduleWorkerRetry(token, selection, attempt, WORKER_BUSY_RETRY_MS);
				return;
			}
			scheduleWorkerRetry(token, selection, attempt + 1, nextPollDelay(attempt));
			return;
		}
		if (
			!result?.ok &&
			result?.code === "invalid_request" &&
			selection.month !== "current" &&
			!selection.currentFallback
		) {
			// The oldest selectable month can leave the window at site midnight.
			// Resolve "current" on the server once; do not keep resending stale bounds.
			return requestSelection(token, {
				...selection,
				month: "current",
				viewMonth: month.value,
				currentFallback: true,
			});
		}
		if (!result?.ok) return accessFailure(result, token);
		waitingForWorker.value = false;
		const status = result.data;
		if (!status?.snapshot_id || status.state === "failed")
			return fail({ state: "failed", code: status?.code }, token);
		pendingSnapshotId = status.snapshot_id;
		pollStarted = performance.now();
		await poll(token, status.snapshot_id, selection, 0);
	}
	function wantsAnotherMonth(selection) {
		return (
			refreshing.value &&
			selection.month &&
			(selection.month === "current"
				? selection.viewMonth !== month.value
				: selection.month !== month.value)
		);
	}
	async function poll(token, id, selection, attempt) {
		if (!current(token)) return;
		if (performance.now() - pollStarted >= 900000)
			return fail({ state: "failed", code: "timeout" }, token);
		const result = await api.getSnapshotStatus(id);
		if (!current(token) || pendingSnapshotId !== id) return;
		if (!result?.ok && result?.state === "expired" && wantsAnotherMonth(selection))
			return expire({ result, retry: true });
		if (!result?.ok) return accessFailure(result, token);
		const data = result.data;
		if (data?.state === "failed") {
			if (wantsAnotherMonth(selection))
				return refresh({ background: true, month: month.value });
			return fail({ state: "failed", code: data.code }, token);
		}
		if (data?.state === "complete") {
			// A newer date/month may have been chosen while this accepted job ran.
			// Finish polling it before admitting only the latest requested month.
			if (wantsAnotherMonth(selection))
				return refresh({ background: true, month: month.value });
			if (
				data.scope?.company !== selection.company ||
				(data.scope.month &&
					selection.month &&
					selection.month !== "current" &&
					data.scope.month !== selection.month) ||
				JSON.stringify([...data.scope.accounts].sort()) !==
					JSON.stringify(selection.accounts)
			)
				return fail({ state: "failed", code: "internal_error" }, token);
			if (Date.parse(data.expires_at) <= Date.now()) return expire();
			// Publish one snapshot identity before loading any of its panels.
			snapshotId.value = id;
			upcomingSequence++;
			upcoming.value = [];
			snapshot.value = data;
			layout.value = {
				currency: data.currency,
				scope: data.scope,
				generated_at: data.generated_at,
				precision: data.projection.precision,
			};
			refreshing.value = false;
			refreshIssue.value = null;
			if (caps.value) caps.value = { ...caps.value, settings: data.floor };
			phase.value = "ready";
			notice.value = "";
			let requested = selectedDate.value || initialDate;
			if (data.scope.month && !requested.startsWith(data.scope.month + "-"))
				requested = data.scope.anchor.startsWith(data.scope.month + "-")
					? data.scope.anchor
					: data.scope.month + "-01";
			if (data.scope.month) month.value = data.scope.month;
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
			armTimers();
			emitInvalidated("snapshot");
			await loadUpcoming(token);
			return;
		}
		if (!data || !["queued", "running"].includes(data.state))
			return fail({ state: "expired", code: "expired" }, token);
		if (!snapshot.value) phase.value = data.state;
		const delay = nextPollDelay(attempt);
		pollTimer = setTimeout(() => {
			void poll(token, id, selection, attempt + 1);
		}, delay);
	}

	async function expire({ result, verifyAccess = true, retry = false } = {}) {
		const token = begin("expired");
		emitInvalidated("expiry");
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
		if (!retry) return;
		if (recoveries.take()) {
			// checkClock resumes it when the floor save ends.
			if (floorSaving.value) {
				resumeRefresh = true;
				return;
			}
			return refresh({ automatic: true, background: true, month: month.value });
		}
		error.value = safeError(limitReached);
	}
	function armExpiry() {
		clearTimeout(expiryTimer);
		if (!snapshot.value) return;
		expiryTimer = setTimeout(() => {
			if (refreshIssue.value) {
				clearFinance("expiry");
			} else if (refreshing.value) {
				// Keep the replacement job alive, but never display expired balances.
				clearFinance("expiry");
				phase.value = "running";
			} else if (document.hidden || floorSaving.value) {
				clearFinance("expiry");
				phase.value = "expired";
				resumeRefresh = true;
			} else {
				clearFinance("expiry");
				void refresh({ automatic: true, background: true });
			}
		}, Math.max(1, Date.parse(snapshot.value.expires_at) - Date.now()));
	}
	function armTimers() {
		clearTimeout(refreshTimer);
		clearTimeout(midnightTimer);
		if (!snapshot.value) return;
		armExpiry();
		// Start ahead of the five-minute deadline so a quick job swaps in place.
		refreshAt =
			Date.now() + Math.max(1, (Date.parse(snapshot.value.expires_at) - Date.now()) * 0.9);
		refreshTimer = setTimeout(checkClock, Math.max(1, refreshAt - Date.now()));
		midnightTimer = setTimeout(checkClock, msUntilSiteMidnight(timezone()) + 20);
	}
	function checkClock() {
		if (disposed || document.hidden || floorSaving.value) return;
		if (workerRetry) {
			resumeWorkerRetry();
			return;
		}
		if (refreshing.value) return;
		if (!snapshot.value) {
			if (phase.value === "expired" && resumeRefresh)
				void refresh({ automatic: true, background: true });
			return;
		}
		const today = siteToday(timezone()),
			anchor = snapshot.value.scope.anchor;
		const key = `${today}:${anchor}`;
		if (today && today !== anchor && !handledMidnights.has(key)) {
			handledMidnights.add(key);
			notice.value = "The site date changed. Refreshing the calendar.";
			void refresh({ automatic: true, background: true });
		} else if (Date.now() >= refreshAt) {
			void refresh({ automatic: true, background: true });
		}
	}

	async function selectDate(value) {
		const navigation = snapshot.value?.scope || layout.value?.scope;
		if (!navigation || !isSelectable(value, navigation)) return;
		selectedDate.value = value;
		month.value = value.slice(0, 7);
		emitInvalidated("date");
		if (
			!snapshot.value ||
			(navigation.month &&
				(refreshing.value ||
					refreshIssue.value ||
					!value.startsWith(navigation.month + "-")))
		)
			return loadMonth(month.value);
		await loadUpcoming(generation);
	}
	async function browseMonth(offset) {
		const navigation = snapshot.value?.scope || layout.value?.scope;
		if (!navigation) return;
		const next = addMonths(month.value, offset);
		if (
			next >= navigation.selectable_start.slice(0, 7) &&
			next <= navigation.selectable_end.slice(0, 7)
		) {
			month.value = next;
			if (navigation.month || !snapshot.value) {
				selectedDate.value = next + "-01";
				emitInvalidated("date");
				await loadMonth(next);
			}
		}
	}
	async function loadMonth(value) {
		upcomingSequence++;
		upcoming.value = [];
		upcomingLoading.value = true;
		if (!refreshing.value) await refresh({ month: value, background: true });
	}
	function goToday() {
		const navigation = snapshot.value?.scope || layout.value?.scope;
		return navigation && selectDate(navigation.anchor);
	}

	async function loadUpcoming(token = generation) {
		const data = snapshot.value;
		if (!data || !current(token)) return;
		if (data.scope.month && !selectedDate.value.startsWith(data.scope.month + "-")) return;
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
			if (!disposed) {
				floorSaving.value = false;
				if (resumeRecovery) {
					resumeRecovery = false;
					void refresh({ automatic: true });
				} else checkClock();
			}
		}
	}

	function onActivity() {
		if (disposed || document.hidden) return;
		checkClock();
	}
	window.addEventListener("focus", onActivity);
	document.addEventListener("visibilitychange", onActivity);
	onScopeDispose(() => {
		disposed = true;
		generation++;
		invalidatedListeners.clear();
		abandonedListeners.clear();
		stopTimers();
		clearFinance();
		layout.value = null;
		window.removeEventListener("focus", onActivity);
		document.removeEventListener("visibilitychange", onActivity);
	});
	return {
		get generation() {
			return generation;
		},
		isCurrent: current,
		onPanelsInvalidated: subscribe(invalidatedListeners),
		onPanelsAbandoned: subscribe(abandonedListeners),
		handleRefusal: accessFailure,
		loadingSnapshot,
		waitingForWorker,
		refreshIssue,
		refreshing,
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
		upcoming,
		upcomingLoading,
		error,
		notice,
		floorSaving,
		floorError,
		preferenceSaving,
		preferenceError,
		initialize,
		chooseCompany,
		setAccounts,
		refresh,
		selectDate,
		browseMonth,
		goToday,
		saveFloor,
		reloadCapabilities,
		saveSelection,
	};
}
