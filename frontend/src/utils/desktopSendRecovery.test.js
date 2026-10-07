// Execute the actual SFC send/resend functions at the API boundary. This avoids
// duplicating their control flow while isolating the desktop view's many services.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createPendingSends, planRejectedSend } from "./voiceSendGlue.js";
import { firstSendPicks } from "../lib/firstSendPicks.js";
const source = readFileSync(
	process.env.DESKTOP_CHAT_SOURCE || new URL("../views/ChatView.vue", import.meta.url),
	"utf8"
);
const body = source.slice(
	source.indexOf("function resendFailed(m) {"),
	source.indexOf("// Proactive (Jarvis-initiated) conversation toast.")
);
const file = { file_url: "/private/files/invoice.pdf", file_name: "invoice.pdf" };
const image = { file_url: "/private/files/photo.png", file_name: "photo.png" };
const copy = (value) => JSON.parse(JSON.stringify(value));
function harness() {
	const s = Object.fromEntries(
		[...body.matchAll(/\b(\w+)\.value\b/g)].map((m) => [m[1], { value: null }])
	);
	const calls = [],
		errors = [];
	const noop = () => {};
	let nextId = 0;
	Object.assign(s, {
		_NEW_CHAT_SCOPE: "NEW",
		newSendRequestId: () => String(++nextId).padStart(32, "0"),
		loadConversation: async (id, options) => {
			s.loaded = id;
			s.loadOptions = options;
		},
		_pendingSends: createPendingSends(),
		_currentScope: () => s.currentId.value || "NEW",
		dismissFeedback: noop,
		notify: noop,
		notifyActionError: (_label, error) => errors.push(error),
		parseCompactCommand: () => null,
		isShowCardRequest: () => false,
		nextTick: async () => {
			s.onTick?.();
		},
		scrollBottom: noop,
		planRejectedSend,
		firstSendPicks,
		discardedTokens: () => [],
		removePending: noop,
		markCardsEarlier: noop,
		clearHold: () => {
			s.holdActive.value = false;
		},
		raiseHold: noop,
		recheckMaintenance: noop,
		sendRejectionCopy: () => ({ message: "Rejected" }),
		agentName: "Jarvis",
		voiceStore: null,
		canConnectModel: true,
		store: { conversations: [{ name: "A" }], loadConversations: noop },
		route: { params: { id: "A" } },
		router: { replace: noop },
		_saveConnectorFocusFor: noop,
		_checkPulseOnce: noop,
		onTypedConfirmResolved: async () => {},
		api: {
			sendMessage: async (...args) => {
				calls.push(copy(args));
				return { ok: false, reason: "busy" };
			},
		},
	});
	for (const [key, value] of Object.entries({
		booting: false,
		holdActive: false,
		micState: "idle",
		voiceBusyCount: 0,
		compacting: false,
		currentId: "A",
		input: "Review invoice",
		pendingFiles: [{ ...file }],
		sending: false,
		waiting: false,
		noAiConnected: false,
		promptHistory: [],
		failedUploads: [],
		mention: {},
		messages: [],
		autoView: { visible: true, locked: false, on: true },
		groundNextTurn: false,
		triggerMode: false,
		connectorFocus: { key: "erp", label: "ERP" },
		visiblePendingActions: [{ token: "approval-1" }],
		modelOverride: "model-a",
		thinkingOverride: "high",
		drafts: {},
		pendingActions: [],
	}))
		s[key].value = value;
	s.busy = {
		get value() {
			return s.sending.value || s.waiting.value;
		},
	};
	const f = new Function(
		...Object.keys(s),
		body + "\nreturn {send,resendFailed,setPrefill: value => {_prefillSendContext=value;}};"
	)(...Object.values(s));
	f.setPrefill({ doctype: "Sales Invoice", name: "INV-1" });
	return { s, f, calls, errors, failed: () => s._pendingSends.peek(s._currentScope())[0] };
}
for (const [label, text, files] of [
	["text and PDF", "Read this invoice", [file]],
	["PDF only", "", [file]],
	["image only", "", [image]],
	["literal attachment marker", "Keep 📎 literal text", [file, image]],
]) {
	test(`${label}: Retry sends the original payload and leaves a newer draft intact`, async () => {
		const h = harness();
		h.s.input.value = text;
		h.s.pendingFiles.value = copy(files);
		h.s.api.sendMessage = async (...args) => {
			h.calls.push(copy(args));
			if (h.calls.length === 1) return { ok: false, reason: "busy" };
			return { ok: false, reason: "busy" };
		};
		await h.f.send();
		assert.ok(h.failed(), "failed file request must remain available");
		const original = h.calls[0];
		h.s.input.value = "Newer draft";
		h.s.pendingFiles.value = [{ file_url: "/private/files/new.pdf", file_name: "new.pdf" }];
		h.s.connectorFocus.value.key = "different";
		h.s.visiblePendingActions.value = [{ token: "different" }];
		h.f.setPrefill({ doctype: "Customer", name: "Other" });
		h.s.autoView.value.on = false;
		await h.f.resendFailed(h.failed());
		assert.equal(h.calls.length, 2);
		assert.deepEqual(h.calls[1].slice(0, 9), original.slice(0, 9));
		assert.notEqual(h.calls[1][9], original[9]);
		assert.equal(h.s.input.value, "Newer draft");
		assert.equal(h.s.pendingFiles.value[0].file_name, "new.pdf");
		assert.ok(h.failed(), "repeated rejection stays retryable");
		assert.deepEqual(h.errors, []);
	});
}
for (const guard of ["booting", "holdActive", "compacting", "noAiConnected", "waiting"]) {
	test(`blocked by ${guard}: retry retains its bubble and files`, async () => {
		const h = harness();
		await h.f.send();
		const bubble = h.failed();
		h.s[guard].value = true;
		await h.f.resendFailed(bubble);
		assert.equal(h.calls.length, 1);
		assert.equal(h.failed(), bubble);
		assert.ok(h.s.messages.value.includes(bubble));
	});
}
test("a failed request cannot be replayed from another conversation", async () => {
	const h = harness();
	await h.f.send();
	const bubble = h.failed();
	h.s.currentId.value = "B";
	h.s.messages.value = [];
	await h.f.resendFailed(bubble);
	assert.equal(h.calls.length, 1);
	assert.equal(h.s._pendingSends.peek("A")[0], bubble);
});
test("navigation during nextTick cannot redirect the POST or lose the origin request", async () => {
	const h = harness();
	h.s.onTick = () => {
		h.s.currentId.value = "B";
		h.s.messages.value = [];
		h.s.input.value = "B draft";
	};
	h.s.api.sendMessage = async (...args) => {
		h.calls.push(copy(args));
		throw new Error("network");
	};
	await h.f.send();
	const bubble = h.s._pendingSends.peek("A")[0];
	assert.ok(bubble);
	assert.equal(bubble.sendRequest.conversation, "A");
	assert.equal(h.calls[0][0], "A");
	assert.equal(h.s.messages.value.length, 0);
	assert.equal(h.s.input.value, "B draft");
});
test("new-chat promotion updates destination without changing files or first-send picks", async () => {
	const h = harness();
	h.s.currentId.value = "";
	await h.f.send();
	h.s._pendingSends.reassign("NEW", "created");
	h.s.currentId.value = "created";
	h.s.modelOverride.value = "other";
	h.s.thinkingOverride.value = "low";
	await h.f.resendFailed(h.failed());
	assert.equal(h.calls[1][0], "created");
	assert.equal(h.calls[1][2], "model-a");
	assert.equal(h.calls[1][8], "high");
	assert.deepEqual(h.calls[1][3], [file]);
});
test("successful retry acknowledges only the original voice token", async () => {
	const h = harness();
	const token = [{ id: "voice-1" }];
	const acks = [];
	h.s.voiceStore = {
		captureSentInPayload: () => token,
		failedIdsForScope: () => [],
		markUnsentOrphans: () => {},
		acknowledge: (t) => acks.push(t),
	};
	// Build a fresh evaluator to bind the voice service, then use the actual functions.
	const f = new Function(...Object.keys(h.s), body + "\nreturn {send,resendFailed};")(
		...Object.values(h.s)
	);
	await f.send();
	const bubble = h.failed();
	h.s.api.sendMessage = async (...args) => {
		h.calls.push(copy(args));
		return { ok: true, conversation_id: "A", message_id: "M", run_id: "R" };
	};
	await f.resendFailed(bubble);
	assert.equal(h.calls[1][6], true);
	assert.deepEqual(acks, [token]);
	assert.equal(h.s._pendingSends.peek("A").length, 0);
	assert.deepEqual(h.errors, []);
});

test("a second Retry while its POST is pending cannot send again", async () => {
	const h = harness();
	await h.f.send();
	const bubble = h.failed();
	let finish;
	h.s.api.sendMessage = async (...args) => {
		h.calls.push(copy(args));
		return new Promise((resolve) => {
			finish = resolve;
		});
	};
	const retry = h.f.resendFailed(bubble);
	await Promise.resolve();
	await h.f.resendFailed(bubble);
	assert.equal(h.calls.length, 2);
	finish({ ok: false, reason: "busy" });
	await retry;
	assert.ok(h.failed());
});

test("successful retry leaves a newer one-shot context for the next composer send", async () => {
	const h = harness();
	await h.f.send();
	h.f.setPrefill({ doctype: "Customer", name: "NEW" });
	h.s.api.sendMessage = async (...args) => {
		h.calls.push(copy(args));
		return { ok: true, conversation_id: "A", message_id: "M", run_id: "R" };
	};
	await h.f.resendFailed(h.failed());
	h.s.sending.value = false;
	h.s.waiting.value = false;
	h.s.input.value = "New question";
	await h.f.send();
	assert.equal(h.calls[1][4].doctype, "Sales Invoice");
	assert.equal(h.calls[2][4].doctype, "Customer");
	assert.equal(h.calls[2][4].name, "NEW");
	assert.deepEqual(h.errors, []);
});

test("successful retry consumes its original unchanged one-shot context", async () => {
	const h = harness();
	await h.f.send();
	h.s.api.sendMessage = async (...args) => {
		h.calls.push(copy(args));
		return { ok: true, conversation_id: "A", message_id: "M", run_id: "R" };
	};
	await h.f.resendFailed(h.failed());
	h.s.sending.value = false;
	h.s.waiting.value = false;
	h.s.input.value = "Next question";
	await h.f.send();
	assert.equal(h.calls[1][4].doctype, "Sales Invoice");
	assert.equal(h.calls[2][4].doctype, undefined);
	assert.deepEqual(h.errors, []);
});

for (const scenario of ["original document", "newer document", "Wiki selection"]) {
	test(`accepted send after navigation preserves only the next chat's context: ${scenario}`, async () => {
		const h = harness();
		if (scenario === "Wiki selection") h.s.groundNextTurn.value = true;
		let finish;
		h.s.api.sendMessage = async (...args) => {
			h.calls.push(copy(args));
			if (h.calls.length === 1)
				return new Promise((resolve) => {
					finish = resolve;
				});
			return { ok: false, reason: "busy" };
		};
		const firstSend = h.f.send();
		await Promise.resolve();
		assert.equal(h.calls.length, 1);
		h.s.currentId.value = "B";
		h.s.messages.value = [];
		h.s.groundNextTurn.value = scenario === "Wiki selection";
		if (scenario === "newer document") h.f.setPrefill({ doctype: "Customer", name: "NEW" });
		finish({ ok: true, conversation_id: "A", message_id: "M", run_id: "R" });
		await firstSend;
		assert.equal(h.s.groundNextTurn.value, scenario === "Wiki selection");
		h.s.sending.value = false;
		h.s.waiting.value = false;
		h.s.input.value = "Question in B";
		await h.f.send();
		assert.equal(h.calls[1][0], "B");
		if (scenario === "newer document") {
			assert.equal(h.calls[1][4]?.doctype, "Customer");
			assert.equal(h.calls[1][4]?.name, "NEW");
		} else {
			assert.equal(h.calls[1][4]?.doctype, undefined);
			assert.equal(h.calls[1][4]?.name, undefined);
		}
		if (scenario === "Wiki selection") assert.equal(h.calls[1][4].ground_wiki, 1);
		assert.deepEqual(h.errors, []);
	});
}

for (const outcome of ["unknown", "accepted", "rejected", "confirmed", "partial confirmation"]) {
	test(`lost response: Check delivery ${outcome} never dispatches another send`, async () => {
		const h = harness();
		h.s.api.sendMessage = async (...args) => {
			h.calls.push(copy(args));
			throw new Error("lost acknowledgement");
		};
		await h.f.send();
		const original = h.failed();
		assert.equal(original.deliveryState, "uncertain");
		const id = original.sendRequest.requestId;
		h.s.input.value = "Newer draft";
		h.s.pendingFiles.value = [image];
		const checked = [];
		h.s.api.checkMessageDelivery = async (requestId) => {
			checked.push(requestId);
			if (outcome === "unknown") throw new Error("still unknown");
			if (outcome === "rejected") return { ok: false, reason: "busy" };
			if (["confirmed", "partial confirmation"].includes(outcome))
				return {
					ok: outcome === "confirmed",
					confirmed: true,
					conversation_id: "A",
					queued: true,
					run_id: "old",
				};
			return { ok: true, conversation_id: "A", message_id: "M", run_id: "R" };
		};
		// Read-only recovery remains possible while sending is unavailable.
		h.s.holdActive.value = true;
		h.s.noAiConnected.value = true;
		await h.f.resendFailed(original);
		assert.deepEqual(checked, [id]);
		assert.equal(h.calls.length, 1);
		assert.equal(h.s.input.value, "Newer draft");
		assert.deepEqual(h.s.pendingFiles.value, [image]);
		if (outcome === "unknown") {
			assert.equal(h.failed().sendRequest.requestId, id);
			assert.equal(h.failed().deliveryState, "uncertain");
		} else if (["accepted", "confirmed", "partial confirmation"].includes(outcome)) {
			assert.equal(h.failed(), undefined);
			assert.equal(h.s.loaded, "A");
			assert.deepEqual(h.s.loadOptions, { preserveComposer: true });
			assert.equal(h.s.holdActive.value, true);
			assert.equal(h.s.waiting.value, false);
		} else {
			assert.equal(h.failed().deliveryState, "rejected");
			h.s.holdActive.value = false;
			h.s.noAiConnected.value = false;
			await h.f.resendFailed(h.failed());
			assert.equal(h.calls.length, 2);
			assert.notEqual(h.calls[1][9], id);
		}
	});
}
