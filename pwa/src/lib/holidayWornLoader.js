// Loads the worn-item art lazily and never lets a failure reach the app. The factory has
// no imports of its own (the real import path and the error reporter come in from
// holidayWorn.js), so bare `node --test` can run it.
//
// The art is optional decoration, so a failed import (a stale chunk after a deploy, an
// offline page) gives null: the mark simply wears nothing. It must never reload the page
// or throw into the app:
//   - after a failure nothing retries for a minute, so a dead chunk is not requested on
//     every mount;
//   - the first failure of the session is reported, with fixed text (the error reporter
//     drops chunk-load messages as already handled, and a path must not leak).
export const WORN_RETRY_MS = 60 * 1000;

export function createWornLoader({ load, report, now = Date.now }) {
	let loaded = null; // the module, once it has loaded; kept for the life of the page
	let pending = null; // the import in flight
	let retryAt = 0; // no new attempt before this time (ms since epoch)
	let reported = false;

	function fail() {
		retryAt = now() + WORN_RETRY_MS;
		if (!reported) {
			reported = true;
			report({
				error_class: "HolidayArtLoadFailed",
				error_code: "holiday_art",
				message: "The holiday worn-item art could not be loaded",
			});
		}
		return null;
	}

	return {
		/** The art if it has loaded already, else null. Lets a new mark draw it at once. */
		loaded: () => loaded,
		/** Resolves to the art module, or null when it cannot be loaded. Never rejects. */
		load() {
			if (loaded) return Promise.resolve(loaded);
			if (pending) return pending;
			if (now() < retryAt) return Promise.resolve(null);
			pending = Promise.resolve()
				.then(load)
				.then((mod) => {
					mod.installWornCss();
					loaded = mod;
					return mod;
				})
				.catch(fail)
				.finally(() => {
					pending = null;
				});
			return pending;
		},
	};
}
