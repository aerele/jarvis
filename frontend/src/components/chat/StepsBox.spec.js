import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import StepsBox from "./StepsBox.vue";
import { liveBox, foldedHead, stepsFromTexts } from "@/lib/liveTurn.js";

const S1 =
	"I’ll find the invoice contact details, then prepare payment-reminder drafts for your review.";
const S2 =
	"The invoice records don’t contain email addresses, so I’m checking each customer’s linked contacts before I draft the reminders.";
const S3 =
	"I’m locating the linked Contact records so the reminders go only to valid customer email addresses.";

describe("StepsBox, live mode", () => {
	it("renders done, current and upcoming rows with the right classes", () => {
		const view = {
			mode: "live",
			...liveBox({
				steps: stepsFromTexts([S1, S2, S3]),
				toolPhrase: "Checking the Contact structure…",
				elapsed: "34s",
			}),
		};
		const w = mount(StepsBox, { props: { view, open: false } });
		const rows = w.findAll(".jv-steps-row");
		expect(rows).toHaveLength(3);
		expect(rows[0].classes()).toContain("jv-steps-row-done");
		expect(rows[1].classes()).toContain("jv-steps-row-done");
		expect(rows[2].classes()).toContain("jv-steps-row-current");
		expect(rows[0].find(".jv-steps-check").exists()).toBe(true);
		expect(rows[2].find(".jv-steps-dot").exists()).toBe(true);
	});

	it("shows the sub-line only under the current row", () => {
		const view = {
			mode: "live",
			...liveBox({
				steps: stepsFromTexts([S1]),
				toolPhrase: "Checking the Contact structure…",
				elapsed: "34s",
			}),
		};
		const w = mount(StepsBox, { props: { view, open: false } });
		const rows = w.findAll(".jv-steps-row");
		expect(rows).toHaveLength(1);
		expect(rows[0].find(".jv-steps-sub").text()).toBe("Checking the Contact structure… · 34s");
	});

	it("renders an upcoming row with a hollow ring and no sub-line", () => {
		const view = { mode: "live", ...liveBox({ artifactKind: "pdf", waiting: true }) };
		const w = mount(StepsBox, { props: { view, open: false } });
		const upcoming = w.findAll(".jv-steps-row-upcoming");
		expect(upcoming.length).toBeGreaterThan(0);
		expect(upcoming[0].find(".jv-steps-ring").exists()).toBe(true);
		expect(upcoming[0].find(".jv-steps-sub").exists()).toBe(false);
	});

	it("says +1 earlier step and +2 earlier steps", () => {
		const S4 =
			"I’ve confirmed the invoices themselves have no email recipient. I’m checking whether the customers’ linked contacts have usable email addresses.";
		const S5 = "I’m sending the reminder drafts now.";
		const none = { mode: "live", ...liveBox({ steps: stepsFromTexts([S1, S2, S3]) }) };
		expect(
			mount(StepsBox, { props: { view: none } })
				.find(".jv-steps-earlier")
				.exists()
		).toBe(false);
		const one = { mode: "live", ...liveBox({ steps: stepsFromTexts([S1, S2, S3, S4]) }) };
		expect(
			mount(StepsBox, { props: { view: one } })
				.find(".jv-steps-earlier")
				.text()
		).toBe("+1 earlier step");
		const two = { mode: "live", ...liveBox({ steps: stepsFromTexts([S1, S2, S3, S4, S5]) }) };
		expect(
			mount(StepsBox, { props: { view: two } })
				.find(".jv-steps-earlier")
				.text()
		).toBe("+2 earlier steps");
	});

	it("renders the artifact header only when the view provides one", () => {
		const withHeader = {
			mode: "live",
			...liveBox({ artifactKind: "spreadsheet", waiting: true }),
		};
		const withoutHeader = { mode: "live", ...liveBox({ steps: stepsFromTexts([S1]) }) };
		expect(
			mount(StepsBox, { props: { view: withHeader } })
				.find(".jv-steps-header")
				.text()
		).toBe("Exporting your spreadsheet");
		expect(
			mount(StepsBox, { props: { view: withoutHeader } })
				.find(".jv-steps-header")
				.exists()
		).toBe(false);
	});

	it("holds the announce text in the one status region, live mode only", () => {
		const view = { mode: "live", ...liveBox({ steps: stepsFromTexts([S1, S3]) }) };
		const w = mount(StepsBox, { props: { view } });
		const status = w.find('[role="status"]');
		expect(status.attributes("aria-live")).toBe("polite");
		expect(status.text()).toBe(view.announce);
		expect(status.text()).toBe(S3);
	});
});

describe("StepsBox, folded mode", () => {
	it("renders nothing for a null head", () => {
		const w = mount(StepsBox, { props: { view: { mode: "folded", head: null } } });
		expect(w.find(".jv-steps-box").exists()).toBe(false);
		expect(w.html()).toBe("<!--v-if-->");
	});

	it("renders a button with aria-expanded and emits toggle when expandable", async () => {
		const head = foldedHead({ seconds: 48, toolNames: ["get_doc", "get_list", "query"] });
		const w = mount(StepsBox, { props: { view: { mode: "folded", head }, open: false } });
		const btn = w.find("button.jv-steps-head");
		expect(btn.exists()).toBe(true);
		expect(btn.attributes("aria-expanded")).toBe("false");
		expect(w.find(".jv-steps-chev").exists()).toBe(true);
		await btn.trigger("click");
		expect(w.emitted("toggle")).toHaveLength(1);
		expect(w.find(".jv-steps-head-label").text()).toBe("Worked 48s");
		expect(w.find(".jv-steps-head-line").text()).toBe("Worked 48s · 3 lookups");
	});

	it("renders a div with no chevron and no toggle when not expandable", async () => {
		const head = foldedHead({ seconds: 52, toolNames: ["get_doc"], showDetail: false });
		const w = mount(StepsBox, { props: { view: { mode: "folded", head } } });
		expect(w.find("button.jv-steps-head").exists()).toBe(false);
		const div = w.find("div.jv-steps-head");
		expect(div.exists()).toBe(true);
		expect(div.attributes("aria-expanded")).toBeUndefined();
		expect(w.find(".jv-steps-chev").exists()).toBe(false);
		await div.trigger("click");
		expect(w.emitted("toggle")).toBeUndefined();
	});

	it("shows the finishing tail until enrichment lands", () => {
		const head = foldedHead({ seconds: 52, toolNames: ["export_excel"], finishing: true });
		const w = mount(StepsBox, { props: { view: { mode: "folded", head } } });
		expect(w.find(".jv-steps-head-finishing").text()).toBe("finishing");
		expect(w.find(".jv-steps-head-line").text()).toBe("Worked 52s · 1 action · finishing");
	});

	it("never starts the line with a separator when the time is missing", () => {
		// A tab reloaded mid-turn used to show "· 5 lookups" (flow review F3).
		const head = foldedHead({ toolNames: ["get_doc", "query"], finishing: true });
		const w = mount(StepsBox, { props: { view: { mode: "folded", head } } });
		expect(w.find(".jv-steps-head-line").text()).toBe("2 lookups · finishing");
	});

	it("shows the stopped subline on a second line", () => {
		const head = foldedHead({ seconds: 21, toolNames: ["get_doc", "query"], stopped: true });
		const w = mount(StepsBox, { props: { view: { mode: "folded", head } } });
		expect(w.find(".jv-steps-head-subline").text()).toBe("You stopped this reply.");
	});

	it("renders the details slot only when open and expandable", async () => {
		const head = foldedHead({ seconds: 48, toolNames: ["get_doc"] });
		const w = mount(StepsBox, {
			props: { view: { mode: "folded", head }, open: false },
			slots: { details: '<div class="my-detail">tools</div>' },
		});
		expect(w.find(".my-detail").exists()).toBe(false);
		await w.setProps({ open: true });
		expect(w.find(".my-detail").exists()).toBe(true);
	});

	it("never shows the body when the head is not expandable, even if open is true", () => {
		const head = foldedHead({ seconds: 52, toolNames: ["get_doc"], showDetail: false });
		const w = mount(StepsBox, {
			props: { view: { mode: "folded", head }, open: true },
			slots: { details: '<div class="my-detail">tools</div>' },
		});
		expect(w.find(".my-detail").exists()).toBe(false);
	});
});

describe("StepsBox, queued mode", () => {
	it("renders the label and emits cancel from its Cancel button", async () => {
		const w = mount(StepsBox, {
			props: { view: { mode: "queued", label: "Queued · ~1 ahead" } },
		});
		expect(w.find(".jv-steps-queued-label").text()).toBe("Queued · ~1 ahead");
		expect(w.find(".jv-steps-ring").exists()).toBe(true);
		await w.find(".jv-steps-cancel").trigger("click");
		expect(w.emitted("cancel")).toHaveLength(1);
	});
});
