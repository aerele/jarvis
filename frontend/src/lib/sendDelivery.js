// Request identity survives uncertain delivery; only definite rejection gets a new ID.
export function newSendRequestId(rng = globalThis.crypto) {
	const bytes = rng.getRandomValues(new Uint8Array(16));
	return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function deliveryOutcome(envelope) {
	const r = envelope?.delivery === "settled" ? envelope.result : null;
	if (r?.confirmed === true)
		return typeof r.ok === "boolean" && r.conversation_id ? "confirmed" : "uncertain";
	if (r?.ok === true && r.conversation_id && r.message_id && r.run_id) return "accepted";
	return r?.ok === false ? "rejected" : "uncertain";
}

// The deadline exposes recovery; it does not discard a later authoritative result.
export async function observeDelivery(promise, onTimeout, timeout = 30000) {
	const timer = setTimeout(onTimeout, timeout);
	try {
		return await promise;
	} finally {
		clearTimeout(timer);
	}
}

// Fixed vocabulary: server/transport exception text never becomes recovery copy.
export function deliveryDiagnostic(status) {
	return (
		{
			missing: "No delivery receipt was found. Retry the same request to submit it safely.",
			pending:
				"The server received this request but has no final outcome. It may still be working or may have stopped unexpectedly. Check the conversation before sending a new request; retrying this same request will not dispatch it twice.",
			interrupted:
				"Processing was interrupted after work may have started. Some actions may have completed. Check the conversation and action receipts before sending a new request; retrying this same request will not repeat the work.",
			unavailable:
				"The delivery receipt is no longer available. Check the conversation before sending a new request.",
		}[status] || "Delivery not confirmed. Check delivery before sending again."
	);
}

export function settledSendResult(envelope) {
	if (deliveryOutcome(envelope) !== "uncertain") return envelope.result;
	const error = new Error(deliveryDiagnostic(envelope?.receipt_status));
	error.deliveryUncertain = true;
	throw error;
}

export async function boundedDelivery(promise, timeout = 30000) {
	let timer;
	try {
		return await Promise.race([
			promise,
			new Promise((_, reject) => {
				timer = setTimeout(() => reject(new Error("Delivery check timed out")), timeout);
			}),
		]);
	} finally {
		clearTimeout(timer);
	}
}
