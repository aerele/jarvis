// One history entry for an open draft sheet, so the phone's Back always lands on the sheet's guard.
//
// Without it, a chat opened straight from a notification is the FIRST history entry: Back leaves the
// app and a dirty form is lost without a question. While the sheet is open there is one extra entry
// (the same route with the hash `#editing`); Back pops it and the router asks the guard, which
// closes a clean sheet (the pop stands: the entry is consumed) or asks about a dirty one (the guard
// refuses and vue-router puts the entry back). When the sheet closes any other way (the close
// control, a save, a discard, a dead card) the entry is popped again with `router.back()`, so the
// history neither grows nor holds a stale entry, and one Back then leaves the chat as before.
//
// A navigation that LEAVES the chat while the marker entry is on top (a notification tap, a link, the
// chat list) would stack its destination on the marker and leave one stale entry behind (one extra Back
// lands on the same chat again). The guard holds it back and `leaveTo` continues it as a REPLACE of the
// marker, so the history reads as if the sheet had never been open. A page that is RELOADED with the
// marker in its URL (`#editing`, no sheet any more) is cleaned the same way by `clearStale`.
//
// `own()` is true while this module's own push, pop or replace is running: the guard must let it through.
// Pure of Vue; the router is injected (a memory-history router in the test).
export const SHEET_HASH = "#editing";

export function makeSheetHistory(router) {
	let entry = false; // the marker entry is on top of the history
	let running = 0; // our own navigations in flight

	// Runs a navigation of our own and resolves when the router has finished it (or after a second:
	// a navigation that never settles must not hold the guard open for ever).
	function run(start) {
		running++;
		return new Promise((resolve) => {
			let done = false;
			let off = () => {};
			const finish = () => {
				if (done) return;
				done = true;
				off();
				running--;
				resolve();
			};
			off = router.afterEach(() => finish());
			setTimeout(finish, 1000);
			try {
				const result = start();
				if (result && typeof result.catch === "function") result.catch(finish);
			} catch {
				finish();
			}
		});
	}

	return {
		own: () => running > 0,
		hasEntry: () => entry,
		// The sheet opened: push the marker entry (the same route, the hash added).
		async mark() {
			if (entry || !router || !router.currentRoute) return;
			entry = true;
			const here = router.currentRoute.value;
			await run(() => router.push({ path: here.path, query: here.query, hash: SHEET_HASH }));
		},
		// The sheet closed by itself: pop the marker entry.
		async release() {
			if (!entry || !router) return;
			entry = false;
			await run(() => router.back());
		},
		// The pop or the navigation that is happening already took the entry (or the chat is left
		// with it still in the history: it cannot be popped from another chat).
		consumed() {
			entry = false;
		},
		// Continue a navigation the guard held back (a clean sheet, or "Discard changes"): replace the
		// marker entry with it when the marker is on top, else a plain push. Never re-enters the guard
		// (`own()` holds while it runs).
		leaveTo(to) {
			if (!router || !to) return Promise.resolve();
			const target = { path: to.path, query: to.query, hash: to.hash };
			if (!entry) return router.push(target);
			entry = false;
			return run(() => router.replace(target));
		},
		// The page was loaded (or reloaded) with the marker in its URL but no sheet: the sheet's state
		// is gone, so take the hash off the URL in place (no history entry added).
		clearStale() {
			if (entry || !router || !router.currentRoute) return Promise.resolve(false);
			const here = router.currentRoute.value;
			if (here.hash !== SHEET_HASH) return Promise.resolve(false);
			return run(() =>
				router.replace({ path: here.path, query: here.query, hash: "" })
			).then(() => true);
		},
	};
}
