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
	isShapeComplete,
	serializeClauses,
	parseClauseParam,
	isUrlPayloadTooLarge,
	reconcileClauses,
	droppedNotice,
	skippedNotice,
	boundedNotice,
	labelsFor,
	UNREADABLE_NOTICE,
	ROLLED_BACK_NOTICE,
	URL_TOO_LARGE_NOTICE,
	URL_TOO_LARGE_UNSHARED_NOTICE,
	filterErrorInfo,
	ERR_VIEW_NOT_FILTERABLE,
	ERR_VIEW_ROLLED_BACK,
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
	// The view's root DocType. NOT a field catalog — that only ever comes from
	// the server — but the view's identity, mirroring its list_registry entry. It
	// is what lets a quick filter become a canonical, shareable clause BEFORE the
	// catalog has been fetched; once the schema lands its own `root_doctype` wins.
	rootDoctype: declaredRoot = "",
	route = null, // vue-router route/router; omitted ⇒ no URL state
	router = null,
	fetchSchema = null, // injectable for tests; defaults to the real endpoint
	fetchCapabilities = null, // injectable for tests; defaults to the capability probe
	// How long a typed value waits before it becomes a request (P2-3). Applies to
	// the whole clause model, so it covers every control family regardless of what
	// props the individual input happens to support.
	clauseDebounceMs = 300,
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
	// P1-05: the server rolled this view back to legacy transport (its schema
	// endpoint declined with view_not_filterable). We stop SENDING filters_v2 —
	// the request goes the legacy way — but the Filter action stays visible; it
	// simply reports that the catalog is unavailable. The compile path server-side
	// still honours any v2 a stale tab sends, so nothing is silently dropped.
	const filtersV2Disabled = ref(false);
	const index = computed(() => schemaIndex(schema.value));
	const schemaFetcher = fetchSchema || ((key) => api.getListFilterSchema(key));
	// The runtime capability probe (P1-05 / M1.3): a single cheap call that returns
	// {view_key: enabled} for every migrated view, so we can learn this view's v2
	// availability BEFORE any filters_v2 emission — the quick-filter and URL paths
	// can otherwise emit before the (lazy) schema fetch would have declined. Defends
	// against a missing api export in unit tests by resolving null (⇒ assume ON).
	const capabilityFetcher =
		fetchCapabilities ||
		(() =>
			api.getListFilterCapabilities
				? api.getListFilterCapabilities()
				: Promise.resolve(null));
	let capabilityKnown = false;
	let capabilityInflight = null;

	/**
	 * Mark this view rolled back to legacy transport. Idempotent. Sets a DISTINCT
	 * schema state so the panel shows honest "turned off" copy with no retry (UX1),
	 * never the transient "couldn't be loaded / Try again" it cannot escape.
	 */
	function markRolledBack() {
		filtersV2Disabled.value = true;
		schemaState.value = "disabled";
		schemaError.value = {
			code: ERR_VIEW_ROLLED_BACK,
			kind: "disabled",
			message:
				"Field filters have been switched off for this list. The list still works without them.",
		};
	}

	/**
	 * Learn this view's v2 capability once (cached). Resolves to whether v2 is
	 * enabled. Failure or an unreadable map ⇒ assume ON: the default IS on and the
	 * server's compile path honours a submitted clause anyway, so a probe blip must
	 * never silently strip a working filter.
	 */
	function ensureCapability() {
		if (!viewKey) return Promise.resolve(true);
		if (capabilityKnown) return Promise.resolve(!filtersV2Disabled.value);
		if (capabilityInflight) return capabilityInflight;
		capabilityInflight = Promise.resolve()
			.then(() => capabilityFetcher(viewKey))
			.then((map) => {
				if (map && typeof map === "object" && map[viewKey] === false) markRolledBack();
				capabilityKnown = true;
				return !filtersV2Disabled.value;
			})
			.catch(() => {
				capabilityKnown = true;
				return true;
			})
			.finally(() => {
				capabilityInflight = null;
			});
		return capabilityInflight;
	}

	// monotonic request id - drops stale responses (same guard as ChatView.loadConversation)
	let reqId = 0;
	// separate fence for the schema: a retry must not be overwritten by the slow
	// failure that provoked it.
	let schemaSeq = 0;
	let schemaInflight = null;

	// The URL is parsed BEFORE the first fetch so a shared link's clauses are in
	// the request that builds page one, not applied a render later.
	const urlSeed = route
		? parseClauseParam(route.query && route.query[URL_PARAM], viewKey)
		: null;
	if (urlSeed && urlSeed.clauses.length) filterClauses.value = urlSeed.clauses;
	// The last `fv2` value THIS composable put in the URL. Two jobs: recognising
	// the router's echo of our own write, and telling an authored clear apart
	// from a navigation that dropped our query (see the watcher).
	let lastWrittenUrl = (route && route.query && route.query[URL_PARAM]) || "";

	function wireClauses() {
		// Rolled back to legacy transport (P1-05): send no v2 clauses at all.
		if (filtersV2Disabled.value) return [];
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
					// A view rolled back via the runtime flag (or never migrated)
					// declines here: drop to legacy transport with an HONEST, distinct
					// "turned off" state — no retry (UX1) — so the list still loads.
					if (
						info.code === ERR_VIEW_ROLLED_BACK ||
						info.code === ERR_VIEW_NOT_FILTERABLE
					) {
						markRolledBack();
						return null;
					}
					schemaState.value = "error";
					schemaError.value = info;
					return null;
				}
				schema.value = res || null;
				schemaState.value = res ? "ready" : "error";
				if (!res)
					schemaError.value = {
						code: "",
						kind: "transient",
						message: "No fields returned.",
					};
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
		noteFilter(droppedNotice(dropped, index.value));
		writeUrl();
		return true;
	}

	/**
	 * ONE combined notice for everything a shared link asked for but did not get
	 * (N4): values too large to carry (bounded), rows that were not clauses
	 * (skipped), and fields no longer available (dropped). Each names the affected
	 * field labels where the catalog can resolve them (UX2). All three are reported
	 * together rather than one suppressing the others.
	 */
	function linkNotice(parsed, dropped) {
		const parts = [
			boundedNotice(
				parsed ? parsed.bounded : 0,
				labelsFor(parsed && parsed.boundedFields, index.value)
			),
			skippedNotice(
				parsed ? parsed.skipped : 0,
				labelsFor(parsed && parsed.skippedFields, index.value)
			),
			droppedNotice(dropped, index.value),
		].filter(Boolean);
		return parts.join(" ");
	}
	/** Newest notice wins; they are all "something you asked for did not happen". */
	function noteFilter(message) {
		if (message) filterNotice.value = message;
	}
	function dismissFilterNotice() {
		filterNotice.value = "";
	}

	// ── quick-filter ⇄ clause synchronization (plan §6.4) ────────────────────
	// Only 1:1 mappings live in `quickClauses`: a quick control that is an
	// ownership/scope pseudo-filter (Skills' mine|shared) has no canonical field
	// behind it and stays legacy-only.
	// The schema's answer wins once it exists; before that, the view's declared
	// identity stands in, so a quick filter is canonical (and shareable) from the
	// first click rather than only after someone opens the panel.
	function rootDoctype() {
		return (schema.value && schema.value.root_doctype) || declaredRoot || "";
	}
	function clausesOn(fieldname) {
		const root = rootDoctype();
		return filterClauses.value.filter((c) => c.doctype === root && c.fieldname === fieldname);
	}

	/** quick control → canonical clause (replaces whatever was on that field). */
	function materializeQuick(keys) {
		const root = rootDoctype();
		if (!root || !keys || !keys.length) return false;
		let next = filterClauses.value;
		let touched = false;
		for (const key of keys) {
			const fieldname = quickClauses[key];
			if (!fieldname) continue;
			// With a catalog, a quick key that is not in it stays legacy-only — we
			// will not invent a clause the server would reject. Without one, the
			// shape is all we can check, and that is enough to build the clause.
			if (schema.value && !entryFor(index.value, { doctype: root, fieldname })) continue;
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
		return touched;
	}

	/**
	 * canonical clause → quick control. The quick select shows a value only when
	 * exactly ONE complete `=` clause sits on its field; two clauses, or any other
	 * operator, is something the select cannot express, so it goes blank (and
	 * leaves the legacy argument out of the request) rather than lying.
	 */
	function reflectQuick() {
		const root = rootDoctype();
		if (!root) return;
		for (const [key, fieldname] of Object.entries(quickClauses)) {
			const entry = entryFor(index.value, { doctype: root, fieldname });
			if (schema.value && !entry) continue;
			const matches = clausesOn(fieldname);
			const only = matches.length === 1 ? matches[0] : null;
			const complete = only && (entry ? isComplete(only, entry) : isShapeComplete(only));
			if (only && only.operator === "=" && complete) filters[key] = String(only.value);
			else delete filters[key];
		}
	}

	/**
	 * Whatever the toolbar already shows becomes canonical. Runs on mount and
	 * again when the catalog lands, and it must run BEFORE `reflectQuick` in both
	 * places: reflect derives the quick control FROM the clauses, so reflecting
	 * first on a URL-seeded mount would see no clause for `enabled` and delete the
	 * page's own initial quick filter (waves 2-4 inherit this path — File Box
	 * mounts with a status filter already set).
	 */
	function adoptQuickOnSchemaReady() {
		const keys = Object.keys(quickClauses).filter(
			(k) =>
				filters[k] !== undefined &&
				String(filters[k]) !== "" &&
				!clausesOn(quickClauses[k]).length
		);
		return materializeQuick(keys);
	}

	/**
	 * @param next   the new clause array
	 * @param opts   `{immediate: false}` for a value still being typed — the model
	 *               and the panel update at once, but the request and the URL wait
	 *               out `clauseDebounceMs`. Discrete picks stay instant.
	 */
	function setClauses(next, opts) {
		filterClauses.value = Array.isArray(next) ? next : [];
		filterError.value = null;
		reflectQuick();
		if (opts && opts.immediate === false) return scheduleClauseCommit();
		return commitClauses();
	}

	// One debounce for the whole clause model (P2-3). Debouncing inside each
	// control would cover only the families whose input happens to support it —
	// this covers every one of them, including future ones.
	let clauseTimer = null;
	function scheduleClauseCommit() {
		clearTimeout(clauseTimer);
		clauseTimer = setTimeout(() => {
			clauseTimer = null;
			commitClauses();
		}, clauseDebounceMs);
	}
	function commitClauses() {
		clearTimeout(clauseTimer);
		clauseTimer = null;
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
		// A payload past the bound would be written and then read back as
		// UNREADABLE on the next load — the app handing the user a broken link it
		// made itself. Keep the last shareable param instead, and say so once.
		if (isUrlPayloadTooLarge(param)) {
			if (!urlTooLarge) {
				urlTooLarge = true;
				// Whether a shareable address survives decides which half of the
				// truth to tell: an older param is still copyable, nothing at all is
				// not, and a user who copies the URL either way must not be misled.
				const current = (route.query && route.query[URL_PARAM]) || "";
				noteFilter(current ? URL_TOO_LARGE_NOTICE : URL_TOO_LARGE_UNSHARED_NOTICE);
			}
			return;
		}
		urlTooLarge = false;
		const current = (route.query && route.query[URL_PARAM]) || "";
		if (param === current) {
			lastWrittenUrl = param;
			return;
		}
		// Having nothing to write is not a licence to delete someone else's
		// payload: on a shared route the param may belong to a sibling tab's list.
		if (!param && current && parseClauseParam(current, viewKey) === null) return;
		const query = { ...(route.query || {}) };
		if (param) query[URL_PARAM] = param;
		else delete query[URL_PARAM];
		lastWrittenUrl = param;
		const nav = router.replace({ query });
		if (nav && typeof nav.catch === "function") nav.catch(() => {});
	}
	let urlTooLarge = false;

	/**
	 * The ONE path from a `fv2` string to applied state — used by the first load
	 * and by every later navigation, so back/forward gets exactly the grace the
	 * first load gets: fetch the catalog if we do not have one, reconcile against
	 * it, report what was dropped, then adopt-and-reflect the quick controls.
	 */
	async function applyUrlClauses(raw) {
		const parsed = parseClauseParam(raw, viewKey, schema.value);
		if (parsed === null && raw) return false; // addressed to a sibling tab's list
		if (parsed === null) {
			filterClauses.value = []; // no payload: an authored clear
		} else if (parsed.unreadable) {
			filterClauses.value = [];
			noteFilter(UNREADABLE_NOTICE);
		} else {
			filterClauses.value = parsed.clauses;
		}
		filterError.value = null;
		// The view may be rolled back to legacy transport (M1.4): its v2 clauses are
		// NOT applied and there is nothing to retry. Say so honestly, count nothing,
		// and do not chase a catalog we will not use. (Learn the flag first if the
		// mount probe has not run — e.g. a later navigation.)
		if (!capabilityKnown && filterClauses.value.length) await ensureCapability();
		if (filtersV2Disabled.value) {
			if (
				parsed &&
				!parsed.unreadable &&
				(parsed.clauses.length || parsed.bounded || parsed.skipped)
			)
				noteFilter(ROLLED_BACK_NOTICE);
			adoptQuickOnSchemaReady();
			reflectQuick();
			return true;
		}
		if (filterClauses.value.length && !schema.value) await ensureSchema();
		if (filtersV2Disabled.value) {
			// The schema fetch just revealed the rollback: same honest handling.
			if (
				parsed &&
				!parsed.unreadable &&
				(parsed.clauses.length || parsed.bounded || parsed.skipped)
			)
				noteFilter(ROLLED_BACK_NOTICE);
			adoptQuickOnSchemaReady();
			reflectQuick();
			return true;
		}
		// Reconcile against the catalog, then report bounded + skipped + dropped
		// TOGETHER (N4), each naming its fields where resolvable (UX2).
		let dropped = [];
		if (schema.value) {
			const rec = reconcileClauses(filterClauses.value, index.value);
			dropped = rec.dropped;
			filterClauses.value = rec.kept;
		}
		noteFilter(linkNotice(parsed, dropped));
		// Adopt BEFORE reflect: see adoptQuickOnSchemaReady.
		adoptQuickOnSchemaReady();
		reflectQuick();
		writeUrl();
		// A catalog we could not build is OUR failure, not the link's: the clauses
		// still go to the server, which re-validates them and answers with a coded
		// rejection if they are wrong. Dropping them here would silently show an
		// UNfiltered list for a filtered URL.
		return true;
	}

	let stopUrlWatch = null;
	if (viewKey && route) {
		stopUrlWatch = watch(
			() => (route.query && route.query[URL_PARAM]) || "",
			async (raw) => {
				// Our own write, echoed back by the router - not a navigation.
				if (raw === lastWrittenUrl) return;
				// A transition to NO param that we did not author. Inside one mounted
				// list that is a navigation which dropped the query - a tab switch
				// through `router.push({name})` or `{hash}`, both of which resolve
				// without the current query - not an intentional clear, because an
				// intentional clear goes through setClauses and sets lastWrittenUrl
				// first. Re-assert what is actually in force rather than silently
				// wiping the user's filters.
				if (!raw && lastWrittenUrl) {
					writeUrl();
					return;
				}
				lastWrittenUrl = raw;
				if (await applyUrlClauses(raw)) resetLoad();
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
		if (viewKey) {
			// Learn v2 availability BEFORE the first emission whenever something could
			// emit before the panel opens (M1.3): a declaredRoot lets a quick filter
			// become a v2 clause pre-schema, and a URL arrives with clauses. A plain
			// lazy list emits nothing until its panel opens, so it pays for no probe —
			// the schema fetch on first open learns the flag for it. Flag off ⇒
			// filtersV2Disabled is set here, so ZERO filters_v2 leaves on any path.
			if (declaredRoot || urlSeed !== null) await ensureCapability();
			// A link may have arrived with filters: the catalog decides which of
			// them this caller may still use, and that answer must precede page one.
			// Same function the watcher uses, so the two cannot drift.
			if (urlSeed !== null)
				await applyUrlClauses((route.query && route.query[URL_PARAM]) || "");
			else {
				// No payload at all — but the page may still have handed us initial
				// quick filters, and those are canonical too.
				adoptQuickOnSchemaReady();
				reflectQuick();
				writeUrl();
			}
		}
		resetLoad();
	});
	onBeforeUnmount(() => {
		clearTimeout(searchTimer);
		clearTimeout(clauseTimer);
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
		// Rolled back to legacy transport ⇒ no v2 clause is applied, so the chip must
		// not show an active count over filters that are NOT in force (M1.4).
		filtersDisabled: filtersV2Disabled.value,
		activeCount: filtersV2Disabled.value ? 0 : activeCount(filterClauses.value, index.value),
	}));

	/**
	 * The panel's single entry point: first open, and every Try-again after a
	 * failure. `ensureSchema` short-circuits on "ready" and on an in-flight
	 * request, so a retry has to reopen the door explicitly.
	 */
	function requestSchema() {
		// Rolled back to legacy transport: there is no catalog to fetch and nothing a
		// retry could achieve, so the panel keeps its honest "turned off" state (UX1).
		if (filtersV2Disabled.value) return Promise.resolve(null);
		if (schemaState.value === "loading") return schemaInflight || Promise.resolve(null);
		if (schemaState.value === "error") {
			schemaState.value = "idle";
			schemaError.value = null;
		}
		return ensureSchema().then((res) => {
			if (res) {
				if (adoptQuickOnSchemaReady()) writeUrl();
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
