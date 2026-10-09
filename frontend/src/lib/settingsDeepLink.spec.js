import { describe, it, expect } from "vitest";
import { parseSettingsDeepLink } from "./settingsDeepLink.js";

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
