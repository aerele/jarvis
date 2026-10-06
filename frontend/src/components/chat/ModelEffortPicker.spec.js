import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

// The picker only reads preferredPersona from the shell store; the real store pulls in frappe-ui.
vi.mock("@/stores/shell", () => ({ useShellStore: () => ({ preferredPersona: "Jarvis" }) }));
vi.mock("@/components/chat/PersonaOptions.vue", () => ({ default: { render: () => null } }));

import ModelEffortPicker from "./ModelEffortPicker.vue";

const ADMIN_TIP = "Sign-in expired. Reconnect it in AI models.";
const MEMBER_TIP = "Sign-in expired. Ask your workspace admin to reconnect it.";
const MAP = { "gpt-5.6": { upstream: "codex", label: "ChatGPT" } };
const GROUPS = [{ provider: "openai", models: [{ model: "gpt-5.6" }, { model: "claude-x" }] }];

function build(props = {}) {
	return mount(ModelEffortPicker, {
		props: { modelsByProvider: GROUPS, expiredModels: MAP, ...props },
	});
}
const dot = (w) => w.find('[data-testid="mep-expired-dot"]');
const pill = (w) => w.find(".mep-pill");

describe("ModelEffortPicker expired sign-in", () => {
	it("shows the dot and admin tooltip for an expired explicit pick", () => {
		const w = build({ modelOverride: "gpt-5.6", canAddProvider: true });
		expect(dot(w).exists()).toBe(true);
		expect(dot(w).classes()).toEqual(
			expect.arrayContaining(["size-1.5", "shrink-0", "rounded-full", "bg-surface-red-5"])
		);
		expect(pill(w).attributes("title")).toBe(ADMIN_TIP);
	});

	it("shows the member tooltip when the viewer cannot add providers", () => {
		const w = build({ modelOverride: "gpt-5.6", canAddProvider: false });
		expect(pill(w).attributes("title")).toBe(MEMBER_TIP);
	});

	it("matches a prefixed id against a bare map key", () => {
		const w = build({ modelOverride: "openai/gpt-5.6" });
		expect(dot(w).exists()).toBe(true);
	});

	it("shows the dot when the pill names an expired default model", () => {
		const w = build({ modelOverride: "", defaultModel: "gpt-5.6", canAddProvider: true });
		expect(w.find(".mep-model").text()).toBe("gpt-5.6");
		expect(dot(w).exists()).toBe(true);
		expect(pill(w).attributes("title")).toBe(ADMIN_TIP);
	});

	it("shows no dot on Auto", () => {
		const w = build({ modelOverride: "", defaultModel: "", canAddProvider: true });
		expect(w.find(".mep-model").text()).toBe("Auto");
		expect(dot(w).exists()).toBe(false);
		expect(pill(w).attributes("title")).toBe("Model and effort");
	});

	it("shows no dot for a model that is not expired", () => {
		const w = build({ modelOverride: "claude-x" });
		expect(dot(w).exists()).toBe(false);
		expect(pill(w).attributes("title")).toBe("Model and effort");
	});

	it("marks an expired menu row and keeps it clickable", async () => {
		const w = build({ modelOverride: "claude-x" });
		await pill(w).trigger("click");
		const rows = w.findAll(".mep-item").filter((r) => r.text().includes("gpt-5.6"));
		expect(rows).toHaveLength(1);
		expect(rows[0].find(".mep-desc").text()).toBe("Sign-in expired");
		await rows[0].trigger("click");
		expect(w.emitted("select-model")[0]).toEqual(["gpt-5.6"]);
		const other = build({ modelOverride: "claude-x" });
		await pill(other).trigger("click");
		expect(other.text()).toContain("claude-x");
		expect(
			other.findAll(".mep-desc").filter((d) => d.text() === "Sign-in expired")
		).toHaveLength(1);
	});
});

describe("ChatView wiring", () => {
	const HERE = path.dirname(fileURLToPath(import.meta.url));
	const src = fs.readFileSync(path.join(HERE, "..", "..", "views", "ChatView.vue"), "utf8");

	it("passes :expired-models to the model picker", () => {
		expect(src).toContain(':expired-models="expiredModels"');
	});
});
