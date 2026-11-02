import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";

// ChatView cannot be mounted in a unit test (see ChatView.unavailable.spec.js for how far
// it goes), so the three places it shows the holiday mark are checked in its source: which
// component, with which props. The mark's own behaviour is HolidayMark.spec.js; what the
// hero says is WelcomeHero.spec.js.
const src = fs.readFileSync(path.resolve(__dirname, "ChatView.vue"), "utf8");
const template = src.slice(0, src.indexOf("<script setup"));

// The first self-closing <Tag ... /> at or after `anchor`, as an attribute map.
function tagAfter(tag, anchor) {
	const from = template.indexOf(anchor);
	expect(from, `anchor not found: ${anchor}`).toBeGreaterThan(-1);
	const rest = template.slice(from);
	const open = new RegExp(`<${tag}\\b([^>]*?)/>`).exec(rest);
	expect(open, `<${tag} after ${anchor}`).not.toBeNull();
	// Nothing but whitespace, comments and the anchor's own wrapper between the anchor and
	// the tag: the tag really belongs to that spot.
	expect(rest.slice(0, open.index)).not.toMatch(/<(?:Holiday|Jarvis)Mark\b/);
	const attrs = {};
	for (const m of open[1].matchAll(/([:@\w-]+)(?:="([^"]*)")?/g)) attrs[m[1]] = m[2] ?? true;
	return attrs;
}
const squash = (value) => String(value).replace(/\s+/g, " ").trim();

describe("ChatView holiday call sites", () => {
	it("imports HolidayMark and WelcomeHero", () => {
		expect(src).toContain('import HolidayMark from "@/components/holiday/HolidayMark.vue";');
		expect(src).toContain('import WelcomeHero from "@/components/holiday/WelcomeHero.vue";');
	});

	it("the welcome hero is WelcomeHero with the greeting, the first name and the hold mood", () => {
		const hero = tagAfter("WelcomeHero", '<div v-else-if="showWelcome"');
		expect(hero[":greeting"]).toBe("greeting");
		expect(hero[":first-name"]).toBe("firstName");
		expect(hero[":mood"]).toBe("holdActive ? 'upgrading' : 'star'");
		// The old inline h1 and its fixed greeting line are gone.
		expect(template).not.toMatch(/jv-welcome-h1/);
		expect(template).not.toContain("<span>{{ greeting }}, {{ firstName }}</span>");
	});

	it("the reply avatar is a HolidayMark: thinking while streaming, happy once on completion", () => {
		const avatar = tagAfter("HolidayMark", "<template #avatar>");
		expect(avatar[":size"]).toBe("28");
		expect(avatar[":radius"]).toBe("7");
		expect(squash(avatar[":mood"])).toBe(
			"m.streaming ? 'thinking' : justCompletedId === m.name ? 'happy' : 'star'"
		);
		// Not the welcome mark: no smile, no button on a reply.
		expect(avatar.welcome).toBeUndefined();
		expect(avatar.style).toBe("margin-top: 2px");
	});

	it("the in-flight row is a HolidayMark, thinking once the turn runs", () => {
		const row = tagAfter("HolidayMark", '<div v-if="inFlightRowVisible"');
		expect(row[":size"]).toBe("28");
		expect(row[":radius"]).toBe("7");
		expect(row[":mood"]).toBe("queuedTurn ? 'star' : 'thinking'");
		expect(row.welcome).toBeUndefined();
	});

	it("only those two places use HolidayMark; the rest keep the plain mark", () => {
		expect(template.match(/<HolidayMark\b/g)).toHaveLength(2);
		// The compacting banner and the proactive toast stay plain JarvisMark.
		expect(template).toMatch(/<JarvisMark[^>]*mood="thinking"/);
		expect(template).toMatch(/<JarvisMark :size="30" :radius="8" \/>/);
	});

	it("the stage layout is its own branch ahead of the normal one, with the same cards", () => {
		const stage = template.indexOf("<HolidayStage");
		const normal = template.indexOf('<div v-else-if="showWelcome" class="jv-welcome-scroll">');
		expect(stage).toBeGreaterThan(-1);
		expect(stage).toBeLessThan(normal);
		const open = /<HolidayStage\b([^>]*)>/.exec(template.slice(stage));
		expect(open[1]).toMatch(/v-else-if="showWelcome && stageOn"/);
		expect(open[1]).toMatch(/:hold-active="holdActive"/);
		expect(open[1]).toMatch(/:composer-empty="!input"/);
		// Both layouts render the starters through the one component, so they cannot drift.
		expect(template.match(/<WelcomeStarters\b/g)).toHaveLength(2);
		expect(template).not.toContain("<CapabilityCatalog");
		expect(src).toContain("stageEligible");
		expect(src).toContain("holdAtMount: holdAtWelcomeMount.value");
		expect(src).toContain("phone: store.mobile");
	});

	it("takes the first name from a trimmed full name", () => {
		expect(src).toContain("fullName.trim().split(/\\s+/)[0]");
	});
});
