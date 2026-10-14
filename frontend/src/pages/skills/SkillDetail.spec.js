import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// The skill editor's File Box controls: "Use in File Box" + the "File Box creates"
// picker, the server's refusal shown on that field, and read-only for a viewer.
vi.mock("vue-router", () => ({
	useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
	onBeforeRouteLeave: vi.fn(),
}));
vi.mock("frappe-ui", () => ({
	Button: { props: ["label"], template: "<button>{{ label }}</button>" },
	Badge: { props: ["label"], template: "<span>{{ label }}</span>" },
	Avatar: { template: "<i/>" },
	FeatherIcon: { template: "<i/>" },
	Tooltip: { template: "<span><slot/></span>" },
	Dropdown: {
		name: "Dropdown",
		props: ["options"],
		template: '<div class="overflow"><slot/></div>',
	},
	FormControl: {
		props: ["label", "modelValue", "disabled"],
		template:
			'<div class="fc" :data-label="label" :data-disabled="String(!!disabled)">{{ modelValue }}</div>',
	},
	Switch: {
		name: "Switch",
		props: ["modelValue", "label", "description", "disabled"],
		emits: ["update:modelValue"],
		template: `<label class="switch" :data-label="label">
			<input type="checkbox" :checked="modelValue" :disabled="disabled"
				@change="$emit('update:modelValue', $event.target.checked)" />
			<span class="desc">{{ description }}</span></label>`,
	},
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "label"],
		template:
			'<div class="ac" :data-label="label"><i v-for="o in options" :key="o.value" class="opt">{{ o.value }}</i></div>',
	},
	toast: { success: vi.fn(), error: vi.fn() },
	confirmDialog: vi.fn(),
}));
vi.mock("@/components/doc/DocPage.vue", () => ({
	default: {
		template: '<div><slot name="actions" /><slot name="main" /><slot name="aside" /></div>',
	},
}));
vi.mock("@/components/doc/DocSection.vue", () => ({
	default: { props: ["label"], template: '<section :data-label="label"><slot /></section>' },
}));
vi.mock("@/components/Banner.vue", () => ({
	default: {
		props: ["type", "title", "message"],
		template:
			'<div class="banner" :data-type="type"><b>{{ title }}</b><span class="msg">{{ message }}</span></div>',
	},
}));
vi.mock("@/components/doc/DocMetaPanel.vue", () => ({
	default: { template: '<div class="docmeta-panel"/>' },
}));
vi.mock("@/components/doc/CommentsSection.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/composables/useDocmeta", () => ({ useDocmeta: vi.fn(() => ({ reload: vi.fn() })) }));
vi.mock("@/composables/useShortcuts", () => ({ useShortcuts: vi.fn() }));
vi.mock("./SyncPill.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("./ShareDialog.vue", () => ({
	default: { name: "ShareDialog", props: ["modelValue", "skill"], template: "<i/>" },
}));
vi.mock("@/components/skills/PromotionRequestDialog.vue", () => ({
	default: { template: "<i/>" },
}));
vi.mock("@/components/skills/PromotionStatusChip.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("@/api/personalise", () => ({ getSkillsAreaCaps: vi.fn(async () => ({})) }));
vi.mock("@/api/skills", () => ({
	requestSkillPromotion: vi.fn(),
	mySkillPromotion: vi.fn(async () => ({})),
	promotableTargetRoles: vi.fn(async () => ({ roles: ["Sales User", "Stock User"] })),
}));
vi.mock("@/markdown", () => ({ renderMarkdown: () => "" }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "now" }));
vi.mock("@/theme", async () => {
	const { ref } = await import("vue");
	return { useJarvisTheme: () => ({ effectiveDark: ref(false), paletteVars: {} }) };
});
vi.mock("@/api", () => ({
	getCustomSkill: vi.fn(),
	createCustomSkill: vi.fn(),
	updateCustomSkill: vi.fn(async () => ({ ok: true })),
	fileBoxDoctypes: vi.fn(async () => ["Purchase Invoice", "Sales Invoice"]),
	getSkillShares: vi.fn(async () => ({ users: [] })),
	deleteCustomSkill: vi.fn(),
	applyCustomSkills: vi.fn(),
}));

import * as api from "@/api";
import { getSkillsAreaCaps } from "@/api/personalise";
import { mySkillPromotion, promotableTargetRoles } from "@/api/skills";
import { useDocmeta } from "@/composables/useDocmeta";
import { toast } from "frappe-ui";
import SkillDetail from "./SkillDetail.vue";

const SAVED = {
	name: "SK-1",
	skill_name: "bills",
	description: "handles supplier bills",
	instructions: "do it",
	user_invocable: 1,
	enabled: 1,
	can_edit: 1,
	use_in_file_box: 0,
	file_box_creates: "Purchase Invoice",
};
const CREATES_REFUSED =
	"File Box creates must be a submittable document type File Box may draft (e.g. Purchase Invoice).";

async function mountSkill(props, skill = SAVED) {
	api.getCustomSkill.mockResolvedValue(skill);
	const w = mount(SkillDetail, { props });
	await flushPromises();
	return w;
}
const fileBoxSwitch = (w) =>
	w.findAllComponents({ name: "Switch" }).find((s) => s.props("label") === "Use in File Box");
const picker = (w) => w.findComponent({ name: "Autocomplete" });
const save = (w) =>
	w
		.findAll("button")
		.find((b) => b.text() === "Save")
		.trigger("click");

describe("SkillDetail: File Box", () => {
	let w;
	beforeEach(() => vi.clearAllMocks());
	afterEach(() => {
		w && w.unmount();
		vi.useRealTimers();
	});

	it("a new skill is used in File Box by default, with any document type", async () => {
		w = await mountSkill({ isNew: true });
		const sw = fileBoxSwitch(w);
		expect(sw.props("modelValue")).toBe(true);
		expect(sw.props("description")).toBe(
			"Let File Box use this skill automatically for matching documents"
		);
		expect(picker(w).props("label")).toBe("File Box creates");
		expect(picker(w).props("modelValue")).toBe("");
		expect(w.findAll(".ac .opt").map((o) => o.text())).toEqual([
			"",
			"Purchase Invoice",
			"Sales Invoice",
		]);
	});

	it("loads both fields and saves only what changed", async () => {
		w = await mountSkill({ id: "SK-1" });
		expect(fileBoxSwitch(w).props("modelValue")).toBe(false);
		expect(picker(w).props("modelValue")).toBe("Purchase Invoice");
		await fileBoxSwitch(w).find("input").setValue(true);
		picker(w).vm.$emit("update:modelValue", {
			label: "Sales Invoice",
			value: "Sales Invoice",
		});
		await flushPromises();
		await save(w);
		await flushPromises();
		expect(api.updateCustomSkill).toHaveBeenCalledWith({
			name: "SK-1",
			use_in_file_box: 1,
			file_box_creates: "Sales Invoice",
		});
	});

	it("clearing the picker saves an empty type", async () => {
		w = await mountSkill({ id: "SK-1" });
		picker(w).vm.$emit("update:modelValue", null);
		await flushPromises();
		await save(w);
		await flushPromises();
		expect(api.updateCustomSkill).toHaveBeenCalledWith({ name: "SK-1", file_box_creates: "" });
	});

	it("shows the server's File Box creates refusal on the field, not as a toast", async () => {
		const refusal = Object.assign(new Error(CREATES_REFUSED), {
			exc_type: "FileBoxCreatesError",
			messages: [CREATES_REFUSED],
		});
		api.updateCustomSkill.mockRejectedValueOnce(refusal);
		w = await mountSkill({ id: "SK-1" });
		picker(w).vm.$emit("update:modelValue", { value: "Sales Invoice" });
		await flushPromises();
		await save(w);
		await flushPromises();
		expect(w.find("[role=alert]").text()).toBe(CREATES_REFUSED);
		expect(toast.error).not.toHaveBeenCalled();
		picker(w).vm.$emit("update:modelValue", { value: "Purchase Invoice" });
		await flushPromises();
		expect(w.find("[role=alert]").exists()).toBe(false); // a new pick clears it
	});

	it("keeps any other save error a toast", async () => {
		api.updateCustomSkill.mockRejectedValueOnce(
			Object.assign(new Error("Only a reviewer can change the content"), {
				exc_type: "PermissionError",
			})
		);
		w = await mountSkill({ id: "SK-1" });
		await fileBoxSwitch(w).find("input").setValue(true);
		await save(w);
		await flushPromises();
		expect(toast.error).toHaveBeenCalledTimes(1);
		expect(w.find("[role=alert]").exists()).toBe(false);
	});

	it("searches the types File Box may draft as you type", async () => {
		vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
		w = await mountSkill({ id: "SK-1" });
		api.fileBoxDoctypes.mockClear();
		picker(w).vm.$emit("update:query", "Sal");
		picker(w).vm.$emit("update:query", "Sale");
		await vi.advanceTimersByTimeAsync(300);
		expect(api.fileBoxDoctypes).toHaveBeenCalledTimes(1);
		expect(api.fileBoxDoctypes).toHaveBeenCalledWith("Sale");
	});

	it("disables the picker while saving", async () => {
		let done;
		api.updateCustomSkill.mockImplementationOnce(() => new Promise((r) => (done = r)));
		w = await mountSkill({ id: "SK-1" });
		const box = w.find("fieldset");
		expect(box.attributes("disabled")).toBeUndefined();
		picker(w).vm.$emit("update:modelValue", { value: "Sales Invoice" });
		await flushPromises();
		await save(w);
		expect(box.attributes("disabled")).toBeDefined();
		picker(w).vm.$emit("update:modelValue", { value: "Purchase Invoice" });
		await flushPromises();
		expect(picker(w).props("modelValue")).toBe("Sales Invoice"); // no pick mid-save
		done({ ok: true });
		await flushPromises();
		expect(box.attributes("disabled")).toBeUndefined();
	});

	it("keeps the latest search's types when an earlier one answers late", async () => {
		vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
		w = await mountSkill({ id: "SK-1" });
		let late;
		api.fileBoxDoctypes
			.mockImplementationOnce(() => new Promise((r) => (late = r)))
			.mockResolvedValueOnce(["Sales Invoice"]);
		picker(w).vm.$emit("update:query", "S");
		await vi.advanceTimersByTimeAsync(300);
		picker(w).vm.$emit("update:query", "Sal");
		await vi.advanceTimersByTimeAsync(300);
		late(["Stock Entry"]);
		await flushPromises();
		expect(w.findAll(".ac .opt").map((o) => o.text())).toEqual([
			"",
			"Purchase Invoice",
			"Sales Invoice",
		]);
	});

	it("describes the picker by its hint, then by its error", async () => {
		api.updateCustomSkill.mockRejectedValueOnce(
			Object.assign(new Error(CREATES_REFUSED), { exc_type: "FileBoxCreatesError" })
		);
		w = await mountSkill({ id: "SK-1" });
		const described = () => w.find(`#${w.find("fieldset").attributes("aria-describedby")}`);
		expect(described().text()).toContain("The document type File Box drafts");
		picker(w).vm.$emit("update:modelValue", { value: "Sales Invoice" });
		await flushPromises();
		await save(w);
		await flushPromises();
		expect(described().attributes("role")).toBe("alert");
		expect(described().text()).toBe(CREATES_REFUSED);
	});

	it("tells a non-reviewer who can turn a shared skill back on", async () => {
		const shared = { ...SAVED, scope: "Org" };
		w = await mountSkill({ id: "SK-1" }, { ...shared, use_in_file_box: 1 });
		expect(fileBoxSwitch(w).props("disabled")).toBe(false); // opting out is free
		expect(fileBoxSwitch(w).props("description")).toContain(
			"only a skill reviewer can turn it back on"
		);
		w.unmount();
		w = await mountSkill({ id: "SK-1" }, shared); // saved off
		expect(fileBoxSwitch(w).props("disabled")).toBe(true);
		expect(fileBoxSwitch(w).props("description")).toContain(
			"Only a skill reviewer can turn it on"
		);
		w.unmount();
		getSkillsAreaCaps.mockResolvedValueOnce({ review: 1 });
		w = await mountSkill({ id: "SK-1" }, shared); // a reviewer may
		expect(fileBoxSwitch(w).props("disabled")).toBe(false);
		expect(fileBoxSwitch(w).props("description")).toBe(
			"Let File Box use this skill automatically for matching documents"
		);
		w.unmount();
		w = await mountSkill({ id: "SK-1" }); // a private skill: no hint
		expect(fileBoxSwitch(w).props("disabled")).toBe(false);
	});

	it("locks Enabled for a non-reviewer once a shared skill is saved off", async () => {
		const enabledSwitch = (w) =>
			w.findAllComponents({ name: "Switch" }).find((s) => s.props("label") === "Enabled");
		const shared = { ...SAVED, scope: "Org" };
		w = await mountSkill({ id: "SK-1" }, shared); // saved on: switching off is free
		expect(enabledSwitch(w).props("disabled")).toBe(false);
		expect(enabledSwitch(w).props("description")).toContain(
			"Once off, only a skill reviewer can turn a shared skill back on"
		);
		w.unmount();
		w = await mountSkill({ id: "SK-1" }, { ...shared, enabled: 0 });
		expect(enabledSwitch(w).props("disabled")).toBe(true);
		expect(enabledSwitch(w).props("description")).toContain(
			"Only a skill reviewer can turn a shared skill back on"
		);
		w.unmount();
		getSkillsAreaCaps.mockResolvedValueOnce({ review: 1 });
		w = await mountSkill({ id: "SK-1" }, { ...shared, enabled: 0 }); // a reviewer may
		expect(enabledSwitch(w).props("disabled")).toBe(false);
		w.unmount();
		w = await mountSkill({ id: "SK-1" }, { ...SAVED, enabled: 0 }); // a private skill
		expect(enabledSwitch(w).props("disabled")).toBe(false);
		expect(enabledSwitch(w).props("description")).toBe(
			"Off = saved as a draft, not used by the assistant."
		);
	});

	it("locks Enabled right after a non-reviewer saves a shared skill off", async () => {
		const enabledSwitch = (w) =>
			w.findAllComponents({ name: "Switch" }).find((s) => s.props("label") === "Enabled");
		w = await mountSkill({ id: "SK-1" }, { ...SAVED, scope: "Role" });
		expect(enabledSwitch(w).props("disabled")).toBe(false);
		await enabledSwitch(w).find("input").setValue(false);
		expect(enabledSwitch(w).props("disabled")).toBe(false); // not saved yet: still theirs to undo
		await save(w);
		await flushPromises();
		expect(api.updateCustomSkill).toHaveBeenCalledWith({ name: "SK-1", enabled: 0 });
		expect(enabledSwitch(w).props("disabled")).toBe(true);
	});

	it("is read-only for a viewer who can't edit the skill", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...SAVED, can_edit: 0, use_in_file_box: 1 });
		expect(fileBoxSwitch(w).props("disabled")).toBe(true);
		expect(picker(w).exists()).toBe(false);
		const shown = w.find('.fc[data-label="File Box creates"]');
		expect(shown.text()).toBe("Purchase Invoice");
		expect(shown.attributes("data-disabled")).toBe("true");
		expect(api.fileBoxDoctypes).not.toHaveBeenCalled();
	});
});

// An administrator's hold on the owner's own skill: said first, the switches locked
// with the reason in text, and nothing the form sends can switch it back on.
describe("SkillDetail: an administrator's hold", () => {
	let w;
	beforeEach(() => vi.clearAllMocks());
	afterEach(() => w && w.unmount());

	const HELD = { ...SAVED, enabled: 0, admin_hold: 1, admin_hold_reason: "Emails customers" };
	const switchOf = (w, label) =>
		w.findAllComponents({ name: "Switch" }).find((s) => s.props("label") === label);

	it("says it is on hold, with the reason, above the form", async () => {
		w = await mountSkill({ id: "SK-1" }, HELD);
		const banner = w.find(".banner");
		expect(banner.attributes("data-type")).toBe("warning");
		expect(banner.text()).toContain("On hold");
		expect(banner.find(".msg").text()).toBe(
			"On hold by an administrator: Emails customers. The assistant does not use this skill until an admin releases it."
		);
	});

	it("does not double a full stop the reason already has", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...HELD, admin_hold_reason: "Paused." });
		expect(w.find(".banner .msg").text()).toContain("administrator: Paused. The assistant");
		expect(w.find(".banner .msg").text()).not.toContain("..");
	});

	it("locks Enabled off, with the reason as its text", async () => {
		w = await mountSkill({ id: "SK-1" }, HELD);
		expect(switchOf(w, "Enabled").props("disabled")).toBe(true);
		expect(switchOf(w, "Enabled").props("modelValue")).toBe(false);
		expect(switchOf(w, "Enabled").props("description")).toBe(
			"On hold by an administrator: Emails customers"
		);
	});

	it("locks Allow Approve & run too, with the same reason", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...HELD, can_arm: 1 });
		expect(switchOf(w, "Allow Approve & run").props("disabled")).toBe(true);
		expect(switchOf(w, "Allow Approve & run").props("description")).toBe(
			"On hold by an administrator: Emails customers"
		);
	});

	it("says it is on hold even when no reason was given", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...HELD, admin_hold_reason: "" });
		expect(switchOf(w, "Enabled").props("description")).toBe("On hold by an administrator");
		expect(w.find(".banner .msg").text()).toContain(
			"On hold by an administrator. The assistant"
		);
	});

	it("shows nothing of it on a skill that is not held, or from a server that sends no hold", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...SAVED, admin_hold: 0, admin_hold_reason: "" });
		expect(w.find(".banner").exists()).toBe(false);
		expect(switchOf(w, "Enabled").props("disabled")).toBe(false);
		w.unmount();
		w = await mountSkill({ id: "SK-1" });
		expect(w.find(".banner").exists()).toBe(false);
	});

	it("is released when a later load shows no hold", async () => {
		w = await mountSkill({ id: "SK-1" }, HELD);
		api.getCustomSkill.mockResolvedValue({ ...SAVED, enabled: 0 });
		await w.setProps({ id: "SK-2" });
		await flushPromises();
		expect(w.find(".banner").exists()).toBe(false);
		expect(switchOf(w, "Enabled").props("disabled")).toBe(false);
	});
});

// A skill reviewer on a shared skill someone else owns (5c): editing follows can_edit,
// the owner's affordances follow is_owner, and the form says whose skill it is.
describe("SkillDetail: a skill reviewer editing someone else's shared skill", () => {
	let w;
	beforeEach(() => {
		vi.clearAllMocks();
		getSkillsAreaCaps.mockResolvedValue({ review: 1 });
	});
	afterEach(() => {
		w && w.unmount();
		getSkillsAreaCaps.mockResolvedValue({});
	});

	const SHARED = {
		...SAVED,
		scope: "Role",
		target_role: "Sales User",
		allowed_roles: ["Sales User"],
		can_edit: true,
		is_owner: false,
		mine: 0,
		shared_by: "Administrator",
		owner_name: "Asha Rao",
		allow_approve_run: 1,
		can_arm: 1,
	};
	const switchOf = (w, label) =>
		w.findAllComponents({ name: "Switch" }).find((s) => s.props("label") === label);
	const field = (w, label) => w.find(`.fc[data-label="${label}"]`);
	const banner = (w) => w.find('[data-testid="reviewer-banner"]');

	it("says whose skill it is and who the change reaches", async () => {
		w = await mountSkill({ id: "SK-1" }, SHARED);
		expect(banner(w).attributes("data-type")).toBe("info");
		expect(banner(w).find(".msg").text()).toBe(
			"You are editing Asha Rao's skill. Changes apply to everyone it is shared with."
		);
	});

	it("shows no banner to the owner, to a reader, or on an older server's payload", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...SHARED, is_owner: true, owner_name: "" });
		expect(banner(w).exists()).toBe(false);
		w.unmount();
		w = await mountSkill({ id: "SK-1" }, { ...SHARED, can_edit: false });
		expect(banner(w).exists()).toBe(false);
		w.unmount();
		// No is_owner: before 5c, can_edit meant the owner.
		const older = { ...SHARED };
		delete older.is_owner;
		w = await mountSkill({ id: "SK-1" }, older);
		expect(banner(w).exists()).toBe(false);
	});

	it("puts the hold first when the skill is also on hold", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...SHARED, enabled: 0, admin_hold: 1 });
		const banners = w.findAll(".banner");
		expect(banners.map((b) => b.attributes("data-testid"))).toEqual([
			"hold-banner",
			"reviewer-banner",
		]);
		expect(switchOf(w, "Enabled").props("disabled")).toBe(true);
	});

	it("keeps the name and Approve & run read-only, and the text editable", async () => {
		w = await mountSkill({ id: "SK-1" }, SHARED);
		expect(field(w, "Name").attributes("data-disabled")).toBe("true");
		expect(field(w, "Description").attributes("data-disabled")).toBe("false");
		const arm = switchOf(w, "Allow Approve & run");
		expect(arm.props("disabled")).toBe(true);
		expect(arm.props("description")).toBe(
			"A skill reviewer cannot change this on someone else's skill."
		);
		expect(switchOf(w, "Enabled").props("disabled")).toBe(false);
		expect(switchOf(w, "User invocable").props("disabled")).toBe(false);
	});

	it("hides the owner's promote and delete, and never asks for them", async () => {
		w = await mountSkill({ id: "SK-1" }, SHARED);
		expect(w.findComponent({ name: "Dropdown" }).exists()).toBe(false);
		expect(w.find(".docmeta-panel").exists()).toBe(false);
		expect(w.find(".jv-skill-reviewer-card").text()).toContain("Asha Rao");
		expect(useDocmeta).not.toHaveBeenCalled();
		expect(mySkillPromotion).not.toHaveBeenCalled();
		// Still the editor: Save is there.
		expect(w.findAll("button").some((b) => b.text() === "Save")).toBe(true);
	});

	it("lets the reviewer see and manage who it is shared with", async () => {
		api.getSkillShares.mockResolvedValueOnce({
			users: [{ name: "ben@example.test", full_name: "Ben Rao" }],
		});
		w = await mountSkill({ id: "SK-1" }, SHARED);
		expect(api.getSkillShares).toHaveBeenCalledWith("SK-1");
		const block = w.find(".jv-skill-reviewer-card .jv-skill-shares");
		expect(block.text()).toContain("Shared with");
		expect(block.text()).not.toContain("Not shared");
		const dialog = w.findComponent({ name: "ShareDialog" });
		expect(dialog.props("modelValue")).toBe(false);
		await block
			.findAll("button")
			.find((b) => b.text() === "Manage")
			.trigger("click");
		expect(dialog.props("modelValue")).toBe(true);
		expect(dialog.props("skill")).toEqual({ name: "SK-1", skill_name: "bills" });
	});

	it("shows a reader no shares and does not ask for them", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...SHARED, can_edit: false });
		expect(w.find(".jv-skill-shares").exists()).toBe(false);
		expect(api.getSkillShares).not.toHaveBeenCalled();
	});

	it("gives the owner the overflow, the panel and the shares", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...SHARED, is_owner: true });
		expect(w.findComponent({ name: "Dropdown" }).exists()).toBe(true);
		expect(w.find(".docmeta-panel").exists()).toBe(true);
		expect(useDocmeta).toHaveBeenCalled();
		expect(api.getSkillShares).toHaveBeenCalledWith("SK-1");
		expect(w.find(".jv-skill-reviewer-card").exists()).toBe(false);
	});

	it("edits a Role skill's roles and saves only them", async () => {
		w = await mountSkill({ id: "SK-1" }, SHARED);
		await flushPromises();
		expect(promotableTargetRoles).toHaveBeenCalled();
		const roles = () => w.findAll(".jv-skill-role");
		expect(roles().map((b) => b.text())).toEqual(["Sales User", "Stock User"]);
		await roles()[1].trigger("click");
		await save(w);
		await flushPromises();
		expect(api.updateCustomSkill).toHaveBeenCalledWith({
			name: "SK-1",
			allowed_roles: ["Sales User", "Stock User"],
		});
	});

	it("refuses to save a Role skill with no role", async () => {
		w = await mountSkill({ id: "SK-1" }, SHARED);
		await flushPromises();
		await w.findAll(".jv-skill-role")[0].trigger("click");
		expect(w.find("[role=alert]").text()).toBe("Keep at least one role.");
		await save(w);
		await flushPromises();
		expect(api.updateCustomSkill).not.toHaveBeenCalled();
		expect(toast.error).toHaveBeenCalledWith("Keep at least one role.");
	});

	it("shows no roles control on a company skill or to a non-reviewer", async () => {
		w = await mountSkill({ id: "SK-1" }, { ...SHARED, scope: "Org", allowed_roles: [] });
		await flushPromises();
		expect(w.find(".jv-skill-role").exists()).toBe(false);
		w.unmount();
		getSkillsAreaCaps.mockResolvedValue({});
		w = await mountSkill({ id: "SK-1" }, { ...SHARED, is_owner: true });
		await flushPromises();
		expect(w.find(".jv-skill-role").exists()).toBe(false);
	});
});
