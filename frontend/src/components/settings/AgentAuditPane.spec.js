import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

// The audit pane is a read-only, metadata-only feed. These specs pin: rows
// render with humanised tool + outcome Badge + record link, a null target shows
// no link, a bulk row shows its count, the outcome filter refetches, the person
// filter is debounced, and a load failure renders a hand-rolled inline retry
// (NOT SettingsPane's error prop).

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "iconLeft", "icon", "loading", "disabled"],
		emits: ["click"],
		template: `<button class="stub-button" @click="$emit('click')">{{ label }}</button>`,
	},
	FeatherIcon: {
		name: "FeatherIcon",
		props: ["name"],
		template: `<i class="stub-icon" :data-name="name" />`,
	},
	Badge: {
		name: "Badge",
		props: ["theme", "label"],
		template: `<span class="stub-badge" :data-theme="theme">{{ label }}</span>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<span class="stub-err">{{ message }}</span>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["type", "options", "modelValue", "label", "placeholder"],
		emits: ["update:modelValue"],
		template: `
			<select v-if="type === 'select'" class="stub-select" :value="modelValue"
				@change="$emit('update:modelValue', $event.target.value)">
				<option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option>
			</select>
			<input v-else class="stub-input" :value="modelValue"
				@input="$emit('update:modelValue', $event.target.value)" />`,
	},
}));

vi.mock("@/utils/datetime", () => ({ timeAgo: () => "just now" }));

const api = vi.hoisted(() => ({
	adminListAgentWrites: vi.fn(),
	adminAgentWriteSummary: vi.fn(),
}));
vi.mock("@/api", () => api);

import AgentAuditPane from "./AgentAuditPane.vue";

function row(over = {}) {
	return {
		name: "AW-1",
		at: "2026-09-20 09:00:00",
		actor: "a@example.test",
		actor_name: "Alice",
		tool: "create_doc",
		outcome: "applied",
		provenance: "chat",
		ref_doctype: "Sales Invoice",
		ref_name: "SINV-2026-00042",
		bulk_count: 0,
		model: "",
		...over,
	};
}

async function mountWith(rows, { has_more = false } = {}) {
	api.adminListAgentWrites.mockResolvedValue({ ok: true, data: { rows, has_more } });
	api.adminAgentWriteSummary.mockResolvedValue({
		ok: true,
		data: { days: 7, writes: 12, failed: 2, actors: 3 },
	});
	const w = mount(AgentAuditPane);
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
});

describe("AgentAuditPane", () => {
	it("renders a row with actor name, humanised tool, outcome badge and a record link", async () => {
		const w = await mountWith([row()]);
		expect(w.text()).toContain("Alice");
		expect(w.text()).toContain("Create doc"); // snake_case → Sentence case
		const badge = w.find(".stub-badge");
		expect(badge.text()).toBe("Applied");
		expect(badge.attributes("data-theme")).toBe("green");
		const link = w.find("a");
		expect(link.exists()).toBe(true);
		expect(link.attributes("href")).toBe("/app/sales-invoice/SINV-2026-00042");
		expect(link.attributes("target")).toBe("_blank");
	});

	it("shows the summary strip from adminAgentWriteSummary", async () => {
		const w = await mountWith([row()]);
		expect(api.adminAgentWriteSummary).toHaveBeenCalled();
		expect(w.text()).toContain("This week:");
		expect(w.text()).toContain("12");
		expect(w.text()).toContain("2 failed");
		expect(w.text()).toContain("3 people");
	});

	it("bundles the bypass + retention disclosure", async () => {
		const w = await mountWith([row()]);
		expect(w.text()).toContain("records you may not have permission to open");
		expect(w.text()).toContain("Retained 90 days");
	});

	it("a discarded row with no target shows the doctype and its count, no link", async () => {
		const w = await mountWith([
			row({
				name: "AW-2",
				outcome: "discarded",
				ref_name: "",
				bulk_count: 3,
				tool: "create_docs",
			}),
		]);
		expect(w.find("a").exists()).toBe(false);
		expect(w.text()).toContain("×3"); // action column count
		expect(w.text()).toContain("(3)"); // record column count
		const badge = w.find(".stub-badge");
		expect(badge.text()).toBe("Discarded");
		expect(badge.attributes("data-theme")).toBe("gray");
	});

	it("a row with neither doctype nor name shows an em dash", async () => {
		const w = await mountWith([row({ ref_doctype: "", ref_name: "" })]);
		expect(w.find("a").exists()).toBe(false);
		expect(w.text()).toContain("—");
	});

	it("empty result renders the empty state", async () => {
		const w = await mountWith([]);
		expect(w.text()).toContain("No agent activity yet.");
	});

	it("the outcome filter refetches with the chosen outcome", async () => {
		const w = await mountWith([row()]);
		api.adminListAgentWrites.mockClear();
		await w.find(".stub-select").setValue("failed");
		await flushPromises();
		expect(api.adminListAgentWrites).toHaveBeenCalledTimes(1);
		expect(api.adminListAgentWrites.mock.calls[0][0]).toMatchObject({
			outcome: "failed",
			start: 0,
		});
	});

	it("the person filter is debounced — one refetch, not one per keystroke", async () => {
		vi.useFakeTimers();
		api.adminListAgentWrites.mockResolvedValue({
			ok: true,
			data: { rows: [row()], has_more: false },
		});
		api.adminAgentWriteSummary.mockResolvedValue({
			ok: true,
			data: { days: 7, writes: 0, failed: 0, actors: 0 },
		});
		const w = mount(AgentAuditPane);
		await flushPromises();
		api.adminListAgentWrites.mockClear();
		const input = w.find(".stub-input");
		await input.setValue("a");
		await input.setValue("al");
		await input.setValue("ali");
		expect(api.adminListAgentWrites).not.toHaveBeenCalled(); // still debouncing
		vi.advanceTimersByTime(300);
		await flushPromises();
		expect(api.adminListAgentWrites).toHaveBeenCalledTimes(1);
		expect(api.adminListAgentWrites.mock.calls[0][0]).toMatchObject({ actor: "ali" });
		vi.useRealTimers();
	});

	it("load more appends the next page and preserves the first", async () => {
		const w = await mountWith([row({ name: "AW-1" })], { has_more: true });
		api.adminListAgentWrites.mockResolvedValue({
			ok: true,
			data: { rows: [row({ name: "AW-2", ref_name: "SINV-2026-00043" })], has_more: false },
		});
		await w.find("button.stub-button").trigger("click"); // "Load more"
		await flushPromises();
		expect(w.text()).toContain("SINV-2026-00042");
		expect(w.text()).toContain("SINV-2026-00043");
	});

	it("a load failure renders the hand-rolled inline retry (not the pane error prop)", async () => {
		api.adminListAgentWrites.mockResolvedValue({ ok: false, reason: "boom" });
		api.adminAgentWriteSummary.mockResolvedValue({ ok: false });
		const w = mount(AgentAuditPane);
		await flushPromises();
		expect(w.text()).toContain("Could not load the audit trail.");
		expect(w.find(".stub-err").exists()).toBe(false); // NOT SettingsPane's error prop
		// Retry succeeds → table renders.
		api.adminListAgentWrites.mockResolvedValue({
			ok: true,
			data: { rows: [row()], has_more: false },
		});
		await w.find("button.stub-button").trigger("click"); // "Retry"
		await flushPromises();
		expect(w.text()).toContain("Alice");
	});
});
