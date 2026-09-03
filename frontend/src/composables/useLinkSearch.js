import { ref } from "vue";

/**
 * useLinkSearch - the debounced + monotonic-sequence + prime-on-focus remote
 * search pattern, extracted from three near-identical copies (jarvis#1062):
 * ConfigForm.vue's Company/Fiscal year picker, AgentAccessEditor.vue's people
 * picker, and FilterValueControl.vue's generic Link search. All three share
 * the same shape - an Autocomplete fed by a remote lookup that:
 *
 *   - loads its first page on focus/open rather than sitting empty until a
 *     keystroke ("prime"), but only once per prime cycle;
 *   - debounces further typing (each site's own interval - AgentAccessEditor
 *     used 200ms, the other two 300ms, so the debounce here defaults to
 *     300ms but stays a per-call option);
 *   - fences every response with a monotonic sequence number, so a slow
 *     earlier lookup can never overwrite a newer one's results; and
 *   - can be told to forget it was ever primed - after a pick clears the
 *     menu (AgentAccessEditor), or the search target itself changes
 *     (FilterValueControl's field/operator switch) - so the next open (or an
 *     immediate follow-up prime()) fetches fresh instead of reusing a stale
 *     "already primed" flag.
 *
 * `fetcher(query)` is the only required argument - an async function
 * returning raw rows (or throwing). Everything else is optional:
 *   - `debounceMs` (default 300)
 *   - `mapper(rows)` - reshape raw rows into whatever the caller's
 *     Autocomplete needs (default: pass rows through unchanged), and
 *   - `onSettled({ rows, query, error })` - a hook for state that depends on
 *     how a search resolved beyond the options list itself (e.g.
 *     FilterValueControl's "no matches, enter the name directly" fallback
 *     copy). Never called for a response a newer request has superseded.
 */
export function useLinkSearch(fetcher, opts = {}) {
	const { debounceMs = 300, mapper = (rows) => rows || [], onSettled } = opts;

	const query = ref("");
	const options = ref([]);
	const loading = ref(false);
	const primed = ref(false);
	let seq = 0;
	let timer = null;

	async function runSearch(q) {
		const mySeq = ++seq;
		loading.value = true;
		try {
			const rows = (await fetcher(q || "")) || [];
			if (mySeq !== seq) return; // a newer request already landed
			options.value = mapper(rows);
			loading.value = false;
			if (onSettled) onSettled({ rows, query: q || "", error: null });
		} catch (error) {
			if (mySeq !== seq) return;
			options.value = [];
			loading.value = false;
			if (onSettled) onSettled({ rows: [], query: q || "", error });
		}
	}

	/** Loads the first page once per prime cycle - a no-op if already primed. */
	function prime() {
		if (primed.value) return;
		primed.value = true;
		runSearch(query.value);
	}

	/**
	 * Forgets the current prime cycle and any in-flight/pending search, and
	 * clears back to empty - the next prime() (immediate, or on the next
	 * focus/open) fetches fresh. Does not fetch by itself; call prime()
	 * right after it for an immediate reload.
	 */
	function reprime() {
		seq += 1; // fence out a response still in flight from the old cycle
		clearTimeout(timer);
		primed.value = false;
		query.value = "";
		options.value = [];
		loading.value = false;
	}

	function onQuery(q) {
		query.value = q || "";
		primed.value = true;
		clearTimeout(timer);
		timer = setTimeout(() => runSearch(query.value), debounceMs);
	}

	/** Cancels a pending debounced search - call from onBeforeUnmount. */
	function cleanup() {
		clearTimeout(timer);
	}

	return { options, query, onQuery, prime, reprime, loading, cleanup };
}
