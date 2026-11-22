import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
	setUserAgentEnabled: vi.fn(),
	startUserAgentChat: vi.fn(),
	runUserAgentNow: vi.fn(),
	retryUserAgentSync: vi.fn(),
	deleteUserAgent: vi.fn(),
}));
vi.mock("@/api/userAgents", () => api);

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => router }));

vi.mock("frappe-ui", () => ({
	toast: { success: vi.fn(), error: vi.fn() },
	confirmDialog: vi.fn(),
	dayjs: () => ({ format: () => "" }),
	dayjsLocal: (d) => ({ fromNow: () => "5 minutes ago", format: () => String(d) }),
	Badge: {
		props: ["label", "theme"],
		template: `<span class="badge" :data-theme="theme">{{ label }}</span>`,
	},
	Button: {
		props: ["label", "disabled", "loading", "tooltip"],
		emits: ["click"],
		template: `<button :disabled="disabled" :data-label="label" :data-tooltip="tooltip" @click="$emit('click')">{{ label }}</button>`,
	},
	Dropdown: {
		props: ["options"],
		template: `<div class="menu"><slot /><button v-for="o in options" :key="o.label" class="option" @click="o.onClick()">{{ o.label }}</button></div>`,
	},
	FeatherIcon: { template: "<i />" },
	ListView: {
		props: ["columns", "rows"],
		template: `<div class="list"><div v-for="row in rows" :key="row.name" class="row" :data-name="row.name"><div v-for="c in columns" :key="c.key" :class="'cell-' + c.key"><slot name="cell" :column="c" :row="row" :item="row[c.key]" /></div></div></div>`,
	},
	ListHeader: { template: "<div><slot /></div>" },
	ListHeaderItem: { template: "<div />" },
	ListRows: { template: "<div><slot /></div>" },
	Switch: {
		props: ["modelValue", "disabled"],
		emits: ["update:modelValue"],
		template: `<input class="toggle" type="checkbox" :checked="modelValue" :disabled="disabled" @change="$emit('update:modelValue', $event.target.checked)" />`,
	},
	Tooltip: {
		props: ["text"],
		template: `<span class="tooltip" :data-text="text"><slot /></span>`,
	},
}));
vi.mock("@/components/list/PlainRows.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./UserAgentDialog.vue", () => ({
	default: {
		name: "UserAgentDialog",
		props: ["modelValue", "agent"],
		emits: ["update:modelValue", "saved"],
		template: `<div v-if="modelValue" class="dialog" :data-agent="agent ? agent.name : ''" />`,
	},
}));

import { toast, confirmDialog } from "frappe-ui";
import MyAgentsTab from "./MyAgentsTab.vue";

const agent = (name, extra = {}) => ({
	name,
	agent_id: "agent-u-" + name,
	agent_name: `Agent ${name}`,
	description: `About ${name}`,
	instructions: "Do it.",
	enabled: true,
	allow_shell: false,
	tools: [],
	sync: { status: "Synced", error: "", message: "", synced_at: "2026-11-22 09:00:00" },
	schedule: {
		enabled: false,
		frequency: "daily",
		weekday: "",
		day_of_month: null,
		time: null,
		prompt: "Go",
		next_run_at: null,
	},
	last_run: { at: null, status: "", error: "", message: "", conversation: null },
	ready: true,
	...extra,
});
const syncOf = (status, message = "") => ({
	status,
	error: message ? "x" : "",
	message,
	synced_at: null,
});

function mountTab(props = {}) {
	return mount(MyAgentsTab, { props: { agents: [], maxAgents: 5, ...props } });
}
const row = (w, name) => w.get(`.row[data-name="${name}"]`);
const btn = (el, label) => el.find(`button[data-label="${label}"]`);

beforeEach(() => vi.clearAllMocks());

describe("MyAgentsTab: empty and loading", () => {
	it("offers one New agent button when there are no agents", async () => {
		const w = mountTab();
		expect(w.text()).toContain("No agents yet");
		const news = w.findAll('button[data-label="New agent"]');
		expect(news).toHaveLength(1);
		await news[0].trigger("click");
		expect(w.get(".dialog").attributes("data-agent")).toBe("");
	});

	it("says it is loading", () => {
		expect(mountTab({ loading: true }).text()).toContain("Loading your agents");
	});

	it("shows why the list did not load, with a way to try again", async () => {
		const w = mountTab({ error: "Jarvis is offline." });
		expect(w.text()).toContain("Jarvis is offline.");
		await btn(w, "Try again").trigger("click");
		expect(w.emitted("reload")).toHaveLength(1);
	});
});

describe("MyAgentsTab: rows", () => {
	it("shows name, description and a badge for each state", () => {
		const w = mountTab({
			agents: [
				agent("a"),
				agent("b", { sync: syncOf("Pending") }),
				agent("c", {
					sync: syncOf(
						"Failed",
						"Your runtime is too old for agents. Ask your admin to update it."
					),
				}),
				agent("d", { enabled: false, ready: false }),
			],
		});
		const badge = (n) => row(w, n).get(".badge");
		expect(badge("a").text()).toBe("Ready");
		expect(badge("b").text()).toBe("Syncing");
		expect(badge("c").text()).toBe("Sync failed");
		expect(badge("d").text()).toBe("Off");
		expect(row(w, "a").text()).toContain("Agent a");
		expect(row(w, "a").text()).toContain("About a");
	});

	it("prints the stored sync sentence in the row", () => {
		const message = "Your runtime is too old for agents. Ask your admin to update it.";
		const w = mountTab({ agents: [agent("c", { sync: syncOf("Failed", message) })] });
		expect(row(w, "c").text()).toContain(message);
	});

	it("summarises the schedule and the last run", () => {
		const w = mountTab({
			agents: [
				agent("a", {
					schedule: {
						...agent("a").schedule,
						enabled: true,
						frequency: "weekly",
						weekday: "Monday",
						time: "09:00:00",
					},
					last_run: {
						at: "2026-11-22 09:00:00",
						status: "Failed",
						error: "usage_limit",
						message: "Your monthly usage limit is reached.",
					},
				}),
				agent("b"),
			],
		});
		expect(row(w, "a").text()).toContain("Weekly on Monday · 9:00 am");
		expect(row(w, "a").text()).toContain("Failed");
		expect(row(w, "a").get(".tooltip").attributes("data-text")).toBe(
			"Your monthly usage limit is reached."
		);
		expect(row(w, "b").get(".cell-schedule").text()).toBe("Off");
		expect(row(w, "b").get(".cell-last_run").text()).toBe("-");
	});

	it("has no cap note until the cap is reached, then disables New agent", () => {
		const few = mountTab({ agents: [agent("a")] });
		expect(few.text()).not.toContain("up to");
		expect(btn(few, "New agent").attributes("disabled")).toBeUndefined();
		const full = mountTab({ agents: [agent("a"), agent("b")], maxAgents: 2 });
		expect(full.text()).toContain("You can have up to 2 agents. Delete one to add another.");
		expect(btn(full, "New agent").attributes("disabled")).toBeDefined();
	});
});

describe("MyAgentsTab: actions", () => {
	it("opens a new chat with the agent", async () => {
		api.startUserAgentChat.mockResolvedValue({
			conversation: "CONV-1",
			agent_id: "x",
			agent_name: "y",
		});
		const w = mountTab({ agents: [agent("a")] });
		await btn(row(w, "a"), "Chat").trigger("click");
		await flushPromises();
		expect(api.startUserAgentChat).toHaveBeenCalledWith("a");
		expect(router.push).toHaveBeenCalledWith("/c/CONV-1");
	});

	it("runs now and opens the conversation", async () => {
		api.runUserAgentNow.mockResolvedValue({ conversation: "CONV-2", run_id: "r" });
		const w = mountTab({ agents: [agent("a")] });
		await btn(row(w, "a"), "Run now").trigger("click");
		await flushPromises();
		expect(api.runUserAgentNow).toHaveBeenCalledWith("a");
		expect(router.push).toHaveBeenCalledWith("/c/CONV-2");
	});

	it("keeps Chat and Run now off until the agent is ready, and says why", () => {
		const w = mountTab({
			agents: [
				agent("a", { ready: false, sync: syncOf("Pending") }),
				agent("b", { enabled: false, ready: false }),
				agent("c", { schedule: { ...agent("c").schedule, prompt: "" } }),
			],
		});
		expect(btn(row(w, "a"), "Chat").attributes("disabled")).toBeDefined();
		expect(btn(row(w, "a"), "Chat").attributes("data-tooltip")).toContain(
			"once the agent has synced"
		);
		expect(btn(row(w, "b"), "Chat").attributes("data-tooltip")).toBe(
			"Turn this agent on to chat with it."
		);
		expect(btn(row(w, "c"), "Chat").attributes("disabled")).toBeUndefined();
		expect(btn(row(w, "c"), "Run now").attributes("disabled")).toBeDefined();
		expect(btn(row(w, "c"), "Run now").attributes("data-tooltip")).toContain("add a prompt");
	});

	it("shows the server's sentence when Chat is refused", async () => {
		api.startUserAgentChat.mockRejectedValue({
			messages: ["This agent is switched off. Turn it on to use it."],
		});
		const w = mountTab({ agents: [agent("a")] });
		await btn(row(w, "a"), "Chat").trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"This agent is switched off. Turn it on to use it."
		);
		expect(router.push).not.toHaveBeenCalled();
	});

	it("turns an agent off and hands the result to the host", async () => {
		const off = agent("a", { enabled: false, ready: false });
		api.setUserAgentEnabled.mockResolvedValue(off);
		const w = mountTab({ agents: [agent("a")] });
		await row(w, "a").get(".toggle").setValue(false);
		await flushPromises();
		expect(api.setUserAgentEnabled).toHaveBeenCalledWith("a", false);
		expect(w.emitted("upsert")[0][0]).toBe(off);
	});

	it("opens the dialog on the agent", async () => {
		const w = mountTab({ agents: [agent("a"), agent("b")] });
		await btn(row(w, "b"), "Edit").trigger("click");
		expect(w.get(".dialog").attributes("data-agent")).toBe("b");
	});

	it("asks before deleting, then deletes", async () => {
		api.deleteUserAgent.mockResolvedValue({ ok: true, name: "a" });
		const w = mountTab({ agents: [agent("a")] });
		await row(w, "a")
			.findAll(".option")
			.find((o) => o.text() === "Delete")
			.trigger("click");
		expect(api.deleteUserAgent).not.toHaveBeenCalled();
		const opts = confirmDialog.mock.calls[0][0];
		expect(opts.title).toBe("Delete Agent a?");
		const hideDialog = vi.fn();
		await opts.onConfirm({ hideDialog });
		expect(api.deleteUserAgent).toHaveBeenCalledWith("a");
		expect(w.emitted("remove")[0]).toEqual(["a"]);
		expect(hideDialog).toHaveBeenCalled();
	});

	it("keeps the agent and says why when a delete fails", async () => {
		api.deleteUserAgent.mockRejectedValue({
			messages: ["Only the owner can delete this agent."],
		});
		const w = mountTab({ agents: [agent("a")] });
		await row(w, "a")
			.findAll(".option")
			.find((o) => o.text() === "Delete")
			.trigger("click");
		const hideDialog = vi.fn();
		await confirmDialog.mock.calls[0][0].onConfirm({ hideDialog });
		expect(toast.error).toHaveBeenCalledWith("Only the owner can delete this agent.");
		expect(w.emitted("remove")).toBeUndefined();
		expect(hideDialog).not.toHaveBeenCalled();
	});

	it("offers Retry sync only for a failed sync", async () => {
		api.retryUserAgentSync.mockResolvedValue(agent("c"));
		const w = mountTab({
			agents: [
				agent("a"),
				agent("c", { sync: syncOf("Failed", "The runtime refused the update.") }),
			],
		});
		const labels = (n) =>
			row(w, n)
				.findAll(".option")
				.map((o) => o.text());
		expect(labels("a")).toEqual(["Delete"]);
		expect(labels("c")).toEqual(["Retry sync", "Delete"]);
		await row(w, "c").findAll(".option")[0].trigger("click");
		await flushPromises();
		expect(api.retryUserAgentSync).toHaveBeenCalledWith("c");
		expect(w.emitted("upsert")).toHaveLength(1);
	});
});
