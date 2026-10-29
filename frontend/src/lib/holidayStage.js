// Small pure rules of the holiday stage layout (components/holiday/HolidayStage.vue).
import { FEST } from "@jsshared/holiday/copy.mjs";
import { hasScene } from "@/lib/holidayScenes";

/**
 * Does the welcome screen use the stage layout? Only on a day or a birthday the server
 * marked as a scene day (the first day of a run, not white label), for a design that has
 * a scene in this build, with the item to wear, on a viewport that is not phone mode and
 * with no maintenance hold when the screen opened. Everything else keeps the normal
 * layout, which already shows the day heading.
 */
export function stageEligible({ answer, phone, whiteLabel, holdAtMount }) {
	if (!answer || phone || whiteLabel || holdAtMount) return false;
	if (answer.kind !== "day" && answer.kind !== "birthday") return false;
	return answer.scene === true && answer.worn === true && hasScene(answer.design);
}

/**
 * The starter cards with the festive greeting card in place of the last one. Never on a
 * birthday (it stays private). The input is not changed.
 */
export function withFestiveCard(cards, answer) {
	const fest = answer && answer.kind === "day" ? FEST[answer.occasion] : null;
	if (!fest || !cards.length) return cards;
	return [...cards.slice(0, -1), { category: "draft", title: fest[0], prompt: fest[1] }];
}
