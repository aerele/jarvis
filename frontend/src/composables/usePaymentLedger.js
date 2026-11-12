import { getCurrentScope, onScopeDispose, reactive, ref } from "vue";
import * as calendarApi from "@/api/paymentCalendar";
import { isSelectable } from "@/lib/paymentCalendar/dates";
import { safeError } from "@/lib/paymentCalendar/polling";

const PANEL_ROWS = 5;
const MODAL_PAGE_SIZE = 25;
// Reads wait this long, so a burst of clicks sends only the last one.
const READ_DELAY_MS = 150;
// Keep in sync with SIDES in CalendarMethod.vue, PaymentCalendarPage.vue, calendarDay.js and
// ledgerCopy.js (the server accepts exactly these two).
const SIDES = ["Payable", "Receivable"];
const DATED_SCOPES = ["date", "month"];
const SCOPES = [...DATED_SCOPES, "immediate", "now", "today", "overdue", "undated"];
// Keep in sync with SORT_FIELDS in api/paymentCalendar.js (the API drops any other key).
const DEFAULT_DIRECTION = { remaining: "desc", due_date: "asc", party: "asc" };

const blankArea = () => ({
	rows: [],
	total: null,
	total_remaining: null,
	has_more: false,
	loading: false,
	error: null,
	unavailable: false,
});
// Only these replies change what the user may see; they go to the core recovery.
const isRefusal = (result) =>
	result?.state === "forbidden" ||
	result?.state === "expired" ||
	result?.code === "scope_changed";
const isReadable = (data) => Array.isArray(data?.rows) && Number.isFinite(data.total);

// Owns the side ledger: panel and full-details modal scope, side, sort and reads.
// Money is never computed here; every figure comes from a server reply.
export function usePaymentLedger(core, { api = calendarApi } = {}) {
	const panelScope = ref("date"),
		panelSide = ref("Payable"),
		sortKey = ref("remaining"),
		sortDirection = ref("desc");
	const panel = reactive(blankArea());
	const modal = reactive({
		...blankArea(),
		open: false,
		scope: "date",
		side: "Payable",
		date: "",
		page: 1,
	});
	const areas = {
		panel: { state: panel, sequence: 0, date: () => core.selectedDate.value },
		modal: { state: modal, sequence: 0, date: () => modal.date },
	};

	// A reply is applied only if all four match (same rule as the core detail reads).
	const accepts = (read) =>
		core.isCurrent(read.token) &&
		read.sequence === read.area.sequence &&
		core.snapshotId.value === read.snapshotId &&
		read.area.date() === read.date;

	function requestOf(area) {
		const state = area.state;
		return area === areas.panel
			? {
					scope: panelScope.value,
					side: panelSide.value,
					date: core.selectedDate.value,
					page: 1,
			  }
			: { scope: state.scope, side: state.side, date: state.date, page: state.page };
	}
	const sorting = () => ({ sort: sortKey.value, order: sortDirection.value });
	// Month scope has no date of its own: it reads the snapshot's window bucket.
	function fetchRows(snapshotId, { scope, side, date, page }, pageSize) {
		if (scope === "date")
			return api.getDayDetail(snapshotId, date, side, page, 1, sorting(), pageSize);
		const bucket = scope === "month" ? "window" : scope;
		return api.getBucketDetail(snapshotId, bucket, side, page, 1, sorting(), pageSize);
	}
	// A dated read needs a snapshot of that month; the month fetch is still pending.
	function waitsForMonth(request, snapshot) {
		const month = snapshot.scope.month;
		return (
			DATED_SCOPES.includes(request.scope) && month && !request.date.startsWith(month + "-")
		);
	}
	// Without a snapshot an area never reads "empty": it waits, or shows why it cannot.
	function waiting(area) {
		const issue = core.refreshIssue.value;
		if (issue) area.state.error = safeError(issue);
		else area.state.loading = true;
	}

	// Reads wait in `queued` for READ_DELAY_MS; only the newest read of an area is sent.
	const queued = new Map();
	let flushTimer = null;
	function enqueue(reading) {
		return new Promise((resolve) => {
			queued.set(reading.area, { ...reading, resolve });
			clearTimeout(flushTimer);
			flushTimer = setTimeout(flush, READ_DELAY_MS);
		});
	}
	function dequeue(area) {
		queued.get(area)?.resolve();
		queued.delete(area);
	}
	// A shared read: the panel and the modal of the same scope, side, date and page.
	const keyOf = ({ request }) =>
		[
			request.scope,
			request.side,
			DATED_SCOPES.includes(request.scope) ? request.date : "",
			request.page,
		].join("|");
	async function flush() {
		flushTimer = null;
		const entries = [...queued.values()];
		queued.clear();
		// A read whose token, sequence, snapshot or date is already superseded is never sent.
		const sendable = entries.filter((reading) => accepts(reading));
		entries.filter((reading) => !accepts(reading)).forEach((reading) => reading.resolve());
		const groups = {};
		sendable.forEach((reading) => (groups[keyOf(reading)] ||= []).push(reading));
		await Promise.all(Object.values(groups).map(send));
	}
	async function send(group) {
		const shared = group.some((reading) => reading.area === areas.modal);
		const [first] = group;
		const result = await fetchRows(
			first.snapshotId,
			first.request,
			shared ? MODAL_PAGE_SIZE : PANEL_ROWS
		);
		group.forEach((reading) => {
			apply(reading, result);
			reading.resolve();
		});
	}
	function apply(reading, result) {
		const { area, token } = reading;
		if (!accepts(reading)) return;
		area.state.loading = false;
		if (isRefusal(result)) return core.handleRefusal(result, token);
		// An ordinary failure keeps the page and the date; retry() reads again.
		if (!result?.ok) {
			area.state.error = safeError(result);
			return;
		}
		show(area, result.data);
	}

	function read(area, token = core.generation) {
		const request = requestOf(area),
			snapshot = core.snapshot.value;
		const sequence = ++area.sequence;
		dequeue(area);
		Object.assign(area.state, blankArea());
		if (!snapshot) return waiting(area);
		if (!request.date) return;
		if (waitsForMonth(request, snapshot)) {
			area.state.loading = true;
			return;
		}
		if (snapshot.unavailable?.[request.side]) {
			area.state.unavailable = true;
			return;
		}
		area.state.loading = true;
		return enqueue({
			area,
			token,
			sequence,
			request,
			snapshotId: snapshot.snapshot_id,
			date: request.date,
		});
	}
	function show(area, data) {
		if (!isReadable(data)) {
			area.state.unavailable = true;
			return;
		}
		// A new snapshot can hold fewer pages: fall back to the first one.
		if (area === areas.modal && area.state.page > 1 && !data.rows.length && data.total > 0) {
			area.state.page = 1;
			void read(area);
			return;
		}
		area.state.rows = area === areas.panel ? data.rows.slice(0, PANEL_ROWS) : data.rows;
		area.state.total = data.total;
		area.state.total_remaining = data.total_remaining ?? null;
		area.state.has_more = !!data.has_more;
	}
	function readAll(token = core.generation) {
		return Promise.all([
			read(areas.panel, token),
			modal.open ? read(areas.modal, token) : null,
		]);
	}

	function resetAll() {
		areas.panel.sequence++;
		dequeue(areas.panel);
		Object.assign(panel, blankArea());
		closeModal();
		waiting(areas.panel);
	}
	// The modal survives a swap only if its date and side still belong to the snapshot.
	function modalStillValid() {
		const snapshot = core.snapshot.value;
		return (
			!!snapshot &&
			isSelectable(modal.date, snapshot.scope) &&
			!waitsForMonth({ scope: modal.scope, date: modal.date }, snapshot)
		);
	}
	function onInvalidated({ reason }) {
		if (reason === "date" || reason === "scope") panelScope.value = "date";
		if (reason === "date") return read(areas.panel);
		// A scope change is followed by a new snapshot: wait for its swap.
		if (reason === "scope" || !core.snapshot.value) return resetAll();
		if (modal.open && !modalStillValid()) closeModal();
		// The modal keeps its page: `show` returns it to page 1 if the page is gone.
		return readAll();
	}
	// The core gave up waiting for a snapshot: a waiting area says so and offers Retry.
	function onAbandoned() {
		Object.values(areas).forEach(({ state }) => {
			if (!state.loading) return;
			state.loading = false;
			state.error = safeError(core.refreshIssue.value);
		});
	}

	function setScope(scope) {
		if (!SCOPES.includes(scope)) return;
		panelScope.value = scope;
		if (!DATED_SCOPES.includes(scope)) panelSide.value = "Payable";
		return read(areas.panel);
	}
	const backToDate = () => {
		panelSide.value = "Payable";
		return setScope("date");
	};
	function setSide(side) {
		if (!SIDES.includes(side) || !DATED_SCOPES.includes(panelScope.value)) return;
		panelSide.value = side;
		return read(areas.panel);
	}
	// One generation covers the panel and the modal, so neither shows a mixed order.
	function setSort(key) {
		if (!(key in DEFAULT_DIRECTION)) return;
		const toggled = sortDirection.value === "asc" ? "desc" : "asc";
		sortDirection.value = key === sortKey.value ? toggled : DEFAULT_DIRECTION[key];
		sortKey.value = key;
		modal.page = 1;
		return readAll();
	}
	// With no snapshot (or one of another month) only the core can bring the rows back.
	function retry() {
		const snapshot = core.snapshot.value;
		if (snapshot && !waitsForMonth(requestOf(areas.panel), snapshot)) return readAll();
		return core.refresh({ background: true });
	}

	function openModal({ scope, side, date } = {}) {
		const chosen = scope || panelScope.value;
		Object.assign(modal, blankArea(), {
			open: true,
			scope: chosen,
			side: DATED_SCOPES.includes(chosen) ? side || panelSide.value : "Payable",
			date: date || core.selectedDate.value,
			page: 1,
		});
		return read(areas.modal);
	}
	function closeModal() {
		areas.modal.sequence++;
		dequeue(areas.modal);
		Object.assign(modal, blankArea(), { open: false, page: 1 });
	}
	function setModalSide(side) {
		if (!modal.open || !SIDES.includes(side) || !DATED_SCOPES.includes(modal.scope)) return;
		modal.side = side;
		modal.page = 1;
		return read(areas.modal);
	}
	function changeModalPage(page) {
		if (!modal.open || !Number.isInteger(page) || page < 1) return;
		modal.page = page;
		return read(areas.modal);
	}

	const stops = [core.onPanelsInvalidated(onInvalidated), core.onPanelsAbandoned(onAbandoned)];
	if (getCurrentScope())
		onScopeDispose(() => {
			stops.forEach((stop) => stop());
			clearTimeout(flushTimer);
			dequeue(areas.panel);
			dequeue(areas.modal);
		});

	return {
		panelScope,
		panelSide,
		sortKey,
		sortDirection,
		panel,
		modal,
		setScope,
		backToDate,
		setSide,
		setSort,
		retry,
		openModal,
		closeModal,
		setModalSide,
		changeModalPage,
	};
}
