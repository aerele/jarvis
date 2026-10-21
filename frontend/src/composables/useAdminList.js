// The state behind an admin's list of other people's records (the Admin tabs of
// Macros and Skills): the three kinds of load, the focus a keyboard user keeps
// across a reload, and the debounced search.
//
// Three kinds of load, as the Macros Runs tab has (RunsTab.vue, #1597):
//   "reset"  page 1, replaces the list (first load, a filter or search change,
//            Try again with nothing on screen)
//   "more"   the next page, appended (Load more)
//   "keep"   every page already on screen again, replacing the list (Refresh,
//            after an action)
// Only the newest request may write to the list: a slow answer to an earlier
// filter must not replace the rows of the one chosen after it.
//
// The rows on screen were always fetched under ONE search and set of filters
// (`loadedUnder`), and are only ever joined with rows fetched under the same.
// A filter change that fails therefore clears them: they belong to the filter
// that was left, and Load more would otherwise append the new filter's second
// page to the old filter's rows.
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { errMessage } from "@/lib/errors";

export const SEARCH_WAIT_MS = 300;

/**
 * @param {object} o
 * @param {Function} o.fetchPage  ({ search, filters, start, pageLength }) -> { rows, total?, has_more }
 * @param {Ref<string>} o.search  the typed search; the composable debounces its changes
 * @param {object} o.filters      reactive { key: value }; an empty value is "not filtering"
 * @param {Ref} o.rootEl          the tab's root element (focus is only restored inside it)
 * @param {Ref} o.listEl          the list box (focus falls back to it)
 * @param {Function} o.dialogOpen () -> true while a dialog is open over the tab
 * @param {string} o.searchSelector  the search box, inside rootEl (its input gets focus)
 * @param {string} o.openSelector    each row's "open" button, inside listEl
 * @param {string} o.failureText     the sentence for a load that failed with no message
 * @param {Function} [o.onEnvelope]  (answer) called with each answer this list keeps, for a
 *                                   page that reads an extra key of it (a sync status)
 * @param {number} [o.pageSize]      rows of the first page and of each Load more
 * @param {number} [o.pageMax]       most rows one request may ask for
 * @param {number} [o.keepMaxPages]  most requests a "keep" makes; past it the list
 *                                   falls back to its first rows and Load more continues
 */
export function useAdminList(o) {
	const { fetchPage, search, filters, rootEl, listEl, dialogOpen } = o;
	const pageSize = o.pageSize || 20;
	const pageMax = o.pageMax || 100;
	const keepMaxPages = o.keepMaxPages || 5;

	const rows = ref([]);
	const total = ref(0);
	const hasMore = ref(false);
	const loading = ref(false);
	const reloading = ref(false);
	const loaded = ref(false);
	const error = ref("");
	// The load that failed was a next page (Load more): its message goes beside that button.
	const moreFailed = ref(false);

	let latest = 0;
	let loadedUnder = "";
	let searchTimer = null;

	function query() {
		const active = {};
		for (const [k, v] of Object.entries(filters)) if (v !== "") active[k] = v;
		return { search: search.value.trim(), filters: active };
	}

	// Every row on screen again, in pages of `pageMax`, one request after the other,
	// all under `q`. Returns null when a newer load took over part-way.
	async function fetchLoadedPages(q, mine) {
		const want = Math.min(Math.max(rows.value.length, pageSize), pageMax * keepMaxPages);
		const out = [];
		const seen = new Set();
		let res = {};
		let start = 0;
		while (start < want) {
			const pageLength = Math.min(pageMax, want - start);
			res = (await fetchPage({ ...q, start, pageLength })) || {};
			if (mine !== latest) return null;
			// A row made between two requests pushes one from the end of a page
			// to the top of the next: list it once.
			for (const r of res.rows || []) {
				if (!seen.has(r.name)) {
					seen.add(r.name);
					out.push(r);
				}
			}
			start += pageLength;
			if (!res.has_more) break;
		}
		// The last answer's other keys (a page's extras) ride along with the joined rows.
		return { ...res, rows: out, has_more: !!res.has_more };
	}

	async function load(mode = "reset") {
		// Every load carries what is typed in the search box: one still waiting to run
		// would only ask the same again (and take the focus a second time).
		clearTimeout(searchTimer);
		const q = query();
		const under = JSON.stringify(q);
		// The search box is typed in and the rows are still the old search's (the wait
		// before it searches): there is no "next page" or "same rows again" of those.
		if (mode !== "reset" && under !== loadedUnder) mode = "reset";
		const mine = ++latest;
		loading.value = true;
		// The rows on screen are another filter's until this answers: shown as such.
		reloading.value = mode === "reset" && under !== loadedUnder && rows.value.length > 0;
		try {
			const res =
				mode === "keep"
					? await fetchLoadedPages(q, mine)
					: (await fetchPage({
							...q,
							start: mode === "more" ? rows.value.length : 0,
							pageLength: pageSize,
					  })) || {};
			if (mine !== latest || !res) return;
			const page = res.rows || [];
			if (mode === "more") {
				// A row made or deleted between two pages shifts the rows: never list
				// one twice.
				const seen = new Set(rows.value.map((r) => r.name));
				rows.value = [...rows.value, ...page.filter((r) => !seen.has(r.name))];
			} else {
				rows.value = page;
			}
			total.value = Number(res.total) || 0;
			hasMore.value = !!res.has_more;
			loadedUnder = under;
			error.value = "";
			moreFailed.value = false;
			loaded.value = true;
			if (o.onEnvelope) o.onEnvelope(res);
		} catch (e) {
			if (mine !== latest) return;
			error.value = errMessage(e, o.failureText);
			moreFailed.value = mode === "more";
			loaded.value = true;
			// A refresh that fails keeps the last-good rows. A FILTER CHANGE that fails
			// does not: the rows on screen belong to the filter that was left.
			if (under !== loadedUnder) {
				rows.value = [];
				total.value = 0;
				hasMore.value = false;
			}
		} finally {
			if (mine === latest) {
				loading.value = false;
				reloading.value = false;
			}
		}
	}

	// A button that is loading is disabled, and the browser takes focus off a disabled
	// button: a keyboard user would be dropped at the top of the page. So a load that a
	// control here starts goes through `loadKeepingFocus`, which puts focus back where
	// it was once the load has answered.
	//
	// Only for a press made without a pointer. A mouse click focuses a button too, but
	// putting focus back after one opens that button's tooltip with nothing to close
	// it, and in a text box it raises a phone's keyboard over the list. And never while
	// a dialog is open over the tab, or when the user has moved focus themselves.
	//
	// Which it was is read off the click: one that comes from a key, or from assistive
	// technology pressing the button, counts no pointer clicks (`detail` 0). The typed
	// search has no click, so it goes by the last thing used in the tab.
	//
	// A pointer pressed anywhere on the page counts, not only in the tab: the sidebar
	// and the page header are outside it, and a click there while a load runs means
	// the user has gone elsewhere just the same.
	let keyboard = false;
	let pointerUses = 0;
	function usedKeyboard(yes) {
		keyboard = yes;
		if (!yes) pointerUses += 1;
	}
	const onPagePointer = () => usedKeyboard(false);
	const withoutPointer = (event) =>
		event && typeof event.detail === "number" ? event.detail === 0 : keyboard;
	const searchBox = () =>
		rootEl.value && rootEl.value.querySelector(`${o.searchSelector} input`);

	// `event` is the click that started the load (none for the typed search).
	// `after(lost)` picks where focus goes, given the element that had it (which may
	// be gone or disabled by now); the default is that element, else the list.
	async function loadKeepingFocus(mode, event, after) {
		const at = document.activeElement;
		const mine = withoutPointer(event) && !!rootEl.value && rootEl.value.contains(at);
		const pointerBefore = pointerUses;
		await load(mode);
		// A pointer used while it loaded: the user is somewhere else now.
		if (!mine || pointerUses !== pointerBefore) return;
		await nextTick();
		if (!focusLost() || dialogOpen() || !rootEl.value) return;
		const usable = (el) => !!el && el.isConnected && !el.disabled;
		const target =
			(after && after(at)) || (usable(at) ? at : null) || listEl.value || searchBox();
		if (usable(target)) target.focus();
	}

	// The next page. Focus goes to the first row it brought, so the next Tab carries on
	// down the list; when it brought none, or failed, it stays on the button.
	function loadMore(event) {
		const before = rows.value.length;
		return loadKeepingFocus("more", event, () => {
			const opens = listEl.value ? listEl.value.querySelectorAll(o.openSelector) : [];
			return opens.length > before ? opens[before] : null;
		});
	}

	function onSearch(value) {
		search.value = value == null ? "" : String(value);
		clearTimeout(searchTimer);
		// Tab from the box lands on Refresh, which this load disables for a moment.
		searchTimer = setTimeout(() => loadKeepingFocus("reset"), SEARCH_WAIT_MS);
	}

	// Resolves once no load is in flight (at once when none is): what a caller reads
	// from `rows` after it is the answer, not a list half way through a reload.
	function settled() {
		if (!loading.value) return Promise.resolve();
		return new Promise((done) => {
			const stop = watch(loading, (now) => {
				if (now) return;
				stop();
				done();
			});
		});
	}

	onMounted(() => document.addEventListener("pointerdown", onPagePointer, true));
	onBeforeUnmount(() => {
		document.removeEventListener("pointerdown", onPagePointer, true);
		clearTimeout(searchTimer);
		latest++; // an answer still on its way is nobody's now
	});

	return {
		rows,
		total,
		hasMore,
		loading,
		reloading,
		loaded,
		error,
		moreFailed,
		load,
		loadKeepingFocus,
		loadMore,
		onSearch,
		searchBox,
		settled,
		usedKeyboard,
	};
}

// Nothing has focus: the element that had it went away or was disabled.
export function focusLost() {
	const el = document.activeElement;
	return !el || el === document.body;
}
