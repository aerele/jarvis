// The holiday touch the phone shows today. The store core lives in
// jarvis/public/js/shared/holiday/store.mjs (shared with the desktop SPA and the Desk
// widget); this file wires it to Vue, the error reporter and the page's branding. The
// behaviour is described there. The store, the words and the answer reader load eagerly
// (main.js imports this), so no precached view needs a chunk that is not precached; only
// the worn art (lib/holidayWorn.js) is lazy.
import { computed, ref } from "vue";
import { createHolidayStore } from "@jsshared/holiday/store.mjs";
import { report } from "@shared/lib/errorReporter";
import { DESIGNS } from "@jsshared/holiday/answer.mjs";
import { isWhitelabeled } from "@/branding";
import { loadWorn } from "./lib/holidayWorn";

export const holidayTouch = createHolidayStore({
	ref,
	computed,
	boot: window.holiday_touch,
	report,
	// Start fetching the worn art early when the page boots with something to wear.
	loadArt: loadWorn,
	canWear: () => !isWhitelabeled,
	// The default fetch is a plain GET: the endpoint is GET-only, and frappe-ui's call() POSTs.
});

// The answer when today asks the mark to wear something, else null. Never on a branded
// mark: a custom logo has no face, and a custom agent name means the product is not
// presented as Jarvis (the words still show). The hero's room and the mark both read this.
export const wornAnswer = computed(() => {
	const answer = holidayTouch.answer.value;
	return answer.kind !== "none" &&
		answer.worn &&
		DESIGNS.includes(answer.design) &&
		!isWhitelabeled
		? answer
		: null;
});

// Read only, for support (see the store). Not part of the app.
Object.defineProperty(window, "__holidayTouch", {
	get: () => ({ ...holidayTouch.debugView }),
	configurable: true,
});
