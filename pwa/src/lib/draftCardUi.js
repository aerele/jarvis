// The per-card UI state of a server draft card on the phone: which button is in flight and the
// inline error line. Kept BY TOKEN, outside the pending items, because loadPending replaces the
// item objects on every read: a flag kept on an item would vanish in the middle of the card's own
// Create or Cancel. The desktop keeps the same flags on the item (BUSY_ON_ITEM in
// frontend/src/lib/toolDraftHost.js); `where` below is the accessor the phone passes instead.
//
// Pure: `reactive` is injected (Vue's, in the view; the identity function in a test), so this file
// imports nothing and `node --test` can run it.

const EMPTY = Object.freeze({ busyKind: "", error: "" });

export function makeDraftUi(reactive = (x) => x) {
	const state = reactive({});

	const entry = (token) => state[token] || EMPTY;
	// An entry with nothing in it is not kept, so the store never grows past the cards in flight.
	function put(token, patch) {
		if (!token) return;
		const next = { ...entry(token), ...patch };
		if (!next.busyKind && !next.error) {
			if (token in state) delete state[token];
			return;
		}
		state[token] = next;
	}

	const ui = {
		busyKind: (token) => entry(token).busyKind,
		error: (token) => entry(token).error,
		// The tokens whose Create or Cancel is in flight: loadPending must not drop their cards.
		busyTokens: () => Object.keys(state).filter((t) => state[t].busyKind),
		setKind: (token, kind) => put(token, { busyKind: kind || "" }),
		setError: (token, text) => put(token, { error: text || "" }),
		forget: (token) => {
			if (token in state) delete state[token];
		},
		// A conversation switch: nothing of the previous chat's cards stays.
		reset: () => {
			for (const token of Object.keys(state)) delete state[token];
		},
	};
	// The accessor for toolDraftHost: withBusyKind's `where`, `d.where` of the form's Save / Create /
	// Cancel, and toolDraftCtxMap's `readBusy`. `busy` is always false here: the phone's draft
	// cards have no other busy flag than the kind (a Save sets the kind "save", which locks the card).
	ui.where = {
		read: (pa) => {
			const token = pa && pa.token;
			return {
				busy: false,
				busyKind: ui.busyKind(token),
				error: ui.error(token) || undefined,
			};
		},
		setKind: (pa, kind) => ui.setKind(pa && pa.token, kind),
		setBusy: () => {},
	};
	return ui;
}
