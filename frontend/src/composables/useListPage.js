// useListPage - server-envelope list state for the v3 list kit (DESIGN-V3 §5.1).
// One instance per list page; feeds ListPage.vue. Wire format matches api.js
// `_page()` ({search, filters, sort_field, sort_dir, start, page_length})
// against the frozen envelope {rows, total, has_more, start, page_length[, facets]}.
// Ported behaviors from round-2 FeatureListPage: monotonic request id (stale
// responses dropped), errors keep last-good rows + toast, facets captured from
// page-1 responses.
//
// Plan 08 addition (§6.4): when a page passes a `viewKey`, this composable also
// owns the CANONICAL filter model — the ordered clause list, the per-caller
// schema fetch, the versioned URL param, quick-filter synchronization and the
// mapping from the server's stable rejection codes to something the panel can
// act on. `filters` (the legacy `{key: value}` object) keeps working untouched
// for every surface that has not migrated, and rides alongside `filters_v2`
// during the compatibility window.
import { ref, reactive, computed, watch, onMounted, onBeforeUnmount } from "vue";
import { useStorage } from "@vueuse/core";
import { toast } from "frappe-ui";
import * as api from "@/api";
import {
	URL_PARAM,
	schemaIndex,
	entryFor,
	toWire,
	activeCount,
	makeClause,
	isComplete,
	serializeClauses,
	parseClauseParam,
	reconcileClauses,
	droppedNotice,
	filterErrorInfo,
} from "@/components/list/filterModel";

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

export function useListPage({
	fetchFn,
	defaultSort = { field: "", dir: "" },
	storageKey,
	initialFilters = {},
	// ── plan 08 ──
	viewKey = "", // registered list view; "" keeps the legacy-only behaviour
	quickClauses = {}, // {quickFilterKey: fieldname} — the 1:1 mappings only
	route = null, // vue-router route/router; omitted ⇒ no URL state
	router = null,
	fetchSchema = null, // injectable for tests; defaults to the real endpoint
}) {
	const rows = ref([]);
	const total = ref(0);
	const hasMore = ref(false);
	const loading = ref(false);
	const error = ref("");
	const facets = ref({});

	const search = ref("");
	const filters = reactive({});
	for (const [k, v] of Object.entries(initialFilters || {})) {
		if (v !== "" && v != null) filters[k] = v;
	}
	const sort = ref({ field: defaultSort.field || "", dir: defaultSort.dir || "" });
	const pageLength = useStorage(`jarvis-pl-${storageKey}`, 20);

	// ── canonical filter state ───────────────────────────────────────────────
	const filterClauses = ref([]);
	const schema = ref(null);
	const schemaState = ref("idle"); // idle | loading | ready | error
	const schemaError = ref(null);
	const filterError = ref(null); // {code, kind, message} from the last request
	const filterNotice = ref(""); // "N filters ... were removed."
	const index = computed(() => schemaIndex(schema.value));
	const schemaFetcher = fetchSchema || ((key) => api.getListFilterSchema(key));

	// monotonic request id - drops stale responses (same guard as ChatView.loadConversation)
	let reqId = 0;
	// separate fence for the schema: a retry must not be overwritten by the slow
	// failure that provoked it.
	let schemaSeq = 0;
	let schemaInflight = null;

	// The URL is parsed BEFORE the first fetch so a shared link's clauses are in
	// the request that builds page one, not applied a render later.
	const urlSeed = route ? parseClauseParam(route.query && route.query[URL_PARAM], viewKey) : null;
	if (urlSeed && urlSeed.clauses.length) filterClauses.value = urlSeed.clauses;
	let lastWrittenUrl = (route && route.query && route.query[URL_PARAM]) || "";

	function wireClauses() {
		// Only COMPLETE clauses reach the wire; a half-filled row is pending, and
		// pending is not a filter (plan §6.4: "no fetch until an incomplete clause
		// becomes valid").
		return toWire(filterClauses.value, index.value);
	}

	// mode: "reset" (page 1, replaces rows) | "more" (start=rows.length, appends)
	//     | "keep"  (silent refetch of the loaded window 0..min(rows.length,100))
	async function fetchRows(mode = "reset") {
		if (!fetchFn) return;
		const id = ++reqId;
		const append = mode === "more";
		const pl =
			mode === "keep"
				? Math.min(Math.max(rows.value.length || pageLength.value, 1), 100)
				: pageLength.value;
		if (mode !== "keep") loading.value = true;
		try {
			const res =
				(await fetchFn({
					search: search.value.trim(),
					filters: { ...filters },
					filters_v2: wireClauses(),
					sort_field: sort.value.field || "",
					sort_dir: sort.value.dir || "",
					start: append ? rows.value.length : 0,
					page_length: pl,
				})) || {};
			if (id !== reqId) return; // stale - a newer request superseded this one
			// A rejection can also arrive as a resolved envelope if the deliberate
			// 4xx ever fails to be set: treat it as the rejection it is, never as
			// an empty page.
			const envelope = res && res.ok === false ? filterErrorInfo(res) : null;
			if (envelope) {
				applyFilterError(envelope);
				return;
			}
			const nr = res.rows || [];
			rows.value = append ? [...rows.value, ...nr] : nr;
			total.value = res.total != null ? res.total : rows.value.length;
			hasMore.value =
				res.has_more != null ? !!res.has_more : rows.value.length < total.value;
			if (!append && res.facets) facets.value = res.facets;
			error.value = "";
			filterError.value = null;
		} catch (e) {
			if (id !== reqId) return;
			const info = filterErrorInfo(e);
			if (info) {
				applyFilterError(info);
				return;
			}
			error.value = errMsg(e); // keep last-good rows visible
			toast.error(error.value);
		} finally {
			if (id === reqId) loading.value = false;
		}
	}

	/**
	 * A coded filter rejection is NOT a generic failure: it gets the panel's
	 * mapped copy instead of a toast the user can only dismiss, and the field
	 * catalog is refetched when the code says the catalog itself moved (a field
	 * renamed, un-permitted or newly withheld by the endpoint).
	 */
	function applyFilterError(info) {
		filterError.value = info;
		error.value = info.message;
		if (info.kind !== "schema" || !viewKey) return;
		// The catalog moved under us. Refetch it, drop what this caller can no
		// longer use, and RELOAD — otherwise the user is left staring at a
		// rejection with no way out but Clear all. The clause list strictly
		// shrinks, so this cannot cycle.
		schemaState.value = "idle";
		schema.value = null;
		ensureSchema().then(() => {
			if (dropUnavailableClauses()) resetLoad();
		});
	}

	function resetLoad() {
		return fetchRows("reset");
	}
	function loadMore() {
		if (!hasMore.value || loading.value) return;
		return fetchRows("more");
	}
	function refreshKeep() {
		return fetchRows("keep");
	}

	function setFilter(key, value) {
		if (value === "" || value == null) delete filters[key];
		else filters[key] = value;
		materializeQuick([key]);
		writeUrl();
		return resetLoad();
	}
	// Replace the whole filter set (ListPage's update:filters emits a plain
	// object); empty values are stripped so the backend's strict key/value
	// whitelists never see blanks.
	function setFilters(next) {
		const before = { ...filters };
		for (const k of Object.keys(filters)) delete filters[k];
		for (const [k, v] of Object.entries(next || {})) {
			if (v !== "" && v != null) filters[k] = v;
		}
		// Only the quick keys the user actually MOVED are materialized. Running
		// the mapping over every key on every change would let one quick filter's
		// edit wipe a richer clause the user built in the panel on another field.
		const changed = Object.keys(quickClauses).filter(
			(k) => String(before[k] ?? "") !== String(filters[k] ?? "")
		);
		materializeQuick(changed);
		writeUrl();
		return resetLoad();
	}
	function setSort(field, dir) {
		sort.value = { field: field || "", dir: dir || "" };
		return resetLoad();
	}

	// ── schema ───────────────────────────────────────────────────────────────
	/**
	 * Fetch the caller's field catalog once. Lazy by default (FilterGroup asks on
	 * first open), eager only when the URL arrived with clauses — those have to be
	 * reconciled against a real schema before page one is fetched (plan §8).
	 */
	function ensureSchema() {
		if (!viewKey) return Promise.resolve(null);
		if (schemaState.value === "ready") return Promise.resolve(schema.value);
		if (schemaInflight) return schemaInflight;
		const seq = ++schemaSeq;
		schemaState.value = "loading";
		schemaError.value = null;
		schemaInflight = Promise.resolve()
			.then(() => schemaFetcher(viewKey))
			.then((res) => {
				if (seq !== schemaSeq) return null;
				const info = res && res.ok === false ? filterErrorInfo(res) : null;
				if (info) {
					schemaState.value = "error";
					schemaError.value = info;
					return null;
				}
				schema.value = res || null;
				schemaState.value = res ? "ready" : "error";
				if (!res) schemaError.value = { code: "", kind: "transient", message: "No fields returned." };
				return schema.value;
			})
			.catch((e) => {
				if (seq !== schemaSeq) return null;
				schemaState.value = "error";
				schemaError.value = filterErrorInfo(e) || {
					code: "",
					kind: "transient",
					message: errMsg(e),
				};
				return null;
			})
			.finally(() => {
				if (seq === schemaSeq) schemaInflight = null;
			});
		return schemaInflight;
	}


	/**
	 * Plan §8 steps 3-4: keep what is still filterable, and SAY what was not.
	 * Returns whether anything was dropped, so a caller can reload.
	 */
	function dropUnavailableClauses() {
		if (!schema.value) return false;
		const { kept, dropped } = reconcileClauses(filterClauses.value, index.value);
		if (!dropped.length) return false;
		filterClauses.value = kept;
		filterNotice.value = droppedNotice(dropped);
		writeUrl();
		return true;
	}
	function dismissFilterNotice() {
		filterNotice.value = "";
	}

	// ── quick-filter ⇄ clause synchronization (plan §6.4) ────────────────────
	// Only 1:1 mappings live in `quickClauses`: a quick control that is an
	// ownership/scope pseudo-filter (Skills' mine|shared) has no canonical field
	// behind it and stays legacy-only.
	function rootDoctype() {
		return (schema.value && schema.value.root_doctype) || "";
	}
	function clausesOn(fieldname) {
		const root = rootDoctype();
		return filterClauses.value.filter((c) => c.doctype === root && c.fieldname === fieldname);
	}

	/** quick control → canonical clause (replaces whatever was on that field). */
	function materializeQuick(keys) {
		if (!schema.value || !keys || !keys.length) return;
		const root = rootDoctype();
		let next = filterClauses.value;
		let touched = false;
		for (const key of keys) {
			const fieldname = quickClauses[key];
			if (!fieldname) continue;
			const entry = entryFor(index.value, { doctype: root, fieldname });
			if (!entry) continue;
			const value = filters[key];
			next = next.filter((c) => !(c.doctype === root && c.fieldname === fieldname));
			if (value !== undefined && value !== null && String(value) !== "") {
				next = [
					...next,
					makeClause({ doctype: root, fieldname, operator: "=", value: String(value) }),
				];
			}
			touched = true;
		}
		if (touched) filterClauses.value = next;
	}

	/**
	 * canonical clause → quick control. The quick select shows a value only when
	 * exactly ONE complete `=` clause sits on its field; two clauses, or any other
	 * operator, is something the select cannot express, so it goes blank (and
	 * leaves the legacy argument out of the request) rather than lying.
	 */
	function reflectQuick() {
		if (!schema.value) return;
		const root = rootDoctype();
		for (const [key, fieldname] of Object.entries(quickClauses)) {
			const entry = entryFor(index.value, { doctype: root, fieldname });
			if (!entry) continue;
			const matches = clausesOn(fieldname);
			const only = matches.length === 1 ? matches[0] : null;
			if (only && only.operator === "=" && isComplete(only, entry)) filters[key] = String(only.value);
			else delete filters[key];
		}
	}

	/**
	 * The one-time handshake when the schema lands: whatever the toolbar already
	 * shows becomes canonical, so opening the panel does not appear to lose the
	 * quick filter the user set before it loaded.
	 */
	function adoptQuickOnSchemaReady() {
		const keys = Object.keys(quickClauses).filter(
			(k) => filters[k] !== undefined && String(filters[k]) !== "" && !clausesOn(quickClauses[k]).length
		);
		materializeQuick(keys);
	}

	function setClauses(next) {
		filterClauses.value = Array.isArray(next) ? next : [];
		filterError.value = null;
		reflectQuick();
		writeUrl();
		return resetLoad();
	}

	// ── URL state ────────────────────────────────────────────────────────────
	// `replace`, not `push`: a filter edit is a refinement of the view you are on,
	// and pushing one history entry per debounced keystroke would make Back mean
	// "undo one character". Reload, bookmark and Back-to-this-page all still
	// reconstruct the list, and the watcher below picks up any external change.
	function writeUrl() {
		if (!viewKey || !route || !router) return;
		const param = serializeClauses(viewKey, filterClauses.value, index.value);
		const current = (route.query && route.query[URL_PARAM]) || "";
		if (param === current) {
			lastWrittenUrl = param;
			return;
		}
		const query = { ...(route.query || {}) };
		if (param) query[URL_PARAM] = param;
		else delete query[URL_PARAM];
		lastWrittenUrl = param;
		const nav = router.replace({ query });
		if (nav && typeof nav.catch === "function") nav.catch(() => {});
	}

	let stopUrlWatch = null;
	if (viewKey && route) {
		stopUrlWatch = watch(
			() => (route.query && route.query[URL_PARAM]) || "",
			(raw) => {
				// Our own write, echoed back by the router - not a navigation.
				if (raw === lastWrittenUrl) return;
				lastWrittenUrl = raw;
				const parsed = parseClauseParam(raw, viewKey, schema.value);
				filterClauses.value = parsed ? parsed.clauses : [];
				filterError.value = null;
				if (schema.value) dropUnavailableClauses();
				reflectQuick();
				resetLoad();
			}
		);
	}

	// search debounced 300ms → resetLoad
	let searchTimer = null;
	watch(search, () => {
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => resetLoad(), 300);
	});
	// page-length switch resets to page 1 (D16)
	watch(pageLength, () => resetLoad());

	onMounted(async () => {
		if (viewKey && filterClauses.value.length) {
			// A link arrived with filters: the catalog decides which of them this
			// caller may still use, and that answer must precede page one.
			await ensureSchema();
			if (schema.value) {
				dropUnavailableClauses();
				reflectQuick();
			}
			// A schema we could not build is OUR failure, not the link's: the
			// clauses still go to the server, which re-validates them and answers
			// with a coded rejection if they are wrong. Dropping them here would
			// silently show an UNfiltered list for a filtered URL.
		}
		resetLoad();
	});
	onBeforeUnmount(() => {
		clearTimeout(searchTimer);
		schemaSeq += 1; // fence any in-flight schema response
		if (stopUrlWatch) stopUrlWatch();
	});

	// One object for ListPage → FilterGroup, so the page wires a single prop
	// instead of seven.
	const filterState = computed(() => ({
		viewKey,
		schema: schema.value,
		schemaState: schemaState.value,
		schemaError: schemaError.value,
		clauses: filterClauses.value,
		error: filterError.value,
		notice: filterNotice.value,
		activeCount: activeCount(filterClauses.value, index.value),
	}));

	/**
	 * The panel's single entry point: first open, and every Try-again after a
	 * failure. `ensureSchema` short-circuits on "ready" and on an in-flight
	 * request, so a retry has to reopen the door explicitly.
	 */
	function requestSchema() {
		if (schemaState.value === "loading") return schemaInflight || Promise.resolve(null);
		if (schemaState.value === "error") {
			schemaState.value = "idle";
			schemaError.value = null;
		}
		return ensureSchema().then((res) => {
			if (res) {
				adoptQuickOnSchemaReady();
				dropUnavailableClauses();
			}
			return res;
		});
	}

	return {
		rows,
		total,
		hasMore,
		loading,
		error,
		facets,
		search,
		filters,
		setFilter,
		setFilters,
		sort,
		setSort,
		pageLength,
		resetLoad,
		loadMore,
		refreshKeep,
		// plan 08
		filterClauses,
		setClauses,
		filterSchema: schema,
		filterSchemaState: schemaState,
		filterError,
		filterNotice,
		filterState,
		requestSchema,
		dismissFilterNotice,
	};
}
