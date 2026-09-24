// The Approval Board's "Needs your decision" rows: held File Box writes and the
// viewer's own chat cards (list_pending_actions_lane), plus wiki notes for a skill
// reviewer (list_wiki_write_proposals). Each source keeps its own endpoint and
// authz; a non-reviewer's wiki 403 is silent and never asked again. Refetches
// (debounced) on the realtime frames that mean "something is waiting or settled"
// and when the tab becomes visible. All row text is model/user-derived: render it
// as text, never HTML.
import { ref, computed, inject, onMounted, onBeforeUnmount } from "vue";
import { listPendingActionsLane } from "@/api/approvals";
import { listWikiWriteProposals } from "@/api";
import { errMessage } from "@/lib/errors";
import { filesWaiting } from "@/lib/heldActions";
import { isPermissionDenied, proposalHeadline, dropperLabel } from "@/lib/wikiReview";

const REFRESH_KINDS = new Set([
	"action:pending",
	"approval:new",
	"action:confirmed",
	"action:settled",
]);
const REFRESH_DEBOUNCE_MS = 1000;
// the oldest wiki notes lead, so they must be the page fetched; 100 = the clamp max
const WIKI_QUERY = { order: "oldest", page_length: 100 };
const PENDING = { label: "Pending", theme: "orange" };

const words = (...parts) => parts.filter(Boolean).join(" ").toLowerCase();

function laneRow(r) {
	const kind = r.kind === "chat" ? "chat" : "held";
	const running = r.status === "Executing";
	const title = r.summary || (kind === "chat" ? "Action waiting" : "New record");
	return {
		key: kind + ":" + r.name,
		kind,
		name: r.name,
		status: r.status,
		title,
		meta:
			kind === "chat"
				? r.conversation_title || "Chat"
				: filesWaiting(r.waiters_count) +
				  (r.for_user ? " · dropped by " + r.for_user : ""),
		document_type: r.document_type || "",
		created_at: r.created_at || "",
		badge: running
			? { label: kind === "chat" ? "Running…" : "Creating…", theme: "blue" }
			: PENDING,
		search: words(title, r.conversation_title, r.for_user),
		raw: r,
	};
}

function wikiRow(p) {
	const headline = proposalHeadline(p);
	const title = p.title || headline;
	const pv = p.preview || {};
	return {
		key: "wiki:" + p.name,
		kind: "wiki",
		name: p.name,
		status: p.status,
		title,
		meta: headline + " · dropped by " + dropperLabel(p),
		document_type: "",
		created_at: p.creation || "",
		badge: p.needs_retry ? { label: "Needs retry", theme: "red" } : PENDING,
		search: words(title, pv.summary, pv.slug),
		raw: p,
	};
}

// Blank reads "Unclassified", like the server's type facet.
export function actionRowType(row) {
	return ((row && row.document_type) || "").trim() || "Unclassified";
}

// The toolbar's search + type filter over the rows. Wiki notes carry no doctype,
// so a specific type hides them.
export function filterActionRows(rows, { search = "", document_type = "" } = {}) {
	const q = String(search || "")
		.trim()
		.toLowerCase();
	return (rows || []).filter(
		(r) =>
			(!document_type || (r.kind !== "wiki" && actionRowType(r) === document_type)) &&
			(!q || r.search.includes(q))
	);
}

export function useActionRows() {
	const socket = inject("$socket", null);
	const laneRows = ref([]);
	const wikiRows = ref([]);
	const wikiTotal = ref(0);
	const laneError = ref("");
	const wikiError = ref("");
	const loaded = ref(false); // both sources show an answer: the rail may say "empty"
	let wikiDenied = false;
	// Per source: request ids, and the newest one whose answer is on screen. An
	// answer lands unless a newer one already did, so a burst of reloads can't
	// starve the rows and an older answer never overwrites a newer. Hence once any
	// load settles, both sources show a real answer (its own or a newer one).
	let laneReq = 0;
	let laneOnScreen = 0;
	let wikiReq = 0;
	let wikiOnScreen = 0;
	// A row decided here stays gone from any answer asked for before the decision:
	// key -> its source's request id when it was removed.
	const decidedAt = new Map();
	const undecided = (rows, id) => rows.filter((r) => !(decidedAt.get(r.key) >= id));

	// oldest first: what has waited longest leads
	const rows = computed(() =>
		[...laneRows.value, ...wikiRows.value].sort((a, b) =>
			a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0
		)
	);
	const error = computed(() => laneError.value || wikiError.value);
	const wikiShown = computed(() => wikiRows.value.length);

	async function loadLane() {
		const id = ++laneReq;
		try {
			const res = (await listPendingActionsLane()) || {};
			if (id < laneOnScreen) return;
			laneOnScreen = id;
			laneRows.value = undecided((Array.isArray(res.rows) ? res.rows : []).map(laneRow), id);
			laneError.value = "";
		} catch (e) {
			if (id < laneOnScreen) return;
			laneOnScreen = id;
			laneError.value = errMessage(e, "Waiting approvals could not be loaded.");
		}
	}

	async function loadWiki() {
		if (wikiDenied) return;
		const id = ++wikiReq;
		try {
			const res = (await listWikiWriteProposals(WIKI_QUERY)) || {};
			if (id < wikiOnScreen) return;
			wikiOnScreen = id;
			wikiRows.value = undecided((Array.isArray(res.rows) ? res.rows : []).map(wikiRow), id);
			wikiTotal.value = Number(res.total) || wikiRows.value.length;
			wikiError.value = "";
		} catch (e) {
			if (id < wikiOnScreen) return;
			wikiOnScreen = id;
			if (isPermissionDenied(e)) {
				// not a skill reviewer: no wiki rows, and no point asking again (only
				// the latest answer says so: a newer request may already know better)
				wikiDenied = id === wikiReq;
				wikiRows.value = [];
				wikiError.value = "";
			} else {
				wikiError.value = errMessage(e, "Wiki notes could not be loaded.");
			}
		}
	}

	async function load() {
		await Promise.allSettled([loadLane(), loadWiki()]);
		loaded.value = true;
	}

	function remove(key) {
		decidedAt.set(key, key.startsWith("wiki:") ? wikiReq : laneReq);
		laneRows.value = laneRows.value.filter((r) => r.key !== key);
		wikiRows.value = wikiRows.value.filter((r) => r.key !== key);
	}

	let timer = null;
	function onEvent(p) {
		if (!p || !REFRESH_KINDS.has(p.kind) || timer) return;
		timer = setTimeout(() => {
			timer = null;
			load();
		}, REFRESH_DEBOUNCE_MS);
	}
	function onVisibility() {
		if (document.visibilityState === "visible") load();
	}

	onMounted(() => {
		load();
		socket && socket.on && socket.on("jarvis:event", onEvent);
		document.addEventListener("visibilitychange", onVisibility);
	});
	onBeforeUnmount(() => {
		socket && socket.off && socket.off("jarvis:event", onEvent);
		document.removeEventListener("visibilitychange", onVisibility);
		clearTimeout(timer);
	});

	return { rows, loaded, error, wikiShown, wikiTotal, load, remove };
}
