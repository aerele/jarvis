import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import fs from "fs";
import path from "path";

import WelcomeAssistantMessage from "./WelcomeAssistantMessage.vue";

/**
 * The static first-chat introduction. Two things are pinned here: the copy
 * (every truthfulness claim the product committed to, plus its brand/persona
 * parameterisation) and the honesty of the presentation (no timestamp, no model
 * badge, no live region, no tool row).
 */

const props = (over = {}) => ({ speaker: "Jarvis", persona: "Jarvis", firstName: "Vignesh", ...over });

describe("WelcomeAssistantMessage copy", () => {
	it("greets by first name and signs with the speaker", () => {
		const w = mount(WelcomeAssistantMessage, { props: props() });
		expect(w.text()).toContain("Hi Vignesh — I'm Jarvis, your AI teammate inside your ERP.");
		expect(w.find(".jv-wam-name").text()).toBe("Jarvis");
	});

	it("is FROM Jara for a Jara user, name and mark together", () => {
		const w = mount(WelcomeAssistantMessage, { props: props({ speaker: "Jara", persona: "Jara" }) });
		expect(w.find(".jv-wam-name").text()).toBe("Jara");
		expect(w.text()).toContain("I'm Jara, your AI teammate");
		expect(w.text()).not.toContain("I'm Jarvis");
		// Jara's own mark, as PersonaPill draws her.
		expect(w.find(".jv-wam-orb.jara").exists()).toBe(true);
		expect(w.find(".jv-mark").exists()).toBe(false);
	});

	it("carries the tenant brand with no hardcoded Jarvis anywhere", () => {
		const w = mount(WelcomeAssistantMessage, { props: props({ speaker: "Aria" }) });
		expect(w.find(".jv-wam-name").text()).toBe("Aria");
		expect(w.text()).toContain("I'm Aria, your AI teammate");
		expect(w.text()).not.toContain("Jarvis");
	});

	it("renders the default brand mark for the default persona", () => {
		const w = mount(WelcomeAssistantMessage, { props: props() });
		expect(w.find(".jv-mark").exists()).toBe(true);
		expect(w.find(".jv-wam-orb").exists()).toBe(false);
	});

	it("falls back to a neutral greeting when the user has no first name", () => {
		const w = mount(WelcomeAssistantMessage, { props: props({ firstName: "  " }) });
		expect(w.text()).toContain("Hi there — I'm Jarvis");
	});

	it("states every capability claim the product committed to, and no more", () => {
		const t = mount(WelcomeAssistantMessage, { props: props() }).text();
		// permissions: what it can see, never "all your ERP data"
		expect(t).toContain("I only see the records your Frappe permissions allow");
		// propose-and-confirm, honestly hedged: per-conversation auto-apply and the
		// File Box reversible-write path both exist, so "by default" is load-bearing.
		expect(t).toContain("by default I propose a change and ask you to confirm");
		expect(t).toContain("destructive actions always ask");
		// File Box drops become REVIEWABLE drafts, not silent writes
		expect(t).toContain("File Box");
		expect(t).toContain("drafts you review");
		// dashboards on live data
		expect(t).toContain("dashboards that read your live data");
		// hands off rather than lecturing further (TourIntro owns the walkthrough)
		expect(t).toContain("What would you like to work on?");
	});

	it("stays short: four sentences, no walkthrough", () => {
		const w = mount(WelcomeAssistantMessage, { props: props() });
		expect(w.findAll(".jv-wam-body p")).toHaveLength(4);
	});
});

describe("WelcomeAssistantMessage presentation honesty", () => {
	it("is a labelled region, never a live region", () => {
		const w = mount(WelcomeAssistantMessage, { props: props({ speaker: "Aria" }) });
		const root = w.find("section.jv-wam");
		expect(root.attributes("aria-label")).toBe("Welcome message from Aria");
		expect(root.attributes("aria-live")).toBeUndefined();
		expect(root.attributes("role")).toBeUndefined(); // <section> + name IS a region
		expect(root.attributes("data-presentation-only")).toBe("true");
	});

	it("claims no timestamp, no model and no tool activity", () => {
		const w = mount(WelcomeAssistantMessage, { props: props() });
		const html = w.html();
		expect(html).not.toContain("jv-msgtime");
		expect(html).not.toContain("jv-activity");
		expect(html).not.toContain("jv-tool");
		expect(w.text()).not.toMatch(/\b(gpt|claude|model)\b/i);
	});

	it("emits its seen ack exactly once, on render", () => {
		const w = mount(WelcomeAssistantMessage, { props: props() });
		expect(w.emitted("seen")).toHaveLength(1);
	});
});

/**
 * ChatView.vue cannot be mounted in a unit test (it is a ~12k-line view with a
 * socket, a router and two dozen bootstrap calls), so the two gates that live
 * in its template are pinned at the source level instead. These assertions are
 * deliberately literal: they fail loudly if someone moves the bubble out of the
 * empty-state branch or drops the greeting-banner suppression, which is exactly
 * the regression they exist to catch. They prove wiring, not behaviour.
 */
describe("ChatView wiring", () => {
	const src = fs.readFileSync(
		path.resolve(__dirname, "../../views/ChatView.vue"),
		"utf8"
	);

	it("renders the bubble only inside the empty-state branch", () => {
		// => it can never draw over a conversation that has messages, which is
		// what keeps proactive conversations (real persisted rows) showing their
		// own content instead of a welcome.
		const branch = src.indexOf('v-else-if="showWelcome"');
		const bubble = src.indexOf("<WelcomeAssistantMessage");
		const composer = src.indexOf("<!-- ===== COMPOSER");
		expect(branch).toBeGreaterThan(-1);
		expect(bubble).toBeGreaterThan(branch);
		expect(composer === -1 || bubble < composer).toBe(true);
		expect(src).toContain('v-if="showHomeIntro"');
	});

	it("suppresses the business-note banner while the introduction shows", () => {
		expect(src).toContain('v-if="bizGreeting.show && !showHomeIntro"');
	});

	it("keeps the suggestion cards below the introduction", () => {
		expect(src.indexOf("<WelcomeAssistantMessage")).toBeLessThan(
			src.indexOf('class="jv-welcome-grid"')
		);
	});
});
