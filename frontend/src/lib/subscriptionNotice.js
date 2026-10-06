import { reactive } from "vue";
import { getSubscriptionNotice } from "@/api";

// One shared reading of the workspace's expired chat-subscription sign-ins
// (jarvis.subscription_health.get_subscription_notice). The chat banner, the failed-message
// card, the AI models pane and the Settings rail all read this, so they cannot disagree and the
// server is asked once per change. `upstreams` is null for a member (never disclosed) and the
// workspace's subscription upstreams for an admin.
export const subscriptionNotice = reactive({ loaded: false, expired: [], upstreams: null });

let inflight = null;

// Never rejects: a failed read leaves the last reading in place (a wrongly empty list would hide a
// real problem, a wrongly full one would cry wolf, so neither is invented).
export function loadSubscriptionNotice() {
	if (inflight) return inflight;
	inflight = Promise.resolve()
		.then(() => getSubscriptionNotice())
		.then((res) => {
			subscriptionNotice.expired = Array.isArray(res && res.expired) ? res.expired : [];
			subscriptionNotice.upstreams =
				res && Array.isArray(res.upstreams) ? res.upstreams : null;
			subscriptionNotice.loaded = true;
		})
		.catch(() => {})
		.finally(() => {
			inflight = null;
		});
	return inflight;
}

// Refetch on the realtime `jarvis:subscription_health` event (published, with no user=, whenever
// the stored health changes). Returns the unsubscribe.
export function watchSubscriptionNotice(socket) {
	if (!socket || !socket.on) return () => {};
	const handler = () => {
		loadSubscriptionNotice();
	};
	socket.on("jarvis:subscription_health", handler);
	return () => {
		if (socket.off) socket.off("jarvis:subscription_health", handler);
	};
}
