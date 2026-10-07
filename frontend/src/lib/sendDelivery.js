// A UI deadline is not a cancellation. Unknown outcomes must only be checked.
export const newSendRequestId = () => crypto.randomUUID().replaceAll("-", "");

export function settledSendResult(envelope) {
	const result = envelope?.delivery === "settled" ? envelope.result : null;
	if (
		result &&
		((result.confirmed === true && typeof result.ok === "boolean" && result.conversation_id) ||
			result.ok === false ||
			(result.ok === true && result.conversation_id && result.message_id && result.run_id))
	)
		return result;
	throw new Error("Delivery not confirmed. Check delivery before sending again.");
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
