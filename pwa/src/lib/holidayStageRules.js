// Decisions of the phone's scene-day welcome (components/HolidayStage.vue), kept out of the
// component so bare `node --test` can run them.

/**
 * Does the welcome screen use the stage? Only on a day or a birthday the server marked as a
 * scene day, with an item to wear, for a design that has a scene in this build, not white
 * label and with no maintenance hold when the screen opened. Anything else keeps the
 * ordinary welcome (which already shows the day heading). `hasScene(design)` is injected so
 * this file needs no bundler alias.
 */
export function stageEligible({ answer, whiteLabel, holdAtMount, hasScene }) {
	if (!answer || whiteLabel || holdAtMount) return false;
	if (answer.kind !== "day" && answer.kind !== "birthday") return false;
	return answer.scene === true && answer.worn === true && hasScene(answer.design);
}

/**
 * "light" or "dark" for the art. The page pins a theme on <html> only for light and dark;
 * system leaves no attribute, so the art's own wrapper has to carry the resolved one.
 */
export function resolveTheme(pinned, systemDark) {
	if (pinned === "light" || pinned === "dark") return pinned;
	return systemDark ? "dark" : "light";
}
