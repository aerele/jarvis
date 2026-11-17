// The per-receipt UI state of the Submit next step on the phone: which one is in flight, the
// inline note under it, and the steps refused for good. Kept by the step's key ("Doctype|name"),
// outside the thread rows, because `get_conversation` replaces them on every reload.
// `reactive` is injected (Vue's in the view, the identity in a test), so node can run this.
export function makeReceiptUi(reactive = (x) => x) {
	const state = reactive({ busy: "", notes: {}, gone: {} });
	return {
		busyKey: () => state.busy,
		note: (key) => state.notes[key] || "",
		goneKeys: () => Object.keys(state.gone),
		setBusy: (key) => {
			state.busy = key || "";
		},
		setNote: (key, text) => {
			if (text) state.notes[key] = text;
			else delete state.notes[key];
		},
		hide: (key) => {
			state.gone[key] = true;
		},
		// A conversation switch.
		reset: () => {
			state.busy = "";
			state.notes = {};
			state.gone = {};
		},
	};
}
