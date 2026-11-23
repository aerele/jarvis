import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
	listPickableTools: vi.fn(),
	createUserAgent: vi.fn(),
	updateUserAgent: vi.fn(),
	setUserAgentSchedule: vi.fn(),
	retryUserAgentSync: vi.fn(),
}));
vi.mock("@/api/userAgents", () => api);

vi.mock("frappe-ui", () => ({
	toast: { success: vi.fn(), error: vi.fn() },
	Button: {
		props: ["label", "disabled", "loading"],
		emits: ["click"],
		template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
	},
	Dialog: {
		props: ["modelValue", "options"],
		template: `<div v-if="modelValue"><h3>{{ options.title }}</h3><slot name="body-content" /><slot name="actions" /></div>`,
	},
	ErrorMessage: {
		props: ["message"],
		template: `<p v-if="message" class="error">{{ message }}</p>`,
	},
	FormControl: {
		props: ["modelValue", "type", "label", "options", "placeholder"],
		emits: ["update:modelValue"],
		template: `<label class="field" :data-label="label"><textarea v-if="type === 'textarea'" :value="modelValue" :placeholder="placeholder" @input="$emit('update:modelValue', $event.target.value)" /><input v-else :value="modelValue" :placeholder="placeholder" @input="$emit('update:modelValue', $event.target.value)" /></label>`,
	},
	Switch: {
		props: ["modelValue", "label", "disabled"],
		emits: ["update:modelValue"],
		template: `<label class="switch" :data-label="label"><input type="checkbox" :checked="modelValue" :disabled="disabled" @change="$emit('update:modelValue', $event.target.checked)" /></label>`,
	},
	TabButtons: {
		props: ["buttons", "modelValue"],
		emits: ["update:modelValue"],
		template: `<div class="modes"><button v-for="b in buttons" :key="b.value" :data-mode="b.value" :aria-pressed="modelValue === b.value" @click="$emit('update:modelValue', b.value)">{{ b.label }}</button></div>`,
	},
	Badge: { props: ["label"], template: "<span>{{ label }}</span>" },
	FeatherIcon: { template: "<i />" },
	Checkbox: {
		props: ["label", "modelValue", "disabled"],
		emits: ["update:modelValue"],
		template: `<label class="tool"><input type="checkbox" :checked="modelValue" :disabled="disabled" @change="$emit('update:modelValue', $event.target.checked)" />{{ label }}</label>`,
	},
	dayjs: () => ({ format: () => "" }),
}));

import { toast } from "frappe-ui";
import UserAgentDialog from "./UserAgentDialog.vue";

const GROUPS = [
	{
		app: "frappe",
		label: "Records and reports",
		tools: [
			{ id: "get_list", label: "Get list", kind: "read" },
			{ id: "get_doc", label: "Get doc", kind: "read" },
		],
	},
];

const agent = (extra = {}) => ({
	name: "UA-1",
	agent_name: "Overdue invoices",
	description: "",
	instructions: "List overdue invoices.",
	enabled: true,
	allow_shell: false,
	tools: ["get_list"],
	sync: { status: "Synced", error: "", message: "", synced_at: "2026-11-22 09:00:00" },
	schedule: {
		enabled: false,
		frequency: "daily",
		weekday: "",
		day_of_month: null,
		time: null,
		prompt: "",
		next_run_at: null,
	},
	last_run: { at: null, status: "", error: "", message: "", conversation: null },
	ready: true,
	...extra,
});

const field = (w, label) =>
	w.get(`.field[data-label="${label}"] input, .field[data-label="${label}"] textarea`);
const button = (w, label) => w.findAll("button").find((b) => b.text() === label);

async function open(props = {}) {
	const w = mount(UserAgentDialog, { props: { modelValue: true, agent: null, ...props } });
	await flushPromises();
	return w;
}

beforeEach(() => {
	vi.clearAllMocks();
	api.listPickableTools.mockResolvedValue({ max_tools: 64, groups: GROUPS });
});

describe("UserAgentDialog: a new agent", () => {
	it("asks for a name and instructions before calling the server", async () => {
		const w = await open();
		await button(w, "Create agent").trigger("click");
		expect(w.text()).toContain("Give the agent a name.");
		expect(w.text()).toContain("Tell the agent what to do.");
		expect(api.createUserAgent).not.toHaveBeenCalled();
	});

	it("shows the instruction limit only when the text is near it", async () => {
		const w = await open();
		expect(w.text()).not.toContain("12,000");
		await field(w, "Instructions").setValue("x".repeat(11000));
		expect(w.text()).toContain("11,000 of 12,000 characters");
	});

	it("refuses instructions over the limit and says what to do", async () => {
		const w = await open();
		await field(w, "Name").setValue("Stock");
		await field(w, "Instructions").setValue("x".repeat(12001));
		await button(w, "Create agent").trigger("click");
		expect(w.text()).toContain("Shorten the instructions to 12,000 characters or fewer.");
		expect(api.createUserAgent).not.toHaveBeenCalled();
	});

	it("creates with the definition only when the schedule stays off", async () => {
		const created = agent();
		api.createUserAgent.mockResolvedValue(created);
		const w = await open();
		await field(w, "Name").setValue("  Overdue invoices ");
		await field(w, "Instructions").setValue("List overdue invoices.");
		await w.findAll('.tool input[type="checkbox"]')[0].setValue(true);
		await w.get('.switch[data-label="Can run code and work with files"] input').setValue(true);
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(api.createUserAgent).toHaveBeenCalledWith({
			agent_name: "Overdue invoices",
			description: "",
			instructions: "List overdue invoices.",
			tools: ["get_list"],
			allow_shell: true,
		});
		expect(api.setUserAgentSchedule).not.toHaveBeenCalled();
		expect(w.emitted("saved")[0][0]).toBe(created);
		expect(w.emitted("update:modelValue")[0][0]).toBe(false);
		expect(toast.success).toHaveBeenCalledWith("Agent saved");
	});

	it("saves the schedule after the create, with the new agent's name", async () => {
		api.createUserAgent.mockResolvedValue(agent());
		api.setUserAgentSchedule.mockResolvedValue(
			agent({ schedule: { ...agent().schedule, enabled: true, prompt: "Go" } })
		);
		const w = await open();
		await field(w, "Name").setValue("Overdue invoices");
		await field(w, "Instructions").setValue("List overdue invoices.");
		await w.get('.switch[data-label="Run automatically"] input').setValue(true);
		await field(w, "Prompt for each run").setValue("Go");
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(api.setUserAgentSchedule).toHaveBeenCalledWith("UA-1", {
			enabled: true,
			frequency: "daily",
			time: "09:00",
			weekday: "",
			day_of_month: 0,
			prompt: "Go",
		});
		expect(w.emitted("saved")).toHaveLength(2);
	});

	it("needs a prompt when the schedule is on", async () => {
		const w = await open();
		await field(w, "Name").setValue("A");
		await field(w, "Instructions").setValue("B");
		await w.get('.switch[data-label="Run automatically"] input').setValue(true);
		await button(w, "Create agent").trigger("click");
		expect(w.text()).toContain("Write what the agent should do on each run.");
		expect(api.createUserAgent).not.toHaveBeenCalled();
	});

	it("keeps the dialog open with the server's sentence when the create is refused", async () => {
		api.createUserAgent.mockRejectedValue({ messages: ["You can have at most 5 agents."] });
		const w = await open();
		await field(w, "Name").setValue("A");
		await field(w, "Instructions").setValue("B");
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("You can have at most 5 agents.");
		expect(w.emitted("update:modelValue")).toBeUndefined();
	});

	it("does not create a second agent when only the schedule call failed", async () => {
		// The server returns what was sent: no tools were picked.
		api.createUserAgent.mockResolvedValue(agent({ tools: [] }));
		api.setUserAgentSchedule.mockRejectedValueOnce({
			messages: ["Schedule time is invalid."],
		});
		const w = await open();
		await field(w, "Name").setValue("Overdue invoices");
		await field(w, "Instructions").setValue("List overdue invoices.");
		await w.get('.switch[data-label="Run automatically"] input').setValue(true);
		await field(w, "Prompt for each run").setValue("Go");
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(w.text()).toContain("Schedule time is invalid.");
		expect(w.emitted("saved")).toHaveLength(1);
		// The host hands the created agent back: the dialog is now an edit of it.
		await w.setProps({ agent: agent({ tools: [] }) });
		expect(w.text()).toContain("Edit agent");
		api.setUserAgentSchedule.mockResolvedValue(agent());
		await button(w, "Save changes").trigger("click");
		await flushPromises();
		expect(api.createUserAgent).toHaveBeenCalledTimes(1);
		expect(api.updateUserAgent).not.toHaveBeenCalled();
		expect(api.setUserAgentSchedule).toHaveBeenCalledTimes(2);
	});
});

describe("UserAgentDialog: an existing agent", () => {
	it("starts from the agent and calls nothing when nothing changed", async () => {
		const w = await open({ agent: agent() });
		expect(w.text()).toContain("Edit agent");
		expect(field(w, "Name").element.value).toBe("Overdue invoices");
		await button(w, "Save changes").trigger("click");
		await flushPromises();
		expect(api.updateUserAgent).not.toHaveBeenCalled();
		expect(api.setUserAgentSchedule).not.toHaveBeenCalled();
	});

	it("updates the definition without touching the schedule", async () => {
		api.updateUserAgent.mockResolvedValue(agent({ agent_name: "Renamed" }));
		const w = await open({ agent: agent() });
		await field(w, "Name").setValue("Renamed");
		await button(w, "Save changes").trigger("click");
		await flushPromises();
		expect(api.updateUserAgent).toHaveBeenCalledWith("UA-1", {
			agent_name: "Renamed",
			description: "",
			instructions: "List overdue invoices.",
			tools: ["get_list"],
			allow_shell: false,
		});
		expect(api.setUserAgentSchedule).not.toHaveBeenCalled();
	});

	it("saves only the schedule when only the schedule changed", async () => {
		api.setUserAgentSchedule.mockResolvedValue(agent());
		const w = await open({ agent: agent() });
		await field(w, "Prompt for each run").setValue("Go");
		await button(w, "Save changes").trigger("click");
		await flushPromises();
		expect(api.updateUserAgent).not.toHaveBeenCalled();
		expect(api.setUserAgentSchedule).toHaveBeenCalledWith(
			"UA-1",
			expect.objectContaining({ enabled: false, prompt: "Go" })
		);
	});

	it("shows the stored sync sentence and retries the sync", async () => {
		const failed = agent({
			sync: {
				status: "Failed",
				error: "unsupported_image",
				message:
					"Your workspace runtime is too old for agents. Ask your admin to update it.",
				synced_at: null,
			},
		});
		api.retryUserAgentSync.mockResolvedValue(agent());
		const w = await open({ agent: failed });
		expect(w.text()).toContain("Your workspace runtime is too old for agents.");
		await button(w, "Retry sync").trigger("click");
		await flushPromises();
		expect(api.retryUserAgentSync).toHaveBeenCalledWith("UA-1");
		expect(w.emitted("saved")[0][0].sync.status).toBe("Synced");
	});

	it("shows a support sentence with its code", async () => {
		const message =
			"Jarvis could not set up this agent on your workspace. Contact support and share this code: sandbox_users_failed.";
		const w = await open({
			agent: agent({
				sync: {
					status: "Failed",
					error: "sandbox_users_failed",
					message,
					synced_at: null,
				},
			}),
		});
		expect(w.get(".error").text()).toBe(message);
	});

	it("offers no retry when the sync is fine", async () => {
		const w = await open({ agent: agent() });
		expect(button(w, "Retry sync")).toBeUndefined();
	});

	it("says so when the tools list could not load", async () => {
		api.listPickableTools.mockRejectedValue({ messages: ["Custom agents are turned off."] });
		const w = await open({ agent: agent() });
		expect(w.text()).toContain("Custom agents are turned off.");
	});
});

describe("UserAgentDialog: the web browser option", () => {
	const BROWSER = { enabled: true, max: 3, inUse: 1 };
	const toggle = (w) => w.get('.switch[data-label="Can use a web browser"] input');
	const warning = "A web page can trick the agent into sending data it has read to any site.";
	const fill = async (w) => {
		await field(w, "Name").setValue("Researcher");
		await field(w, "Instructions").setValue("Look things up.");
	};

	it("is hidden while the site does not offer a browser", async () => {
		const w = await open();
		expect(w.find('.switch[data-label="Can use a web browser"]').exists()).toBe(false);
		await fill(w);
		api.createUserAgent.mockResolvedValue(agent());
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(api.createUserAgent.mock.calls[0][0]).not.toHaveProperty("allow_browser");
	});

	it("shows the toggle with its help line, and the modes only once it is on", async () => {
		const w = await open({ browser: BROWSER });
		expect(w.text()).toContain(
			"The agent gets its own private browser. Logins stay saved for this agent only."
		);
		expect(w.find(".modes").exists()).toBe(false);
		await toggle(w).setValue(true);
		expect(w.findAll(".modes button").map((b) => b.text())).toEqual([
			"Listed sites only",
			"Any public site",
		]);
		expect(w.get('.modes [data-mode="listed"]').attributes("aria-pressed")).toBe("true");
		expect(field(w, "Sites, one per line").attributes("placeholder")).toBe("example.com");
	});

	it("sends the cleaned site list in listed mode", async () => {
		api.createUserAgent.mockResolvedValue(agent());
		const w = await open({ browser: BROWSER });
		await fill(w);
		await toggle(w).setValue(true);
		await field(w, "Sites, one per line").setValue(
			"  example.com \n\n docs.example.org\r\n   \n"
		);
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(api.createUserAgent).toHaveBeenCalledWith(
			expect.objectContaining({
				allow_browser: true,
				browser_mode: "listed",
				browser_sites: ["example.com", "docs.example.org"],
			})
		);
	});

	it("shows the warning only in public mode, and hides the site list there", async () => {
		const w = await open({ browser: BROWSER });
		await toggle(w).setValue(true);
		expect(w.text()).not.toContain(warning);
		expect(w.find('.field[data-label="Sites, one per line"]').exists()).toBe(true);
		await w.get('.modes [data-mode="public"]').trigger("click");
		expect(w.text()).toContain(warning);
		expect(w.text()).toContain("Use listed sites unless you need this.");
		expect(w.find('.field[data-label="Sites, one per line"]').exists()).toBe(false);
		api.createUserAgent.mockResolvedValue(agent());
		await fill(w);
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(api.createUserAgent).toHaveBeenCalledWith(
			expect.objectContaining({ allow_browser: true, browser_mode: "public" })
		);
	});

	it("counts the sites only near the limit", async () => {
		const w = await open({ browser: BROWSER });
		await toggle(w).setValue(true);
		await field(w, "Sites, one per line").setValue("a.com\nb.com");
		expect(w.text()).not.toContain("of 30 sites");
		const many = Array.from({ length: 26 }, (_, i) => `site${i}.com`).join("\n");
		await field(w, "Sites, one per line").setValue(many);
		expect(w.text()).toContain("26 of 30 sites");
	});

	it("disables the toggle at the cap, with the reason, unless the agent already has a browser", async () => {
		const full = { enabled: true, max: 3, inUse: 3 };
		const w = await open({ browser: full });
		expect(toggle(w).attributes("disabled")).toBeDefined();
		expect(w.text()).toContain("Only 3 agents per workspace can have a browser.");
		const own = agent({ browser: { enabled: true, mode: "listed", sites: ["example.com"] } });
		const w2 = await open({ browser: full, agent: own });
		expect(toggle(w2).attributes("disabled")).toBeUndefined();
		expect(field(w2, "Sites, one per line").element.value).toBe("example.com");
	});

	it("updates the browser fields on an edit, and only when they changed", async () => {
		const own = agent({ browser: { enabled: true, mode: "listed", sites: ["example.com"] } });
		api.updateUserAgent.mockResolvedValue(own);
		const w = await open({ browser: BROWSER, agent: own });
		await button(w, "Save changes").trigger("click");
		await flushPromises();
		expect(api.updateUserAgent).not.toHaveBeenCalled();
		await field(w, "Sites, one per line").setValue("example.com\nexample.org");
		await button(w, "Save changes").trigger("click");
		await flushPromises();
		expect(api.updateUserAgent).toHaveBeenCalledWith(
			"UA-1",
			expect.objectContaining({
				allow_browser: true,
				browser_mode: "listed",
				browser_sites: ["example.com", "example.org"],
			})
		);
	});

	it("shows the server's sentence about a refused site and keeps the dialog open", async () => {
		api.createUserAgent.mockRejectedValue({
			messages: ['"localhost" is not a public site. Remove that line.'],
		});
		const w = await open({ browser: BROWSER });
		await fill(w);
		await toggle(w).setValue(true);
		await field(w, "Sites, one per line").setValue("localhost");
		await button(w, "Create agent").trigger("click");
		await flushPromises();
		expect(w.text()).toContain('"localhost" is not a public site. Remove that line.');
		expect(w.emitted("update:modelValue")).toBeUndefined();
	});
});
