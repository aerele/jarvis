import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("frappe-ui", () => ({
	Button: {
		props: ["label", "disabled", "loading", "theme", "variant"],
		emits: ["click"],
		template:
			'<button :disabled="disabled" :data-theme="theme" @click="$emit(\'click\')">{{ label }}</button>',
	},
	toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() },
}));
vi.mock("@/api", () => ({
	approveWikiWrite: vi.fn(),
	rejectWikiWrite: vi.fn(),
	retryWikiWrite: vi.fn(),
}));
vi.mock("@/components/JvSpinner.vue", () => ({
	default: { props: ["label"], template: '<span role="status">{{ label }}</span>' },
}));

import { toast } from "frappe-ui";
import * as api from "@/api";
import WikiProposalDetail from "./WikiProposalDetail.vue";

const HOSTILE = '<img src=x onerror="window.__pwned=1">';

const proposal = (over = {}, preview = {}) => ({
	name: "AR-1",
	title: "Fake Co",
	status: "Pending",
	can_approve: 1,
	needs_retry: 0,
	apply_reason: null,
	wiki_digest: "d1",
	dropper: "asha@example.com",
	preview: {
		slug: "party-fake-co",
		page_type: "Reference",
		summary: "",
		append_md: "## Terms\n\n**Net 30** from invoice date.",
		...preview,
	},
	...over,
});

const button = (w, label) => w.findAll("button").find((b) => b.text() === label);

let onDecided, onChanged;
function mountWith(p = proposal(), extra = {}) {
	return mount(WikiProposalDetail, {
		props: { name: "AR-1", proposal: p, loaded: true, onDecided, onChanged, ...extra },
	});
}

describe("WikiProposalDetail", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		onDecided = vi.fn();
		onChanged = vi.fn();
	});

	it("renders the proposed note as sanitized markdown, with its exact source a click away", async () => {
		const w = mountWith(proposal({}, { append_md: `**Net 30** ${HOSTILE}` }));
		expect(w.find("strong").text()).toBe("Net 30");
		expect(w.element.querySelectorAll("img, script").length).toBe(0);
		expect(w.find("pre").exists()).toBe(false);
		await button(w, "View source").trigger("click");
		expect(w.find("pre").text()).toBe(`**Net 30** ${HOSTILE}`);
		expect(window.__pwned).toBeUndefined();
		expect(w.text()).toContain("party-fake-co · Reference · dropped by asha@example.com");
	});

	it.each([
		["a plain link", "See [the bank](https://evil.example/x) for terms."],
		[
			"a code span that ends in ]",
			"See [`https://docs.example.com/a]`](https://evil.example/x)",
		],
		["a NUL between text and URL", "[docs]\u0000(https://evil.example/x)"],
	])("opens a note whose render has a link (%s) in source view", (_label, body) => {
		const w = mountWith(proposal({}, { append_md: body }));
		expect(w.find("pre").text()).toContain("https://evil.example/x");
		expect(w.find("a").exists()).toBe(false);
		expect(button(w, "View rendered")).toBeTruthy();
	});

	it("opens a note without a link rendered", () => {
		const w = mountWith(
			proposal({}, { append_md: "## Terms\n\n**Net 30**, see [3] (below)" })
		);
		expect(w.find("pre").exists()).toBe(false);
		expect(w.find("strong").text()).toBe("Net 30");
		expect(button(w, "View source")).toBeTruthy();
	});

	it("shows the preview summary, and the metadata-only state when there is no body", () => {
		const w = mountWith(proposal({}, { append_md: "", summary: "Refresh the page's tags" }));
		expect(w.text()).toContain("Refresh the page's tags");
		expect(w.text()).toContain("(no body — a metadata-only page refresh)");
		expect(button(w, "View source")).toBeFalsy();
	});

	it("asks the reviewer to scroll only for a long note", () => {
		expect(mountWith().text()).not.toContain("scroll the box");
		const long = mountWith(proposal({}, { append_md: "x".repeat(700) }));
		expect(long.text()).toContain("scroll the box");
	});

	it("offers Approve to any reviewer, the dropper included", () => {
		const w = mountWith(proposal({ can_approve: 1 }));
		expect(button(w, "Approve")).toBeTruthy();
		expect(w.text()).not.toContain("a different reviewer");
	});

	it("offers no Approve on a note that is no longer approvable", () => {
		const w = mountWith(proposal({ can_approve: 0 }));
		expect(button(w, "Approve")).toBeFalsy();
		expect(button(w, "Reject")).toBeTruthy();
	});

	it("approves with the digest of the note the reviewer read, then is decided", async () => {
		api.approveWikiWrite.mockResolvedValue({ ok: true, applied: true });
		const w = mountWith();
		await button(w, "Approve").trigger("click");
		await flushPromises();
		expect(api.approveWikiWrite).toHaveBeenCalledWith("AR-1", "d1");
		expect(toast.success).toHaveBeenCalled();
		expect(onDecided).toHaveBeenCalledTimes(1);
		expect(onChanged).not.toHaveBeenCalled();
	});

	it("an approved write that did not land stays for a retry", async () => {
		api.approveWikiWrite.mockResolvedValue({ ok: true, applied: false });
		const w = mountWith();
		await button(w, "Approve").trigger("click");
		await flushPromises();
		expect(toast.warning).toHaveBeenCalledWith(
			"Approved, but the write did not land. Retry to re-drive it."
		);
		expect(onChanged).toHaveBeenCalledTimes(1);
		expect(onDecided).not.toHaveBeenCalled();
	});

	it("an approve that lands as page_full warns without pointing at Retry", async () => {
		// MAJOR-1: page_full can never be fixed by retrying, so the toast must
		// not say "Use Retry" the way an ordinary transient failure does.
		api.approveWikiWrite.mockResolvedValue({ ok: true, applied: false, reason: "page_full" });
		const w = mountWith();
		await button(w, "Approve").trigger("click");
		await flushPromises();
		expect(toast.warning).toHaveBeenCalledWith(
			"The wiki page is full, so nothing was changed. Retry will keep failing until the page has room: record the note on another page, or reject it."
		);
	});

	it("a refused approve asks for a fresh list; it is not a decision", async () => {
		api.approveWikiWrite.mockRejectedValue(
			Object.assign(new Error("This proposal changed since you opened it"), {
				exc_type: "PermissionError",
			})
		);
		const w = mountWith();
		await button(w, "Approve").trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalled();
		expect(onChanged).toHaveBeenCalledTimes(1);
		expect(onDecided).not.toHaveBeenCalled();
	});

	it("a note that changed since it was opened must be reloaded before Approve", async () => {
		const w = mountWith();
		await w.setProps({ proposal: proposal({ wiki_digest: "d2" }, { append_md: "new note" }) });
		expect(w.find('[role="alert"]').text()).toContain("This note changed since you opened it");
		expect(w.text()).toContain("Net 30");
		expect(w.text()).not.toContain("new note");
		expect(button(w, "Approve").attributes("disabled")).toBeDefined();
		await button(w, "Approve").trigger("click");
		expect(api.approveWikiWrite).not.toHaveBeenCalled();
		await button(w, "Reload").trigger("click");
		expect(w.find('[role="alert"]').exists()).toBe(false);
		expect(w.text()).toContain("new note");
		api.approveWikiWrite.mockResolvedValue({ ok: true, applied: true });
		await button(w, "Approve").trigger("click");
		await flushPromises();
		expect(api.approveWikiWrite).toHaveBeenCalledWith("AR-1", "d2");
	});

	it("rejects", async () => {
		api.rejectWikiWrite.mockResolvedValue({ ok: true });
		const w = mountWith();
		await button(w, "Reject").trigger("click");
		await flushPromises();
		expect(api.rejectWikiWrite).toHaveBeenCalledWith("AR-1");
		expect(onDecided).toHaveBeenCalledTimes(1);
	});

	it("offers Retry AND Reject for an approved write that did not land", async () => {
		// MAJOR-1: Retry alone stranded a doomed row forever (page_full and the
		// like never lands) with no way off the board - Reject is now offered
		// alongside it, so a reviewer can say "won't land" instead.
		const w = mountWith(
			proposal({ status: "Approved", can_approve: 0, needs_retry: 1, apply_reason: "busy" })
		);
		expect(button(w, "Approve")).toBeFalsy();
		expect(button(w, "Reject")).toBeTruthy();
		expect(w.text()).toContain("did not land (busy)");
		api.retryWikiWrite.mockResolvedValue({ ok: true, applied: true });
		await button(w, "Retry").trigger("click");
		await flushPromises();
		expect(api.retryWikiWrite).toHaveBeenCalledWith("AR-1");
		expect(onDecided).toHaveBeenCalledTimes(1);
	});

	it("rejects an approved write that never landed, next to Retry", async () => {
		api.rejectWikiWrite.mockResolvedValue({ ok: true, status: "Rejected" });
		const w = mountWith(
			proposal({
				status: "Approved",
				can_approve: 0,
				needs_retry: 1,
				apply_reason: "page_full",
			})
		);
		await button(w, "Reject").trigger("click");
		await flushPromises();
		expect(api.rejectWikiWrite).toHaveBeenCalledWith("AR-1");
		expect(api.retryWikiWrite).not.toHaveBeenCalled();
		expect(onDecided).toHaveBeenCalledTimes(1);
	});

	it("shows human copy for a page_full apply_reason, but still offers Retry", () => {
		const w = mountWith(
			proposal({
				status: "Approved",
				can_approve: 0,
				needs_retry: 1,
				apply_reason: "page_full",
			})
		);
		expect(w.text()).toContain(
			"The wiki page is full, so nothing was changed. Retry will keep failing until the page has room: record the note on another page, or reject it."
		);
		expect(w.text()).not.toContain("did not land (page_full)");
		expect(button(w, "Retry")).toBeTruthy();
		expect(button(w, "Reject")).toBeTruthy();
	});

	it("reports the outcome even after the board switched away mid-call", async () => {
		let resolve;
		api.approveWikiWrite.mockReturnValue(new Promise((r) => (resolve = r)));
		const w = mountWith();
		await button(w, "Approve").trigger("click");
		w.unmount();
		resolve({ ok: true, applied: false });
		await flushPromises();
		expect(onChanged).toHaveBeenCalledTimes(1);
	});

	it("is busy while deciding: a second click does nothing", async () => {
		let resolve;
		api.approveWikiWrite.mockReturnValue(new Promise((r) => (resolve = r)));
		const w = mountWith();
		await button(w, "Approve").trigger("click");
		expect(button(w, "Reject").attributes("disabled")).toBeDefined();
		await button(w, "Reject").trigger("click");
		expect(api.rejectWikiWrite).not.toHaveBeenCalled();
		resolve({ ok: true, applied: true });
		await flushPromises();
	});

	it("waits for the list, then says when the note isn't waiting for this reviewer", async () => {
		const w = mount(WikiProposalDetail, {
			props: { name: "AR-1", proposal: null, loaded: false },
		});
		expect(w.text()).toContain("Loading the wiki note");
		await w.setProps({ loaded: true });
		expect(w.text()).toContain("isn't waiting for your review");
		expect(w.findAll("button")).toHaveLength(0);
	});

	it("opens once the list brings the note", async () => {
		const w = mount(WikiProposalDetail, {
			props: { name: "AR-1", proposal: null, loaded: false },
		});
		await w.setProps({ proposal: proposal(), loaded: true });
		expect(w.text()).toContain("Net 30");
		expect(button(w, "Approve")).toBeTruthy();
	});

	it("a note decided elsewhere stays readable, with no decisions left", async () => {
		const w = mountWith();
		await w.setProps({ proposal: null });
		expect(w.text()).toContain("Net 30");
		expect(w.text()).toContain("no longer waiting for review");
		expect(button(w, "Approve")).toBeFalsy();
		expect(button(w, "Reject")).toBeFalsy();
	});
});
