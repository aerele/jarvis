/**
 * Pre-connect chat status phrases — the copy shown before the first token
 * streams, while the bench is still reaching (or setting up) the assistant.
 *
 * These take priority over tool/thinking phrases in ChatView's `liveStatus`
 * because at this point no tool is running yet: the turn is blocked on the WS
 * connect. One phase lives here:
 *   - "pairing" — the one-time device (re)pair (agent 9.3 "Mechanism A"):
 *     the bench has to pair with the gateway before it can talk to it, which
 *     can take tens of seconds. Rendering this (rather than a dead spinner or a
 *     bare "_Stopped._") is the locked UX for the connect-first pairing flow.
 *
 * Kept as a pure function so it can be unit-tested without mounting the
 * 10k-line ChatView SFC. "waking" (sent when the chat has no agent session yet)
 * is not a connect phase here: the steps box shows it as "Preparing your
 * session" (liveTurn.SESSION_PREP) for the whole first reply, not just the
 * connect.
 */
export function preConnectStatusLabel(phase) {
	if (phase === "pairing") return "Setting up your assistant…";
	return null;
}
