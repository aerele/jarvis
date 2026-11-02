// The personal "Show holiday touches" switch, as a small state machine so bare
// `node --test` can run it. The server row is the truth (default on):
//   - the row stays disabled (state.loaded false) until the server has answered, so it
//     never shows "on" for a user who turned it off;
//   - the store follows only after a successful save, so turning it on never refetches
//     before the preference is stored;
//   - a failed save puts the switch back.
// A failed read keeps the row disabled with a hint (state.loadFailed) and is retried by
// retry(), which the view calls when the page regains focus or becomes visible.
// `state` is a reactive object the caller owns: { value, loaded, loadFailed, saving }.
export function createHolidaySwitch({ state, getSettings, saveSetting, setShown, onError }) {
	let reading = false;

	async function load() {
		if (reading) return;
		reading = true;
		try {
			const res = await getSettings();
			if (!res || res.ok === false) throw new Error("settings read failed");
			const data = res.data;
			if (data && data.show_holiday_touches !== undefined) {
				state.value = !!data.show_holiday_touches;
				// The store starts on, so this is what makes a user booted with it off
				// (the page carried "none") ask the server once they turn it on.
				setShown(state.value);
			}
			state.loaded = true;
			state.loadFailed = false;
		} catch {
			state.loadFailed = true;
		} finally {
			reading = false;
		}
	}

	/** Read again after a failed read; nothing once loaded. */
	async function retry() {
		if (!state.loaded) await load();
	}

	async function toggle(next) {
		if (state.saving) return;
		const previous = state.value;
		state.value = !!next;
		state.saving = true;
		try {
			await saveSetting(next ? 1 : 0);
			setShown(!!next);
		} catch (error) {
			state.value = previous;
			onError(error);
		} finally {
			state.saving = false;
		}
	}

	return { load, retry, toggle };
}
