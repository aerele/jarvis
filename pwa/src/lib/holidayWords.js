// The words on the PWA welcome hero. The ordinary greeting already carries its full stop
// ("Good morning, Priya."), so the day heading keeps one too. The text itself lives in
// the shared table, so every surface says the same thing. A relative import, not the
// @jsshared alias, so bare `node --test` can run this file.
import { headingFor, leadupLineFor } from "../../../jarvis/public/js/shared/holiday/copy.mjs";

/**
 * {heading, leadup} for the hero. A day or birthday replaces the greeting; a lead-up keeps
 * it and adds the advance wish under it; anything else (or an unknown occasion) is the
 * ordinary greeting.
 */
export function heroWords(answer, { greeting, firstName }) {
	if (answer.kind === "day" || answer.kind === "birthday") {
		const heading = headingFor(answer.occasion, firstName);
		if (heading) return { heading: `${heading}.`, leadup: null };
	}
	if (answer.kind === "leadup") {
		const leadup = leadupLineFor(answer.occasion);
		if (leadup) return { heading: greeting, leadup };
	}
	return { heading: greeting, leadup: null };
}
