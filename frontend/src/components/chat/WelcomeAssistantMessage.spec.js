import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import fs from "fs";
import path from "path";

import WelcomeAssistantMessage from "./WelcomeAssistantMessage.vue";
import { homeIntroPersona, homeIntroSpeaker } from "@/lib/homeIntro";

/**
 * The static first-chat introduction. Two things are pinned here: the copy
 * (every truthfulness claim the product committed to, plus its brand/persona
 * parameterisation) and the honesty of the presentation (no timestamp, no model
 * badge, no live region, no tool row).
 */

const props = (over = {}) => ({
	speaker: "Jarvis",
	persona: "Jarvis",
	firstName: "Vignesh",
	...over,
});

describe("WelcomeAssistantMessage copy", () => {
	it("greets by first name and signs with the speaker", () => {
		const w = mount(WelcomeAssistantMessage, { props: props() });
		expect(w.text()).toContain("Hi Vignesh — I'm Jarvis, your AI teammate inside your ERP.");
	});

	it("is FROM Jara for a Jara user, name and mark together", () => {
		const w = mount(WelcomeAssistantMessage, {
			props: props({ speaker: "Jara", persona: "Jara" }),
		});
		expect(w.text()).toContain("I'm Jara, your AI teammate");
		expect(w.text()).not.toContain("I'm Jarvis");
		// Jara's own mark, as PersonaPill draws her.
		expect(w.find(".jv-wam-orb.jara").exists()).toBe(true);
		expect(w.find(".jv-mark").exists()).toBe(false);
	});

	it("carries the tenant brand with no hardcoded Jarvis anywhere", () => {
		const w = mount(WelcomeAssistantMessage, { props: props({ speaker: "Aria" }) });
		expect(w.text()).toContain("I'm Aria, your AI teammate");
		expect(w.text()).not.toContain("Jarvis");
	});

	it("shows the tenant's own mark, not Jara's orb, for a branded Jara user", () => {
		// The reconciliation end to end: the resolver decides both halves, and the
		// component must render a brand mark (which is where a tenant logo lands)
		// alongside the brand name - never an orb beside a brand name.
		const branded = { agentName: "Aria", isWhitelabeled: true, persona: "Jara" };
		const persona = homeIntroPersona(branded);
		const speaker = homeIntroSpeaker({ ...branded, persona });
		const w = mount(WelcomeAssistantMessage, { props: props({ speaker, persona }) });
		expect(w.find(".jv-mark").exists()).toBe(true);
		expect(w.find(".jv-wam-orb").exists()).toBe(false);
		expect(w.text()).toContain("I'm Aria, your AI teammate");
		expect(w.text()).not.toContain("Jara");
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
		const h = w.find("h1.jv-wam-sr");
		// The name lives in ONE place — the hidden heading — and the section points
		// at it with aria-labelledby, so a screen reader announces it once (not the
		// old region label + heading duplicate).
		expect(root.attributes("aria-labelledby")).toBe(h.attributes("id"));
		expect(root.attributes("aria-label")).toBeUndefined();
		expect(h.text()).toBe("Welcome message from Aria");
		expect(root.attributes("aria-live")).toBeUndefined();
		expect(root.attributes("role")).toBeUndefined(); // <section> + name IS a region
		expect(root.attributes("data-presentation-only")).toBe("true");
	});

	it("draws no visible name line, matching a real assistant turn", () => {
		// ChatView passes no `sender` to Message.vue, so assistant rows render the
		// avatar and the body only. A bold name here would make the introduction
		// look like a different KIND of message than every reply that follows it.
		const w = mount(WelcomeAssistantMessage, { props: props({ speaker: "Aria" }) });
		expect(w.find(".jv-wam-name").exists()).toBe(false);
		expect(w.find(".jv-wam-who").exists()).toBe(false);
	});

	it("restores a coherent level-one heading, visually hidden", () => {
		// The compact hero's <h1> is replaced by the intro; the landmark returns as
		// a visually hidden <h1> (NOT an <h2>), so the empty state keeps a single,
		// correct top-level heading instead of jumping straight to level 2.
		const w = mount(WelcomeAssistantMessage, { props: props({ speaker: "Aria" }) });
		expect(w.find("h2").exists()).toBe(false);
		const h = w.find("h1");
		expect(h.exists()).toBe(true);
		expect(h.text()).toBe("Welcome message from Aria");
		// Visually hidden, NOT removed from the accessibility tree.
		expect(h.classes()).toContain("jv-wam-sr");
		expect(h.attributes("aria-hidden")).toBeUndefined();
	});

	it("gives each welcome a unique heading id so two on a page never collide", () => {
		const a = mount(WelcomeAssistantMessage, { props: props() });
		const b = mount(WelcomeAssistantMessage, { props: props() });
		const idA = a.find("h1").attributes("id");
		const idB = b.find("h1").attributes("id");
		expect(idA).toBeTruthy();
		expect(idA).not.toBe(idB);
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
 * socket, a router and two dozen bootstrap calls), so the template GATES it owns
 * are pinned at the source level. The boot/latch/ack BEHAVIOUR now lives in the
 * useHomeIntro composable and is executed in composables/useHomeIntro.spec.js —
 * these assertions are honestly labelled architectural tripwires (placement and
 * wiring), not the lifecycle matrix.
 */
describe("ChatView wiring (source tripwires, not behaviour)", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../../views/ChatView.vue"), "utf8");
	const composable = fs.readFileSync(
		path.resolve(__dirname, "../../composables/useHomeIntro.js"),
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

	it("drives the intro state from the extracted composable, not inline in the view", () => {
		expect(src).toContain('import { useHomeIntro } from "@/composables/useHomeIntro"');
		expect(src).toContain("} = useHomeIntro({");
		expect(src).toContain("initHomeIntro();"); // boot init call
	});

	it("gates the bubble on the empty state AND the pending flag (in the composable)", () => {
		// The conjunct IS the invariant. `homeIntroPending` alone would draw the
		// bubble over a live thread; `showWelcome` alone would draw it on every
		// empty chat forever. Dropping either half is the regression.
		expect(composable).toContain(
			"const showHomeIntro = computed(() => showWelcome.value && homeIntroPending.value);"
		);
	});

	it("suppresses the business-note banner while the introduction shows", () => {
		expect(src).toContain('v-if="bizGreeting.show && !showHomeIntro"');
	});

	it("renders the minimal greeting over SYNTHESISED starter cards", () => {
		// The empty state is the brand mark inline with the greeting, then starter
		// cards whose copy comes from the user's own history — never the old
		// hard-coded four. A regression that reintroduces a static list trips here.
		expect(src).toContain("<span>{{ greeting }}, {{ firstName }}</span>");
		expect(src).toContain('class="jv-welcome-grid"');
		expect(src).toContain('v-for="(s, i) in promptSuggestions"');
		expect(src).not.toContain("const suggestions = [");
	});

	it("a starter card fills the composer and never sends", () => {
		// The do-not-regress rule the original cards carried: clicking a suggestion
		// puts it in the box for the user to edit, it does not fire a turn.
		expect(src).toContain('@click="fillInput(s.prompt)"');
		const card = src.slice(src.indexOf('v-for="(s, i) in promptSuggestions"'));
		expect(card.slice(0, card.indexOf("</button>"))).not.toContain("sendMessage");
	});

	it("moves the welcome viewport off inline styles onto overflow-safe classes", () => {
		// P1-01: the scroll viewport must not centre with justify-content:center
		// (unsafe centring clips the top when the content overflows). Classes, not
		// inline styles, so the overflow-safe rules can win and tests can target it.
		expect(src).toContain('class="jv-welcome-scroll"');
		expect(src).toContain('class="jv-welcome-col"');
		expect(src).toContain("justify-content: flex-start;");
		expect(src).toContain("margin-block: auto;");
	});
});
