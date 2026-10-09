import { it, expect, vi } from "vitest";
const { call } = vi.hoisted(() => ({ call: vi.fn(async () => ({ delivery: "unknown" })) }));
vi.mock("frappe-ui", () => ({ call }));
import { sendRecoverableMessage, checkDelivery } from "../../../pwa/src/api.js";
it("new-chat retries preserve first-send preferences and use the shared receipt endpoint", async () => {
	const request = {
		id: "a".repeat(32),
		conversation: "",
		text: "Review",
		attachments: [{ file_url: "/private/files/invoice.pdf", name: "invoice.pdf" }],
		approvalTokens: [],
		model: "model-a",
		thinking: "high",
		autoMode: true,
	};
	await sendRecoverableMessage(request);
	expect(call).toHaveBeenLastCalledWith(
		"jarvis.chat.send_requests.send_message",
		expect.objectContaining({
			request_id: request.id,
			model_override: "model-a",
			thinking_override: "high",
			auto_mode: 1,
			conversation: "",
		})
	);
	await checkDelivery(request.id);
	expect(call).toHaveBeenLastCalledWith("jarvis.chat.send_requests.check_delivery", {
		request_id: request.id,
	});
});
