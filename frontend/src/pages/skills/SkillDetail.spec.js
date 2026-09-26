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
	Tooltip: { template: "<span><slot/></span>" },
	Dropdown: { template: "<div><slot/></div>" },
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
	default: { template: '<div><slot name="actions" /><slot name="main" /></div>' },
}));
vi.mock("@/components/doc/DocSection.vue", () => ({
	default: { props: ["label"], template: '<section :data-label="label"><slot /></section>' },
}));
vi.mock("@/components/doc/DocMetaPanel.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/components/doc/CommentsSection.vue", () => ({ default: { template: "<div/>" } }));
vi.mock("@/composables/useDocmeta", () => ({ useDocmeta: () => ({ reload: vi.fn() }) }));
vi.mock("@/composables/useShortcuts", () => ({ useShortcuts: vi.fn() }));
vi.mock("./SyncPill.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("./ShareDialog.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("@/components/skills/PromotionRequestDialog.vue", () => ({
	default: { template: "<i/>" },
}));
vi.mock("@/components/skills/PromotionStatusChip.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("@/api/personalise", () => ({ getSkillsAreaCaps: vi.fn(async () => ({})) }));
vi.mock("@/api/skills", () => ({
	requestSkillPromotion: vi.fn(),
	mySkillPromotion: vi.fn(async () => ({})),
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

	it("tells a non-reviewer that only a reviewer turns a shared skill back on", async () => {
		const shared = { ...SAVED, scope: "Org" };
		w = await mountSkill({ id: "SK-1" }, { ...shared, use_in_file_box: 1 });
		expect(fileBoxSwitch(w).props("disabled")).toBe(false); // opting out is free
		expect(fileBoxSwitch(w).props("description")).toContain(
			"only a reviewer can turn it back on"
		);
		w.unmount();
		w = await mountSkill({ id: "SK-1" }, shared); // saved off
		expect(fileBoxSwitch(w).props("disabled")).toBe(true);
		expect(fileBoxSwitch(w).props("description")).toContain("Only a reviewer can turn it on");
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
