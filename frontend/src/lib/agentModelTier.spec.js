import { describe, it, expect } from "vitest";
import {
	tierTheme,
	requirementPhrase,
	providerLabel,
	groupByProvider,
	shouldShowModelCard,
	shouldConfirmInstallModel,
	isPendingApply,
	isNoneEligible,
	isCatalogUnknown,
	currentModelLabel,
	stateBannerText,
	stateBannerType,
	shouldShowNoteBanner,
	noteBannerText,
} from "./agentModelTier";

describe("tierTheme", () => {
	it("maps the three curated tiers to their badge colour", () => {
		expect(tierTheme("Standard")).toBe("gray");
		expect(tierTheme("Advanced")).toBe("blue");
		expect(tierTheme("Frontier")).toBe("green");
	});
	it("falls back to gray for an unknown/absent tier", () => {
		expect(tierTheme("")).toBe("gray");
		expect(tierTheme(undefined)).toBe("gray");
		expect(tierTheme("Mystery")).toBe("gray");
	});
});

describe("requirementPhrase", () => {
	it("uses 'an' before a vowel-leading tier", () => {
		expect(requirementPhrase("Advanced")).toBe("Needs an Advanced model or better.");
	});
	it("uses 'a' before a consonant-leading tier", () => {
		expect(requirementPhrase("Standard")).toBe("Needs a Standard model or better.");
		expect(requirementPhrase("Frontier")).toBe("Needs a Frontier model or better.");
	});
	it("reads as no requirement when the listing declares none", () => {
		expect(requirementPhrase(null)).toBe("No minimum model required.");
		expect(requirementPhrase("")).toBe("No minimum model required.");
	});
});

describe("providerLabel", () => {
	it("title-cases a known provider id", () => {
		expect(providerLabel("anthropic")).toBe("Anthropic");
	});
	it("reads a blank/unresolved provider as Other", () => {
		expect(providerLabel("")).toBe("Other");
		expect(providerLabel(null)).toBe("Other");
	});
});

describe("groupByProvider", () => {
	it("partitions a flat best-first list into provider groups, order preserved", () => {
		const models = [
			{ provider: "anthropic", model: "claude-a" },
			{ provider: "openai", model: "gpt-a" },
			{ provider: "anthropic", model: "claude-b" },
		];
		const groups = groupByProvider(models);
		expect(groups.map((g) => g.provider)).toEqual(["Anthropic", "Openai"]);
		expect(groups[0].models.map((m) => m.model)).toEqual(["claude-a", "claude-b"]);
		expect(groups[1].models.map((m) => m.model)).toEqual(["gpt-a"]);
	});
	it("groups a blank/unresolved provider under Other", () => {
		const groups = groupByProvider([{ provider: "", model: "x" }]);
		expect(groups).toEqual([{ provider: "Other", models: [{ provider: "", model: "x" }] }]);
	});
	it("returns an empty list for no models", () => {
		expect(groupByProvider([])).toEqual([]);
		expect(groupByProvider(undefined)).toEqual([]);
	});
});

// ── flag-off guard (mutation-verified - see the task report) ────────────────
describe("shouldShowModelCard", () => {
	it("is false while info hasn't loaded yet", () => {
		expect(shouldShowModelCard(null)).toBe(false);
	});
	it("is false when the flag is off, even with a requirement/row present", () => {
		expect(shouldShowModelCard({ enforced: 0, min_model: { tier: "Advanced" } })).toBe(false);
		expect(shouldShowModelCard({ enforced: 0, state: "auto" })).toBe(false);
	});
	it("is false when the flag is on but nothing governs this agent yet", () => {
		expect(shouldShowModelCard({ enforced: 1, min_model: null, state: null })).toBe(false);
	});
	it("is true when the flag is on and the listing declares a requirement", () => {
		expect(
			shouldShowModelCard({ enforced: 1, min_model: { tier: "Advanced" }, state: null })
		).toBe(true);
	});
	it("is true when the flag is on and a row already exists (e.g. legacy)", () => {
		expect(shouldShowModelCard({ enforced: 1, min_model: null, state: "legacy" })).toBe(true);
	});
});

describe("shouldConfirmInstallModel", () => {
	it("is false off, or on with no requirement", () => {
		expect(shouldConfirmInstallModel(null)).toBe(false);
		expect(shouldConfirmInstallModel({ enforced: 0, min_model: { tier: "Advanced" } })).toBe(
			false
		);
		expect(shouldConfirmInstallModel({ enforced: 1, min_model: null })).toBe(false);
	});
	it("is true only when enforced AND the listing declares a requirement", () => {
		expect(shouldConfirmInstallModel({ enforced: 1, min_model: { tier: "Advanced" } })).toBe(
			true
		);
	});
});

// ── pending-apply badge condition (mutation-verified - see the task report) ─
describe("isPendingApply", () => {
	it("is false when info is absent or pending_apply is falsy", () => {
		expect(isPendingApply(null)).toBe(false);
		expect(isPendingApply({ pending_apply: 0 })).toBe(false);
	});
	it("is true only when the server says pending_apply", () => {
		expect(isPendingApply({ pending_apply: 1 })).toBe(true);
	});
});

describe("isNoneEligible / isCatalogUnknown", () => {
	it("none-eligible only fires on a confirmed empty catalog", () => {
		expect(isNoneEligible({ catalog: "ok", models: [] })).toBe(true);
		expect(isNoneEligible({ catalog: "ok", models: [{ model: "x" }] })).toBe(false);
	});
	it("an unknown catalog is never read as none-eligible", () => {
		expect(isNoneEligible({ catalog: "unknown", models: [] })).toBe(false);
		expect(isCatalogUnknown({ catalog: "unknown" })).toBe(true);
		expect(isCatalogUnknown({ catalog: "ok" })).toBe(false);
	});
	it("handles a missing eligible payload", () => {
		expect(isNoneEligible(null)).toBe(false);
		expect(isCatalogUnknown(null)).toBe(false);
	});
});

describe("currentModelLabel", () => {
	it("prefers the enriched catalog label", () => {
		expect(
			currentModelLabel({ choice: { label: "Claude Opus 5", model: "claude-opus-5" } })
		).toBe("Claude Opus 5");
	});
	it("falls back to the bare model id when no label was resolved", () => {
		expect(currentModelLabel({ choice: { model: "claude-opus-5" } })).toBe("claude-opus-5");
	});
	it("reads as the tenant default when nothing is pinned (legacy/no row)", () => {
		expect(currentModelLabel({ choice: null })).toBe("Your default model");
		expect(currentModelLabel(null)).toBe("Your default model");
	});
});

describe("stateBannerText / stateBannerType", () => {
	it("legacy", () => {
		expect(stateBannerText({ state: "legacy" })).toBe(
			"Using your default model. Choose one that meets this agent's requirement."
		);
		expect(stateBannerType({ state: "legacy" })).toBe("info");
	});
	it("changed names the model it moved to", () => {
		expect(
			stateBannerText({ state: "changed", choice: { label: "GPT-5", model: "gpt-5" } })
		).toBe("Your chosen model is no longer available, so this agent moved to GPT-5.");
		expect(stateBannerType({ state: "changed" })).toBe("warning");
	});
	it("needs_model reads as paused", () => {
		expect(stateBannerText({ state: "needs_model" })).toBe(
			"This agent is paused: none of your models meet its requirement."
		);
		expect(stateBannerType({ state: "needs_model" })).toBe("warning");
	});
	it("healthy states (auto/chosen) have no state banner", () => {
		expect(stateBannerText({ state: "auto" })).toBe("");
		expect(stateBannerText({ state: "chosen" })).toBe("");
		expect(stateBannerText(null)).toBe("");
	});
});

describe("shouldShowNoteBanner", () => {
	it("fires for a healthy pinned state carrying a note", () => {
		expect(
			shouldShowNoteBanner({
				state: "auto",
				note: "Apply catalog changes to finish updating this agent",
			})
		).toBe(true);
	});
	it("fires when fleet_unresolved is set even without a note string", () => {
		expect(shouldShowNoteBanner({ state: "chosen", fleet_unresolved: 1, note: "" })).toBe(
			true
		);
	});
	it("never fires for legacy/changed/needs_model - they have their own canned copy", () => {
		expect(shouldShowNoteBanner({ state: "legacy", note: "irrelevant" })).toBe(false);
		expect(shouldShowNoteBanner({ state: "changed", note: "irrelevant" })).toBe(false);
		expect(shouldShowNoteBanner({ state: "needs_model", note: "irrelevant" })).toBe(false);
	});
	it("is false when there is nothing to say", () => {
		expect(shouldShowNoteBanner({ state: "auto", note: "" })).toBe(false);
		expect(shouldShowNoteBanner(null)).toBe(false);
	});
});

describe("noteBannerText - canApply-aware, never the raw backend note", () => {
	const CARRYING = {
		state: "auto",
		fleet_unresolved: 1,
		note: "Apply catalog changes to finish updating this agent",
	};

	it("a reviewer/admin (canApply) is told to apply it themselves, never the raw note", () => {
		const text = noteBannerText(CARRYING, true);
		expect(text).toBe(
			"This agent's model setup needs to be applied. Apply catalog changes on the Agents page."
		);
		expect(text).not.toContain("finish updating this agent"); // never echoes the backend string
	});
	it("a plain user (canApply=false) is told to ask an admin, never the raw note", () => {
		const text = noteBannerText(CARRYING, false);
		expect(text).toBe("An admin needs to apply the latest agent changes.");
		expect(text).not.toContain("finish updating this agent");
	});
	it("stays empty when shouldShowNoteBanner is false, regardless of canApply", () => {
		expect(noteBannerText({ state: "legacy", note: "irrelevant" }, true)).toBe("");
		expect(noteBannerText(null, false)).toBe("");
	});
});
