// Runs the desktop's real handlers for Create / Confirm, Approve & run and Cancel / Discard of a
// pending card, and records everything they do. Not part of the app bundle: chatCardDoors.spec.js
// uses it on the handlers' text in ChatView.vue (the working tree, or any commit through `git show`).
//
// The handlers live inside ChatView.vue's <script setup>, so they are CUT OUT of the view's text
// (four slices, between comments that stay in the view) and evaluated with `new Function` against
// fake I/O: the api, the toast, the conversation reload, the shell store, the view's refs. The real
// lib helpers they call (keepsChatCard, chatRefusalMessage, thrownActionError, withBusyKind...) are
// the real ones: the scope is every export of the libs the view imports from, so a name the old
// text uses and a name the new text uses both resolve, and a name that does not resolve throws.
//
// What is recorded for one run: the ordered list of effects (api calls with the card's busy state
// at that moment, toasts, reloads, every write to the pending list, to a card's `busy`, `busyKind`
// and `error`, to the safety-update state, to `queuedTurn`, `sending`, `approveRunBusyToken`), then
// the end state (cards, list, in-flight tokens...). Compared as JSON, so what JSON cannot hold
// (`undefined`) is a visible marker.
import * as chatCardActions from "./chatCardActions.js";
import * as chatErrorCopy from "./chatErrorCopy.js";
import * as actionSummary from "./actionSummary.js";
import * as toolDraft from "./toolDraft.js";
import * as toolDraftHost from "./toolDraftHost.js";
import { REPLY_ROWS, thrownError } from "./chatCardReplyCases.js";

export const NOW_MS = 1_700_000_000_000;

const UNDEFINED = "$undefined";
const plain = (v) => (v === undefined ? UNDEFINED : JSON.parse(JSON.stringify(v)));

const SAFETY_UPDATE_COPY =
	"This confirmation was cancelled during an agent safety update. Submit the action again if it is still needed.";

// The four slices of the view's text. Each starts and ends at a line the view keeps.
const SLICES = [
	['function captureChatView(token = "") {', "// A normal Vue watcher can batch"],
	["function removePending(token) {", "// Enqueue a parked confirmation, deduped"],
	["const inflightTokens = new Set();", "// Next step offered on a create receipt"],
	["async function createToolDraft(pa) {", "const toolDraftBuilding = ref"],
];

function cut(source, from, to) {
	const a = source.indexOf(from);
	if (a < 0) throw new Error(`not found: ${from}`);
	const b = source.indexOf(to, a + from.length);
	if (b < 0) throw new Error(`not found: ${to}`);
	return source.slice(a, b);
}

// The handlers' code from the text of ChatView.vue.
export function handlersCode(source) {
	return [
		"let chatViewGeneration = 0;",
		...SLICES.map(([from, to]) => cut(source, from, to)),
		'const cancelToolDraft = (pa) => withBusyKind(pa, "cancel", discardPending);',
		'const switchChat = () => { chatViewGeneration++; currentId.value = "c2"; };',
		"return { confirmPending, approveAndRunPending, discardPending, createToolDraft, cancelToolDraft, inflightTokens, switchChat };",
	].join("\n");
}

const DRAFT_CARD = {
	title: "New Timesheet",
	draft: { verb: "create", doctype: "Timesheet", title: "New Timesheet", values: {} },
};

function makeItem(kind, token, { expires, conversation = "c1", busy = false } = {}, rec) {
	const expiresAt =
		expires === "past"
			? NOW_MS / 1000 - 100
			: expires === "future"
			? NOW_MS / 1000 + 1000
			: null;
	const item = {
		conversation,
		token,
		tool: kind === "draft" ? "create_doc" : "submit_doc",
		summary: "",
		preview: { card: kind === "draft" ? DRAFT_CARD : { title: "Submit Sales Invoice" } },
		run_id: null,
		created_at: null,
		expires_at: expiresAt,
		seq: null,
		recent: true,
		pending_card_safe: true,
		busy,
		error: null,
	};
	// every write to the card is an effect
	return new Proxy(item, {
		set(target, key, value) {
			rec(`card ${token}.${String(key)}`, value);
			target[key] = value;
			return true;
		},
	});
}

// The world one handler runs in. `scenario` changes what happens around the call.
function makeWorld({ kind, row, scenario }) {
	const log = [];
	const rec = (name, ...args) => log.push([name, ...args.map(plain)]);

	const card = makeItem(kind, "t1", { expires: row.expires }, rec);
	const sibling = makeItem("ordinary", "t2", {}, rec);
	if (scenario === "the card is already busy at the press") card.busy = true;
	if (scenario === "the card belongs to another chat") card.conversation = "c2";
	log.length = 0; // set-up writes are not effects

	let list = [card, sibling];
	const pendingActions = {
		get value() {
			return list;
		},
		set value(next) {
			list = next;
			rec(
				"pendingActions",
				next.map((x) => x.token)
			);
		},
	};
	const logged = (name, initial) => {
		let held = initial;
		return {
			get value() {
				return held;
			},
			set value(next) {
				held = next;
				rec(name, next instanceof Set ? [...next] : next);
			},
		};
	};
	const safetySettledTokens = logged("safetySettledTokens", new Set());
	const queuedTurn = logged("queuedTurn", null);
	const sending = logged("sending", false);
	const approveRunBusyToken = logged("approveRunBusyToken", null);
	const currentId = { value: "c1" };
	const convStreaming = { value: scenario === "a turn is streaming at the press" };
	const messages = { value: [] };

	let handlers;
	const switchChat = () => handlers.switchChat();
	const called = (name, token, conversation) => {
		rec(name, token, conversation, {
			busy: card.busy,
			busyKind: card.busyKind,
			inflight: handlers.inflightTokens.has(token),
			approveRunBusyToken: approveRunBusyToken.value,
		});
	};
	const answer = async () => {
		await null;
		if (scenario === "the chat is switched during the call") switchChat();
		if (scenario === "the card leaves the list during the call")
			pendingActions.value = list.filter((x) => x.token !== "t1");
		if (row.thrown) throw thrownError(row.thrown);
		return row.reply;
	};
	const api = {
		confirmTool: async (token, conversation) => {
			called("api.confirmTool", token, conversation);
			return answer();
		},
		approveAndRun: async (token, conversation) => {
			called("api.approveAndRun", token, conversation);
			return answer();
		},
		dismissTool: async (token, conversation) => {
			called("api.dismissTool", token, conversation);
			return answer();
		},
	};
	const store = { loadConversations: () => rec("store.loadConversations") };
	const notify = (message, opts) => rec("notify", message, opts);
	const loadConversation = async (id, opts) => {
		rec("loadConversation", id, opts);
		await null;
		if (scenario === "the chat is switched during the reload") switchChat();
		// the reload brings the decided row of the card, as the server would
		messages.value = [
			...messages.value,
			{ role: "tool", tool_call_id: "t1", action_outcome: "confirmed" },
		];
	};
	const enqueueSafetyUpdateNotice = (notice) => rec("enqueueSafetyUpdateNotice", notice);

	const scope = {
		...chatCardActions,
		...chatErrorCopy,
		...actionSummary,
		...toolDraft,
		...toolDraftHost,
		pendingActions,
		safetySettledTokens,
		queuedTurn,
		sending,
		approveRunBusyToken,
		currentId,
		convStreaming,
		messages,
		api,
		store,
		notify,
		loadConversation,
		enqueueSafetyUpdateNotice,
		SAFETY_UPDATE_COPY,
	};
	const end = () => ({
		cards: [card, sibling].map((c) => ({
			token: c.token,
			conversation: c.conversation,
			busy: c.busy,
			busyKind: c.busyKind === undefined ? UNDEFINED : c.busyKind,
			error: plain(c.error),
		})),
		pending: list.map((x) => x.token),
		inflight: [...handlers.inflightTokens],
		safetySettledTokens: [...safetySettledTokens.value],
		queuedTurn: plain(queuedTurn.value),
		sending: sending.value,
		approveRunBusyToken: approveRunBusyToken.value,
		currentId: currentId.value,
		receiptRows: messages.value.length,
	});
	return {
		log,
		card,
		scope,
		end,
		bind: (h) => {
			handlers = h;
		},
	};
}

// One run: the handler of `door` on a card of `kind`, with `row`'s reply, in `scenario`.
export async function runOne(code, { door, kind, row, scenario = "normal" }) {
	const world = makeWorld({ kind, row, scenario });
	const names = Object.keys(world.scope);
	const handlers = new Function(...names, code)(...names.map((n) => world.scope[n]));
	world.bind(handlers);
	const handler =
		door === "confirm"
			? kind === "draft"
				? handlers.createToolDraft
				: handlers.confirmPending
			: door === "approve"
			? handlers.approveAndRunPending
			: kind === "draft"
			? handlers.cancelToolDraft
			: handlers.discardPending;
	const returned = await handler(world.card);
	return { effects: world.log, end: world.end(), returned: plain(returned) };
}

// The scenarios beyond the plain call, each on the replies that exercise its branch.
const SCENARIO_ROWS = {
	confirm: [
		"ok",
		"ok, the continuation turn queued (a draft that continues)",
		"busy (the row is locked)",
		"stale: the record changed after it was proposed",
		"failed after the card was spent (the tool refused)",
		"storage unavailable (confirm_tool)",
		"safety update (a pending action)",
		"thrown: the server said why (417)",
	],
	approve: ["ok", "busy (the row is locked)", "thrown: a plain error"],
	dismiss: ["discarded", "discard refused: busy", "thrown: a plain error"],
};
export const SCENARIOS = [
	"the chat is switched during the call",
	"the chat is switched during the reload",
	"the card leaves the list during the call",
	"a turn is streaming at the press",
	"the card is already busy at the press",
	"the card belongs to another chat",
];

// Every run the golden holds: [key, { door, kind, row, scenario }].
export function allRuns() {
	const runs = [];
	for (const door of ["confirm", "approve", "dismiss"]) {
		for (const kind of ["ordinary", "draft"]) {
			for (const row of REPLY_ROWS.filter((r) => r.doors.includes(door))) {
				runs.push([`normal | ${door} | ${kind} | ${row.id}`, { door, kind, row }]);
			}
			for (const scenario of SCENARIOS) {
				for (const id of SCENARIO_ROWS[door]) {
					const row = REPLY_ROWS.find((r) => r.id === id);
					runs.push([
						`${scenario} | ${door} | ${kind} | ${id}`,
						{ door, kind, row, scenario },
					]);
				}
			}
		}
	}
	return runs;
}

export async function recordAll(source) {
	const code = handlersCode(source);
	const record = {};
	for (const [key, run] of allRuns()) record[key] = plain(await runOne(code, run));
	return record;
}
