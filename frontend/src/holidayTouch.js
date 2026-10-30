// The holiday touch the desktop shows today. The store core lives in
// jarvis/public/js/shared/holiday/store.mjs (shared with the Desk widget); this file wires it
// to Vue, the SPA's API call, error reporter and branding. The behaviour is described there.
import { computed, ref } from "vue";
import {
	REFRESH_MIN_MS,
	STALE_RETRY_MAX_MS,
	STALE_RETRY_MS,
	createHolidayStore,
} from "@jsshared/holiday/store.mjs";
import { getHolidayToday } from "@/api";
import { brand } from "@/branding";
import { report as reportError } from "@/lib/errorReporter";
import { loadWorn } from "@/lib/holidayWorn";

export { REFRESH_MIN_MS, STALE_RETRY_MAX_MS, STALE_RETRY_MS };

export function createHolidayTouch({
	report = reportError,
	// Start fetching the worn art early when the page boots with something to wear.
	loadArt = loadWorn,
	canWear = () => !brand.isWhitelabeled,
	...options
}) {
	return createHolidayStore({ ref, computed, report, loadArt, canWear, ...options });
}

export const holidayTouch = createHolidayTouch({
	boot: window.holiday_touch,
	// Looked up at call time, so a spec that mocks @/api without it still imports this.
	fetchToday: () => getHolidayToday(),
});

// Read only, for support (see the store). Not part of the app.
Object.defineProperty(window, "__holidayTouch", {
	get: () => ({ ...holidayTouch.debugView }),
	configurable: true,
});
