// The router's reaction to Vite's `vite:preloadError`, kept apart from the router so it
// can be tested without loading every view.
import { isHolidayLoading } from "@/lib/holidayWorn";

// A file name in the message that belongs to the holiday art chunk (`holiday-*.js`).
export function isHolidayChunk(err) {
	return /\/holiday-[^/\s]*\.js/.test(String((err && (err.message || err)) || ""));
}

// The holiday art is optional decoration with its own loader, which catches a failure
// and leaves the mark plain. A failed holiday chunk must never reload the page (a
// reload loses an unsent draft). Chrome and Firefox name the file in the message;
// Safari says only "Importing a module script failed.", so the loader's own
// in-flight flag is the second test.
export function isHolidayPreloadError(payload) {
	return isHolidayLoading() || isHolidayChunk(payload);
}

/**
 * The listener for `vite:preloadError`. A stale route chunk is recovered by one
 * reload; a holiday chunk is left alone.
 *   isChunkLoadError(payload)  true when the failure is a missing/stale chunk
 *   recover()                  reloads to the route being navigated to
 */
export function createPreloadErrorHandler({ isChunkLoadError, recover }) {
	return (event) => {
		const payload = event && event.payload;
		if (isHolidayPreloadError(payload)) return;
		if (isChunkLoadError(payload)) {
			event.preventDefault();
			recover();
		}
	};
}
