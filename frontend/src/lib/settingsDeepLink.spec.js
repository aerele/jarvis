import { describe, it, expect } from "vitest";
import { movedSettingsRoute, parseSettingsDeepLink } from "./settingsDeepLink.js";

const KEYS = new Set(["general", "aimodels", "usage"]);

describe("parseSettingsDeepLink", () => {
	it("ignores a plain load", () => {
		expect(parseSettingsDeepLink("", KEYS)).toEqual({ section: "", intent: null, rest: null });
		expect(parseSettingsDeepLink("?reconnect=1", KEYS).rest).toBeNull();
	});

	it("opens a known pane and strips the param", () => {
		expect(parseSettingsDeepLink("?settings=usage&x=1", KEYS)).toEqual({
			section: "usage",
			intent: null,
			rest: "x=1",
		});
	});

	it("carries reconnect as an intent for AI models and strips it too (I8)", () => {
		expect(parseSettingsDeepLink("?settings=aimodels&reconnect=SUB_ab12", KEYS)).toEqual({
			section: "aimodels",
			intent: { reconnect: "SUB_ab12" },
			rest: "",
		});
	});

	it("carries the direct-mode key verbatim", () => {
		expect(
			parseSettingsDeepLink("?settings=aimodels&reconnect=direct:openai", KEYS).intent
		).toEqual({
			reconnect: "direct:openai",
		});
	});

	it("drops reconnect for any other pane and an unknown pane opens nothing", () => {
		expect(parseSettingsDeepLink("?settings=usage&reconnect=A1", KEYS).intent).toBeNull();
		expect(parseSettingsDeepLink("?settings=nope", KEYS)).toEqual({
			section: "",
			intent: null,
			rest: "",
		});
	});
});

describe("movedSettingsRoute", () => {
	it("sends the old macro admin key to the Macros page's Admin tab", () => {
		expect(movedSettingsRoute("macroadmin")).toEqual({
			name: "MacroAdmin",
			query: {},
			hash: "",
		});
	});

	it("keeps the rest of the address", () => {
		const link = parseSettingsDeepLink(
			"?settings=macroadmin&x=1&y=two",
			new Set(["macroadmin"])
		);
		expect(movedSettingsRoute(link.section, link.rest, "#top")).toEqual({
			name: "MacroAdmin",
			query: { x: "1", y: "two" },
			hash: "#top",
		});
	});

	it("is null for a pane that is still a pane, and for no key", () => {
		expect(movedSettingsRoute("usage", "x=1")).toBeNull();
		expect(movedSettingsRoute("")).toBeNull();
	});
});
